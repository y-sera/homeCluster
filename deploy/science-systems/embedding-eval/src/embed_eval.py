"""埋め込みモデルと検索方式の比較 (設計メモ 5.5 / 5.7).

  python embed_eval.py --models qwen3-0.6b,bge-m3 [--limit-docs N]

- 入力 (RustFS s3://arxiv-raw/eval/v1/): corpus.parquet (文書集合), queries.parquet (質問と正解)
- 方式: BM25 (bm25s, title + abstract), kNN (各モデル, 内積 = コサイン. 全件の厳密な検索), hybrid (BM25 と kNN の上位 100 件を RRF, k=60),
  hybrid-lang (日本語などの質問は kNN だけ, 英語は hybrid)
- 指標: Recall@10, Recall@50, MRR (上位 100 件まで), nDCG@10. 正解は 1 件. 引用元の論文 (exclude_id) は結果から除く
- 出力: s3://arxiv-raw/eval/v1/results/<run>/<model>.json と標準出力. 文書のベクトルは .../emb/<model>-<variant>.npy (fp16) に保存し, 次回は再利用する
"""
import argparse
import io
import json
import math
import os
import re
import time

import numpy as np
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import torch

BASE = "arxiv-raw/eval/v1"
INSTRUCT = "Instruct: Given a research question, retrieve relevant arXiv papers\nQuery: "
MODELS = {
    # name: (HF repo, revision, query prompt, doc max tokens)
    "qwen3-0.6b": ("Qwen/Qwen3-Embedding-0.6B", "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3", INSTRUCT, 512),
    "qwen3-4b": ("Qwen/Qwen3-Embedding-4B", "5cf2132abc99cad020ac570b19d031efec650f2b", INSTRUCT, 512),
    "bge-m3": ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181", "", 512),
}
TOPK = 100
NON_LATIN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def fs():
    return pafs.S3FileSystem(endpoint_override=os.environ.get("RUSTFS_ENDPOINT", "http://science-rustfs-svc.science-systems.svc:9000"),
                             access_key=os.environ["RUSTFS_ACCESS_KEY"], secret_key=os.environ["RUSTFS_SECRET_KEY"],
                             region="us-east-1", scheme="http")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def doc_text(r, variant):
    t = f"Title: {clean(r['title'])}\n\nAbstract: {clean(r['abstract'])}"
    if variant == "cat":
        t = f"Categories: {r['categories']}\n\n" + t
    return t


def metrics(ranks):
    """ranks: 正解の順位 (1 始まり, なければ None) のリスト."""
    n = len(ranks)
    r = [x for x in ranks]
    return {
        "n": n,
        "recall@10": sum(1 for x in r if x and x <= 10) / n,
        "recall@50": sum(1 for x in r if x and x <= 50) / n,
        "mrr@100": sum(1 / x for x in r if x and x <= TOPK) / n,
        "ndcg@10": sum(1 / math.log2(x + 1) for x in r if x and x <= 10) / n,
    }


def rank_of(ids, gold):
    """gold: paper_id か, そのリスト (手作りの質問は正解が複数). 最も上の正解の順位."""
    golds = [gold] if isinstance(gold, str) else list(gold)
    rs = [ids.index(g) + 1 for g in golds if g in ids]
    return min(rs) if rs else None


def golds(q):
    return q.get("gold_ids") or q["gold_id"]


def topk_dense(D, Q, excl, doc_ids):
    """D: (N, d) fp16 on GPU, Q: (M, d). 引用元を除いた上位 TOPK の doc id のリスト."""
    out = []
    for i in range(0, Q.shape[0], 256):
        s = Q[i:i + 256] @ D.T
        v, idx = torch.topk(s.float(), TOPK + 1, dim=1)
        for j, row in enumerate(idx.cpu().numpy()):
            ids = [doc_ids[k] for k in row if doc_ids[k] != excl[i + j]]
            out.append(ids[:TOPK])
    return out


def rrf(lists, k=60):
    sc = {}
    for lst in lists:
        for r, d in enumerate(lst, 1):
            sc[d] = sc.get(d, 0.0) + 1.0 / (k + r)
    return [d for d, _ in sorted(sc.items(), key=lambda x: -x[1])][:TOPK]


