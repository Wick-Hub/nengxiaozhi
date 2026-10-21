"""会话上下文测试（任务 1.8：多轮对话 ≥10 轮）。

覆盖：
- ``normalize_history`` 纯函数：角色映射 / 空内容 / 窗口截断 / 配对修正
- ``load_context``：首轮无历史、≥10 轮上下文完整、跨会话不串
- ``history_stats`` / ``rewrite_query``
- pipeline 集成：传给生成层的上下文首条必为 ``user``

会话文件由 ``tests/conftest.py`` 的 autouse fixture 隔离到 tmp_path。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import settings
from app.db import chatlog
from app.session import context as ctx


@dataclass
class _Gen:
    """最小 GenerationResult 替身。"""

    answer: str = "回答"
    citations: list = field(default_factory=list)
    confidence: float = 0.7
    refused: bool = False
    usage: dict | None = None
    model: str = "mock"
    latency_ms: int = 1
    first_token_ms: int | None = None
    # 任务 2.4 新增：编排层会把这两个诊断字段回填进 Answer，替身需同形
    model_code: str = ""
    model_source: str = ""


def _seed(sid: str, turns: int) -> None:
    for i in range(turns):
        chatlog.save_qa(f"问题{i}", None, _Gen(answer=f"回答{i}"), sid)


# ------------------------------------------------------------------ 纯函数
def test_normalize_maps_ai_to_assistant() -> None:
    out = ctx.normalize_history(
        [
            {"role": "user", "content": "问"},
            {"role": "ai", "content": "答"},
            {"role": "system", "content": "系统"},
        ]
    )
    assert [m["role"] for m in out] == ["user", "assistant", "user"]


def test_normalize_drops_empty_content() -> None:
    out = ctx.normalize_history(
        [
            {"role": "user", "content": "   "},
            {"role": "ai", "content": ""},
            {"role": "user", "content": "有效"},
        ]
    )
    assert out == [{"role": "user", "content": "有效"}]


def test_normalize_truncates_to_limit() -> None:
    msgs = [{"role": "user", "content": f"q{i}"} for i in range(30)]
    out = ctx.normalize_history(msgs, limit=5)
    assert len(out) == 5
    assert out[-1]["content"] == "q29"


def test_normalize_keeps_user_first() -> None:
    """截断后首条为 assistant 时应丢弃，保证配对从 user 起。"""
    msgs = [
        {"role": "ai", "content": "a0"},
        {"role": "user", "content": "q1"},
        {"role": "ai", "content": "a1"},
    ]
    out = ctx.normalize_history(msgs)
    assert out[0] == {"role": "user", "content": "q1"}
    assert len(out) == 2


def test_normalize_can_keep_assistant_first() -> None:
    msgs = [{"role": "ai", "content": "a0"}, {"role": "user", "content": "q1"}]
    out = ctx.normalize_history(msgs, ensure_user_first=False)
    assert out[0]["role"] == "assistant"


def test_normalize_all_assistant_becomes_empty() -> None:
    out = ctx.normalize_history([{"role": "ai", "content": "a"}, {"role": "ai", "content": "b"}])
    assert out == []


# ------------------------------------------------------------------ load_context
def test_load_context_without_session_is_empty() -> None:
    assert ctx.load_context(None) == []
    assert ctx.load_context("") == []
    assert ctx.load_context("S_NOT_EXIST_AT_ALL") == []


def test_load_context_returns_full_window_for_ten_turns() -> None:
    sid = chatlog.new_session_id()
    _seed(sid, 10)  # 20 条消息 = 10 轮

    out = ctx.load_context(sid)
    assert len(out) == 20
    assert out[0] == {"role": "user", "content": "问题0"}
    assert out[-1] == {"role": "assistant", "content": "回答9"}
    assert ctx.history_stats(out) == {"messages": 20, "turns": 10}


def test_load_context_keeps_at_least_ten_turns_beyond_window() -> None:
    """12 轮（24 条）超窗后仍应保留 ≥10 轮上下文，且首条为 user。"""
    sid = chatlog.new_session_id()
    _seed(sid, 12)

    out = ctx.load_context(sid)
    assert out[0]["role"] == "user"
    stats = ctx.history_stats(out)
    assert stats["turns"] >= ctx.MIN_TURNS, stats
    assert stats["turns"] == 10
    # 最近一轮必须在
    assert out[-1] == {"role": "assistant", "content": "回答11"}


def test_load_context_honours_explicit_limit() -> None:
    sid = chatlog.new_session_id()
    _seed(sid, 10)
    out = ctx.load_context(sid, limit=6)
    assert len(out) == 6
    assert out[-1]["content"] == "回答9"


def test_load_context_isolates_sessions() -> None:
    """跨会话不串：各自只看到自己的消息。"""
    a, b = chatlog.new_session_id(), chatlog.new_session_id()
    chatlog.save_qa("A 的问题", None, _Gen(answer="A 的回答"), a)
    chatlog.save_qa("B 的问题", None, _Gen(answer="B 的回答"), b)

    ca = [m["content"] for m in ctx.load_context(a)]
    cb = [m["content"] for m in ctx.load_context(b)]
    assert ca == ["A 的问题", "A 的回答"]
    assert cb == ["B 的问题", "B 的回答"]
    assert not set(ca) & set(cb)


def test_default_window_is_twenty() -> None:
    """窗口默认值应为 20 条（≈10 轮），不得回退到原型的 8 条。"""
    assert settings.history_max_messages >= 2 * ctx.MIN_TURNS


# ------------------------------------------------------------------ 预留钩子
def test_rewrite_query_is_identity_for_now() -> None:
    assert ctx.rewrite_query("它怎么用") == "它怎么用"
    assert ctx.rewrite_query("它怎么用", [{"role": "user", "content": "校园网"}]) == "它怎么用"


# ------------------------------------------------------------------ pipeline 集成
def test_pipeline_passes_user_first_context(monkeypatch) -> None:
    """pipeline 应把规范化后的上下文交给生成层（首条必为 user）。"""
    from app.core import pipeline

    sid = chatlog.new_session_id()
    _seed(sid, 10)  # 20 条
    chatlog.append_message(sid, "ai", "孤立的回答", confidence=0.5)  # 第 21 条 → 窗口首条变 assistant

    captured: dict = {}

    class _FakeGenerator:
        # system_prompt 是任务 2.3 新增的助手提示词入参；model_code/temperature/top_p/max_tokens
        # 是任务 2.4 新增的生成参数入参——桩必须与 Generator.generate 同形。
        def generate(
            self,
            question,
            results,
            role=None,
            history=None,
            system_prompt=None,
            model_code=None,
            temperature=None,
            top_p=None,
            max_tokens=None,
        ):
            captured["history"] = history
            return _Gen()

    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: [object()])
    monkeypatch.setattr(pipeline, "get_generator", lambda: _FakeGenerator())

    pipeline.answer_question("新问题", session_id=sid, save=False)

    hist = captured["history"]
    assert hist, "应传入非空上下文"
    assert hist[0]["role"] == "user", "截断后首条必须是 user"
    assert hist[0]["content"] == "问题1"  # 问题0 与其回答一并被裁掉
