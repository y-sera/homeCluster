"""paper_text 形式の parquet を正規化・節の抽出・チャンク分割し, 結果と論文ごとの指標を保存する.

  python process.py --input sample.parquet --out /work/out/run1 [--workers 4] [--limit N]

出力 (--out):
  papers.parquet    論文ごとの指標 (処理時間, 節数, チャンク数, エラーなど)
  sections.parquet  節 (paper_sections 相当)
  chunks.parquet    チャンク (paper_chunks 相当)
  unknown_cmds.json 正規化で未知だったコマンドの出現数 (正規化の改善用)
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import sys
import time
from multiprocessing import Pool

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(__file__))
import chunker  # noqa: E402
import texnorm  # noqa: E402

TOKENIZER_REPO = "Qwen/Qwen3-Embedding-0.6B"
TIMEOUT_S = 120
STRUCT_TYPES_EXCLUDED = {"front", "body", "abstract", "references"}

_counter = None


def stratum(cat: str) -> str:
    cat = cat or ""
    if cat in ("cs.AI", "cs.LG", "cs.CL", "cs.CV"):
        return cat
    head = cat.split(".")[0]
    if head == "cs":
        return "cs.other"
    if head.startswith("hep"):
        return "hep"
    if head in ("math", "astro-ph", "cond-mat", "quant-ph", "physics", "stat"):
        return head
    return "other"


def _init(tok_path):
    global _counter
    from tokenizers import Tokenizer

    _counter = chunker.Counter(Tokenizer.from_file(tok_path))


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def work(row):
    pid = row["paper_id"]
    text = row["text"] or ""
    rec = {
        "paper_id": pid, "primary_category": row.get("primary_category"), "stratum": stratum(row.get("primary_category")),
        "resolution": row.get("resolution"), "text_encoding": row.get("text_encoding"), "text_chars": len(text),
        "stub_autoignore": text.lstrip().startswith("%auto-ignore"), "stub_includepdf": "\\includepdf" in text and len(text) < 2000,
        "error": None,
    }
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(TIMEOUT_S)
    t0 = time.perf_counter()
    try:
        res = texnorm.parse(text)
        t1 = time.perf_counter()
        chunks = chunker.chunk_paper(pid, res.sections, _counter)
        t2 = time.perf_counter()
    except _Timeout:
        rec.update(error="timeout", t_parse=float(TIMEOUT_S), t_chunk=0.0)
        return rec, [], [], {}
    except Exception as e:  # noqa: BLE001
        rec.update(error=f"{type(e).__name__}: {e}"[:300], t_parse=time.perf_counter() - t0, t_chunk=0.0)
        return rec, [], [], {}
    finally:
        signal.alarm(0)

    secs = res.sections
    body_chars = sum(len(s.text) for s in secs if s.section_type not in ("abstract", "references"))
    rec.update(
        has_document=res.has_document, n_macros=res.n_macros, raw_body_chars=res.raw_body_chars, body_chars=body_chars,
        n_sections=sum(1 for s in secs if s.section_type not in STRUCT_TYPES_EXCLUDED),
        n_sections_all=len(secs), n_heads=res.n_heads, src_has_sec=res.src_has_sec, dropped_prog_chars=res.dropped_prog_chars, has_abstract=any(s.section_type == "abstract" for s in secs),
        has_references=any(s.section_type == "references" for s in secs),
        n_tables=res.n_tables, n_figures=res.n_figures, n_cites=res.n_cites, n_refs=res.n_refs,
        n_cite_keys=len(set(res.cite_keys)), n_unknown_cmds=sum(res.unknown.values()), n_chunks=len(chunks),
        chunk_tokens=sum(c.n_tokens for c in chunks), t_parse=t1 - t0, t_chunk=t2 - t1,
        section_types=",".join(sorted({s.section_type for s in secs})),
    )
    srows = [{
        "paper_id": pid, "section_id": s.section_id, "parent_id": s.parent_id, "level": s.level, "ordinal": s.ordinal,
        "title": s.title, "section_type": s.section_type, "text": s.text, "n_chars": len(s.text),
        "normalizer_version": texnorm.NORMALIZER_VERSION,
    } for s in secs]
    crows = [{
        "chunk_id": c.chunk_id, "paper_id": pid, "section_id": c.section_id, "section_path": c.section_path,
        "section_type": c.section_type, "chunk_index": c.chunk_index, "content_type": c.content_type, "text": c.text,
        "n_tokens": c.n_tokens, "content_hash": c.content_hash, "chunker_version": chunker.CHUNKER_VERSION,
    } for c in chunks]
    return rec, srows, crows, res.unknown


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    from huggingface_hub import hf_hub_download

    tok_path = hf_hub_download(TOKENIZER_REPO, "tokenizer.json")
    cols = ["paper_id", "text", "resolution", "text_encoding", "primary_category"]
    pf = pq.ParquetFile(a.input)
    rows = []
    for b in pf.iter_batches(batch_size=200, columns=cols):
        rows.extend(b.to_pylist())
        if a.limit and len(rows) >= a.limit:
            rows = rows[: a.limit]
            break
    # 大きい論文から始めて, 並列の終わりの待ちを減らす
    rows.sort(key=lambda r: -len(r["text"] or ""))
    print(f"papers: {len(rows)}", flush=True)

    papers, sections, chunks = [], [], []
    unknown = collections.Counter()
    t0 = time.time()
    with Pool(a.workers, initializer=_init, initargs=(tok_path,)) as pool:
        for i, (rec, srows, crows, unk) in enumerate(pool.imap_unordered(work, rows, chunksize=4), 1):
            papers.append(rec)
            sections.extend(srows)
            chunks.extend(crows)
            unknown.update(unk)
            if i % 500 == 0:
                print(f"  {i}/{len(rows)} {time.time() - t0:.0f}s", flush=True)
    wall = time.time() - t0
    pq.write_table(pa.Table.from_pylist(papers), f"{a.out}/papers.parquet")
    pq.write_table(pa.Table.from_pylist(sections), f"{a.out}/sections.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(chunks), f"{a.out}/chunks.parquet", compression="zstd")
    with open(f"{a.out}/unknown_cmds.json", "w") as f:
        json.dump(dict(unknown.most_common(500)), f, indent=0)
    with open(f"{a.out}/run.json", "w") as f:
        json.dump({"input": a.input, "papers": len(rows), "workers": a.workers, "wall_s": wall,
                   "normalizer_version": texnorm.NORMALIZER_VERSION, "chunker_version": chunker.CHUNKER_VERSION,
                   "tokenizer": TOKENIZER_REPO}, f, indent=1)
    print(f"done: {len(rows)} papers, {len(sections)} sections, {len(chunks)} chunks, {wall:.0f}s", flush=True)


if __name__ == "__main__":
    main()
