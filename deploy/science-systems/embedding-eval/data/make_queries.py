"""評価の質問を 1 つにまとめる: 引用の文脈 500 件 (13 層で均等) + LLM の質問 (英語・日本語) → queries.parquet.

  python make_queries.py cite_kept.parquet corpus.parquet llm_questions.json llm_papers.json queries.parquet [manual_questions.json]

manual_questions.json (手作りの質問, 任意): [{"query": "...", "gold_ids": ["2401.01234", ...], "note": "..."}]
  正解は複数でよい (評価では最初に見つかった正解の順位を使う). 正解の論文は文書集合 (corpus.parquet) に入れておく
"""
import collections, json, random, sys
import pyarrow as pa, pyarrow.parquet as pq

cite_kept, corpus_p, llm_q, llm_papers, out_p = sys.argv[1:6]
corpus = set(pq.read_table(corpus_p, columns=["paper_id"]).column(0).to_pylist())
cite = [r for r in pq.read_table(cite_kept).to_pylist() if r["gold_id"] in corpus]
random.seed(42)
by = collections.defaultdict(list)
for r in cite:
    by[r["stratum"]].append(r)
order = sorted(by, key=lambda s: len(by[s]))
out, left = [], 500
for i, s in enumerate(order):
    k = min(len(by[s]), left // (len(order) - i))
    out += random.sample(by[s], k)
    left -= k
rows = [{"qid": f"cite-{i:04d}", "set": "cite", "query": r["query"], "gold_id": r["gold_id"], "exclude_id": r["citing_id"],
         "stratum": r["stratum"]} for i, r in enumerate(out)]
pick = {p["paper_id"]: p for p in json.load(open(llm_papers))}
for i, q in enumerate(json.load(open(llm_q))):
    cat = pick[q["paper_id"]]["primary_category"]
    rows.append({"qid": f"llm-en-{i:03d}", "set": "llm_en", "query": q["en"], "gold_id": q["paper_id"], "exclude_id": None, "stratum": cat})
    rows.append({"qid": f"llm-ja-{i:03d}", "set": "llm_ja", "query": q["ja"], "gold_id": q["paper_id"], "exclude_id": None, "stratum": cat})
if len(sys.argv) > 6:
    for i, q in enumerate(json.load(open(sys.argv[6]))):
        rows.append({"qid": f"manual-{i:03d}", "set": "manual", "query": q["query"], "gold_id": q["gold_ids"][0],
                     "gold_ids": q["gold_ids"], "exclude_id": None, "stratum": None})
for r in rows:
    r.setdefault("gold_ids", [r["gold_id"]])
pq.write_table(pa.Table.from_pylist(rows), out_p)
print(len(rows), collections.Counter(r["set"] for r in rows))
