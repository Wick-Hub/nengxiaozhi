"""精排层测试（任务 1.6）。

覆盖：跳过精排的条件、降级路径、排序效果、以及配置开关。
依赖模型的用例在模型不可用时自动 skip。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.core.chunker import Chunk
from app.core.reranker import Reranker, get_reranker, reset_reranker
from app.core.retriever import Retrieved

STORE_JSON = Path(__file__).resolve().parent.parent.parent / "kb-engine" / "data" / "store.json"


def mk(cid: str, text: str) -> Chunk:
    return Chunk(chunk_id=cid, doc_id="D1", text=text, embed_text=text)


def cands(n: int) -> list[Retrieved]:
    return [Retrieved(chunk=mk(f"c{i}", f"候选文本{i}")) for i in range(n)]


@pytest.fixture(scope="module")
def rr() -> Reranker:
    r = Reranker()
    if not r.load():
        pytest.skip(f"精排模型不可用：{r.status()['error']}")
    return r


# ------------------------------------------------------------------ 跳过精排
def test_skip_when_candidates_le_topk() -> None:
    """候选数 ≤ topK 时原序返回且不截断（与原型一致）。"""
    r = Reranker()
    items = cands(3)
    out = r.rerank("任意问题", items, top_k=5)
    assert out is items  # 原对象、原序
    assert len(out) == 3


def test_skip_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rerank_enabled", False)
    r = Reranker()
    items = cands(10)
    out = r.rerank("任意问题", items, top_k=3)
    assert out is items  # 未启用 → 原序全量返回
    assert len(out) == 10


# ------------------------------------------------------------------ 降级
def test_fallback_when_model_missing() -> None:
    """模型不可用时降级为原序前 topK，不抛异常。"""
    r = Reranker(model_name="nonexistent/model-xyz")
    assert r.load() is False
    items = cands(10)
    out = r.rerank("任意问题", items, top_k=3)
    assert [x.chunk_id for x in out] == ["c0", "c1", "c2"]
    assert r.status()["available"] is False


def test_failed_load_not_retried() -> None:
    """加载失败后不再重试（与原型 loadFailed 语义一致）。"""
    r = Reranker(model_name="nonexistent/model-xyz")
    assert r.load() is False
    assert r.load() is False
    assert r.status()["error"]


def test_score_before_load_raises() -> None:
    r = Reranker(model_name="nonexistent/model-xyz")
    with pytest.raises(RuntimeError):
        r.score("q", ["t"])


# ------------------------------------------------------------------ 依赖模型
def test_score_length(rr: Reranker) -> None:
    scores = rr.score("一卡通", ["文本一", "文本二", "文本三"])
    assert len(scores) == 3
    assert all(isinstance(s, float) for s in scores)


def test_score_empty(rr: Reranker) -> None:
    assert rr.score("一卡通", []) == []


def test_rerank_puts_relevant_first(rr: Reranker) -> None:
    items = [
        Retrieved(chunk=mk("rel", "一卡通丢失后请携带学生证到卡务中心补办，工本费 20 元。")),
        Retrieved(chunk=mk("food", "食堂今天中午供应红烧肉和青菜。")),
        Retrieved(chunk=mk("net", "校园网开户需要到信网中心服务大厅办理。")),
    ]
    out = rr.rerank("一卡通丢失怎么办", items, top_k=1)
    assert out[0].chunk_id == "rel"
    assert "rerank" in out[0].extra  # 保留精排分便于调试


def test_rerank_returns_top_k(rr: Reranker) -> None:
    items = cands(12)
    out = rr.rerank("候选文本", items, top_k=5)
    assert len(out) == 5
    assert len({x.chunk_id for x in out}) == 5


def test_rerank_is_permutation(rr: Reranker) -> None:
    """精排只是重排，不得丢失或引入候选。"""
    items = cands(12)
    ids_before = {x.chunk_id for x in items}
    out = rr.rerank("候选文本", items, top_k=5)
    assert {x.chunk_id for x in out} <= ids_before


def test_singleton() -> None:
    reset_reranker()
    assert get_reranker() is get_reranker()
    reset_reranker()


# ------------------------------------------------------------------ 真实索引
@pytest.mark.skipif(not STORE_JSON.is_file(), reason="未找到索引文件")
def test_real_index_rerank_relevance(rr: Reranker) -> None:
    store = json.loads(STORE_JSON.read_text(encoding="utf-8"))
    chunks = [Chunk.from_dict(c) for c in store["chunks"][:20]]
    items = [Retrieved(chunk=c) for c in chunks]
    out = rr.rerank("一卡通丢失了怎么办", items, top_k=5)
    assert len(out) == 5
    # 精排后首条应属于与一卡通相关的文档（D004 为一卡通主题）
    assert out[0].chunk_id.startswith("D004")
