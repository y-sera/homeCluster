"""TeX の軽い正規化と節の抽出 (設計メモ 5.4).

paper_text.text (組み立て済みの TeX) を受け取り, 節のリストを返す. 各節は本文のブロック
(text / table / code) を持つ. 数式は LaTeX のまま残す.

方針
- コメント, \\iffalse..\\fi, comment 環境を除く
- プリアンブルの単純なマクロ (\\newcommand / \\renewcommand / \\def. 引数なし・固定引数) を展開する
- 書式コマンドは中身だけ残す. \\cite → [cite] (キーは別に保持), \\ref 系 → [ref], \\label は除く
- 図はキャプションを本文の段落 ("Figure: ...") として残す. 表はキャプションとセルの文字列を table ブロックにする
- コードの環境 (verbatim, lstlisting, minted) は中身をそのまま code ブロックにする
- 節は \\part / \\chapter / \\section / \\subsection / \\subsubsection. \\paragraph は本文中の見出しとして残す
- abstract 環境, thebibliography 環境は独立した節 (section_type abstract / references) にする
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

NORMALIZER_VERSION = "v0.7"

# ---------------------------------------------------------------- 基本の読み取り

def read_group(s: str, i: int) -> tuple[str, int]:
    """s[i] == '{' の対応する '}' までを返す (中身, 次の位置). 対応しなければ末尾まで."""
    depth = 0
    n = len(s)
    j = i
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1 : j], j + 1
        j += 1
    return s[i + 1 :], n


def read_opt(s: str, i: int) -> tuple[str, int]:
    """s[i] == '[' の対応する ']' まで (波括弧の入れ子を考慮)."""
    depth = 0
    n = len(s)
    j = i + 1
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "]" and depth <= 0:
            return s[i + 1 : j], j + 1
        j += 1
    return s[i + 1 :], n


_WS = re.compile(r"[ \t]*\n?[ \t]*")


def skip_ws(s: str, i: int) -> int:
    """空白と高々 1 つの改行を読み飛ばす (引数の間の空白)."""
    m = _WS.match(s, i)
    return m.end() if m else i


def read_args(s: str, i: int, n_req: int, opt: bool = True) -> tuple[list[str], list[str], int]:
    """コマンド名の直後から, 省略可能な [..] と n_req 個の {..} を読む."""
    opts: list[str] = []
    args: list[str] = []
    j = i
    while True:
        k = skip_ws(s, j)
        if k < len(s) and s[k] == "*":
            j = k + 1
            continue
        if opt and k < len(s) and s[k] == "[":
            o, j = read_opt(s, k)
            opts.append(o)
            continue
        break
    for _ in range(n_req):
        k = skip_ws(s, j)
        if k < len(s) and s[k] == "{":
            a, j = read_group(s, k)
            args.append(a)
        elif k < len(s) and s[k] == "\\":
            m = re.compile(r"\\([A-Za-z@]+|.)").match(s, k)
            args.append(m.group(0))
            j = m.end()
        elif k < len(s):
            args.append(s[k])
            j = k + 1
        else:
            break
        # 引数の後の [..] (例: \\begin{figure}[t])
    return opts, args, j


# ---------------------------------------------------------------- 前処理

_COMMENT = re.compile(r"(?m)(?<!\\)((?:\\\\)*)%.*$")
_COMMENT_ENV = re.compile(r"\\begin\{comment\}.*?\\end\{comment\}", re.S)
_IFFALSE = re.compile(r"\\iffalse\b.*?\\fi\b", re.S)


def strip_comments(s: str) -> str:
    s = _COMMENT.sub(r"\1", s)
    s = _COMMENT_ENV.sub("", s)
    s = _IFFALSE.sub("", s)
    return s


APPENDED_MARK = "\n\n%%APPENDED%%\n\n"  # 連結部分の始まり (strip_comments の後に入れるため残る)
_SUBDOC = re.compile(r"\\documentclass(?:\[[^\]]*\])?\{[^}]*\}.*?\\begin\{document\}", re.S)


def split_document(s: str) -> tuple[str, str, bool]:
    """(プリアンブル, 本文, \\begin{document} があったか).
    HF の paper_text の resolved では, \\input で取り込んだファイルが \\end{document} の後ろに連結されている.
    後ろに本文がある場合は本文に含める (サブファイルの \\documentclass..\\begin{document} と \\end{document} は除く)."""
    i = s.find("\\begin{document}")
    if i < 0:
        return "", s, False
    body = s[i + len("\\begin{document}") :]
    j = body.find("\\end{document}")
    if j >= 0:
        after = body[j + len("\\end{document}") :]
        body = body[:j]
        if len(after.strip()) > 500:
            after = _SUBDOC.sub("\n\n", after).replace("\\end{document}", "\n\n")
            body = body + APPENDED_MARK + after
    return s[:i], body, True


# ---------------------------------------------------------------- マクロの展開

_NEWCMD = re.compile(r"\\(?:re)?(?:newcommand|providecommand|DeclareRobustCommand)\*?\s*")
_DEF = re.compile(r"\\def\s*\\([A-Za-z@]+)((?:#\d)*)\s*\{")
_MACRO_NAME = re.compile(r"\\([A-Za-z@]+)")
_LET = re.compile(r"\\let\s*\\([A-Za-z@]+)\s*=?\s*\\([A-Za-z@]+)")
# 展開しない名前 (標準コマンドの再定義. 展開すると節の見出しや引用の処理が壊れる)
PROTECTED = {
    "part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph", "appendix",
    "begin", "end", "item", "caption", "label", "ref", "eqref", "cite", "footnote", "emph", "textbf", "textit",
    "title", "author", "maketitle", "bibitem", "bibliography", "thebibliography", "url", "href", "input", "include",
    "includegraphics", "newline", "par", "and", "thanks", "abstract", "verb", "def", "let", "newcommand",
}


def _self_ref(key: str, body: str) -> bool:
    """body が \\key 自身を含むか (\\be と \\begin を区別する)."""
    return re.search(r"\\" + re.escape(key) + r"(?![A-Za-z@])", body) is not None


@dataclass
class Macro:
    nargs: int
    body: str


def collect_macros(s: str) -> tuple[dict[str, Macro], str]:
    """マクロの定義を集め, 定義自体を取り除いた文字列を返す.
    省略可能な引数の既定値を持つもの, 自分自身を含むものは展開しない (定義は取り除く)."""
    macros: dict[str, Macro] = {}
    out: list[str] = []
    pos = 0
    for m in _NEWCMD.finditer(s):
        if m.start() < pos:
            continue
        j = m.end()
        if j < len(s) and s[j] == "{":
            name, j = read_group(s, j)
        else:
            mm = _MACRO_NAME.match(s, j)
            if not mm:
                continue
            name, j = mm.group(0), mm.end()
        name = name.strip()
        if not name.startswith("\\"):
            continue
        nargs = 0
        has_default = False
        k = skip_ws(s, j)
        if k < len(s) and s[k] == "[":
            o, j = read_opt(s, k)
            try:
                nargs = int(o.strip())
            except ValueError:
                nargs = -1
            k = skip_ws(s, j)
            if k < len(s) and s[k] == "[":
                _, j = read_opt(s, k)
                has_default = True
        k = skip_ws(s, j)
        if k >= len(s) or s[k] != "{":
            continue
        body, j = read_group(s, k)
        out.append(s[pos : m.start()])
        pos = j
        key = name[1:]
        if m.group(0).startswith("\\re") or key in PROTECTED or "@" in key or "@" in body:
            continue
        if nargs >= 0 and not has_default and not _self_ref(key, body):
            macros[key] = Macro(nargs, body)
    out.append(s[pos:])
    s = "".join(out)

    out = []
    pos = 0
    for m in _DEF.finditer(s):
        if m.start() < pos:
            continue
        body, j = read_group(s, m.end() - 1)
        out.append(s[pos : m.start()])
        pos = j
        key = m.group(1)
        nargs = len(m.group(2)) // 2
        if key not in PROTECTED and "@" not in key and "@" not in body and not _self_ref(key, body):
            macros[key] = Macro(nargs, body)
    out.append(s[pos:])
    s = "".join(out)
    # \let\be=\beq (定義済みのマクロの別名のみ)
    for m in _LET.finditer(s):
        a, b = m.group(1), m.group(2)
        if a not in PROTECTED and b in macros:
            macros[a] = macros[b]
    s = _LET.sub("", s)
    return macros, s


def expand_macros(s: str, macros: dict[str, Macro], passes: int = 3) -> str:
    if not macros:
        return s
    names = sorted(macros, key=len, reverse=True)
    pat = re.compile(r"\\(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z@])")
    limit = len(s) * 2 + 10000
    for _ in range(passes):
        out: list[str] = []
        pos = 0
        changed = False
        for m in pat.finditer(s):
            if m.start() < pos:
                continue
            mac = macros[m.group(1)]
            j = m.end()
            if mac.nargs:
                _, args, j = read_args(s, j, mac.nargs, opt=False)
                if len(args) < mac.nargs:
                    continue
                body = mac.body
                for k, a in enumerate(args, 1):
                    body = body.replace("#" + str(k), a)
            else:
                body = mac.body
                # \\foo の後の空白は TeX では消える
                if j < len(s) and s[j] == " ":
                    j += 1
            out.append(s[pos : m.start()])
            out.append(body if mac.nargs else body + " " if body[-1:].isalpha() else body)
            pos = j
            changed = True
        out.append(s[pos:])
        s = "".join(out)
        if not changed or len(s) > limit:
            break
    return s


# ---------------------------------------------------------------- 数式の保護

MATH_ENVS = (
    "equation|equation\\*|align|align\\*|alignat|alignat\\*|gather|gather\\*|multline|multline\\*|"
    "eqnarray|eqnarray\\*|flalign|flalign\\*|displaymath|math|dmath|dmath\\*|subequations"
)
_MATH_TOK = re.compile(
    r"\\\\|\\\$|\\\[|\\\]|\\\(|\\\)|\$\$|\$|\\begin\{(" + MATH_ENVS + r")\}|\\end\{(" + MATH_ENVS + r")\}"
)
PH_OPEN, PH_CLOSE = "\x00", "\x01"


def protect_math(s: str, store: list[str]) -> str:
    """数式を \\x00<番号>\\x01 に置き換え, 中身を store に入れる."""
    out: list[str] = []
    pos = 0
    state = None  # None / "$" / "$$" / "\\[" / "\\(" / env 名
    start = 0
    depth = 0
    for m in _MATH_TOK.finditer(s):
        tok = m.group(0)
        if tok in ("\\\\", "\\$"):
            continue
        if state is None:
            if tok in ("$", "$$", "\\[", "\\("):
                state, start = tok, m.start()
            elif m.group(1):
                state, start, depth = m.group(1), m.start(), 1
            continue
        end = None
        if state in ("$", "$$") and tok == state:
            end = m.end()
        elif state == "\\[" and tok == "\\]":
            end = m.end()
        elif state == "\\(" and tok == "\\)":
            end = m.end()
        elif state not in ("$", "$$", "\\[", "\\("):
            if m.group(1) == state:
                depth += 1
            elif m.group(2) == state:
                depth -= 1
                if depth == 0:
                    end = m.end()
        if end is not None:
            out.append(s[pos:start])
            store.append(s[start:end])
            out.append(f"{PH_OPEN}{len(store) - 1}{PH_CLOSE}")
            pos = end
            state = None
    out.append(s[pos:])
    return "".join(out)


_PH = re.compile(PH_OPEN + r"(\d+)" + PH_CLOSE)

# 本文に混ざる TeX のプログラム (連結されたスタイルファイルなど) と biblatex の .bbl
_PROG = re.compile(r"\\(?:csname|endcsname|expandafter|def|edef|gdef|xdef|let|ifx|ifnum|ifdim|if|else|fi|catcode|"
                   r"relax|noexpand|futurelet|afterassignment|global|long|outer|advance|multiply|divide|count|dimen|"
                   r"toks|newif|newdimen|newcount|newtoks|newskip|setbox|unskip|penalty|fontdimen|font|begingroup|"
                   r"endgroup|makeatletter|makeatother|ProvidesPackage|NeedsTeXFormat|DeclareOption|ProcessOptions|"
                   r"pdfglyphtounicode)(?![A-Za-z])|\\[A-Za-z]*@[A-Za-z@]*")
_BIBLATEX = re.compile(r"\\entry\{.*?\\endentry|\\(?:refsection|datalist|sortlist)\b.*?\\end(?:refsection|datalist|sortlist)\b"
                       r"|\\pdfglyphtounicode\{[^}]*\}\{[^}]*\}", re.S)
_RAW_PARA = re.compile(r"\n[ \t]*\n")


def drop_programming(raw: str) -> tuple[str, int]:
    """TeX のプログラムが多い段落を除く (段落の 1 文字あたりのプログラムのコマンドの密度で判定).
    返り値: (残した文字列, 除いた文字数)."""
    raw = _BIBLATEX.sub("\n\n", raw)
    keep, dropped = [], 0
    for p in _RAW_PARA.split(raw):
        n = len(_PROG.findall(p))
        if n >= 3 and n * 60 > len(p):
            dropped += len(p)
            continue
        keep.append(p)
    return "\n\n".join(keep), dropped


def restore_math(s: str, store: list[str]) -> str:
    return _PH.sub(lambda m: store[int(m.group(1))], s)


# ---------------------------------------------------------------- インラインの正規化

ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304",
           ".": "\u0307", "c": "\u0327", "v": "\u030c", "u": "\u0306", "H": "\u030b", "k": "\u0328", "r": "\u030a"}
SYMBOLS = {"ss": "ß", "o": "ø", "O": "Ø", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "aa": "å", "AA": "Å",
           "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ", "dag": "†", "ddag": "‡", "S": "§", "P": "¶", "copyright": "©",
           "ldots": "...", "dots": "...", "textellipsis": "...", "LaTeX": "LaTeX", "TeX": "TeX", "today": "",
           "textendash": "–", "textemdash": "—", "textquoteleft": "‘", "textquoteright": "’", "textbackslash": "\\",
           "textasciitilde": "~", "textbar": "|", "textless": "<", "textgreater": ">", "textdegree": "°", "euro": "€",
           "item": "\n- ", "par": "\n\n", "newline": " ", "linebreak": " ", "noindent": "", "indent": "",
           "quad": " ", "qquad": " ", "smallskip": "\n\n", "medskip": "\n\n", "bigskip": "\n\n"}
CITE = re.compile(r"(?:[Cc]ite[a-zA-Z]*|citealp|citealt|parencite|textcite|autocite|footcite)$")
REF = {"ref", "eqref", "autoref", "cref", "Cref", "pageref", "vref", "Vref", "nameref", "subref", "Autoref", "crefrange"}
# 引数ごと捨てるコマンド (引数の数)
DROP = {
    "label": 1, "nocite": 1, "vspace": 1, "hspace": 1, "vskip": 0, "hskip": 0, "includegraphics": 1,
    "bibliographystyle": 1, "bibliography": 1, "setlength": 2, "addtolength": 2, "setcounter": 2, "addtocounter": 2,
    "newtheorem": 2, "usepackage": 1, "RequirePackage": 1, "documentclass": 1, "documentstyle": 1, "input": 1,
    "include": 1, "pagestyle": 1, "thispagestyle": 1, "newpage": 0, "clearpage": 0, "cleardoublepage": 0,
    "maketitle": 0, "tableofcontents": 0, "listoffigures": 0, "listoftables": 0, "title": 1, "author": 1,
    "affiliation": 1, "affil": 1, "address": 1, "email": 1, "thanks": 1, "date": 1, "institute": 1, "keywords": 1,
    "pacs": 1, "subjclass": 1, "received": 1, "accepted": 1, "published": 1, "altaffiliation": 1, "homepage": 1,
    "author": 1, "icmltitle": 1, "icmlauthor": 2, "icmlaffiliation": 2, "icmlcorrespondingauthor": 2,
    "icmlkeywords": 1, "titlerunning": 1, "authorrunning": 1, "markboth": 2, "orcid": 1, "corresp": 1,
    "color": 1, "pagenumbering": 1, "bibitem": 1, "hypersetup": 1, "graphicspath": 1, "centering": 0,
    "raggedright": 0, "raggedleft": 0, "footnotesize": 0, "small": 0, "large": 0, "Large": 0, "LARGE": 0,
    "huge": 0, "Huge": 0, "normalsize": 0, "tiny": 0, "scriptsize": 0, "bf": 0, "it": 0, "em": 0, "rm": 0,
    "sf": 0, "tt": 0, "sc": 0, "sl": 0, "bfseries": 0, "itshape": 0, "rmfamily": 0, "ttfamily": 0, "hline": 0,
    "toprule": 0, "midrule": 0, "bottomrule": 0, "cline": 1, "cmidrule": 1, "vfill": 0, "hfill": 0, "protect": 0,
    "appendix": 0, "frontmatter": 0, "mainmatter": 0, "backmatter": 0, "IEEEpeerreviewmaketitle": 0,
    "acknowledgments": 0, "nonumber": 0, "notag": 0, "relax": 0, "onecolumn": 0, "twocolumn": 0,
    "printbibliography": 0, "makeatletter": 0, "makeatother": 0, "selectfont": 0, "fontsize": 2, "linespread": 1,
    "definecolor": 3, "newenvironment": 3, "renewenvironment": 3, "newcounter": 1, "numberwithin": 2,
    "preprint": 1, "DeclareMathOperator": 2, "DeclareMathAlphabet": 5, "SetMathAlphabet": 6,
    "DeclareSymbolFont": 5, "SetSymbolFont": 6, "DeclareFontFamily": 3, "DeclareFontShape": 6,
    "DeclareMathSymbol": 4, "DeclareSymbolFontAlphabet": 2, "DeclareUnicodeCharacter": 2,
    "captionsetup": 1, "rowcolor": 1, "cellcolor": 1, "columncolor": 1, "arraystretch": 0, "tabcolsep": 0, "lstset": 1, "theoremstyle": 1, "allowdisplaybreaks": 0, "balance": 0, "sloppy": 0,
    "endinput": 0, "newline": 0, "baselineskip": 0, "textwidth": 0, "linewidth": 0, "columnwidth": 0,
}
# 中身を残すコマンド (最後の引数を残す. 例: \\textcolor{red}{x} → x, \\href{url}{text} → text)
KEEP_LAST = {"multicolumn": 3, "multirow": 3, "textcolor": 2, "href": 2, "colorbox": 2, "foreignlanguage": 2, "texorpdfstring": 2, "resizebox": 3,
             "scalebox": 2, "raisebox": 2, "makebox": 1, "parbox": 2, "fbox": 1, "framebox": 1}
FOOTNOTE = {"footnote", "footnotetext", "marginpar"}
# 中身を残す既知のコマンド (未知のコマンドと同じ処理だが, 未知の数には数えない)
KEEP_CONTENT = {"textbf", "textit", "emph", "texttt", "textsc", "textsf", "textrm", "textup", "textsl", "textmd",
                "textnormal", "underline", "uline", "mbox", "hbox", "text", "ensuremath", "boldsymbol", "bm",
                "mathrm", "mathbf", "mathit", "mathcal", "mathbb", "mathsf", "mathtt", "mathfrak", "emph",
                "textsuperscript", "textsubscript", "xspace",
                "makecell", "thead", "shortstack", "sout", "st", "hl", "textls", "textup", "small", "centerline",
                "leftline", "rightline", "vbox", "vtop", "newblock", "numprint", "num", "SI", "si", "unit", "qty",
                "nprounddigits", "acro", "ac", "acp", "acs", "acl", "gls", "glspl", "Gls"}
SEMANTIC_ENVS = {"theorem", "lemma", "proposition", "corollary", "definition", "remark", "example", "proof",
                 "conjecture", "assumption", "claim", "fact", "observation", "problem", "question", "note",
                 "hypothesis", "notation", "exercise", "algorithm"}

_CMD = re.compile(r"\\([A-Za-z@]+)\*?|\\(.)", re.S)


@dataclass
class Ctx:
    cite_keys: list[str] = field(default_factory=list)
    unknown: dict[str, int] = field(default_factory=dict)
    n_cites: int = 0
    n_refs: int = 0


def inline(s: str, ctx: Ctx) -> str:
    """数式を保護済みの文字列のコマンドを処理する (再帰的に引数も処理)."""
    out: list[str] = []
    pos = 0
    n = len(s)
    for m in _CMD.finditer(s):
        if m.start() < pos:
            continue
        out.append(s[pos : m.start()])
        name, sym = m.group(1), m.group(2)
        j = m.end()
        if sym is not None:
            if sym in ACCENTS:
                # \\'e, \\'{e}
                k = j
                if k < n and s[k] == "{":
                    base, k = read_group(s, k)
                elif k < n:
                    base, k = s[k], k + 1
                else:
                    base = ""
                base = base.replace("\\i", "i").replace("\\j", "j")
                out.append(unicodedata.normalize("NFC", base[:1] + ACCENTS[sym] + base[1:]) if base else "")
                j = k
            elif sym == "\\":
                out.append("\n")
                # \\\\[2pt]
                k = skip_ws(s, j)
                if k < n and s[k] == "[":
                    _, j = read_opt(s, k)
            elif sym in "&%$#_{}":
                out.append(sym)
            elif sym in " ,;:!>/-":
                out.append(" " if sym in " ,;:>" else "")
            else:
                out.append(sym)
            pos = j
            continue
        if name in ACCENTS and len(name) == 1:  # \\c{c}, \\v{s}, \\u{a}, \\H{o}
            k = skip_ws(s, j)
            if k < n and s[k] == "{":
                base, k = read_group(s, k)
                base = base.replace("\\i", "i")
                out.append(unicodedata.normalize("NFC", base[:1] + ACCENTS[name] + base[1:]) if base else "")
                pos = k
                continue
        if name in SYMBOLS:
            out.append(SYMBOLS[name])
            # \\ss{}
            if j + 1 < n and s[j] == "{" and s[j + 1] == "}":
                j += 2
            pos = j
            continue
        if CITE.match(name):
            _, args, j = read_args(s, j, 1)
            if name != "nocite":
                ctx.n_cites += 1
                ctx.cite_keys.extend(k.strip() for k in (args[0] if args else "").split(",") if k.strip())
                out.append("[cite]")
            pos = j
            continue
        if name in REF:
            _, _, j = read_args(s, j, 2 if name == "crefrange" else 1)
            ctx.n_refs += 1
            out.append("[ref]")
            pos = j
            continue
        if name in DROP:
            _, _, j = read_args(s, j, DROP[name])
            pos = j
            continue
        if name in KEEP_LAST:
            _, args, j = read_args(s, j, KEEP_LAST[name])
            out.append(inline(args[-1], ctx) if args else "")
            pos = j
            continue
        if name in FOOTNOTE:
            _, args, j = read_args(s, j, 1)
            out.append(" (" + inline(args[0], ctx).strip() + ")" if args else "")
            pos = j
            continue
        if name == "verb":
            if j < n and not s[j].isalpha():
                d = s[j]
                k = s.find(d, j + 1)
                if k < 0:
                    k = n
                out.append(s[j + 1 : k])
                pos = k + 1
                continue
        if name == "url" or name == "path":
            _, args, j = read_args(s, j, 1, opt=False)
            out.append(args[0] if args else "")
            pos = j
            continue
        if name == "paragraph" or name == "subparagraph":
            _, args, j = read_args(s, j, 1)
            out.append("\n\n" + (inline(args[0], ctx).strip() + ". " if args else ""))
            pos = j
            continue
        if name == "caption":
            _, args, j = read_args(s, j, 1)
            out.append(inline(args[0], ctx) if args else "")
            pos = j
            continue
        if name in ("begin", "end"):
            _, args, j = read_args(s, j, 1, opt=False)
            env = (args[0] if args else "").strip()
            base = env.rstrip("*")
            if name == "begin":
                opts, _, j2 = read_args(s, j, 0)
                if base in ("itemize", "enumerate", "description", "list"):
                    out.append("\n")
                elif base in SEMANTIC_ENVS:
                    title = base.capitalize()
                    if opts:
                        title += " (" + inline(opts[0], ctx).strip() + ")"
                    out.append("\n\n" + title + ". ")
                    j = j2
                elif base in ("tabular", "tabularx", "array"):
                    _, _, j = read_args(s, j, 2 if base == "tabularx" else 1)
                else:
                    out.append("\n\n" if base in ("center", "quote", "quotation", "flushleft", "flushright") else "")
                    j = j2
            else:
                out.append("\n\n" if base in SEMANTIC_ENVS or base in ("itemize", "enumerate", "description") else "")
            pos = j
            continue
        # 未知のコマンド: 名前と [..] を捨て, {..} の中身は残す (波括弧は最後に消す)
        if name not in KEEP_CONTENT:
            ctx.unknown[name] = ctx.unknown.get(name, 0) + 1
        k = j
        while True:
            k2 = skip_ws(s, k)
            if k2 < n and s[k2] == "[":
                _, k = read_opt(s, k2)
            else:
                break
        # 数値の寸法 (\\vskip 3pt など) は捨てる
        mm = re.compile(r"\s*-?[\d.]+\s*(?:pt|em|ex|cm|mm|in|bp|sp|mu)\b").match(s, k)
        if mm:
            k = mm.end()
        pos = k
    out.append(s[pos:])
    t = "".join(out)
    return t


_TEXT_CLEAN = [
    (re.compile(r"[{}]"), ""),
    (re.compile(r"~"), " "),
    (re.compile(r"---"), "—"),
    (re.compile(r"--"), "–"),
    (re.compile(r"``|''"), '"'),
    (re.compile(r"[ \t\r\f\v]+"), " "),
    (re.compile(r" *\n *"), "\n"),
]
_PARA = re.compile(r"\n\s*\n+")
_WORD = re.compile(r"[^\W\d_]{2,}")


def finish(t: str, min_words: int = 2) -> list[str]:
    """数式を戻す前の文字列を段落のリストにする.
    min_words: 本文の段落はこれより単語の少ないものを除く (見出し, キャプション, セルは 0)."""
    for p, r in _TEXT_CLEAN:
        t = p.sub(r, t)
    paras = []
    for p in _PARA.split(t):
        # 段落内の単独の改行は空白にする (箇条書きの "- " は残す)
        p = re.sub(r"\n(?!- )", " ", p).strip()
        # 単語 (2 文字以上の英字) が 2 つ未満の段落は除く. 数式やブロックの印を含む段落は残す
        if p and (min_words == 0 or PH_OPEN in p or "\x02" in p
                  or len(_WORD.findall(p.replace("[ref]", "").replace("[cite]", ""))) >= min_words):
            paras.append(p)
    return paras


# ---------------------------------------------------------------- ブロック (図表, コード)

FLOAT = re.compile(r"\\begin\{(figure\*?|table\*?|wrapfigure|wraptable|sidewaysfigure|sidewaystable|SCfigure|"
                   r"longtable|subfigure|minipage)\}")
CODE = re.compile(r"\\begin\{(verbatim\*?|Verbatim|lstlisting|minted|alltt|algorithmic|algorithm2e|code)\}")
DROP_ENV = re.compile(r"\\begin\{(tikzpicture|pspicture|picture|filecontents\*?|axis|tikzcd|xy|psmatrix|"
                      r"titlepage|IEEEbiography|IEEEbiographynophoto|biography|keywords?|acknowledgements?|"
                      r"CCSXML|teaserfigure)\}")
TABULAR = re.compile(r"\\begin\{(tabular\*?|tabularx|tabulary|longtable|array|supertabular|tblr)\}")
CAPTION = re.compile(r"\\caption\*?\s*")
BLOCK_MARK = re.compile("\x02([TFC])(\\d+)\x03")


def find_env_end(s: str, env: str, start: int) -> int:
    """\\begin{env} の後 start から対応する \\end{env} の終わりの位置 (なければ -1)."""
    b, e = "\\begin{" + env + "}", "\\end{" + env + "}"
    depth = 1
    i = start
    while True:
        ie = s.find(e, i)
        if ie < 0:
            return -1
        ib = s.find(b, i)
        if 0 <= ib < ie:
            depth += 1
            i = ib + len(b)
            continue
        depth -= 1
        i = ie + len(e)
        if depth == 0:
            return i


def extract_envs(s: str, pat: re.Pattern, handler) -> str:
    out: list[str] = []
    pos = 0
    while True:
        m = pat.search(s, pos)
        if not m:
            break
        env = m.group(1)
        end = find_env_end(s, env, m.end())
        if end < 0:
            # 終わりがない (マクロ内の \begin など): 開始の印だけ除く
            out.append(s[pos : m.start()])
            pos = m.end()
            continue
        else:
            inner = s[m.end() : end - len("\\end{" + env + "}")]
        out.append(s[pos : m.start()])
        out.append(handler(env, inner))
        pos = end
    out.append(s[pos:])
    return "".join(out)


def table_text(inner: str, ctx: Ctx, store: list[str]) -> list[str]:
    """tabular の中身を行のリストにする (セルは " | " で区切る)."""
    rows = []
    for row in re.split(r"\\\\(?:\s*\[[^\]]*\])?|\\tabularnewline", inner):
        cells = re.split(r"(?<!\\)&", row)
        cells = [restore_math(" ".join(finish(inline(protect_math(c, store), ctx), 0)), store) for c in cells]
        cells = [c.strip() for c in cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


# ---------------------------------------------------------------- 節

SEC = re.compile(r"\\(part|chapter|section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{")
SEC_ANY = re.compile(r"\\(?:chapter|section|subsection)\*?\s*[\[{]")
SEC_LEVELS = ["part", "chapter", "section", "subsection", "subsubsection"]
ABSTRACT = re.compile(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", re.S)
THEBIB = re.compile(r"\\begin\{thebibliography\}(.*?)(?:\\end\{thebibliography\}|$)", re.S)
APPENDIX = re.compile(r"\\appendix\b|\\begin\{appendix\}|\\begin\{appendices\}")

TYPE_RULES = [
    ("references", r"\breferences?\b|bibliograph|literature cited"),
    ("abstract", r"^abstract$"),
    ("intro", r"introduc|^overview$|motivation"),
    ("related", r"related work|previous work|prior work|literature|background|state of the art"),
    ("conclusion", r"conclu|summary|final remarks|closing remarks"),
    ("discussion", r"discussion|limitation|outlook|future work|implication|broader impact"),
    ("experiment", r"experiment|evaluation|simulation|implementation|dataset|benchmark|empirical|numerical|"
                   r"observation|data|setup|measurement|case stud"),
    ("result", r"result|finding|performance|comparison|analysis"),
    ("method", r"method|approach|model|framework|algorithm|propos|architecture|formulation|theory|theoretical|"
               r"preliminar|notation|definition|formalism|derivation|construction|design|problem statement|"
               r"main theorem|proof"),
    ("appendix", r"appendix|supplement"),
]
TYPE_RULES = [(t, re.compile(p, re.I)) for t, p in TYPE_RULES]


def classify(title: str, in_appendix: bool) -> str:
    t = re.sub(r"^\s*(?:[\dIVX]+|[A-Z])(?:\.\d+)*\.?\s+", "", title.strip()) if title else ""
    for typ, p in TYPE_RULES:
        if p.search(t):
            if typ == "references":
                return typ
            return "appendix" if in_appendix else typ
    return "appendix" if in_appendix else "other"


@dataclass
class Block:
    kind: str  # text / table / code
    text: str


@dataclass
class Section:
    section_id: str
    parent_id: str | None
    level: int
    ordinal: int
    title: str
    section_type: str
    blocks: list[Block]

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


@dataclass
class Result:
    sections: list[Section]
    has_document: bool
    n_macros: int
    raw_body_chars: int
    cite_keys: list[str]
    unknown: dict[str, int]
    n_cites: int
    n_refs: int
    n_tables: int
    n_figures: int
    n_heads: int  # 抽出した見出しの数
    src_has_sec: bool  # 元の TeX (コメントを除く) に \section 系があるか
    dropped_prog_chars: int  # TeX のプログラムとして除いた文字数


def normalize_body(raw: str, ctx: Ctx, counters: dict) -> list[Block]:
    """節の本文 (TeX) をブロックのリストにする."""
    blocks_store: dict[str, list] = {"T": [], "F": [], "C": []}
    math: list[str] = []

    raw = extract_envs(raw, DROP_ENV, lambda env, inner: "\n\n")
    raw, dropped = drop_programming(raw)
    counters["dropped_prog_chars"] += dropped

    def code_h(env, inner):
        if env.startswith("minted"):
            m = re.match(r"\s*(?:\[[^\]]*\])?\s*\{[^}]*\}", inner)
            if m:
                inner = inner[m.end() :]
        elif env == "lstlisting":
            m = re.match(r"\s*\[[^\]]*\]", inner)
            if m:
                inner = inner[m.end() :]
        blocks_store["C"].append(inner.strip("\n"))
        return f"\n\n\x02C{len(blocks_store['C']) - 1}\x03\n\n"

    raw = extract_envs(raw, CODE, code_h)

    def float_h(env, inner):
        caps = []
        for m in CAPTION.finditer(inner):
            j = m.end()
            if j < len(inner) and inner[j] == "[":
                _, j = read_opt(inner, j)
                j = skip_ws(inner, j)
            if j < len(inner) and inner[j] == "{":
                c, _ = read_group(inner, j)
                c = " ".join(finish(inline(protect_math(c, math), ctx), 0)).strip()
                if c:
                    caps.append(c)
        is_table = env.startswith(("table", "wraptable", "sidewaystable", "longtable")) or bool(TABULAR.search(inner))
        if is_table:
            rows = []
            for m in TABULAR.finditer(inner):
                end = find_env_end(inner, m.group(1), m.end())
                body = inner[m.end() : end - len("\\end{" + m.group(1) + "}") if end > 0 else len(inner)]
                # 列の指定 {lcr} (tabularx は {幅}{列})
                k = skip_ws(body, 0)
                nspec = 2 if m.group(1) in ("tabularx", "tabulary", "tabular*") else 1
                for _ in range(nspec):
                    k = skip_ws(body, k)
                    if k < len(body) and body[k] == "[":
                        _, k = read_opt(body, k)
                        k = skip_ws(body, k)
                    if k < len(body) and body[k] == "{":
                        _, k = read_group(body, k)
                rows.extend(table_text(body[k:], ctx, math))
            text = "\n".join((["Table: " + " ".join(caps)] if caps else []) + rows)
            if not rows and not caps:
                return "\n\n"
            counters["tables"] += 1
            blocks_store["T"].append(text)
            return f"\n\n\x02T{len(blocks_store['T']) - 1}\x03\n\n"
        if env == "minipage":
            return "\n\n" + inner + "\n\n"
        counters["figures"] += 1
        if not caps:
            return "\n\n"
        blocks_store["F"].append("Figure: " + " ".join(caps))
        return f"\n\n\x02F{len(blocks_store['F']) - 1}\x03\n\n"

    raw = extract_envs(raw, FLOAT, float_h)
    # float の外の tabular
    raw = extract_envs(raw, TABULAR, lambda env, inner: float_h("table", "\\begin{tabular}" + inner + "\\end{tabular}"))

    t = inline(protect_math(raw, math), ctx)
    blocks: list[Block] = []
    for p in finish(t):
        # ブロックの印だけの段落とそれ以外に分ける
        last = 0
        for m in BLOCK_MARK.finditer(p):
            pre = p[last : m.start()].strip()
            if pre:
                blocks.append(Block("text", restore_math(pre, math)))
            kind, idx = m.group(1), int(m.group(2))
            txt = blocks_store[kind][idx]
            if kind == "T":
                blocks.append(Block("table", restore_math(txt, math)))
            elif kind == "C":
                blocks.append(Block("code", txt))
            else:
                blocks.append(Block("text", restore_math(txt, math)))
            last = m.end()
        rest = p[last:].strip()
        if rest:
            blocks.append(Block("text", restore_math(rest, math)))
    return blocks


def parse(text: str) -> Result:
    ctx = Ctx()
    counters = {"tables": 0, "figures": 0, "dropped_prog_chars": 0}
    s = strip_comments(text)
    pre, body, has_doc = split_document(s)
    macros, pre = collect_macros(pre)
    m2, body = collect_macros(body)
    macros.update(m2)
    body = expand_macros(body, macros)
    raw_body_chars = len(body)

    sections: list[Section] = []

    # 要旨と参考文献は先に取り出す
    abstract_raw = None
    m = ABSTRACT.search(body)
    if m:
        abstract_raw = m.group(1)
        body = body[: m.start()] + "\n\n" + body[m.end() :]
    else:
        m = ABSTRACT.search(pre)  # revtex などはプリアンブルに置くことがある
        if m:
            abstract_raw = expand_macros(m.group(1), macros)
    bib_raw = None
    m = THEBIB.search(body)
    if m:
        bib_raw = m.group(1)
        body = body[: m.start()] + "\n\n" + body[m.end() :]

    appended_at = body.find(APPENDED_MARK)
    appended_at = appended_at if appended_at >= 0 else None
    body = body.replace(APPENDED_MARK, "\n" * len(APPENDED_MARK))
    app = APPENDIX.search(body)
    app_pos = app.start() if app else None
    # \input のファイルが後ろに連結されている場合, 本体の \appendix の位置は付録の位置を表さない (見出しで判定する)
    if app_pos is not None and appended_at is not None and app_pos < appended_at:
        app_pos = None

    heads = []
    for m in SEC.finditer(body):
        title, end = read_group(body, m.end() - 1)
        heads.append((m.start(), end, m.group(1), title))
    used = sorted({h[2] for h in heads}, key=SEC_LEVELS.index)
    level_of = {name: i + 1 for i, name in enumerate(used)}

    ordinal = 0

    def add(level, title, sec_type, raw, parent):
        nonlocal ordinal
        blocks = normalize_body(raw, ctx, counters)
        sid = f"{ordinal:03d}"
        sections.append(Section(sid, parent, level, ordinal, title, sec_type, blocks))
        ordinal += 1
        return sid

    if abstract_raw is not None:
        add(1, "Abstract", "abstract", abstract_raw, None)

    front = body[: heads[0][0]] if heads else body
    if heads or front.strip():
        blocks_probe = front
        add(1, "", "front" if heads else "body", blocks_probe, None)
        if sections and not sections[-1].blocks:
            sections.pop()
            ordinal -= 1

    stack: list[tuple[int, str]] = []
    type_of: dict[str, str] = {}
    title_app = False
    for idx, (start, end, cmd, title_raw) in enumerate(heads):
        nxt = heads[idx + 1][0] if idx + 1 < len(heads) else len(body)
        level = level_of[cmd]
        tmath: list[str] = []
        title = restore_math(" ".join(finish(inline(protect_math(title_raw, tmath), Ctx()), 0)), tmath)
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else None
        in_app = (app_pos is not None and start > app_pos) or title_app
        typ = classify(title, in_app)
        if level == 1 and re.search(r"^appendi|^supplement", title, re.I):
            title_app = True
            typ = "appendix"
        if typ == "other" and parent is not None:
            typ = type_of[parent]
        sid = add(level, title, typ, body[end:nxt], parent)
        type_of[sid] = typ
        stack.append((level, sid))

    if bib_raw is not None:
        bib = re.sub(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{[^}]*\}", "\n\n", bib_raw)
        # 最初の {99} (最大のラベル) を除く
        bib = re.sub(r"^\s*\{[^}]*\}", "", bib)
        add(1, "References", "references", bib, None)

    return Result(sections, has_doc, len(macros), raw_body_chars, ctx.cite_keys, ctx.unknown,
                  ctx.n_cites, ctx.n_refs, counters["tables"], counters["figures"], len(heads), bool(SEC_ANY.search(s)),
                  counters["dropped_prog_chars"])
