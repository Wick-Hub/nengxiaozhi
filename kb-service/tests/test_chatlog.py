"""会话存储测试（任务 1.8 多轮 / 1.14 数据层）。

使用 tmp_path 隔离 ``sessions.json``，避免污染服务运行数据。
覆盖：ID 生成、落库、多轮历史、窗口截断、列表聚合、ai→assistant 映射。

本文件只覆盖 **JSON 后端**；MySQL 后端需要真实数据库，
测试在 ``tests/test_mysql_store.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.db import chatlog


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(chatlog, "SESSIONS_PATH", tmp_path / "sessions.json")
    yield


@dataclass
class _Gen:
    """最小 GenerationResult 替身。"""

    answer: str = "根据知识库资料[1]，请携带校园卡前往办理。"
    citations: list = None  # type: ignore[assignment]
    confidence: float = 0.72
    refused: bool = False

    def __post_init__(self):
        if self.citations is None:
            self.citations = [{"n": 1, "chunkId": "d1::0"}]


def test_new_ids_unique_and_prefixed() -> None:
    sids = {chatlog.new_session_id() for _ in range(50)}
    assert len(sids) == 50
    assert all(s.startswith("S") for s in sids)
    assert chatlog.new_trace_id().startswith("T")


def test_new_ids_survive_same_millisecond_burst() -> None:
    """同一毫秒内高并发建会话不得碰撞（会话 ID 撞了 = 两个用户串话）。

    这条守卫是为一个**真实缺陷**补的：随机部分原为 ``token_hex(2)``（16 bit，
    65536 种），同一毫秒 50 个会话实测碰撞率约 1.3%、500 个时约 85%。
    原测试只用 50 个样本且断言恰好 50 个唯一，属于**偶发 flaky**——它偶尔红一次，
    很容易被当成「重跑就好了」，而真正的问题是会话串话。

    这里把样本量提到 2000（20 轮）并把 trace_id 一起纳入：修复后（``token_hex(4)``，
    32 bit）同一毫秒 2000 个的理论碰撞率约 0.05%，20 轮里出现碰撞的概率可忽略。
    样本量再大只是拖慢测试，不会提高判别力。
    """
    for _ in range(20):
        sids = {chatlog.new_session_id() for _ in range(2000)}
        assert len(sids) == 2000, f"同一毫秒内会话 ID 碰撞（唯一 {len(sids)}/2000）"
        tids = {chatlog.new_trace_id() for _ in range(2000)}
        assert len(tids) == 2000, f"同一毫秒内链路 ID 碰撞（唯一 {len(tids)}/2000）"


def test_save_qa_writes_pair() -> None:
    sid = chatlog.new_session_id()
    out = chatlog.save_qa("一卡通丢了怎么办", "本科生", _Gen(), sid, "T123")
    assert out["sessionId"] == sid
    assert out["msgId"] == 2  # user=1, ai=2

    sess = chatlog.load_session(sid)
    assert sess is not None
    assert sess["role"] == "本科生"
    msgs = sess["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "一卡通丢了怎么办"
    assert msgs[1]["role"] == "ai"
    assert msgs[1]["confidence"] == pytest.approx(0.72)
    assert msgs[1]["citations"] == [{"n": 1, "chunkId": "d1::0"}]
    assert msgs[1]["traceId"] == "T123"


def test_default_role_is_guest() -> None:
    sid = chatlog.new_session_id()
    chatlog.append_message(sid, "user", "你好")
    assert chatlog.load_session(sid)["role"] == "访客"


def test_multiturn_history_maps_roles() -> None:
    """10 轮问答（20 条消息）应全部可读，且 ai 映射为 assistant。"""
    sid = chatlog.new_session_id()
    for i in range(10):
        chatlog.save_qa(f"问题{i}", "研究生", _Gen(answer=f"回答{i}"), sid)

    hist = chatlog.load_history(sid)
    assert len(hist) == 20
    assert {m["role"] for m in hist} == {"user", "assistant"}
    assert hist[0]["content"] == "问题0"
    assert hist[-1]["content"] == "回答9"


def test_history_window_truncates_oldest() -> None:
    sid = chatlog.new_session_id()
    for i in range(15):
        chatlog.save_qa(f"q{i}", None, _Gen(answer=f"a{i}"), sid)

    hist = chatlog.load_history(sid, limit=6)
    assert len(hist) == 6
    # 保留最近 6 条：a12 之后是 q13, a13, q14, a14 ... 实际为最后 3 轮
    assert hist[-1]["content"] == "a14"
    assert "q9" not in [m["content"] for m in hist]


def test_history_without_session_is_empty() -> None:
    assert chatlog.load_history(None) == []
    assert chatlog.load_history("S_NOT_EXIST") == []


def test_append_message_autoincrements() -> None:
    sid = chatlog.new_session_id()
    m1 = chatlog.append_message(sid, "user", "a")
    m2 = chatlog.append_message(sid, "ai", "b", confidence=0.5)
    assert (m1["msgId"], m2["msgId"]) == (1, 2)


def test_list_sessions_aggregates() -> None:
    s1 = chatlog.new_session_id()
    s2 = chatlog.new_session_id()
    chatlog.save_qa("第一个问题", "本科生", _Gen(), s1)
    chatlog.append_message(s2, "user", "第二个问题", user_role="访客")

    items = chatlog.list_sessions(limit=50)
    ids = {i["sessionId"] for i in items}
    assert {s1, s2} <= ids
    row1 = next(i for i in items if i["sessionId"] == s1)
    assert row1["messageCount"] == 2
    assert row1["lastQuestion"] == "第一个问题"
    assert row1["role"] == "本科生"


def test_corrupt_file_recovers(tmp_path: Path) -> None:
    chatlog.SESSIONS_PATH.write_text("not-json", encoding="utf-8")
    sid = chatlog.new_session_id()
    chatlog.append_message(sid, "user", "hi")  # 不应抛异常
    assert chatlog.load_session(sid)["messages"][0]["content"] == "hi"


# MySQL 后端（chat_session / chat_message / answer_citation / token_usage /
# unrecognized_question）的测试在 tests/test_mysql_store.py 中，需要真实数据库。
