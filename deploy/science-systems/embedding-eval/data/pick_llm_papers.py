"""要旨から質問を作る論文を選ぶ: 文書集合 (corpus.parquet) から, 引用の文脈の正解を除き, 要旨 400 文字以上の論文を 13 層で 8 件ずつ.

  python pick_llm_papers.py corpus.parquet cite_kept.parquet llm_papers.json
選んだ論文の要旨を読んで質問 (英語と日本語) を書き, llm_questions.json に置く (2026-11-28 は Claude が書いた. 題名をなぞらない言い方にする)
"""
import collections, json, random, sys
import pyarrow.parquet as pq
from cite_filter import stratum

t = pq.read_table(sys.argv[1]).to_pylist()
gold = {r["gold_id"] for r in pq.read_table(sys.argv[2]).to_pylist()}
random.seed(5)
random.shuffle(t)
pick, n = [], collections.Counter()
for r in t:
    if r["paper_id"] in gold or len(r["abstract"] or "") < 400:
        continue
    s = stratum(r["primary_category"])
    if n[s] < 8:
        n[s] += 1
        pick.append(r)
json.dump(pick, open(sys.argv[3], "w"), ensure_ascii=False)
print(len(pick))
