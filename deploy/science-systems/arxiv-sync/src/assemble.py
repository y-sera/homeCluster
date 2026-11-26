"""paper_text の組み立て (HF の secemp9/arxiv-complete の paper_text と同じ結果を目指す).

latex の行 (path, ext, content) から 1 論文 1 行の TeX 文字列を作る:
- TeX ファイル: ext が tex / ltx. .bbl は最後に (path 順で) 付ける
- 1 ファイルなら single. 複数なら主ファイルを選び, \\input / \\include / \\subfile を深さ優先でたどって
  ファイルを丸ごと順に連結する (その位置には展開しない) → resolved.
  主ファイルがない, または展開したファイルが TeX の総バイトの半分未満なら, 全 TeX ファイルを path 順に連結 → fallback
- 主ファイル: \\documentclass か \\documentstyle を含む (コメントも含めて探す) ファイルのうち, 階層が浅く, 大きいもの
- 連結は改行. 全体を UTF-8 で解釈し, 失敗したら latin-1
HF の行との一致 (2026-11-26, 2,230 論文で text_sha256 と件数の列が 99.8% 一致): single 100%, resolved (HF の主ファイルを与えた場合) 100%, 主ファイルの選択 99.85%
"""
import hashlib
import posixpath
import re
import sys

TEX_EXT = ("tex", "ltx")
INC = re.compile(rb"\\(?:input|include|subfile)(?![A-Za-z@])\s*(?:\{([^}]*)\}|([^\s{}\\%]+))")
CLS = re.compile(rb"\\document(?:class|style)")
COMMENT = re.compile(rb"(?<!\\)%[^\n]*")


def _includes(content):
    out = []
    for m in INC.finditer(COMMENT.sub(b"", content)):
        name = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if name:
            out.append(name.decode("latin-1"))
    return out


def _resolve(name, cur, paths):
    name = name.strip().strip('"')
    for base in (posixpath.dirname(cur), ""):
        p = posixpath.normpath(posixpath.join(base, name)) if base else posixpath.normpath(name)
        if p.startswith("./"):
            p = p[2:]
        for c in (p, p + ".tex", p + ".ltx"):
            if c in paths:
                return c
    # \input{\figPath/x.tex} のようにマクロを含むパス: 名前が一意に一致すれば使う
    b = posixpath.basename(name)
    hits = [p for p in paths if any(p == x or p.endswith("/" + x) for x in (b, b + ".tex"))]
    return hits[0] if len(hits) == 1 else None


def _expand(main, tex):
    order, seen = [], set()

    def dfs(p):
        if p in seen:
            return
        seen.add(p)
        order.append(p)
        for n in _includes(tex[p]):
            q = _resolve(n, p, tex)
            if q:
                dfs(q)
    lim = sys.getrecursionlimit()
    sys.setrecursionlimit(max(lim, 10000))
    try:
        dfs(main)
    finally:
        sys.setrecursionlimit(lim)
    return order


def choose_main(tex):
    cands = [p for p, c in tex.items() if CLS.search(c)]
    if not cands:
        return None
    return max(cands, key=lambda p: (-p.count("/"), len(tex[p]), [-ord(ch) for ch in p]))


def assemble(rows):
    """rows: [(path, ext, content bytes)]. 返り値: dict (text, main_file, resolution, n_tex_files, n_files_used,
    n_unused_files, text_encoding, text_sha256) または TeX がなければ None."""
    rows = sorted(rows, key=lambda r: r[0])
    tex = {p: c for p, e, c in rows if e in TEX_EXT}
    bbl = [c for p, e, c in rows if e == "bbl"]
    if not tex:
        return None
    if len(tex) == 1:
        main = next(iter(tex))
        order, resolution = [main], "single"
    else:
        main = choose_main(tex)
        order = _expand(main, tex) if main else []
        total = sum(len(c) for c in tex.values())
        if main and sum(len(tex[p]) for p in order) * 2 >= total:
            resolution = "resolved"
        else:
            main, order, resolution = None, list(tex), "fallback"
    b = b"\n".join([tex[p] for p in order] + bbl)
    try:
        text, enc = b.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, enc = b.decode("latin-1"), "latin-1"
    return {"text": text, "main_file": main if resolution != "fallback" else None, "resolution": resolution,
            "n_tex_files": len(tex), "n_files_used": len(order), "n_unused_files": len(tex) - len(order),
            "text_encoding": enc, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