def report(name, results, queries):
    out = {}
    for s in ["all"] + sorted({q["set"] for q in queries}):
        qs = [i for i, q in enumerate(queries) if s == "all" or q["set"] == s]
        out[s] = metrics([rank_of(results[i], golds(queries[i])) for i in qs])
    log(name, " ".join(f"{s}: R@10 {m['recall@10']:.3f} R@50 {m['recall@50']:.3f} MRR {m['mrr@100']:.3f} nDCG@10 {m['ndcg@10']:.3f}"
                       for s, m in out.items()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen3-0.6b")
    ap.add_argument("--variants", default="plain", help="文書側の入力: plain (title + abstract), cat (カテゴリを先頭に付ける)")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--limit-docs", type=int, default=0, help="文書集合を正解 + この件数に絞る (ベクトル化の時間のため)")
    ap.add_argument("--run", default=time.strftime("%Y%m%dt%H%M%S"))
    a = ap.parse_args()
    f = fs()
    corpus = pq.read_table(f.open_input_file(f"{BASE}/corpus.parquet")).to_pylist()
    queries = pq.read_table(f.open_input_file(f"{BASE}/queries.parquet")).to_pylist()
    if a.limit_docs:
        gold = {g for q in queries for g in ([golds(q)] if isinstance(golds(q), str) else golds(q))}
        corpus = [r for r in corpus if r["paper_id"] in gold] + [r for r in corpus if r["paper_id"] not in gold][:a.limit_docs]
    doc_ids = [r["paper_id"] for r in corpus]
    excl = [q["exclude_id"] for q in queries]
    log(f"docs {len(corpus)}, queries {len(queries)}, gpu {torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}")
    results = {}

    # BM25
    import bm25s
    t0 = time.time()
    retr = bm25s.BM25()
    retr.index(bm25s.tokenize([doc_text(r, "plain") for r in corpus], stopwords="en", show_progress=False), show_progress=False)
    res, _ = retr.retrieve(bm25s.tokenize([q["query"] for q in queries], stopwords="en", show_progress=False),
                           k=TOPK + 1, show_progress=False)
    bm25 = [[doc_ids[k] for k in row if doc_ids[k] != excl[i]][:TOPK] for i, row in enumerate(res)]
    log(f"bm25 {time.time() - t0:.0f}s")
    results["bm25"] = report("bm25", bm25, queries)

    from sentence_transformers import SentenceTransformer
    for name in a.models.split(","):
        repo, rev, qprompt, maxlen = MODELS[name]
        t0 = time.time()
        model = SentenceTransformer(repo, revision=rev, device="cuda", model_kwargs={"torch_dtype": torch.bfloat16})
        model.max_seq_length = maxlen
        log(f"{name}: loaded in {time.time() - t0:.0f}s")
        Q = model.encode([q["query"] for q in queries], prompt=qprompt, batch_size=a.batch, normalize_embeddings=True,
                         convert_to_tensor=True).half()
        for variant in a.variants.split(","):
            key = f"{name}-{variant}"
            path = f"{BASE}/emb/{key}{'-limit' + str(a.limit_docs) if a.limit_docs else ''}.npy"
            if f.get_file_info(path).type == pafs.FileType.File:
                with f.open_input_file(path) as fh:
                    D = np.load(io.BytesIO(fh.read()))
                log(f"{key}: loaded saved embeddings {D.shape}")
                speed = None
            else:
                t0 = time.time()
                D = model.encode([doc_text(r, variant) for r in corpus], batch_size=a.batch, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False).astype(np.float16)
                speed = len(corpus) / (time.time() - t0)
                log(f"{key}: embedded {D.shape} in {time.time() - t0:.0f}s ({speed:.0f} docs/s)")
                buf = io.BytesIO()
                np.save(buf, D)
                with f.open_output_stream(path) as fh:
                    fh.write(buf.getvalue())
            Dg = torch.from_numpy(D).cuda()
            dense = topk_dense(Dg, Q, excl, doc_ids)
            results[f"knn:{key}"] = report(f"knn:{key}", dense, queries) | {"docs_per_s": speed}
            hyb = [rrf([bm25[i], dense[i]]) for i in range(len(queries))]
            results[f"hybrid:{key}"] = report(f"hybrid:{key}", hyb, queries)
            # 英語以外 (日本語など) の質問は BM25 が役に立たないため kNN だけにする
            hyb_l = [dense[i] if NON_LATIN.search(queries[i]["query"]) else hyb[i] for i in range(len(queries))]
            results[f"hybrid-lang:{key}"] = report(f"hybrid-lang:{key}", hyb_l, queries)
            # 質問ごとの順位 (分析用)
            results[f"ranks:{key}"] = {q["qid"]: [rank_of(dense[i], golds(q)), rank_of(hyb[i], golds(q))]
                                       for i, q in enumerate(queries)}
            del Dg
        del model
        torch.cuda.empty_cache()
    results["ranks:bm25"] = {q["qid"]: rank_of(bm25[i], golds(q)) for i, q in enumerate(queries)}
    meta = {"docs": len(corpus), "queries": len(queries), "models": {m: MODELS[m][:2] for m in a.models.split(",")},
            "instruct": INSTRUCT, "topk": TOPK}
    with f.open_output_stream(f"{BASE}/results/{a.run}/{a.models.replace(',', '+')}-{a.variants.replace(',', '+')}.json") as fh:
        fh.write(json.dumps({"meta": meta, "results": results}, ensure_ascii=False).encode())
    log("OK")


if __name__ == "__main__":
    main()
