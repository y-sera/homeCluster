"""paper_text を正規化し, 節 (iceberg.arxiv.paper_sections) とチャンク (iceberg.arxiv.paper_chunks) に分ける (設計メモ 5.4).
本文の組み立て (text.py) の後に実行する.

- 対象: paper_text の行のうち, 加工の記録 (arxiv_sync.chunk_builds) がない, 本文 (text_sha256) か
  処理のバージョン (normalizer_version / chunker_version) が記録と違う, または前回が失敗 (status = error) の論文
- 正規化は texnorm.py, チャンク分割は chunker.py (チャンク作成の検証 chunking-poc と同じもの).
  トークン数は Qwen/Qwen3-Embedding-0.6B のトークナイザ (revision 固定)
- version: 処理の時点の最新の版. ソースの版は記録がなく確定できない (paper_files の source は version が NULL) が,
  arXiv の S3 のソースは取り込んだ時点の最新の版のため, これを推定の版とする
- 読み込み: paper_text の parquet を RustFS から直接読む (Trino を通さない). 今の行がどのファイルにあるかは
  Trino の "$path" で調べ, そのファイルの今の行だけを処理する (DELETE で消えた古い行は読まない)
- 並列: --shards n で対象のファイルを n 個に分け (行数で均す), --shard k (または JOB_COMPLETION_INDEX) を処理する.
  1 つのファイルの論文は 1 つの shard にしか入らないため, shard 同士で同じ論文を書かない
- 書き込み: 約 --flush-papers 論文ごとに, 前の行を DELETE してから parquet を add_files する
  (s3://arxiv-warehouse/arxiv/paper_{sections,chunks}/data/<run>/, arxiv_sync/chunk_builds/data/<run>/).
  記録 (chunk_builds) は最後に追加する. 途中で止まった場合, 記録のない行 (孤児) は次回の開始時に見つけて消す
- paper_text からなくなった論文 (TeX がなくなった) は, 節・チャンク・記録を消す (shard 0 が行う)
"""

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import signal
import sys
import time
import concurrent.futures as cf

import pyarrow as pa
import pyarrow.parquet as pq
import requests

import chunker
import texnorm
from common import WAREHOUSE, Trino, run_id, rustfs, setup_logging, write_parquet
from source import sql_str

log = logging.getLogger("arxiv_sync.chunks")

TOKENIZER_REPO = "Qwen/Qwen3-Embedding-0.6B"
TOKENIZER_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
TOKENIZER_SHA256 = "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"
TIMEOUT_S = 120
NO_CHUNK_TYPES = chunker.SKIP_TYPES  # チャンクにしない節 (要旨, 参考文献). 節のトークン数は直接数える

SECTIONS = pa.schema([
    ("paper_id", pa.string()), ("version", pa.int32()), ("section_id", pa.string()), ("parent_id", pa.string()),
    ("level", pa.int32()), ("ordinal", pa.int32()), ("title", pa.string()), ("section_type", pa.string()),
    ("text", pa.large_string()), ("n_chars", pa.int32()), ("n_tokens", pa.int32()), ("normalizer_version", pa.string()),
])
CHUNKS = pa.schema([
    ("chunk_id", pa.string()), ("paper_id", pa.string()), ("version", pa.int32()), ("section_id", pa.string()),
    ("section_path", pa.string()), ("section_type", pa.string()), ("chunk_index", pa.int32()),
    ("content_type", pa.string()), ("text", pa.large_string()), ("n_tokens", pa.int32()),
    ("content_hash", pa.string()), ("chunker_version", pa.string()),
])
BUILDS = pa.schema([
    ("paper_id", pa.string()), ("version", pa.int32()), ("text_sha256", pa.string()),
    ("normalizer_version", pa.string()), ("chunker_version", pa.string()),
    ("status", pa.string()),  # ok / empty (チャンクなし) / error
    ("error", pa.string()), ("n_sections", pa.int32()), ("n_chunks", pa.int32()), ("n_tokens", pa.int64()),
    ("text_chars", pa.int64()), ("body_chars", pa.int64()), ("has_abstract", pa.bool_()), ("has_references", pa.bool_()),
    ("n_tables", pa.int32()), ("n_figures", pa.int32()), ("n_cites", pa.int32()),
    ("cite_keys", pa.string()),  # JSON の配列 (引用抽出用)
    ("seconds", pa.float64()), ("built_at", pa.timestamp("us")),
])
TABLES = {  # テーブル → (スキーマ, 場所, 並び)
    "arxiv.paper_sections": (SECTIONS, "arxiv/paper_sections/iceberg", ["paper_id", "ordinal"]),
    "arxiv.paper_chunks": (CHUNKS, "arxiv/paper_chunks/iceberg", ["paper_id", "section_id", "chunk_index"]),
    "arxiv_sync.chunk_builds": (BUILDS, "arxiv_sync/chunk_builds/iceberg", ["paper_id"]),
}


