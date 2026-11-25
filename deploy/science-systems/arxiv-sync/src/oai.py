"""OAI-PMH (arXivRaw) の差分を取り込み, iceberg.arxiv.metadata / versions に MERGE する (日次).

- 取得範囲: from = metadata の oai_datestamp の最大値の前日 (重なりは MERGE で吸収する). --from で指定もできる
- 応答の XML は s3://arxiv-raw/oai/arXivRaw/<run>/ に gzip で保存する (再処理と検証用)
- metadata は全列を置き換える. versions は OAI 由来の列のみ更新し, ファイル由来の列 (has_pdf, pdf_sha256 など) は残す.
  新しい版はファイル由来の列を「なし」で追加する (pdf の取り込みで埋める)
- 同じ論文が複数回現れた場合は最後のレコードを使う (_seq)
"""

import argparse
import datetime as dt
import gzip
import logging
import time
import xml.etree.ElementTree as ET

import pyarrow as pa
import requests

from common import RAW, Staging, Trino, put_bytes, run_id, rustfs, setup_logging

log = logging.getLogger("arxiv_sync.oai")

ENDPOINT = "https://oaipmh.arxiv.org/oai"
NS = {"o": "http://www.openarchives.org/OAI/2.0/", "r": "http://arxiv.org/OAI/arXivRaw/"}
UA = "homecluster-arxiv-sync/0.1 (OAI-PMH harvester)"

META_SCHEMA = pa.schema([
    ("paper_id", pa.string()), ("title", pa.string()), ("authors", pa.string()), ("abstract", pa.string()),
    ("categories", pa.string()), ("primary_category", pa.string()), ("submitter", pa.string()),
    ("license", pa.string()), ("doi", pa.string()), ("journal_ref", pa.string()), ("comments", pa.string()),
    ("report_no", pa.string()), ("msc_class", pa.string()), ("acm_class", pa.string()), ("proxy", pa.string()),
    ("n_versions", pa.int32()), ("first_version_date", pa.timestamp("us")), ("latest_version_date", pa.timestamp("us")),
    ("oai_datestamp", pa.string()), ("oai_sets", pa.list_(pa.string())), ("arxiv_abs_url", pa.string()),
    ("_seq", pa.int64()),
])
META_COLS = [f.name for f in META_SCHEMA if f.name != "_seq"]

VER_SCHEMA = pa.schema([
    ("paper_id", pa.string()), ("version", pa.int32()), ("version_date", pa.timestamp("us")),
    ("version_date_raw", pa.string()), ("reported_size_kb", pa.int32()), ("is_metadata_only", pa.bool_()),
    ("license", pa.string()), ("arxiv_pdf_url", pa.string()), ("is_latest_version", pa.bool_()),
    ("_seq", pa.int64()),
])

# 要素名 → metadata の列
FIELDS = {
    "submitter": "submitter", "title": "title", "authors": "authors", "abstract": "abstract",
    "categories": "categories", "comments": "comments", "proxy": "proxy", "report-no": "report_no",
    "msc-class": "msc_class", "acm-class": "acm_class", "journal-ref": "journal_ref", "doi": "doi",
    "license": "license",
}


def parse_date(s):
    # "Mon, 21 Jul 2025 06:57:06 GMT"
    return dt.datetime.strptime(s.strip(), "%a, %d %b %Y %H:%M:%S %Z")


