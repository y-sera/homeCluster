"""PDF を GCS の公開ミラー (gs://arxiv-dataset, 匿名で読める) から取得して iceberg.arxiv.pdf / paper_files に追加する.

- 対象: versions で PDF がない版 (has_pdf = false, 大きさ 0kb の版は除く) のうち, paper_files にもまだない版
  (既定では版の日付が --since-days 日以内のもの. それより古い欠落は arXiv 側にもないことが多い)
- ミラーのパス: 新形式 2508.00010 v2 → arxiv/arxiv/pdf/2508/2508.00010v2.pdf,
                旧形式 hep-th/9901001 v1 → arxiv/hep-th/pdf/9901/9901001v1.pdf (HF のデータセットと同じ)
  ミラーは arXiv より数日〜数週間遅れる. 404 の版は次回に再び試す
- 取得した中身は GCS の MD5 (x-goog-hash) と照合し, sha256 を計算する
- 書き込み: parquet (行グループ 50 行. 届いた順に書き出しメモリを抑える. 1 ファイル約 1,000 行) を
  s3://arxiv-warehouse/arxiv/pdf/data/sync/<run>/ に置き, ファイルごとに add_files で pdf と paper_files に追加する (既存の parquet は書き換えない)
- 最後 (と開始時) に versions の has_pdf / pdf_sha256 / is_latest_pdf を paper_files から合わせる (途中で止まっても次回に直る)
"""

import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import hashlib
import logging
import threading

import pyarrow as pa
import pyarrow.parquet as pq
import requests

from common import WAREHOUSE, Trino, add_files, run_id, rustfs, setup_logging

log = logging.getLogger("arxiv_sync.pdf")

GCS = "https://storage.googleapis.com/arxiv-dataset/"
UA = "homecluster-arxiv-sync/0.1"

PDF_SCHEMA = pa.schema([
    ("paper_id", pa.string()), ("version", pa.int32()), ("kind", pa.string()), ("source", pa.string()),
    ("path", pa.string()), ("size", pa.int64()), ("sha256", pa.string()), ("content", pa.large_binary()),
])
FILES_SCHEMA = pa.schema([
    ("paper_id", pa.string()), ("version", pa.int32()), ("kind", pa.string()), ("source", pa.string()),
    ("path", pa.string()), ("path_encoding", pa.string()), ("size", pa.int64()), ("sha256", pa.string()),
    ("stored_as", pa.string()), ("stored_sha256", pa.string()), ("stored_size", pa.int64()),
])


def mirror_path(pid, version):
    if "/" in pid:
        arch, num = pid.split("/", 1)
        return f"arxiv/{arch}/pdf/{num[:4]}/{num}v{version}.pdf"
    return f"arxiv/arxiv/pdf/{pid[:4]}/{pid}v{version}.pdf"


_local = threading.local()


def session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers["User-Agent"] = UA
    return _local.s