def trino_type(t):
    return {pa.string(): "varchar", pa.large_string(): "varchar", pa.int32(): "integer", pa.int64(): "bigint",
            pa.bool_(): "boolean", pa.float64(): "double", pa.timestamp("us"): "timestamp(6)"}[t]


def ensure_tables(tr):
    for name, (schema, loc, order) in TABLES.items():
        cols = ", ".join(f"{f.name} {trino_type(f.type)}" for f in schema)
        props = f"location = 's3://{WAREHOUSE}/{loc}', sorted_by = ARRAY[{', '.join(sql_str(c) for c in order)}]"
        tr.q(f"CREATE TABLE IF NOT EXISTS iceberg.{name} ({cols}) WITH ({props})")


def retry(fn, what, attempts=6):
    """Iceberg の commit の競合 (shard が同時に書く) などはやり直す."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == attempts - 1:
                raise
            log.warning("%s failed (%s), retry %d", what, str(e)[:200], i + 1)
            time.sleep(10 * (i + 1))


def tokenizer_file():
    path = "/tmp/tokenizer.json"
    if not os.path.exists(path):
        url = f"https://huggingface.co/{TOKENIZER_REPO}/resolve/{TOKENIZER_REVISION}/tokenizer.json"
        body = requests.get(url, timeout=120, headers={"User-Agent": "homecluster-arxiv-sync/0.1"}).content
        if hashlib.sha256(body).hexdigest() != TOKENIZER_SHA256:
            raise SystemExit("tokenizer.json sha256 mismatch")
        with open(path, "wb") as f:
            f.write(body)
    return path


# ---------------------------------------------------------------- 論文ごとの処理 (子プロセス)

_counter = None


def _init(tok_path):
    global _counter
    from tokenizers import Tokenizer

    _counter = chunker.Counter(Tokenizer.from_file(tok_path))


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def work(item):
    pid, version, sha, text, had = item
    text = text or ""
    b = {"_had": had, "paper_id": pid, "version": version, "text_sha256": sha, "normalizer_version": texnorm.NORMALIZER_VERSION,
         "chunker_version": chunker.CHUNKER_VERSION, "text_chars": len(text), "error": None}
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(TIMEOUT_S)
    t0 = time.perf_counter()
    try:
        res = texnorm.parse(text)
        chunks = chunker.chunk_paper(pid, res.sections, _counter)
        secs = res.sections
        tok = {}
        for c in chunks:
            tok[c.section_id] = tok.get(c.section_id, 0) + c.n_tokens
        direct = [s for s in secs if s.section_type in NO_CHUNK_TYPES]
        for s, n in zip(direct, _counter.counts([s.text for s in direct])):
            tok[s.section_id] = n
    except _Timeout:
        b.update(status="error", error="timeout", seconds=float(TIMEOUT_S))
        return b, [], []
    except Exception as e:  # noqa: BLE001
        b.update(status="error", error=f"{type(e).__name__}: {e}"[:300], seconds=time.perf_counter() - t0)
        return b, [], []
    finally:
        signal.alarm(0)
    srows = [{"paper_id": pid, "version": version, "section_id": s.section_id, "parent_id": s.parent_id,
              "level": s.level, "ordinal": s.ordinal, "title": s.title, "section_type": s.section_type,
              "text": (t := s.text), "n_chars": len(t), "n_tokens": tok.get(s.section_id, 0),
              "normalizer_version": texnorm.NORMALIZER_VERSION} for s in secs]
    crows = [{"chunk_id": c.chunk_id, "paper_id": pid, "version": version, "section_id": c.section_id,
              "section_path": c.section_path, "section_type": c.section_type, "chunk_index": c.chunk_index,
              "content_type": c.content_type, "text": c.text, "n_tokens": c.n_tokens, "content_hash": c.content_hash,
              "chunker_version": chunker.CHUNKER_VERSION} for c in chunks]
    b.update(status="ok" if chunks else "empty", n_sections=len(secs), n_chunks=len(chunks),
             n_tokens=sum(c.n_tokens for c in chunks),
             body_chars=sum(r["n_chars"] for r in srows if r["section_type"] not in NO_CHUNK_TYPES),
             has_abstract=any(s.section_type == "abstract" for s in secs),
             has_references=any(s.section_type == "references" for s in secs),
             n_tables=res.n_tables, n_figures=res.n_figures, n_cites=res.n_cites,
             cite_keys=json.dumps(sorted(set(res.cite_keys)), ensure_ascii=False),
             seconds=time.perf_counter() - t0)
    return b, srows, crows


# ---------------------------------------------------------------- 対象と分担

def targets(tr):
    """{path: {paper_id: (version, sha256, 前の行があるか)}}"""
    nv, cv = sql_str(texnorm.NORMALIZER_VERSION), sql_str(chunker.CHUNKER_VERSION)
    rows = tr.q(f"""
        WITH live AS (SELECT paper_id, text_sha256, "$path" AS path FROM iceberg.arxiv.paper_text),
             src AS (SELECT paper_id, max(version) AS version FROM iceberg.arxiv.versions GROUP BY 1)
        SELECT l.path, l.paper_id, src.version, l.text_sha256, b.paper_id IS NOT NULL
        FROM live l
        LEFT JOIN iceberg.arxiv_sync.chunk_builds b ON b.paper_id = l.paper_id
        LEFT JOIN src ON src.paper_id = l.paper_id
        WHERE b.paper_id IS NULL OR b.text_sha256 <> l.text_sha256 OR b.status = 'error'
           OR b.normalizer_version <> {nv} OR b.chunker_version <> {cv}""")
    out = {}
    for path, pid, ver, sha, had in rows:
        out.setdefault(path, {})[pid] = (ver, sha, had)
    return out


def orphans(tr):
    """記録のない節・チャンクの論文 (前回が書き込みの途中で止まった)."""
    rows = tr.q("""SELECT DISTINCT paper_id FROM (
                     SELECT paper_id FROM iceberg.arxiv.paper_sections UNION ALL
                     SELECT paper_id FROM iceberg.arxiv.paper_chunks) x
                   WHERE NOT EXISTS (SELECT 1 FROM iceberg.arxiv_sync.chunk_builds b WHERE b.paper_id = x.paper_id)""")
    return {r[0] for r in rows}


def split(todo, n):
    """ファイルを行数で n 個に均す (大きいファイルから, いちばん軽い shard へ)."""
    shards = [[] for _ in range(n)]
    load = [0] * n
    for path in sorted(todo, key=lambda p: (-len(todo[p]), p)):
        k = load.index(min(load))
        shards[k].append(path)
        load[k] += len(todo[path])
    return [sorted(s) for s in shards]


def row_groups(pf, ids):
    """対象の paper_id を含みうる行グループ (統計の最小・最大で判定). 月次の少数の対象でファイル全体を読まないため."""
    import bisect

    col = pf.schema_arrow.get_field_index("paper_id")
    out = []
    for i in range(pf.metadata.num_row_groups):
        st = pf.metadata.row_group(i).column(col).statistics
        if st is None or not st.has_min_max:
            out.append(i)
            continue
        k = bisect.bisect_left(ids, st.min)
        if k < len(ids) and ids[k] <= st.max:
            out.append(i)
    return out


def delete_papers(tr, ids, tables):
    ids = sorted(ids)
    for i in range(0, len(ids), 1000):
        lst = ", ".join(sql_str(p) for p in ids[i:i + 1000])
        for t in tables:
            retry(lambda: tr.q(f"DELETE FROM iceberg.{t} WHERE paper_id IN ({lst})"), f"delete {t}")


# ---------------------------------------------------------------- 本体

def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--shard", type=int, default=int(os.environ.get("JOB_COMPLETION_INDEX", "0")))
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--flush-papers", type=int, default=20000, help="この論文数ごとに書き込む")
    ap.add_argument("--limit", type=int, default=0, help="処理する論文数の上限 (確認用)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tr, fs = Trino(), rustfs()
    rid = f"{run_id()}-s{a.shard}"
    ensure_tables(tr)
    todo = targets(tr)
    mine = split(todo, a.shards)[a.shard]
    n_all = sum(len(v) for v in todo.values())
    n_mine = sum(len(todo[p]) for p in mine)
    log.info("run %s: normalizer %s, chunker %s. %d papers to process in %d files, shard %d/%d: %d papers in %d files",
             rid, texnorm.NORMALIZER_VERSION, chunker.CHUNKER_VERSION, n_all, len(todo), a.shard, a.shards,
             n_mine, len(mine))

    if a.shard == 0 and not a.dry_run:
        live = {r[0] for r in tr.q("SELECT DISTINCT paper_id FROM iceberg.arxiv.paper_text")}
        gone = {r[0] for r in tr.q("SELECT paper_id FROM iceberg.arxiv_sync.chunk_builds")} - live
        if gone:
            log.info("%d papers no longer in paper_text: delete their sections, chunks and builds", len(gone))
            delete_papers(tr, gone, ["arxiv.paper_sections", "arxiv.paper_chunks", "arxiv_sync.chunk_builds"])
    mine_ids = {pid for p in mine for pid in todo[p]}
    all_orph = orphans(tr)
    if a.shard == 0 and not a.dry_run and all_orph - live:
        log.info("%d papers with orphan sections/chunks are no longer in paper_text: delete", len(all_orph - live))
        delete_papers(tr, all_orph - live, ["arxiv.paper_sections", "arxiv.paper_chunks"])
    orph = all_orph & mine_ids
    if orph:
        log.info("%d papers have sections/chunks without a build record (interrupted run): delete before rebuilding",
                 len(orph))
    if a.dry_run or not mine:
        log.info("OK")
        return

    st = {"part": 0, "sec": [], "chk": [], "bld": [], "had": set(), "n": 0}
    stats = {"papers": 0, "ok": 0, "empty": 0, "error": 0, "sections": 0, "chunks": 0, "tokens": 0}
    t_start = time.time()

    def flush():
        if not st["bld"]:
            return
        part = st["part"]
        old = st["had"] | (orph & {b["paper_id"] for b in st["bld"]})
        if old:
            delete_papers(tr, old, ["arxiv.paper_sections", "arxiv.paper_chunks", "arxiv_sync.chunk_builds"])
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        for b in st["bld"]:
            b["built_at"] = now
        for table, rows, schema, rg, prefix in (
                ("arxiv.paper_sections", st["sec"], SECTIONS, 10000, "arxiv/paper_sections/data"),
                ("arxiv.paper_chunks", st["chk"], CHUNKS, 20000, "arxiv/paper_chunks/data"),
                ("arxiv_sync.chunk_builds", [pa.Table.from_pylist(st["bld"], BUILDS)], BUILDS, 100000,
                 "arxiv_sync/chunk_builds/data")):
            tables = [t for t in rows if t.num_rows]
            if not tables:
                continue
            t = pa.concat_tables(tables).sort_by([(c, "ascending") for c in TABLES[table][2]])
            path = f"{WAREHOUSE}/{prefix}/{rid}/part-{part:05d}.parquet"
            write_parquet(fs, path, t, rg)
            retry(lambda: tr.q(f"ALTER TABLE iceberg.{table} EXECUTE add_files(location => 's3://{path}', "
                               f"format => 'PARQUET')"), f"add_files {table}")
        el = time.time() - t_start
        log.info("part %d: %d papers. total %d/%d (ok %d, empty %d, error %d), %d sections, %d chunks, %.2fB tokens, "
                 "%.0f papers/s, ETA %.0f min", part, len(st["bld"]), stats["papers"], n_mine, stats["ok"],
                 stats["empty"], stats["error"], stats["sections"], stats["chunks"], stats["tokens"] / 1e9,
                 stats["papers"] / el, (n_mine - stats["papers"]) / max(stats["papers"] / el, 1e-9) / 60)
        st["part"] += 1
        st["sec"], st["chk"], st["bld"], st["had"], st["n"] = [], [], [], set(), 0

    def items():
        """自分のファイルの今の行を順に返す (paper_id, version, sha256, text)."""
        left = a.limit or None
        for path in mine:
            want = todo[path]
            pf = pq.ParquetFile(fs.open_input_file(path.removeprefix("s3://")))
            groups = row_groups(pf, sorted(want))
            if not groups:
                continue
            for batch in pf.iter_batches(batch_size=200, columns=["paper_id", "text_sha256", "text"], row_groups=groups):
                for pid, sha, text in zip(*(batch.column(i).to_pylist() for i in range(3))):
                    w = want.get(pid)
                    if w is None or w[1] != sha:
                        continue
                    yield pid, w[0], sha, text, w[2]
                    if left is not None:
                        left -= 1
                        if left == 0:
                            return

    buf = {"s": [], "c": []}

    def to_arrow():
        st["sec"].append(pa.Table.from_pylist(buf["s"], SECTIONS))
        st["chk"].append(pa.Table.from_pylist(buf["c"], CHUNKS))
        buf["s"], buf["c"] = [], []

    def collect(b, srows, crows):
        if b.pop("_had"):
            st["had"].add(b["paper_id"])
        stats["papers"] += 1
        stats[b["status"]] += 1
        if b["status"] == "error":
            log.warning("%s: %s", b["paper_id"], b["error"])
        stats["sections"] += len(srows)
        stats["chunks"] += len(crows)
        stats["tokens"] += b.get("n_tokens") or 0
        st["bld"].append(b)
        buf["s"].extend(srows)
        buf["c"].extend(crows)
        if len(buf["s"]) >= 20000 or len(buf["c"]) >= 20000:
            to_arrow()
        if len(st["bld"]) >= a.flush_papers:
            to_arrow()
            flush()

    # 投入する数を抑えて, 読み込んだ本文がメモリに溜まらないようにする (Pool.imap は入力を先読みし続ける)
    with cf.ProcessPoolExecutor(a.workers, initializer=_init, initargs=(tokenizer_file(),),
                                max_tasks_per_child=5000) as ex:
        pending = set()
        it = items()
        while True:
            for item in it:
                pending.add(ex.submit(work, item))
                if len(pending) >= a.workers * 8:
                    break
            if not pending:
                break
            done, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for f in done:
                collect(*f.result())
    to_arrow()
    flush()
    log.info("done: %d papers (ok %d, empty %d, error %d), %d sections, %d chunks, %.2fB tokens, %.0f min",
             stats["papers"], stats["ok"], stats["empty"], stats["error"], stats["sections"], stats["chunks"],
             stats["tokens"] / 1e9, (time.time() - t_start) / 60)
    log.info("OK")


if __name__ == "__main__":
    sys.setrecursionlimit(10000)
    main()