def parse_record(rec, seq):
    """1 レコード → (metadata の行, versions の行のリスト). 削除レコードは None."""
    h = rec.find("o:header", NS)
    if h.get("status") == "deleted":
        return None
    raw = rec.find("o:metadata/r:arXivRaw", NS)
    pid = raw.findtext("r:id", namespaces=NS).strip()
    m = {c: None for c in META_COLS}
    m["paper_id"] = pid
    for el, col in FIELDS.items():
        e = raw.find(f"r:{el}", NS)
        if e is not None:
            # HF のデータセットと同じく前後の空白を除く (abstract は先頭に空白がある).
            # acm_class は OAI の値をそのまま使う (HF のデータセットは msc_class と同じ値になっていたが, 現在の OAI は正しい値を返す)
            m[col] = (e.text or "").strip()
    vers = []
    for v in raw.findall("r:version", NS):
        n = int(v.get("version").lstrip("v"))
        date_raw = v.findtext("r:date", namespaces=NS).strip()
        size = v.findtext("r:size", namespaces=NS).strip()
        kb = int(size[:-2]) if size.endswith("kb") else int(size)
        vers.append({
            "paper_id": pid, "version": n, "version_date": parse_date(date_raw), "version_date_raw": date_raw,
            "reported_size_kb": kb, "is_metadata_only": kb == 0, "license": m["license"],
            "arxiv_pdf_url": f"https://arxiv.org/pdf/{pid}v{n}", "is_latest_version": False, "_seq": seq,
        })
    vers.sort(key=lambda r: r["version"])
    if vers:
        vers[-1]["is_latest_version"] = True
    m["primary_category"] = (m["categories"] or "").split(" ")[0] or None
    m["n_versions"] = len(vers)
    m["first_version_date"] = vers[0]["version_date"] if vers else None
    m["latest_version_date"] = vers[-1]["version_date"] if vers else None
    m["oai_datestamp"] = h.findtext("o:datestamp", namespaces=NS)
    m["oai_sets"] = [s.text for s in h.findall("o:setSpec", NS)]
    m["arxiv_abs_url"] = f"https://arxiv.org/abs/{pid}"
    m["_seq"] = seq
    return m, vers


