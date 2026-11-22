"""節をチャンクに分ける (設計メモ 5.4).

- 節を段落で分け, 長い段落は文で分ける. 短い段落は隣と結合する
- 目標 500〜800 トークン, 最大 1,000, 最小 100. オーバーラップなし. 節をまたがない
- 表 (table) とコード (code) は独立したチャンクにする (長ければ行で分ける)
- トークン数は埋め込みモデル (Qwen3-Embedding) のトークナイザで数える
- 要旨 (abstract) と参考文献 (references) はチャンクにしない (要旨は papers の索引にある)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

CHUNKER_VERSION = "v0.2"
TARGET_MIN, TARGET_MAX, HARD_MAX, MIN_TOKENS = 500, 800, 1000, 100
SKIP_TYPES = {"abstract", "references"}

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\"$\\])")


@dataclass
class Chunk:
    chunk_id: str
    section_id: str
    section_path: str
    section_type: str
    chunk_index: int
    content_type: str
    text: str
    n_tokens: int
    content_hash: str


class Counter:
    """トークン数を数える (tokenizers.Tokenizer)."""

    def __init__(self, tokenizer):
        self.tok = tokenizer

    def counts(self, texts: list[str]) -> list[int]:
        if not texts:
            return []
        return [len(e.ids) for e in self.tok.encode_batch(texts, add_special_tokens=False)]

    def split_tokens(self, text: str, size: int) -> list[str]:
        """トークン数 size ごとに文字位置で切る (1 文が長すぎる場合)."""
        enc = self.tok.encode(text, add_special_tokens=False)
        offs = enc.offsets
        parts = []
        for i in range(0, len(offs), size):
            a = offs[i][0]
            b = offs[min(i + size, len(offs)) - 1][1]
            parts.append(text[a:b])
        return [p for p in parts if p.strip()]


def _units(blocks, counter: Counter):
    """ブロックを (種類, 文字列, トークン数) の単位にする. 長い段落は文で分ける."""
    texts = [b.text for b in blocks]
    ns = counter.counts(texts)
    units = []
    for b, n in zip(blocks, ns):
        if n <= TARGET_MAX:
            units.append((b.kind, b.text, n))
            continue
        if b.kind == "text":
            pieces = _SENT.split(b.text)
        else:
            pieces = b.text.split("\n")
        pns = counter.counts(pieces)
        for p, pn in zip(pieces, pns):
            if pn > HARD_MAX:
                for q in counter.split_tokens(p, TARGET_MAX):
                    units.append((b.kind, q, None))
            else:
                units.append((b.kind, p, pn))
    # split_tokens の部分は数え直す
    idx = [i for i, u in enumerate(units) if u[2] is None]
    if idx:
        cs = counter.counts([units[i][1] for i in idx])
        for i, c in zip(idx, cs):
            units[i] = (units[i][0], units[i][1], c)
    return units


def _pack(units):
    """単位を順に詰める. 返り値は (種類, [文字列], トークン数) のリスト."""
    out = []
    cur, cur_n, cur_kind = [], 0, None

    def flush():
        nonlocal cur, cur_n, cur_kind
        if cur:
            out.append((cur_kind, cur, cur_n))
        cur, cur_n, cur_kind = [], 0, None

    for kind, text, n in units:
        isolated = kind in ("table", "code")
        if isolated and cur_kind != kind:
            flush()
        elif not isolated and cur_kind in ("table", "code"):
            flush()
        if not cur:
            cur, cur_n, cur_kind = [text], n, kind
            continue
        if cur_n + n <= TARGET_MAX or (cur_n < TARGET_MIN and cur_n + n <= HARD_MAX):
            cur.append(text)
            cur_n += n
        else:
            flush()
            cur, cur_n, cur_kind = [text], n, kind
    flush()
    # 末尾の短いチャンクは同じ種類の直前と結合する
    if len(out) >= 2 and out[-1][2] < MIN_TOKENS and out[-1][0] == out[-2][0] and out[-1][2] + out[-2][2] <= HARD_MAX:
        k, t, n = out.pop()
        pk, pt, pn = out.pop()
        out.append((pk, pt + t, pn + n))
    return out


def chunk_paper(paper_id: str, sections, counter: Counter) -> list[Chunk]:
    by_id = {s.section_id: s for s in sections}

    def path(s):
        names = []
        while s is not None:
            names.append(s.title or s.section_type)
            s = by_id.get(s.parent_id) if s.parent_id else None
        return " > ".join(reversed(names))

    chunks: list[Chunk] = []
    pending = []
    for s in sections:
        if s.section_type in SKIP_TYPES or not s.blocks:
            continue
        sp = path(s)
        for i, (kind, texts, _) in enumerate(_pack(_units(s.blocks, counter))):
            text = ("\n" if kind == "table" else "\n\n").join(texts)
            pending.append((s, sp, i, kind, text))
    ns = counter.counts([p[4] for p in pending])
    seen = set()
    idx: dict[str, int] = {}
    for (s, sp, _, kind, text), n in zip(pending, ns):
        h = hashlib.sha256(text.encode()).hexdigest()[:32]
        # 同じ論文の中の同じ内容 (同じ図のキャプションの繰り返し, 二重に取り込まれたファイル) は 1 つにする
        if h in seen:
            continue
        seen.add(h)
        i = idx.get(s.section_id, 0)
        idx[s.section_id] = i + 1
        chunks.append(Chunk(f"{paper_id}:{s.section_id}:{i}", s.section_id, sp, s.section_type, i, kind, text, n, h))
    return chunks
