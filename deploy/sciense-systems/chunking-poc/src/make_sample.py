"""チャンク作成の検証用に paper_text から層別のサンプルを作る (設計メモ 5.7).

  python make_sample.py --src /src/paper_text --out /work/sample10k.parquet --population /work/population.json

1. 全シャードの primary_category だけを読み, 層 (process.stratum) ごとの論文数を数える (population.json)
2. 全行グループ (1 つ 500 行) から --row-groups 個をランダムに選ぶ (paper_id 順に並んでいるため,
   選んだ行グループは時期の異なる区間になる). 論文 1 件ずつ選ぶと全体 (66GB) をほぼ読むことになるため
3. 選んだ行グループの中から, 層ごとに --per-stratum 件前後になる確率で論文を選ぶ
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import random
import sys

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(__file__))
from process import stratum  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/src/paper_text")
    ap.add_argument("--out", required=True)
    ap.add_argument("--population", required=True)
    ap.add_argument("--row-groups", type=int, default=300)
    ap.add_argument("--per-stratum", type=int, default=770)
    ap.add_argument("--seed", type=int, default=20261122)
    a = ap.parse_args()
    files = sorted(glob.glob(f"{a.src}/*.parquet"))

    # 1. 層ごとの論文数 (と行グループの一覧)
    pop = collections.Counter()
    rgs = []
    for f in files:
        pf = pq.ParquetFile(f)
        for i in range(pf.metadata.num_row_groups):
            rgs.append((f, i, pf.metadata.row_group(i).num_rows))
        t = pf.read(columns=["primary_category"])
        pop.update(stratum(c) for c in t.column(0).to_pylist())
        print(f"  {os.path.basename(f)}: {pf.metadata.num_rows} rows", flush=True)
    N = sum(pop.values())
    json.dump(dict(pop), open(a.population, "w"), indent=1)
    print("population", N, dict(pop.most_common()), flush=True)

    # 2. 行グループを選ぶ
    rnd = random.Random(a.seed)
    chosen = sorted(rnd.sample(rgs, a.row_groups))
    pool_rows = sum(r[2] for r in chosen)
    prob = {s: min(1.0, a.per_stratum / (pop[s] / N * pool_rows)) for s in pop}
    print("pool rows", pool_rows, "prob", {k: round(v, 3) for k, v in prob.items()}, flush=True)

    # 3. 層ごとの確率で選ぶ
    writer = None
    got = collections.Counter()
    for k, (f, i, _) in enumerate(chosen, 1):
        t = pq.ParquetFile(f).read_row_group(i)
        cats = t.column("primary_category").to_pylist()
        keep = [j for j, c in enumerate(cats) if rnd.random() < prob[stratum(c)]]
        if keep:
            sub = t.take(pa.array(keep))
            got.update(stratum(cats[j]) for j in keep)
            if writer is None:
                writer = pq.ParquetWriter(a.out, sub.schema, compression="zstd")
            writer.write_table(sub)
        if k % 50 == 0:
            print(f"  {k}/{len(chosen)} row groups, selected {sum(got.values())}", flush=True)
    writer.close()
    print("selected", sum(got.values()), dict(sorted(got.items())), flush=True)


if __name__ == "__main__":
    main()
