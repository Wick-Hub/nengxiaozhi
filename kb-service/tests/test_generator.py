"""生成层测试（任务 1.7）。

覆盖：提示词组装、引用解析（保留原始编号）、拒答与置信度、
mock 生成与流式（无需真实 LLM）。
"""

from __future__ import annotations

import pytest

from app.core.chunker import Chunk
from app.core.generator import (
    SYSTEM_PROMPT,
    Generator,
    build_context,
    build_messages,
    confidence,
    is_refusal,
    parse_citations,
)
from app.core.llm_client import LLMClient, LLMError
from app.core.retriever import Retrieved


def mk(cid: str, text: str, doc_id: str = "D001", dept: str = "信息网络中心", ktype: str = "办事指南") -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id=doc_id,
        dept=dept,
        ktype=ktype,
        title_path="校园网指南 > 开户",
        text=text,
        embed_text=text,
    )


def sample_results() -> list[Retrieved]:
    return [
        Retrieved(chunk=mk("D001-C001", "一卡通丢失后请到卡务中心补办。", "D001"), cos=0.9123),
        Retrieved(chunk=mk("D001-C002", "补办需携带学生证。", "D001"), cos=0.8456),
        Retrieved(chunk=mk("D002-C001", "食堂供应时间。", "D002"), cos=0.7891),
    ]


# ------------------------------------------------------------------ 提示词
def test_system_prompt_has_five_rules() -> None:
    assert "能小智" in SYSTEM_PROMPT
    assert "只能依据" in SYSTEM_PROMPT
    assert "未找到相关信息" in SYSTEM_PROMPT


def test_build_context_format() -> None:
    ctx = build_context(sample_results())
    assert ctx.startswith("[1] 信息网络中心/办事指南 · 校园网指南 > 开户\n")
    assert "\n\n[2] " in ctx
    assert "\n\n[3] " in ctx


def test_build_context_empty() -> None:
    assert build_context([]) == ""


def test_build_messages_without_role() -> None:
    msgs = build_messages("一卡通丢了", sample_results())
    assert msgs[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert len(msgs) == 2
    assert msgs[1]["content"].startswith("知识资料：\n[1] ")
    assert msgs[1]["content"].endswith("问题：一卡通丢了")
    assert "提问者身份" not in msgs[1]["content"]


def test_build_messages_with_role_and_history() -> None:
    history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好"}]
    msgs = build_messages("一卡通丢了", sample_results(), history=history, role="本科生")
    assert len(msgs) == 4
    assert msgs[1] == history[0]
    assert msgs[2] == history[1]
    assert msgs[3]["content"].endswith("（提问者身份：本科生，只能依据其可见资料回答）")


# ------------------------------------------------------------------ 引用解析
def test_parse_citations_keeps_original_number() -> None:
    """核心修复：保留正文原始编号，避免来源面板错位。"""
    results = sample_results()
    cites = parse_citations("结论见[2]，依据见[1]。", results)
    assert [c["n"] for c in cites] == [1, 2]
    assert cites[0]["chunkId"] == results[0].chunk_id
    assert cites[1]["chunkId"] == results[1].chunk_id


def test_parse_citations_out_of_range_ignored() -> None:
    cites = parse_citations("见[9]和[99]和[0]", sample_results())
    assert cites == []


def test_parse_citations_dedup_and_sorted() -> None:
    cites = parse_citations("[3][1][3][2]", sample_results())
    assert [c["n"] for c in cites] == [1, 2, 3]


def test_parse_citations_fields() -> None:
    c = parse_citations("[1]", sample_results())[0]
    assert c["n"] == 1
    assert c["chunkId"] == "D001-C001"
    assert c["docId"] == 1  # "D001" → 1
    assert c["similarity"] == 0.9123
    assert c["titlePath"] == "校园网指南 > 开户"
    assert c["dept"] == "信息网络中心"
    assert c["ktype"] == "办事指南"


def test_parse_citations_no_citation() -> None:
    assert parse_citations("没有引用", sample_results()) == []


def test_parse_citations_docid_non_numeric() -> None:
    results = [Retrieved(chunk=mk("X-C001", "文本", doc_id="DOC"), cos=0.5)]
    assert parse_citations("[1]", results)[0]["docId"] == 0


# ------------------------------------------------------------------ 拒答与置信度
def test_is_refusal() -> None:
    assert is_refusal("抱歉，知识库中未找到相关信息，建议联系相关部门或转人工咨询。") is True
    assert is_refusal("我无法回答这个问题") is True
    assert is_refusal("没有相关资料") is True
    assert is_refusal("一卡通补办需携带学生证。") is False


def test_confidence_refusal() -> None:
    assert confidence(sample_results(), "未找到相关信息") == 0.15


def test_confidence_normal() -> None:
    assert confidence(sample_results(), "正常回答") == 0.9123


def test_confidence_no_results() -> None:
    assert confidence([], "正常回答") == 0.0


# ------------------------------------------------------------------ mock 生成
def test_generate_mock() -> None:
    gen = Generator(LLMClient(mock=True))
    res = gen.generate("一卡通丢了", sample_results(), role="本科生")
    assert "模拟回答" in res.answer
    assert res.refused is False
    assert res.confidence == 0.9123
    assert res.model == "mock"
    assert [c["n"] for c in res.citations] == [1, 2]  # mock 取前两个资料编号
    assert res.usage["total_tokens"] == 666


def test_generate_mock_refusal() -> None:
    gen = Generator(LLMClient(mock=True))
    res = gen.generate("学校有火星移民政策吗", sample_results())
    assert res.refused is True
    assert res.confidence == 0.15
    assert res.citations == []


def test_generate_stream_mock() -> None:
    gen = Generator(LLMClient(mock=True))
    chunks: list[str] = []
    res = gen.generate_stream("一卡通丢了", sample_results(), on_delta=chunks.append)
    assert "".join(chunks) == res.answer
    assert len(chunks) > 1


def test_generate_stream_without_callback() -> None:
    gen = Generator(LLMClient(mock=True))
    res = gen.generate_stream("一卡通丢了", sample_results())
    assert res.answer


# ------------------------------------------------------------------ 客户端
def test_client_available_with_mock() -> None:
    assert LLMClient(mock=True).available() is True


def test_client_unavailable_without_key() -> None:
    client = LLMClient(mock=False, api_key="")
    assert client.available() is False


def test_client_raises_without_key() -> None:
    client = LLMClient(mock=False, api_key="")
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])


def test_mock_deterministic() -> None:
    client = LLMClient(mock=True)
    msgs = [{"role": "user", "content": "资料[1][2] 问题"}]
    assert client.chat(msgs).content == client.chat(msgs).content