def fetch(session, params):
    """OAI-PMH の 1 ページ. 503 (Retry-After) と一時的な失敗は待って再試行する."""
    for attempt in range(10):
        try:
            r = session.get(ENDPOINT, params=params, timeout=300)
        except requests.RequestException as e:
            wait = 30 * (attempt + 1)
            log.warning("request failed (%s), retry in %ds", e, wait)
            time.sleep(wait)
            continue
        if r.status_code == 503:
            wait = int(r.headers.get("Retry-After", "30"))
            log.info("503, retry after %ds", wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.content
    raise RuntimeError("OAI-PMH: too many retries")


def harvest(fs, rid, from_date, until_date, delay):
    """ページごとに (metadata の行, versions の行) を返す."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    params = {"verb": "ListRecords", "metadataPrefix": "arXivRaw", "from": from_date}
    if until_date:
        params["until"] = until_date
    page, seq = 0, 0
    while True:
        body = fetch(s, params)
        put_bytes(fs, f"{RAW}/oai/arXivRaw/{rid}/{page:05d}.xml.gz", gzip.compress(body))
        root = ET.fromstring(body)
        err = root.find("o:error", NS)
        if err is not None:
            if err.get("code") == "noRecordsMatch":
                log.info("no records")
                return
            raise RuntimeError(f"OAI-PMH error {err.get('code')}: {err.text}")
        lr = root.find("o:ListRecords", NS)
        metas, vers, deleted = [], [], 0
        for rec in lr.findall("o:record", NS):
            seq += 1
            p = parse_record(rec, seq)
            if p is None:
                deleted += 1
                continue
            metas.append(p[0])
            vers.extend(p[1])
        tok = lr.find("o:resumptionToken", NS)
        log.info("page %d: %d records (%d deleted), total %s", page, len(metas), deleted,
                 tok.get("completeListSize") if tok is not None else "?")
        yield metas, vers
        page += 1
        if tok is None or not (tok.text or "").strip():
            return
        params = {"verb": "ListRecords", "resumptionToken": tok.text.strip()}
        time.sleep(delay)


def merge(tr, sm, sv):
    dedup_m = (f"(SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY paper_id ORDER BY _seq DESC) AS rn "
               f"FROM {sm.table}) WHERE rn = 1)")
    sets = ", ".join(f"{c} = s.{c}" for c in META_COLS if c != "paper_id")
    tr.q(f"""MERGE INTO iceberg.arxiv.metadata t USING {dedup_m} s ON t.paper_id = s.paper_id
             WHEN MATCHED THEN UPDATE SET {sets}
             WHEN NOT MATCHED THEN INSERT ({", ".join(META_COLS)}) VALUES ({", ".join("s." + c for c in META_COLS)})""")
    # 版は論文ごとに最後のレコードのものだけ使う
    latest_v = (f"(SELECT v.* FROM {sv.table} v JOIN (SELECT paper_id, max(_seq) AS s FROM {sv.table} GROUP BY 1) m "
                f"ON v.paper_id = m.paper_id AND v._seq = m.s)")
    tr.q(f"""MERGE INTO iceberg.arxiv.versions t USING {latest_v} s
             ON t.paper_id = s.paper_id AND t.version = s.version
             WHEN MATCHED THEN UPDATE SET version_date = s.version_date, version_date_raw = s.version_date_raw,
               reported_size_kb = s.reported_size_kb, is_metadata_only = s.is_metadata_only, license = s.license,
               arxiv_pdf_url = s.arxiv_pdf_url, is_latest_version = s.is_latest_version
             WHEN NOT MATCHED THEN INSERT (paper_id, version, version_date, version_date_raw, reported_size_kb,
               is_metadata_only, has_pdf, pdf_sha256, has_ps, n_ps_files, ps_bytes, has_html, n_html_files,
               source_version_proven, license, arxiv_pdf_url, is_latest_version, is_latest_pdf, is_latest_ps)
             VALUES (s.paper_id, s.version, s.version_date, s.version_date_raw, s.reported_size_kb,
               s.is_metadata_only, false, NULL, false, 0, 0, false, 0,
               false, s.license, s.arxiv_pdf_url, s.is_latest_version, false, false)""")


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_date", help="YYYY-MM-DD (既定: metadata の oai_datestamp の最大値の前日)")
    ap.add_argument("--until", dest="until_date")
    ap.add_argument("--delay", type=float, default=5.0, help="ページの間の待ち時間 (秒)")
    ap.add_argument("--dry-run", action="store_true", help="取得と解析のみ (テーブルは変更しない)")
    a = ap.parse_args()

    tr, fs, rid = Trino(), rustfs(), run_id()
    from_date = a.from_date
    if not from_date:
        last = tr.one("SELECT max(oai_datestamp) FROM iceberg.arxiv.metadata")
        from_date = (dt.date.fromisoformat(last) - dt.timedelta(days=1)).isoformat()
    log.info("run %s: harvest from %s until %s", rid, from_date, a.until_date or "-")

    sm = Staging(tr, fs, f"oai_m_{rid}", META_SCHEMA)
    sv = Staging(tr, fs, f"oai_v_{rid}", VER_SCHEMA)
    try:
        mt, vt, nm = [], [], 0
        for metas, vers in harvest(fs, rid, from_date, a.until_date, a.delay):
            mt.append(pa.Table.from_pylist(metas, META_SCHEMA))
            vt.append(pa.Table.from_pylist(vers, VER_SCHEMA))
            nm += len(metas)
        log.info("harvested %d records", nm)
        if a.dry_run or nm == 0:
            return
        # 数百ページ分でも metadata は数百 MB 程度. ページごとのファイルにせずまとめる
        sm.load([pa.concat_tables(mt)])
        sv.load([pa.concat_tables(vt)])
        papers_before = tr.one("SELECT count(*) FROM iceberg.arxiv.metadata")
        merge(tr, sm, sv)
        papers_after = tr.one("SELECT count(*) FROM iceberg.arxiv.metadata")
        new_versions = tr.one(f"SELECT count(*) FROM iceberg.arxiv.versions WHERE NOT has_pdf AND NOT is_metadata_only")
        log.info("merged: papers %d -> %d (+%d). versions without pdf (to fetch): %d",
                 papers_before, papers_after, papers_after - papers_before, new_versions)
        log.info("OK")
    finally:
        sm.drop()
        sv.drop()


if __name__ == "__main__":
    main()
