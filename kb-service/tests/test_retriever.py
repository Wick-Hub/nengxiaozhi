"""检索层测试（任务 1.5）。

覆盖：中文 bigram 分词、BM25、向量检索、RRF 融合、
以及**安全修复点**——角色缺省按「访客」，不再放行全部知识。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.chunker import Chunk
from app.core.retriever import (
    BM25,
    Retrieved,
    bm25_search,
    hybrid_search,
    is_visible,
    tokenize,
    vector_search,
)

STORE_JSON = Path(__file__).resolve().parent.parent.parent / "kb-engine" / "data" / "store.json"


def mk(cid: str, text: str, roles: list[str] | None = None, vec: list[float] | None = None) -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id="D1",
        text=text,
        embed_text=text,
        visible_roles=roles if roles is not None else ["全部"],
        embedding=vec if vec is not None else [1.0, 0.0, 0.0],
    )


# ------------------------------------------------------------------ 分词
def test_tokenize_cjk_bigram() -> None:
    assert tokenize("校园网") == ["校园", "园网"]


def test_tokenize_ascii() -> None:
    assert tokenize("Campus Card 2024") == ["campus", "card", "2024"]


def test_tokenize_mixed_order() -> None:
    # 与原型一致：先 ASCII 词，后 CJK bigram
    assert tokenize("校园网abc") == ["abc", "校园", "园网"]


def test_tokenize_single_cjk_char_has_no_token() -> None:
    # 单字段不产生 bigram（循环为 len-1），与原型一致
    assert tokenize("一") == []


def test_tokenize_empty() -> None:
    assert tokenize("") == []


# ------------------------------------------------------------------ 可见性（安全修复）
def test_visible_default_role_is_visitor() -> None:
    """核心修复：不传角色时按「访客」，受限切片不再被放行。"""
    restricted = mk("c1", "教职工内部通知", roles=["教职工"])
    assert is_visible(restricted, "教职工") is True
    assert is_visible(restricted, None) is False
    assert is_visible(restricted, "本科生") is False


def test_visible_public_chunk() -> None:
    public = mk("c1", "公开信息", roles=["全部"])
    for role in (None, "访客", "本科生", "教职工"):
        assert is_visible(public, role) is True


def test_visible_role_list() -> None:
    c = mk("c1", "面向访客与本科生", roles=["访客", "本科生"])
    assert is_visible(c, "本科生") is True
    assert is_visible(c, "访客") is True
    assert is_visible(c, "研究生") is False
    assert is_visible(c, None) is True  # 缺省访客在列表内


def test_visible_empty_roles_defaults_to_public() -> None:
    c = mk("c1", "未标注", roles=[])
    assert is_visible(c, None) is True
    assert is_visible(c, "本科生") is True


# ------------------------------------------------------------------ BM25
def test_bm25_ranks_matching_doc_first() -> None:
    docs = [
        ("c1", "校园网开户与账号申请流程"),
        ("c2", "食堂菜谱与营养搭配"),
        ("c3", "图书馆开放时间"),
    ]
    hits = BM25(docs).search("校园网开户", 3)
    assert hits and hits[0][0] == "c1"


def test_bm25_no_match_returns_empty() -> None:
    docs = [("c1", "校园网开户"), ("c2", "食堂菜谱")]
    assert BM25(docs).search("zzzzz", 3) == []


def test_bm25_empty_query() -> None:
    assert BM25([("c1", "校园网")]).search("", 3) == []


def test_bm25_all_empty_texts_no_crash() -> None:
    # avg_len=0 时不得抛除零异常
    assert BM25([("c1", ""), ("c2", "")]).search("校园网", 3) == []


# ------------------------------------------------------------------ 检索
def test_vector_search_order() -> None:
    chunks = [
        mk("c1", "甲", vec=[1.0, 0.0]),
        mk("c2", "乙", vec=[0.0, 1.0]),
        mk("c3", "丙", vec=[0.7071, 0.7071]),
    ]
    hits = vector_search(chunks, [1.0, 0.0], top_n=3)
    assert [h.chunk_id for h in hits] == ["c1", "c3", "c2"]
    assert hits[0].cos == pytest.approx(1.0, abs=1e-5)


def test_bm25_search_order() -> None:
    chunks = [
        mk("c1", "校园网开户流程"),
        mk("c2", "食堂菜谱"),
    ]
    hits = bm25_search(chunks, "校园网", top_n=5)
    assert [h.chunk_id for h in hits] == ["c1"]


def test_hybrid_search_returns_top_n() -> None:
    chunks = [mk(f"c{i}", f"校园网事项{i}") for i in range(10)]
    hits = hybrid_search(chunks, [1.0, 0.0, 0.0], "校园网", role="本科生", top_n=3)
    assert len(hits) == 3
    assert all(isinstance(h, Retrieved) for h in hits)


def test_hybrid_search_filters_by_role() -> None:
    chunks = [
        mk("pub", "公开的校园网信息", roles=["全部"]),
        mk("adm", "教职工专用的校园网信息", roles=["教职工"]),
    ]
    got = {h.chunk_id for h in hybrid_search(chunks, [1.0, 0.0, 0.0], "校园网", role=None, top_n=5)}
    assert got == {"pub"}


def test_hybrid_search_empty_pool() -> None:
    assert hybrid_search([], [1.0, 0.0, 0.0], "校园网") == []


def test_hybrid_search_no_embedding_excluded() -> None:
    c = Chunk(chunk_id="c1", embed_text="校园网", text="校园网", embedding=[])
    assert hybrid_search([c], [1.0, 0.0, 0.0], "校园网") == []


def test_rrf_formula() -> None:
    """RRF = 1/(K+vRank) + 1/(K+bRank)，K 取配置值。"""
    from app.config import settings

    chunks = [mk("c1", "校园网开户"), mk("c2", "食堂菜谱")]
    hits = hybrid_search(chunks, [1.0, 0.0, 0.0], "校园网", role=None, top_n=2)
    top = hits[0]
    assert top.v_rank == 1
    assert top.b_rank == 1
    assert top.rrf == pytest.approx(1 / (settings.rrf_k + 1) + 1 / (settings.rrf_k + 1), abs=1e-12)


def test_mode_bm25_ignores_vector_order() -> None:
    chunks = [
        mk("c1", "校园网开户流程", vec=[0.0, 1.0, 0.0]),
        mk("c2", "食堂菜谱", vec=[1.0, 0.0, 0.0]),
    ]
    hits = hybrid_search(chunks, [1.0, 0.0, 0.0], "校园网", role=None, top_n=1, mode="bm25")
    assert [h.chunk_id for h in hits] == ["c1"]


# ------------------------------------------------------------------ 真实索引集成
@pytest.fixture(scope="module")
def real_chunks() -> list[Chunk]:
    if not STORE_JSON.is_file():
        pytest.skip(f"未找到索引文件 {STORE_JSON}")
    store = json.loads(STORE_JSON.read_text(encoding="utf-8"))
    return [Chunk.from_dict(c) for c in store.get("chunks", [])]


def test_real_index_loaded(real_chunks: list[Chunk]) -> None:
    assert len(real_chunks) > 0
    assert all(c.embedding for c in real_chunks)
    assert len(real_chunks[0].embedding) == 512


def test_real_index_default_role_no_privilege_leak(real_chunks: list[Chunk]) -> None:
    """安全回归：不传角色时，不得返回任何「仅教职工可见」的切片。"""
    from app.core.embedder import get_embedder

    emb = get_embedder()
    vec = emb.encode_one("校园网怎么开通 一卡通丢失怎么办 宿舍报修")
    hits = hybrid_search(real_chunks, vec, "校园网怎么开通 一卡通丢失怎么办 宿舍报修", role=None, top_n=10)
    assert hits, "应能召回公共切片"
    for h in hits:
        assert is_visible(h.chunk, None), f"{h.chunk_id} 越权：{h.chunk.visible_roles}"
        assert "全部" in h.chunk.visible_roles or "访客" in h.chunk.visible_roles


def test_real_index_role_filtering_differs_by_role(real_chunks: list[Chunk]) -> None:
    """不同角色看到的切片集合不同，且各自都严格满足可见性规则。

    注意：访客可见集**不是**教职工可见集的子集——存在「访客/本科生专属」切片
    （visible_roles=['访客','本科生']），它们对教职工不可见。
    """
    from app.core.embedder import get_embedder

    emb = get_embedder()
    q = "校园网 上网资费 一卡通 宿舍报修 图书馆 考试"
    vec = emb.encode_one(q)
    anon = {h.chunk_id for h in hybrid_search(real_chunks, vec, q, role=None, top_n=35)}
    staff = {h.chunk_id for h in hybrid_search(real_chunks, vec, q, role="教职工", top_n=35)}
    under = {h.chunk_id for h in hybrid_search(real_chunks, vec, q, role="本科生", top_n=35)}
    by_id = {c.chunk_id: c for c in real_chunks}

    # 教职工专属切片：教职工可见、访客不可见
    staff_only = staff - anon
    assert staff_only, "应存在仅教职工可见的切片"
    assert all("教职工" in by_id[c].visible_roles for c in staff_only)

    # 访客/本科生专属切片：访客可见、教职工不可见
    anon_only = anon - staff
    assert anon_only, "应存在仅访客/本科生可见的切片"
    assert all("教职工" not in by_id[c].visible_roles for c in anon_only)

    # 每个角色的结果都严格满足自身可见性
    for cid in anon:
        assert is_visible(by_id[cid], None)
    for cid in staff:
        assert is_visible(by_id[cid], "教职工")
    for cid in under:
        assert is_visible(by_id[cid], "本科生")