def download(pid, version):
    """(行, None) / (None, 理由)."""
    path = mirror_path(pid, version)
    for attempt in range(3):
        try:
            r = session().get(GCS + path, timeout=600)
        except requests.RequestException as e:
            err = str(e)
            continue
        if r.status_code == 404:
            return None, "404"
        if r.status_code != 200:
            err = f"HTTP {r.status_code}"
            continue
        body = r.content
        md5 = {k: v for k, v in (p.split("=", 1) for p in r.headers.get("x-goog-hash", "").split(",") if "=" in p)}.get("md5")
        if md5 and base64.b64encode(hashlib.md5(body).digest()).decode() != md5:
            err = "md5 mismatch"
            continue
        return {"paper_id": pid, "version": version, "kind": "pdf", "source": "gcs", "path": path,
                "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "content": body}, None
    return None, err


def reconcile(tr):
    """paper_files にある PDF を versions に反映する (has_pdf, pdf_sha256, is_latest_pdf)."""
    tr.q("""MERGE INTO iceberg.arxiv.versions t
            USING (SELECT f.paper_id, f.version, max(f.sha256) AS sha256
                   FROM iceberg.arxiv.paper_files f
                   JOIN iceberg.arxiv.versions v ON v.paper_id = f.paper_id AND v.version = f.version AND NOT v.has_pdf
                   WHERE f.kind = 'pdf' GROUP BY 1, 2) s
            ON t.paper_id = s.paper_id AND t.version = s.version
            WHEN MATCHED THEN UPDATE SET has_pdf = true, pdf_sha256 = s.sha256""")
    # 最新の PDF の印: PDF のある版のうち最大の版. 変わる論文だけ更新する
    tr.q("""MERGE INTO iceberg.arxiv.versions t
            USING (SELECT paper_id, version, lp FROM (
                     SELECT paper_id, version, is_latest_pdf,
                            coalesce(has_pdf AND version = max(CASE WHEN has_pdf THEN version END)
                                     OVER (PARTITION BY paper_id), false) AS lp
                     FROM iceberg.arxiv.versions)
                   WHERE lp <> is_latest_pdf) s
            ON t.paper_id = s.paper_id AND t.version = s.version
            WHEN MATCHED THEN UPDATE SET is_latest_pdf = s.lp""")


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-days", type=int, default=400, help="版の日付がこの日数以内のものを対象にする (0 で全部)")
    ap.add_argument("--limit", type=int, default=0, help="取得する版の上限 (0 で上限なし)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rows-per-file", type=int, default=1000)
    a = ap.parse_args()

    tr, fs, rid = Trino(), rustfs(), run_id()
    reconcile(tr)
    since = ""
    if a.since_days:
        since = f"AND v.version_date >= TIMESTAMP '{(dt.date.today() - dt.timedelta(days=a.since_days)).isoformat()} 00:00:00'"
    todo = tr.q(f"""SELECT v.paper_id, v.version FROM iceberg.arxiv.versions v
                    WHERE NOT v.has_pdf AND NOT v.is_metadata_only {since}
                      AND NOT EXISTS (SELECT 1 FROM iceberg.arxiv.paper_files f
                                      WHERE f.kind = 'pdf' AND f.paper_id = v.paper_id AND f.version = v.version)
                    ORDER BY v.version_date {f"LIMIT {a.limit}" if a.limit else ""}""")
    log.info("run %s: %d versions to fetch", rid, len(todo))

    prefix_pdf = f"arxiv/pdf/data/sync/{rid}"
    prefix_files = f"arxiv/paper_files/data/sync/{rid}"
    stats = {"ok": 0, "404": 0, "error": 0, "bytes": 0}
    # 中身は 50 行 (行グループ) ごとにすぐ書き出し, メモリには 50 行分しか持たない. 1 ファイル --rows-per-file 行で閉じて add_files する
    st = {"part": 0, "writer": None, "buf": [], "meta": [], "rows": 0}

    def path_of(part):
        return f"{WAREHOUSE}/{prefix_pdf}/part-{part:05d}.parquet"

    def write_group():
        if not st["buf"]:
            return
        if st["writer"] is None:
            st["writer"] = pq.ParquetWriter(path_of(st["part"]), PDF_SCHEMA, filesystem=fs, compression="zstd")
        st["buf"].sort(key=lambda r: (r["paper_id"], r["version"]))
        st["writer"].write_table(pa.Table.from_pylist(st["buf"], PDF_SCHEMA))
        st["buf"] = []

    def close_file():
        write_group()
        if st["writer"] is None:
            return
        st["writer"].close()
        st["writer"] = None
        part = st["part"]
        tr.q(f"ALTER TABLE iceberg.arxiv.pdf EXECUTE add_files(location => 's3://{path_of(part)}', format => 'PARQUET')")
        meta = sorted(st["meta"], key=lambda r: (r["paper_id"], r["version"]))
        add_files(tr, fs, "paper_files", f"{prefix_files}/{part:05d}", [pa.Table.from_pylist(meta, FILES_SCHEMA)], 100000)
        log.info("part %d: %d PDFs added (total ok=%d, 404=%d, error=%d, %.1f GB)",
                 part, len(meta), stats["ok"], stats["404"], stats["error"], stats["bytes"] / 1e9)
        st["part"] += 1
        st["meta"], st["rows"] = [], 0

    def add_row(row):
        st["buf"].append(row)
        st["meta"].append({k: row[k] for k in ("paper_id", "version", "kind", "source", "path", "size", "sha256")} |
                          {"path_encoding": "utf8", "stored_as": None, "stored_sha256": None, "stored_size": None})
        st["rows"] += 1
        if len(st["buf"]) >= 50:
            write_group()
        if st["rows"] >= a.rows_per_file:
            close_file()

    with cf.ThreadPoolExecutor(a.workers) as ex:
        # 一度に投入する数を抑えてメモリを一定にする
        it = iter(todo)
        pending = set()
        while True:
            while len(pending) < a.workers * 2:
                try:
                    pid, ver = next(it)
                except StopIteration:
                    break
                pending.add(ex.submit(download, pid, ver))
            if not pending:
                break
            done, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for f in done:
                row, err = f.result()
                if row:
                    stats["ok"] += 1
                    stats["bytes"] += row["size"]
                    add_row(row)
                elif err == "404":
                    stats["404"] += 1
                else:
                    stats["error"] += 1
                    log.warning("error: %s", err)
    close_file()
    reconcile(tr)
    left = tr.one("SELECT count(*) FROM iceberg.arxiv.versions WHERE NOT has_pdf AND NOT is_metadata_only")
    log.info("done: ok=%d 404=%d error=%d %.1f GB. versions without pdf: %d",
             stats["ok"], stats["404"], stats["error"], stats["bytes"] / 1e9, left)
    log.info("OK" if stats["error"] == 0 else "FINISHED WITH ERRORS")


if __name__ == "__main__":
    main()
