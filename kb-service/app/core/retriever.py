"""检索层（任务 1.5）。

对齐 kb-engine/src/retrieve/*：
    - 中文 bigram 分词 BM25（K1=1.5、B=0.75），ASCII 按词切分，规避原生分词依赖
    - 向量余弦（向量已 L2 归一化，点积即余弦）
    - 混合：向量 + BM25 → RRF（K 默认 60）
    - 角色过滤：visible_roles 含「全部」或匹配角色才可见

**与原型的唯一有意差异**：原型 `isVisible(chunk, role)` 在 role 为空时
`return true`（放行全部知识）。本实现缺省按「访客」处理，仅返回公共切片，
堵住「不传角色即越权」的漏洞。该差异已在一致性回归中单独标注。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from app.config import settings
from app.core.chunker import Chunk

# BM25 参数（与原型一致）
K1 = 1.5
B = 0.75

# 角色缺省值：原型为 None 时放行全部，此处收紧为「访客」
DEFAULT_ROLE = "访客"

_ASCII_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

Mode = Literal["hybrid", "vector", "bm25"]


@dataclass
class Retrieved:
    """一条检索结果，字段与原型返回对象对齐。"""

    chunk: Chunk
    score: float = 0.0  # 当前 mode 下的主排序分
    cos: float = 0.0
    bm25: float = 0.0
    v_rank: int = 0
    b_rank: int | None = None
    rrf: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def chunk_id(self) -> str:
        return self.chunk.chunk_id


# ------------------------------------------------------------------ 分词
def tokenize(text: str) -> list[str]:
    """中文按 bigram、ASCII 按词切分（与原型 tokenize 完全一致）。

    注意：CJK 单字段不产生 token（循环为 ``len(seg) - 1``），与原型一致。
    """
    t = str(text).lower()
    tokens: list[str] = list(_ASCII_RE.findall(t))
    for seg in _CJK_RE.findall(t):
        for i in range(len(seg) - 1):
            tokens.append(seg[i : i + 2])
    return tokens


# ------------------------------------------------------------------ BM25
class BM25:
    """中文 bigram BM25（与原型 bm25.js 公式一致）。"""

    def __init__(self, docs: list[tuple[str, str]]) -> None:
        """docs: [(id, text)]"""
        self.entries: list[tuple[str, list[str]]] = [(d[0], tokenize(d[1])) for d in docs]
        self.doc_count = len(self.entries) or 1
        total = sum(len(toks) for _, toks in self.entries)
        # 原型在 avgLen=0 时会产生 NaN 并返回空结果；此处显式取 1.0 以避免除零
        self.avg_len = (total / self.doc_count) if total > 0 else 1.0
        self.df: Counter[str] = Counter()
        for _, toks in self.entries:
            self.df.update(set(toks))

    def search(self, query: str, top_n: int) -> list[tuple[str, float]]:
        qt = tokenize(query)
        if not qt:
            return []
        scored: list[tuple[str, float]] = []
        for cid, toks in self.entries:
            tf = Counter(toks)
            length = len(toks)
            s = 0.0
            for t in qt:
                f = tf.get(t)
                if not f:
                    continue
                df = self.df.get(t, 0)
                idf = math.log(1 + (self.doc_count - df + 0.5) / (df + 0.5))
                s += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * length / self.avg_len))
            if s > 0:
                scored.append((cid, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_n]


# ------------------------------------------------------------------ 可见性
def is_visible(chunk: Chunk, role: str | None) -> bool:
    """角色可见性判定。

    role 为空时按「访客」处理（修复原型缺省放行问题）。
    """
    effective = role or DEFAULT_ROLE
    roles = chunk.visible_roles or ["全部"]
    return "全部" in roles or effective in roles


def filter_visible(chunks: list[Chunk], role: str | None) -> list[Chunk]:
    return [c for c in chunks if is_visible(c, role)]


# ------------------------------------------------------------------ 检索
def _cosine_scores(chunks: list[Chunk], vector: list[float]) -> list[float]:
    """批量余弦（向量已归一化，点积即余弦）。"""
    if not chunks:
        return []
    mat = np.asarray([c.embedding for c in chunks], dtype=np.float32)
    q = np.asarray(vector, dtype=np.float32)
    return (mat @ q).astype(np.float32).tolist()


def vector_search(
    chunks: list[Chunk], vector: list[float], top_n: int | None = None
) -> list[Retrieved]:
    """纯向量检索。"""
    top_n = top_n or settings.top_k
    pool = [c for c in chunks if c.embedding]
    if not pool or not vector:
        return []
    scores = _cosine_scores(pool, vector)
    out = [
        Retrieved(chunk=c, score=s, cos=s, v_rank=i + 1)
        for i, (c, s) in enumerate(sorted(zip(pool, scores), key=lambda x: -x[1]))
    ]
    return out[:top_n]


def bm25_search(
    chunks: list[Chunk], query: str, top_n: int | None = None
) -> list[Retrieved]:
    """纯 BM25 检索。"""
    top_n = top_n or settings.top_k
    pool = [c for c in chunks if c.embedding]
    if not pool:
        return []
    by_id = {c.chunk_id: c for c in pool}
    hits = BM25([(c.chunk_id, c.embed_text) for c in pool]).search(query, len(pool))
    return [
        Retrieved(chunk=by_id[cid], score=s, bm25=s, b_rank=i + 1)
        for i, (cid, s) in enumerate(hits)
    ][:top_n]


def hybrid_search(
    chunks: list[Chunk],
    vector: list[float],
    query: str,
    role: str | None = None,
    top_n: int | None = None,
    mode: Mode | None = None,
) -> list[Retrieved]:
    """混合检索入口：角色过滤 → 向量 + BM25 → RRF 融合。

    :param role: 用户角色；None 按「访客」处理
    :param mode: hybrid（默认，RRF 融合）/ vector / bm25
    """
    top_n = top_n or settings.top_k
    mode = mode or settings.retrieve_mode  # type: ignore[assignment]
    if mode not in ("hybrid", "vector", "bm25"):
        mode = "hybrid"

    pool = [c for c in chunks if c.embedding and is_visible(c, role)]
    if not pool:
        return []

    # 向量召回 + 排名
    scores = _cosine_scores(pool, vector)
    rows = [
        {
            "chunk": c,
            "cos": s,
            "bm25": 0.0,
            "v_rank": 0,
            "b_rank": None,
            "rrf": 0.0,
        }
        for c, s in zip(pool, scores)
    ]
    rows.sort(key=lambda r: -r["cos"])
    for i, r in enumerate(rows):
        r["v_rank"] = i + 1

    # BM25 召回 + 排名
    hits = BM25([(c.chunk_id, c.embed_text) for c in pool]).search(query, len(pool))
    bmap = {cid: (s, i + 1) for i, (cid, s) in enumerate(hits)}

    k = settings.rrf_k
    for r in rows:
        hit = bmap.get(r["chunk"].chunk_id)
        if hit:
            r["bm25"], r["b_rank"] = hit
            r["rrf"] = 1.0 / (k + r["v_rank"]) + 1.0 / (k + hit[1])
        else:
            r["rrf"] = 1.0 / (k + r["v_rank"])

    key = {"hybrid": "rrf", "vector": "cos", "bm25": "bm25"}[mode]
    rows.sort(key=lambda r: -r[key])

    return [
        Retrieved(
            chunk=r["chunk"],
            score=r[key],
            cos=r["cos"],
            bm25=r["bm25"],
            v_rank=r["v_rank"],
            b_rank=r["b_rank"],
            rrf=r["rrf"],
        )
        for r in rows[:top_n]
    ]
