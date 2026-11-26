"""paper_text (論文ごとに組み立てた TeX) を latex から作り直す. 月次のソースの取り込みの後に実行する.

- 対象: (a) ソースを取り込んだ論文 (src_payloads, n_files > 0) のうち, 前回の組み立て (text_builds) より新しいもの,
        (b) latex に TeX があるのに paper_text の行がない論文
- 組み立ては assemble.py (HF のデータセットの paper_text と同じ方式). title / abstract / primary_category / license は metadata から
- 書き込み: 既存の行を DELETE してから parquet を add_files (s3://arxiv-warehouse/arxiv/paper_text/data/sync/<run>/).
  TeX がなくなった論文 (PDF だけの版になったなど) は行を消すだけ
- 状態: iceberg.arxiv_sync.text_builds (paper_id, source_synced_at, resolution, built_at)
"""

import argparse
import logging

import pyarrow as pa

from assemble import assemble
from common import Trino, add_files, run_id, rustfs, setup_logging
from source import NOW, Raw, insert_values, sql_str

log = logging.getLogger("arxiv_sync.text")

SCHEMA = pa.schema([
    ("paper_id", pa.string()), ("text", pa.large_string()), ("main_file", pa.string()), ("resolution", pa.string()),
    ("n_tex_files", pa.int32()), ("n_files_used", pa.int32()), ("n_unused_files", pa.int32()),
    ("text_encoding", pa.string()), ("text_sha256", pa.string()), ("title", pa.string()), ("abstract", pa.string()),
    ("primary_category", pa.string()), ("license", pa.string()),
])


def ts(v):
    return None if v is None else Raw(f"TIMESTAMP '{v.isoformat(sep=' ')}'")


def ensure_state(tr):
    tr.q("""CREATE TABLE IF NOT EXISTS iceberg.arxiv_sync.text_builds (
              paper_id varchar, source_synced_at timestamp(6), resolution varchar, built_at timestamp(6))""")


def targets(tr):
    rows = tr.q("""
        WITH pay AS (SELECT paper_id, max(synced_at) AS synced_at FROM iceberg.arxiv_sync.src_payloads
                     WHERE n_files > 0 GROUP BY 1),
             built AS (SELECT paper_id, max(source_synced_at) AS s FROM iceberg.arxiv_sync.text_builds GROUP BY 1),
             a AS (SELECT pay.paper_id, pay.synced_at FROM pay LEFT JOIN built ON built.paper_id = pay.paper_id
                   WHERE built.s IS NULL OR pay.synced_at > built.s),
             b AS (SELECT DISTINCT l.paper_id, CAST(NULL AS timestamp(6)) AS synced_at FROM iceberg.arxiv.latex l
                   WHERE l.ext IN ('tex', 'ltx')
                     AND NOT EXISTS (SELECT 1 FROM iceberg.arxiv.paper_text p WHERE p.paper_id = l.paper_id)
                     AND NOT EXISTS (SELECT 1 FROM iceberg.arxiv_sync.text_builds t WHERE t.paper_id = l.paper_id))
        SELECT paper_id, max(synced_at) FROM (SELECT * FROM a UNION ALL SELECT * FROM b) GROUP BY 1 ORDER BY 1""")
    return [(r[0], r[1]) for r in rows]


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=300, help="1 回の問い合わせの論文数")
    ap.add_argument("--flush-mb", type=int, default=1000, help="この大きさの本文が溜まったら書き込む")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tr, fs, rid = Trino(), rustfs(), run_id()
    ensure_state(tr)
    todo = targets(tr)
    if a.limit:
        todo = todo[:a.limit]
    log.info("run %s: %d papers to (re)build (new source: %d, missing text: %d)", rid, len(todo),
             sum(1 for _, s in todo if s is not None), sum(1 for _, s in todo if s is None))
    if a.dry_run or not todo:
        log.info("OK")
        return

    st = {"part": 0, "rows": [], "ids": [], "bytes": 0}
    stats = {"built": 0, "no_tex": 0, "bytes": 0}
    synced = dict(todo)

    def flush():
        if not st["ids"]:
            return
        ids = ", ".join(sql_str(p) for p in st["ids"])
        tr.q(f"DELETE FROM iceberg.arxiv.paper_text WHERE paper_id IN ({ids})")
        rows = sorted(st["rows"], key=lambda r: r["paper_id"])
        tables = (pa.Table.from_pylist(rows[i:i + 2000], SCHEMA) for i in range(0, len(rows), 2000))
        add_files(tr, fs, "paper_text", f"arxiv/paper_text/data/sync/{rid}/{st['part']:05d}", tables, 500)
        insert_values(tr, "iceberg.arxiv_sync.text_builds", ["paper_id", "source_synced_at", "resolution", "built_at"],
                      [[p, ts(synced[p]), res, NOW] for p, res in st["built"]])
        log.info("part %d: %d papers (%d without TeX). total built=%d no_tex=%d %.1f GB", st["part"], len(st["ids"]),
                 len(st["ids"]) - len(rows), stats["built"], stats["no_tex"], stats["bytes"] / 1e9)
        st["part"] += 1
        st["rows"], st["ids"], st["built"], st["bytes"] = [], [], [], 0

    st["built"] = []
    for i in range(0, len(todo), a.batch):
        batch = [p for p, _ in todo[i:i + a.batch]]
        ids = ", ".join(sql_str(p) for p in batch)
        files = {}
        for pid, path, ext, content in tr.q(f"""SELECT paper_id, path, ext, content FROM iceberg.arxiv.latex
                                                WHERE paper_id IN ({ids}) AND ext IN ('tex', 'ltx', 'bbl')"""):
            files.setdefault(pid, []).append((path, ext, content))
        meta = {r[0]: r[1:] for r in tr.q(f"""SELECT paper_id, title, abstract, primary_category, license
                                               FROM iceberg.arxiv.metadata WHERE paper_id IN ({ids})""")}
        for pid in batch:
            r = assemble(files.get(pid, []))
            st["ids"].append(pid)
            if r is None:
                stats["no_tex"] += 1
                st["built"].append((pid, "none"))
                continue
            title, abstract, cat, lic = meta.get(pid, (None, None, None, None))
            st["rows"].append({"paper_id": pid, **r, "title": title, "abstract": abstract,
                               "primary_category": cat, "license": lic})
            st["built"].append((pid, r["resolution"]))
            stats["built"] += 1
            n = len(r["text"])
            st["bytes"] += n
            stats["bytes"] += n
        if st["bytes"] > a.flush_mb * 1e6:
            flush()
    flush()
    log.info("done: built=%d no_tex=%d text %.1f GB", stats["built"], stats["no_tex"], stats["bytes"] / 1e9)
    log.info("OK")


if __name__ == "__main__":
    main()
