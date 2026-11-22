"""process.py の出力から統計を出す (設計メモ 5.7 の「取る値」).

  python report.py --run /work/out/run1 [--population /work/population.json] [--json out.json]

--population (make_sample.py が出力する層ごとの論文数) を渡すと, 層の大きさで重み付けした値と
全件 (paper_text 2,856,227 件) への外挿も出す.
"""
from __future__ import annotations

import argparse
import collections
import json

import pyarrow.parquet as pq


def quant(v, ps=(50, 95)):
    v = sorted(v)
    if not v:
        return {}
    r = {"mean": round(sum(v) / len(v), 1)}
    for p in ps:
        r[f"p{p}"] = v[min(len(v) - 1, int(len(v) * p / 100))]
    return r


def wmean(rows, f, w):
    tw = sum(w[r["stratum"]] for r in rows)
    return sum(f(r) * w[r["stratum"]] for r in rows) / tw if tw else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--population")
    ap.add_argument("--json")
    a = ap.parse_args()
    P = pq.read_table(f"{a.run}/papers.parquet").to_pylist()
    C = pq.read_table(f"{a.run}/chunks.parquet",
                      columns=["paper_id", "n_tokens", "content_type", "section_type", "content_hash", "text"]).to_pylist()
    run = json.load(open(f"{a.run}/run.json"))
    out = {"papers": len(P), "wall_s": round(run["wall_s"]), "workers": run["workers"]}

    err = [r for r in P if r["error"]]
    ok = [r for r in P if not r["error"]]
    out["errors"] = dict(collections.Counter(r["error"].split(":")[0] for r in err))
    empty = [r for r in ok if r["body_chars"] < 200]
    out["normalize_ok_rate"] = round(len([r for r in ok if r["body_chars"] >= 200]) / len(P), 4)
    out["empty_body(<200 chars)"] = len(empty)
    out["empty_breakdown"] = {
        "auto-ignore": sum(r["stub_autoignore"] for r in empty),
        "includepdf": sum(r["stub_includepdf"] for r in empty),
        "text<1000": sum(r["text_chars"] < 1000 for r in empty),
        "other": sum(not r["stub_autoignore"] and not r["stub_includepdf"] and r["text_chars"] >= 1000 for r in empty),
    }
    src_sec = [r for r in ok if r["src_has_sec"] and r["body_chars"] >= 200]
    out["section_extract_rate (src has \\section)"] = round(sum(r["n_sections"] > 0 for r in src_sec) / max(1, len(src_sec)), 4)
    out["papers_without_section_cmd"] = sum(not r["src_has_sec"] for r in ok if r["body_chars"] >= 200)
    body = [r for r in ok if r["body_chars"] >= 200]
    out["sections/paper"] = quant([r["n_sections"] for r in body])
    out["chunks/paper"] = quant([r["n_chunks"] for r in body])
    out["tokens/chunk"] = quant([c["n_tokens"] for c in C], (5, 50, 95, 99))
    out["tokens/paper(chunks)"] = quant([r["chunk_tokens"] for r in body])
    b = collections.Counter()
    for c in C:
        n = c["n_tokens"]
        b["<100" if n < 100 else "100-499" if n < 500 else "500-800" if n <= 800 else "801-1000" if n <= 1000 else ">1000"] += 1
    out["chunk_size_buckets"] = {k: f"{v} ({v / len(C):.1%})" for k, v in sorted(b.items())}
    out["content_type"] = dict(collections.Counter(c["content_type"] for c in C))
    out["tables/paper"] = quant([r["n_tables"] for r in body])
    out["chunk_section_type"] = dict(collections.Counter(c["section_type"] for c in C).most_common())
    h = collections.Counter(c["content_hash"] for c in C)
    dup = sum(v for v in h.values() if v > 1)
    out["dup_chunk_rate"] = f"{dup / len(C):.2%} ({dup} chunks in {sum(1 for v in h.values() if v > 1)} groups)"
    out["has_abstract_env"] = round(sum(r["has_abstract"] for r in body) / len(body), 3)
    out["has_thebibliography"] = round(sum(r["has_references"] for r in body) / len(body), 3)
    out["body/raw chars"] = quant([r["body_chars"] / max(1, r["raw_body_chars"]) for r in body], (5, 50, 95))
    out["unknown_cmds/1k chars"] = quant([1000 * r["n_unknown_cmds"] / max(1, r["body_chars"]) for r in body], (50, 95))
    out["prog_dropped"] = {"papers": sum(r.get("dropped_prog_chars", 0) > 0 for r in body),
                           "chars_share": round(sum(r.get("dropped_prog_chars", 0) for r in body) / max(1, sum(r["raw_body_chars"] for r in body)), 4)}
    out["t_paper_s"] = quant([r["t_parse"] + r["t_chunk"] for r in ok], (50, 95, 99))
    out["t_paper_max_s"] = round(max(r["t_parse"] + r["t_chunk"] for r in ok), 2)
    chars = sum(len(c["text"]) for c in C)
    out["chunk_text_bytes(utf8)"] = sum(len(c["text"].encode()) for c in C)

    if a.population:
        pop = json.load(open(a.population))  # {stratum: n}
        cnt = collections.Counter(r["stratum"] for r in P)
        w = {s: pop[s] / cnt[s] for s in cnt}
        N = sum(pop.values())
        nch = collections.Counter(c["paper_id"] for c in C)
        byid = {r["paper_id"]: r for r in P}
        tok = collections.Counter()
        byt = collections.Counter()
        for c in C:
            tok[c["paper_id"]] += c["n_tokens"]
            byt[c["paper_id"]] += len(c["text"].encode())
        e = {
            "population_papers": N,
            "weighted_normalize_ok_rate": round(wmean(P, lambda r: (not r["error"]) and r["body_chars"] >= 200, w), 4),
            "weighted_chunks/paper(all papers)": round(wmean(P, lambda r: nch[r["paper_id"]], w), 1),
            "est_total_chunks": round(N * wmean(P, lambda r: nch[r["paper_id"]], w)),
            "est_total_tokens": round(N * wmean(P, lambda r: tok[r["paper_id"]], w)),
            "est_chunk_text_GB": round(N * wmean(P, lambda r: byt[r["paper_id"]], w) / 1e9, 1),
            "est_cpu_hours(1 core)": round(N * wmean(P, lambda r: (r.get("t_parse") or 0) + (r.get("t_chunk") or 0), w) / 3600, 1),
        }
        out["extrapolation"] = e
        per = {}
        for s in sorted(cnt):
            rs = [r for r in P if r["stratum"] == s]
            rb = [r for r in rs if not r["error"] and r["body_chars"] >= 200]
            ss = [r for r in rb if r["src_has_sec"]]
            per[s] = {
                "n": len(rs), "ok": round(len(rb) / len(rs), 3),
                "sec_extract": round(sum(r["n_sections"] > 0 for r in ss) / max(1, len(ss)), 3),
                "chunks/paper_p50": quant([nch[r["paper_id"]] for r in rb]).get("p50"),
                "tables/paper": quant([r["n_tables"] for r in rb]).get("mean"),
            }
        out["by_stratum"] = per

    txt = json.dumps(out, indent=1, ensure_ascii=False)
    print(txt)
    if a.json:
        open(a.json, "w").write(txt)


if __name__ == "__main__":
    main()
