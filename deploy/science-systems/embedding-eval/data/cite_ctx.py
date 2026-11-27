"""引用の文脈から評価用の質問を作る (設計メモ 5.7).

論文の TeX (paper_text) から:
1. 参考文献 (\\bibitem / biblatex の \\entry) のうち arXiv の ID が書かれているものを集める (キー → arXiv ID)
2. 本文の文のうち, そのキーを 1 つだけ引用している文 (\\cite{k}) を取り, 正規化 (texnorm) して [cite] を除いたものを質問にする
3. 正解 = 引用先の論文. metadata にあり, 引用元と別の論文であること

  python cite_ctx.py citing.parquet out.parquet
"""
import os
import re
import sys

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../arxiv-sync/src"))
import texnorm  # noqa: E402

NEW_ID = r"\d{4}\.\d{4,5}"
OLD_ID = r"(?:[a-z]+(?:-[a-z]+)*(?:\.[A-Z]{2})?)/\d{7}"
ARXIV_IN_BIB = re.compile(
    rf"(?:arXiv\s*[:.]?\s*|arxiv\.org/(?:abs|pdf)/|\\field\{{eprint\}}\{{|eprint\s*=\s*\{{?)\s*({NEW_ID}|{OLD_ID})(?:v\d+)?",
    re.I)
BIBITEM = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
ENTRY = re.compile(r"\\entry\{([^}]+)\}")
CITE = re.compile(r"\\(?:[Cc]ite[a-zA-Z]*|citealp|citealt|parencite|textcite|autocite|footcite)\*?"
                  r"(?:\s*\[[^\]]*\]){0,2}\s*\{([^}]*)\}")
SENT_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\\])|\n\s*\n")


def bib_map(text):
    """キー → arXiv ID."""
    out = {}
    for rx in (BIBITEM, ENTRY):
        ms = list(rx.finditer(text))
        for m, nxt in zip(ms, ms[1:] + [None]):
            body = text[m.end(): nxt.start() if nxt else min(len(text), m.end() + 3000)]
            a = ARXIV_IN_BIB.search(body)
            if a:
                out.setdefault(m.group(1).strip(), a.group(1))
    return out


def sentences_with_cite(body):
    """(文の TeX, 文の中の引用キーのリスト) を返す."""
    spans, last = [], 0
    for m in SENT_END.finditer(body):
        spans.append((last, m.start()))
        last = m.end()
    spans.append((last, len(body)))
    for a, b in spans:
        s = body[a:b]
        if "cite" not in s or len(s) > 1500:
            continue
        keys = []
        for m in CITE.finditer(s):
            keys.append([k.strip() for k in m.group(1).split(",") if k.strip()])
        if keys:
            yield s, keys


def clean(preamble, sent):
    """文の TeX を正規化する (前置きのマクロを使う). [cite] と数式の多い文は除く."""
    try:
        r = texnorm.parse(preamble + "\n\\begin{document}\n" + sent + "\n\\end{document}\n")
    except Exception:  # noqa: BLE001
        return None
    t = " ".join(s.text for s in r.sections).strip()
    t = re.sub(r"\s*\[cite\]", "", t)
    t = re.sub(r"\s*\[ref\]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([.,;:])", r"\1", t)
    return t


def main():
    rows = pq.read_table(sys.argv[1]).to_pylist()
    out = []
    for r in rows:
        text = texnorm.strip_comments(r["text"] or "")
        bib = bib_map(text)
        if not bib:
            continue
        i = text.find("\\begin{document}")
        preamble = text[:i] if i >= 0 else ""
        body = text[i:] if i >= 0 else text
        j = body.find("\\end{document}")
        body = body[:j] if j >= 0 else body
        body = re.split(r"\\begin\{thebibliography\}|\\bibliography\{|\\printbibliography", body)[0]
        for sent, keys in sentences_with_cite(body):
            # 引用が 1 か所・1 キーで, そのキーの arXiv ID が分かる文だけ
            if len(keys) != 1 or len(keys[0]) != 1 or keys[0][0] not in bib:
                continue
            gold = bib[keys[0][0]]
            if gold == r["paper_id"]:
                continue
            q = clean(preamble, sent)
            if not q:
                continue
            words = q.split()
            n_math = q.count("$")
            if not (12 <= len(words) <= 60) or n_math > 4:
                continue
            out.append({"citing_id": r["paper_id"], "primary_category": r["primary_category"],
                        "cite_key": keys[0][0], "gold_id": gold, "query": q, "raw": sent[:1500]})
    pq.write_table(pa.Table.from_pylist(out), sys.argv[2])
    print(f"papers {len(rows)}, with arXiv bib {sum(1 for r in rows if bib_map(r['text'] or ''))}, "
          f"queries {len(out)} from {len({o['citing_id'] for o in out})} papers")


if __name__ == "__main__":
    main()
