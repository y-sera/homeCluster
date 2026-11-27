"""引用元の候補: 狭い paper_id の範囲 (ファイルの統計で読み飛ばせる) から paper_text と metadata を取る."""
import os, sys, trino, pyarrow as pa, pyarrow.parquet as pq
from trino.auth import BasicAuthentication
conn = trino.dbapi.connect(host="trino.cluster.orenet.net", port=443, http_scheme="https", user="admin",
    auth=BasicAuthentication("admin", os.environ["TRINO_PASSWORD"]), catalog="iceberg")
cur = conn.cursor()
rows = []
for lo, hi in (r.split(":") for r in sys.argv[2:]):
    cur.execute(f"""SELECT p.paper_id, p.text, m.primary_category, m.first_version_date FROM arxiv.paper_text p
                    JOIN arxiv.metadata m ON m.paper_id = p.paper_id
                    WHERE p.paper_id >= '{lo}' AND p.paper_id < '{hi}'""")
    got = cur.fetchall()
    rows += [dict(paper_id=a, text=b, primary_category=c, first_date=str(d)) for a, b, c, d in got]
    print(lo, hi, len(got), file=sys.stderr)
pq.write_table(pa.Table.from_pylist(rows), sys.argv[1], compression="zstd")
