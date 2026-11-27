"""文書集合: metadata の 10% (固定の種で抽出) + 引用の文脈の正解. 列: paper_id, title, abstract, categories, primary_category, year."""
import os, sys, trino, pyarrow as pa, pyarrow.parquet as pq
from trino.auth import BasicAuthentication
conn = trino.dbapi.connect(host="trino.cluster.orenet.net", port=443, http_scheme="https", user="admin",
    auth=BasicAuthentication("admin", os.environ["TRINO_PASSWORD"]), catalog="iceberg")
cur = conn.cursor()
gold = sorted({r["gold_id"] for r in pq.read_table(sys.argv[1]).to_pylist()})
cols = "paper_id, title, abstract, categories, primary_category, year(first_version_date)"
# 再現できるよう, 乱数ではなく paper_id のハッシュで 10% を選ぶ
cur.execute(f"SELECT {cols} FROM arxiv.metadata WHERE abstract IS NOT NULL AND from_big_endian_64(substr(xxhash64(to_utf8(paper_id)), 1, 8)) % 10 = 0")
rows = cur.fetchall()
ids = {r[0] for r in rows}
g = []
for i in range(0, len(gold), 500):
    lst = ", ".join("'" + x.replace("'", "''") + "'" for x in gold[i:i+500])
    cur.execute(f"SELECT {cols} FROM arxiv.metadata WHERE paper_id IN ({lst})")
    g += [r for r in cur.fetchall() if r[0] not in ids]
names = ["paper_id", "title", "abstract", "categories", "primary_category", "year"]
t = pa.Table.from_pylist([dict(zip(names, r)) for r in rows + g])
pq.write_table(t, sys.argv[2], compression="zstd")
found = {r[0] for r in rows} | {r[0] for r in g}
print(f"sample {len(rows)}, gold {len(gold)} (in metadata {sum(1 for x in gold if x in found)}), corpus {t.num_rows}")
