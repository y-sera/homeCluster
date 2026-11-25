"""arXiv の S3 (s3://arxiv/src/, requester-pays) からソースを取得して iceberg.arxiv.source / latex / paper_files を更新する (月次).

S3 のソースは論文ごとに 1 つ (版の区別なし. tar を作った時点の最新版) で, tar (約 500MB) は過去の月の分も作り直される.
転送料を抑えるため, 必要な論文のソースだけを取る:
- 必要な論文: (a) ソースがまだない新しい論文 (--new-since 以降の投稿), または
               (b) 前回取り込んだ時点 (HF のデータセットの論文は --baseline) より後に新しい版が出た論文
- manifest で前回から変わった tar (ファイル名と md5) のうち, 必要な論文を含むもの (manifest の first_item / last_item で判定) だけを読む
- 必要な論文が tar の --whole-ratio 以上なら tar ごと取得し, それ以外は tar の見出し (512 バイト) を範囲指定で順に読んで
  目次を作り, 必要なファイルだけを範囲指定で取得する
- 1 回の取得量の上限 --max-gb (転送料 約 $0.09/GB)

ファイルの扱いは HF のデータセットに合わせる: <ID>.gz が tar ならその中のファイル, tar でなければ 1 ファイル (<ID>.tex),
<ID>.pdf は PDF だけの投稿 (<ID>.pdf). 旧形式の ID は hep-th/9901001 → hep-th9901001. version は NULL.
置き換える論文は既存の行を DELETE してから add_files で追加する. paper_text (本文の組み立て) はここでは作り直さない.
状態は名前空間 arxiv_sync のテーブル src_chunks (処理済みの tar) と src_payloads (論文ごとの取り込み済みのソース) に置く.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import hashlib
import io
import logging
import re
import tarfile
import threading
import urllib.parse
import xml.etree.ElementTree as ET

import boto3
import botocore.config
import pyarrow as pa

from common import RAW, WAREHOUSE, Trino, add_files, put_bytes, run_id, rustfs, setup_logging

log = logging.getLogger("arxiv_sync.source")

BUCKET = "arxiv"
LATEX_EXT = {"tex", "bib", "bbl", "sty", "cls", "bst", "clo", "cfg", "ltx", "tikz", "def", "ins", "dtx"}
EXT_RE = re.compile(r"\.([A-Za-z0-9]+)$")

SOURCE_SCHEMA = pa.schema([
    ("paper_id", pa.string()), ("version", pa.int32()), ("kind", pa.string()), ("source", pa.string()),
    ("path", pa.string()), ("size", pa.int64()), ("sha256", pa.string()), ("content", pa.large_binary()),
])
LATEX_SCHEMA = pa.schema([
    ("paper_id", pa.string()), ("path", pa.string()), ("ext", pa.string()), ("size", pa.int64()),
    ("sha256", pa.string()), ("content", pa.large_binary()),
])
FILES_SCHEMA = pa.schema([
    ("paper_id", pa.string()), ("version", pa.int32()), ("kind", pa.string()), ("source", pa.string()),
    ("path", pa.string()), ("path_encoding", pa.string()), ("size", pa.int64()), ("sha256", pa.string()),
    ("stored_as", pa.string()), ("stored_sha256", pa.string()), ("stored_size", pa.int64()),
])


# ---------------------------------------------------------------- ID と tar の対応

def key_of(pid):
    """paper_id → tar の中の名前 (旧形式は / を除く)."""
    return pid.replace("/", "")


def pid_of(key):
    m = re.match(r"^([a-z][a-z.-]*?)(\d{7})$", key)
    return f"{m.group(1)}/{m.group(2)}" if m else key


def yymm_of(pid):
    return pid.split("/", 1)[1][:4] if "/" in pid else pid[:4]


# ---------------------------------------------------------------- S3

class S3:
    def __init__(self):
        self.c = boto3.client("s3", region_name="us-east-1",
                              config=botocore.config.Config(retries={"max_attempts": 10, "mode": "adaptive"},
                                                            max_pool_connections=32))
        self.bytes = 0
        self.lock = threading.Lock()

    def get(self, key, start=None, length=None):
        kw = {"Bucket": BUCKET, "Key": key, "RequestPayer": "requester"}
        if start is not None:
            kw["Range"] = f"bytes={start}-{start + length - 1}"
        body = self.c.get_object(**kw)["Body"].read()
        with self.lock:
            self.bytes += len(body)
        return body

    def stream(self, key):
        return self.c.get_object(Bucket=BUCKET, Key=key, RequestPayer="requester")["Body"]


def parse_manifest(body):
    out = []
    for f in ET.fromstring(body).findall("file"):
        d = {c.tag: (c.text or "") for c in f}
        out.append({
            "filename": d["filename"], "md5sum": d["md5sum"], "timestamp": d["timestamp"], "yymm": d["yymm"],
            "seq_num": int(d["seq_num"]), "first_item": d["first_item"], "last_item": d["last_item"],
            "num_items": int(d["num_items"]), "size": int(d["size"]),
        })
    return out


def tar_index(s3, key):
    """tar の見出しだけを範囲指定で順に読み, (名前, 中身の位置, 大きさ) を順に返す (1 件 1 回の GET)."""
    off = 0
    while True:
        h = s3.get(key, off, 512)
        if len(h) < 512 or h[:1] == b"\0":
            return
        info = tarfile.TarInfo.frombuf(h, "utf-8", "surrogateescape")
        if info.isreg():
            yield info.name, off + 512, info.size
        off += 512 + (info.size + 511) // 512 * 512


# ---------------------------------------------------------------- 中身の展開

def enc_path(name):
    b = name.encode("utf-8", "surrogateescape")
    try:
        return b.decode("utf-8"), "utf8"
    except UnicodeDecodeError:
        return urllib.parse.quote_from_bytes(b, safe="/"), "percent"


def expand(key, member, data):
    """S3 の 1 論文分 (<ID>.gz / <ID>.pdf) → [(path, path_encoding, bytes)]."""
    if member.endswith(".pdf"):
        return [(f"{key}.pdf", "utf8", data)]
    try:
        raw = gzip.decompress(data) if data else b""
    except (OSError, EOFError):
        raw = data
    files = []
    if raw and _is_tar(raw):
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:", encoding="utf-8", errors="surrogateescape") as t:
            for m in t:
                if not m.isreg():
                    continue
                name = m.name[2:] if m.name.startswith("./") else m.name
                p, e = enc_path(name)
                files.append((p, e, t.extractfile(m).read()))
        return files
    # 1 ファイル (HF のデータセットでは <ID>.tex. 空の場合は <ID>.bin)
    ext = "bin" if not raw else ("pdf" if raw.startswith(b"%PDF") else "tex")
    return [(f"{key}.{ext}", "utf8", raw)]


def _is_tar(b):
    try:
        with tarfile.open(fileobj=io.BytesIO(b), mode="r:") as t:
            return t.next() is not None
    except tarfile.TarError:
        return False


def fetch_chunk(s3, chunk, wanted, whole_ratio):
    """1 つの tar から wanted (key → 必要) の論文を取り出す. [(key, member, member_size, files)]."""
    key = chunk["filename"]
    out = []
    if len(wanted) >= chunk["num_items"] * whole_ratio:
        body = s3.stream(key)
        with tarfile.open(fileobj=body, mode="r|", encoding="utf-8", errors="surrogateescape") as t:
            for m in t:
                if not m.isreg():
                    continue
                k = m.name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                data = t.extractfile(m).read()
                with s3.lock:
                    s3.bytes += len(data) + 512
                if k in wanted:
                    out.append((k, m.name, m.size, expand(k, m.name, data)))
        return out, "whole"
    left = set(wanted)
    for name, start, size in tar_index(s3, key):
        k = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if k in left:
            data = s3.get(key, start, size) if size else b""
            out.append((k, name, size, expand(k, name, data)))
            left.discard(k)
            if not left:
                break
    return out, "range"


# ---------------------------------------------------------------- 状態

def ensure_state(tr):
    tr.q("CREATE SCHEMA IF NOT EXISTS iceberg.arxiv_sync")
    tr.q("""CREATE TABLE IF NOT EXISTS iceberg.arxiv_sync.src_chunks (
              filename varchar, md5sum varchar, timestamp varchar, yymm varchar, seq_num integer,
              first_item varchar, last_item varchar, num_items integer, size bigint,
              processed_at timestamp(6), note varchar)""")
    tr.q("""CREATE TABLE IF NOT EXISTS iceberg.arxiv_sync.src_payloads (
              paper_id varchar, tar varchar, member varchar, member_size bigint, tar_timestamp varchar,
              n_files integer, synced_at timestamp(6))""")


def sql_str(s):
    return "NULL" if s is None else "'" + str(s).replace("'", "''") + "'"


class Raw(str):
    """SQL の式としてそのまま埋め込む値."""


NOW = Raw("CAST(current_timestamp AS timestamp(6))")


def sql_val(v):
    if isinstance(v, Raw):
        return str(v)
    if v is None or isinstance(v, str):
        return sql_str(v)
    return str(v)


def insert_values(tr, table, cols, rows, batch=500):
    for i in range(0, len(rows), batch):
        vals = ", ".join("(" + ", ".join(sql_val(v) for v in r) + ")" for r in rows[i:i + batch])
        tr.q(f"INSERT INTO {table} ({', '.join(cols)}) VALUES {vals}")


def mark_chunks(tr, chunks, note):
    cols = ["filename", "md5sum", "timestamp", "yymm", "seq_num", "first_item", "last_item", "num_items", "size",
            "processed_at", "note"]
    insert_values(tr, "iceberg.arxiv_sync.src_chunks", cols,
                  [[c[k] for k in cols[:9]] + [NOW, note] for c in chunks])


# ---------------------------------------------------------------- 書き込み

class Writer:
    def __init__(self, tr, fs, rid):
        self.tr, self.fs, self.rid = tr, fs, rid
        self.part = 0
        self.papers = {}       # key → (pid, chunk, member, member_size, files)
        self.chunks = []
        self.bytes = 0
        self.stats = {"papers": 0, "files": 0, "bytes": 0}

    def add(self, chunk, results):
        for k, member, msize, files in results:
            old = self.papers.get(k)
            if old and old[1]["timestamp"] > chunk["timestamp"]:
                continue
            self.papers[k] = (pid_of(k), chunk, member, msize, files)
            self.bytes += sum(len(f[2]) for f in files)
        self.chunks.append(chunk)

    def flush(self):
        if not self.chunks:
            return
        tr = self.tr
        papers = sorted(self.papers.values(), key=lambda p: p[0])
        if papers:
            ids = ", ".join(sql_str(p[0]) for p in papers)
            # 置き換え: 既存の行を消してから追加する (merge-on-read の削除ファイル. 元の parquet は書き換えない)
            tr.q(f"DELETE FROM iceberg.arxiv.source WHERE paper_id IN ({ids})")
            tr.q(f"DELETE FROM iceberg.arxiv.latex WHERE paper_id IN ({ids})")
            tr.q(f"DELETE FROM iceberg.arxiv.paper_files WHERE kind = 'source' AND paper_id IN ({ids})")
            src, lat, meta = [], [], []
            for pid, chunk, member, msize, files in papers:
                for path, penc, data in sorted(files, key=lambda f: f[0]):
                    sha = hashlib.sha256(data).hexdigest()
                    src.append({"paper_id": pid, "version": None, "kind": "source", "source": "s3_src_extracted",
                                "path": path, "size": len(data), "sha256": sha, "content": data})
                    meta.append({"paper_id": pid, "version": None, "kind": "source", "source": "s3_src_extracted",
                                 "path": path, "path_encoding": penc, "size": len(data), "sha256": sha,
                                 "stored_as": None, "stored_sha256": None, "stored_size": None})
                    m = EXT_RE.search(path)
                    ext = m.group(1).lower() if m else None
                    if ext in LATEX_EXT:
                        lat.append({"paper_id": pid, "path": path, "ext": ext, "size": len(data), "sha256": sha,
                                    "content": data})
            # 中身は 2000 行ずつ arrow にして書く (全部を一度に arrow にすると中身の 2 倍のメモリが要る)
            base = f"sync/{self.rid}/{self.part:05d}"
            # 中身の列: 行グループ 200 行 (HF と同程度)
            add_files(tr, self.fs, "source", f"arxiv/source/data/{base}",
                      (pa.Table.from_pylist(src[i:i + 2000], SOURCE_SCHEMA) for i in range(0, len(src), 2000)), 200)
            add_files(tr, self.fs, "latex", f"arxiv/latex/data/{base}",
                      (pa.Table.from_pylist(lat[i:i + 5000], LATEX_SCHEMA) for i in range(0, len(lat), 5000)), 200)
            add_files(tr, self.fs, "paper_files", f"arxiv/paper_files/data/{base}-src",
                      [pa.Table.from_pylist(meta, FILES_SCHEMA)], 100000)
            insert_values(tr, "iceberg.arxiv_sync.src_payloads",
                          ["paper_id", "tar", "member", "member_size", "tar_timestamp", "n_files", "synced_at"],
                          [[pid, c["filename"], member, msize, c["timestamp"], len(files),
                            NOW] for pid, c, member, msize, files in papers])
            self.stats["papers"] += len(papers)
            self.stats["files"] += len(src)
            self.stats["bytes"] += self.bytes
        mark_chunks(tr, self.chunks, "synced")
        log.info("part %d: %d papers, %d chunks (total papers=%d files=%d %.1f GB)", self.part, len(papers),
                 len(self.chunks), self.stats["papers"], self.stats["files"], self.stats["bytes"] / 1e9)
        self.part += 1
        self.papers, self.chunks, self.bytes = {}, [], 0


# ---------------------------------------------------------------- 本体

def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="2026-09-05", help="HF のデータセットのソースの時点")
    ap.add_argument("--new-since", default="2026-08-01", help="この日以降に投稿された論文はソースがなければ取得する")
    ap.add_argument("--init-baseline", action="store_true",
                    help="初回のみ: manifest の tar のうち baseline 以前のものを処理済みとして記録する")
    ap.add_argument("--whole-ratio", type=float, default=0.3)
    ap.add_argument("--max-gb", type=float, default=200, help="1 回の取得量の上限 (GB)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--flush-gb", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true", help="対象の tar と論文の数を表示するだけ")
    a = ap.parse_args()

    tr, fs, s3, rid = Trino(), rustfs(), S3(), run_id()
    ensure_state(tr)
    body = s3.get("src/arXiv_src_manifest.xml")
    put_bytes(fs, f"{RAW}/s3/manifests/{rid}/arXiv_src_manifest.xml.gz", gzip.compress(body))
    manifest = parse_manifest(body)
    log.info("run %s: manifest %d chunks", rid, len(manifest))

    if a.init_baseline:
        if tr.one("SELECT count(*) FROM iceberg.arxiv_sync.src_chunks"):
            raise SystemExit("src_chunks is not empty")
        base = [c for c in manifest if c["timestamp"] <= a.baseline]
        mark_chunks(tr, base, f"baseline {a.baseline}")
        log.info("baseline: %d chunks marked as processed", len(base))
        return

    done = {(r[0], r[1]) for r in tr.q("SELECT filename, md5sum FROM iceberg.arxiv_sync.src_chunks")}
    changed = [c for c in manifest if (c["filename"], c["md5sum"]) not in done]
    log.info("changed chunks: %d (%.1f GB)", len(changed), sum(c["size"] for c in changed) / 1e9)

    # 必要な論文と, 今あるソースの時点 (None = ソースなし)
    need = {}
    for pid, have_ts in tr.q(f"""
        WITH src AS (SELECT DISTINCT paper_id FROM iceberg.arxiv.paper_files WHERE kind = 'source'),
             pay AS (SELECT paper_id, max(tar_timestamp) AS ts FROM iceberg.arxiv_sync.src_payloads GROUP BY 1),
             lv  AS (SELECT paper_id, min(version_date) AS first_d, max(version_date) AS last_d
                     FROM iceberg.arxiv.versions WHERE NOT is_metadata_only GROUP BY 1)
        SELECT lv.paper_id,
               coalesce(pay.ts, CASE WHEN src.paper_id IS NOT NULL THEN '{a.baseline} 00:00:00' END) AS have_ts
        FROM lv LEFT JOIN src ON src.paper_id = lv.paper_id LEFT JOIN pay ON pay.paper_id = lv.paper_id
        WHERE (src.paper_id IS NULL AND pay.paper_id IS NULL AND lv.first_d >= TIMESTAMP '{a.new_since} 00:00:00')
           OR lv.last_d > CAST(coalesce(pay.ts, '{a.baseline} 00:00:00') AS timestamp(6))"""):
        need[key_of(pid)] = have_ts
    log.info("papers needing source: %d (new: %d)", len(need), sum(v is None for v in need.values()))

    # 変わった tar ごとに, その範囲に入る必要な論文 (tar がソースより新しいもの)
    by_yymm = {}
    for k in need:
        by_yymm.setdefault(yymm_of(pid_of(k)), []).append(k)
    plan, untouched = [], []
    for c in changed:
        ks = [k for k in by_yymm.get(c["yymm"], [])
              if c["first_item"] <= k <= c["last_item"] and (need[k] is None or c["timestamp"] > need[k])]
        (plan if ks else untouched).append((c, set(ks)))
    whole = [p for p in plan if len(p[1]) >= p[0]["num_items"] * a.whole_ratio]
    log.info("chunks to read: %d (whole: %d, %.1f GB; range: %d), papers: %d; chunks without needed papers: %d",
             len(plan), len(whole), sum(p[0]["size"] for p in whole) / 1e9, len(plan) - len(whole),
             sum(len(p[1]) for p in plan), len(untouched))
    if a.dry_run:
        return

    # 必要な論文を含まない tar は処理済みにする
    mark_chunks(tr, [c for c, _ in untouched], "no needed papers")
    w = Writer(tr, fs, rid)
    plan.sort(key=lambda p: p[0]["filename"])
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {}
        it = iter(plan)
        stop = False
        while True:
            while not stop and len(futs) < a.workers:
                try:
                    c, ks = next(it)
                except StopIteration:
                    break
                if s3.bytes > a.max_gb * 1e9:
                    log.warning("reached --max-gb %.0f; stopping (the rest will be read next time)", a.max_gb)
                    stop = True
                    break
                futs[ex.submit(fetch_chunk, s3, c, ks, a.whole_ratio)] = c
            if not futs:
                break
            fin, _ = cf.wait(futs, return_when=cf.FIRST_COMPLETED)
            for f in fin:
                c = futs.pop(f)
                results, mode = f.result()
                log.info("%s (%s): %d papers, downloaded so far %.1f GB", c["filename"], mode, len(results), s3.bytes / 1e9)
                w.add(c, results)
            if w.bytes > a.flush_gb * 1e9:
                w.flush()
    w.flush()
    log.info("done: papers=%d files=%d content %.1f GB, downloaded %.1f GB", w.stats["papers"], w.stats["files"],
             w.stats["bytes"] / 1e9, s3.bytes / 1e9)
    log.info("OK")


if __name__ == "__main__":
    main()
