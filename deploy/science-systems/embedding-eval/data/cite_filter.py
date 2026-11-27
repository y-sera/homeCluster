"""引用の文脈の質問を絞る: 引用先を指すだけの文 (see Ref. など) や図・式の参照を除き, 1 論文 2 件まで, 層別に抽出."""
import random, re, sys, collections
import pyarrow as pa, pyarrow.parquet as pq
POINTER = re.compile(r"\b(?:Refs?|Fig(?:s|ure)?|Eqs?|Equations?|Tables?|Sec(?:tion)?s?|Appendix|Chapter|Theorem|Lemma|Proposition|Corollary|Definition)\b\.?|\bsee\b|\bcf\.|\bet al\.?\b|\bthe authors\b", re.I)
BAD_START = re.compile(r"^(?:In|By|From|Following|See|As in|As shown in|As)\s*[,.]")
def stratum(cat):
    cat = cat or ""
    if cat in ("cs.AI", "cs.LG", "cs.CL", "cs.CV"): return cat
    h = cat.split(".")[0]
    if h == "cs": return "cs.other"
    if h.startswith("hep"): return "hep"
    if h in ("math", "astro-ph", "cond-mat", "quant-ph", "physics", "stat"): return h
    return "other"


def main():
    rows = pq.read_table(sys.argv[1]).to_pylist()
    ok = [r for r in rows if not POINTER.search(r["query"]) and not BAD_START.match(r["query"]) and "$" not in r["query"][:3]]
    random.seed(20261128)
    random.shuffle(ok)
    per = collections.Counter(); seen_q = set(); kept = []
    for r in ok:
        if per[r["citing_id"]] >= 2 or r["query"] in seen_q: continue
        per[r["citing_id"]] += 1; seen_q.add(r["query"]); r["stratum"] = stratum(r["primary_category"]); kept.append(r)
    print(len(rows), "->", len(ok), "->", len(kept), collections.Counter(r["stratum"] for r in kept).most_common())
    pq.write_table(pa.Table.from_pylist(kept), sys.argv[2])


if __name__ == "__main__":
    main()
