"""多轮对话验收（任务 1.8：≥10 轮）。

在真实存储（默认 ``.env`` 的 ``KB_STORE``）上连续提问 >10 轮，验证：

1. 每轮问答完整落库（user + ai 各一条）
2. 传给大模型的上下文窗口保留 **≥10 轮**，且**首条必为 user**
3. 跨会话互不串

用法::

    python tools/verify_multiturn.py                # 12 轮
    python tools/verify_multiturn.py --turns 16
    python tools/verify_multiturn.py --keep         # 保留测试会话（默认清理）
"""

from __future__ import annotations

import argparse
import os
import sys

# 必须在 import app.* 之前固定：用确定性模拟回答，不依赖网络
os.environ.setdefault("LLM_MOCK", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import bindparam, text  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.pipeline import answer_question  # noqa: E402
from app.db import chatlog  # noqa: E402
from app.db.session import db_transaction  # noqa: E402
from app.session.context import MIN_TURNS, history_stats, load_context  # noqa: E402

_PURGE_SQL = [
    # 顺序敏感：先删依赖 chat_message 的引用与计量
    "DELETE c FROM answer_citation c JOIN chat_message m ON m.msg_id = c.msg_id "
    "WHERE m.session_id IN :ids",
    "DELETE FROM token_usage WHERE trace_id IN "
    "(SELECT trace_id FROM chat_message WHERE session_id IN :ids)",
    "DELETE FROM unrecognized_question WHERE session_id IN :ids",
    "DELETE FROM chat_message WHERE session_id IN :ids",
    "DELETE FROM chat_session WHERE session_id IN :ids",
]


def _purge(sids: list[str]) -> None:
    if not sids or settings.kb_store != "mysql":
        return
    with db_transaction() as conn:
        for sql in _PURGE_SQL:
            conn.execute(
                text(sql).bindparams(bindparam("ids", expanding=True)), {"ids": sids}
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="kb-service 多轮对话验收")
    ap.add_argument("--turns", type=int, default=12, help="连续问答轮数（默认 12）")
    ap.add_argument("--keep", action="store_true", help="保留测试会话（默认清理）")
    args = ap.parse_args(argv)

    turns = max(args.turns, MIN_TURNS + 1)
    print("=== 多轮对话验收（任务 1.8）===")
    print(f"存储后端 : {settings.kb_store}")
    print(f"LLM      : {'mock（确定性）' if settings.llm_mock else 'real'}")
    print(f"上下文窗口: {settings.history_max_messages} 条消息（≈{settings.history_max_messages // 2} 轮）")
    print(f"计划轮数 : {turns}")
    print()

    sid_a = chatlog.new_session_id()
    sid_b = chatlog.new_session_id()
    failures: list[str] = []

    try:
        for i in range(turns):
            answer_question(f"第 {i + 1} 轮：校园网怎么开户？", role="本科生", session_id=sid_a)
            ctx = load_context(sid_a)
            st = history_stats(ctx)
            head = ctx[0]["role"] if ctx else "-"
            print(f"  轮 {i + 1:>2} 落库后 → 上下文 {st['messages']:>2} 条 / {st['turns']:>2} 轮 ｜ 首条={head}")

        # ---- 断言 1：消息完整落库
        msgs = chatlog.load_session(sid_a)["messages"]
        if len(msgs) != turns * 2:
            failures.append(f"落库消息数应为 {turns * 2}，实际 {len(msgs)}")

        # ---- 断言 2：窗口保留 ≥10 轮，首条为 user
        final_ctx = load_context(sid_a)
        st = history_stats(final_ctx)
        if not final_ctx or final_ctx[0]["role"] != "user":
            failures.append("上下文首条不是 user（配对错位）")
        if st["turns"] < MIN_TURNS:
            failures.append(f"上下文轮次 {st['turns']} < {MIN_TURNS}")

        # ---- 断言 3：跨会话不串
        ctx_b = load_context(sid_b)
        if ctx_b:
            failures.append(f"会话 B 应为空历史，实际 {len(ctx_b)} 条")

        print()
        print(f"最终上下文：{st['messages']} 条消息 / {st['turns']} 轮 ｜ 首条={final_ctx[0]['role']}")
        print(f"会话 A 落库：{len(msgs)} 条 ｜ 会话 B 历史：{len(ctx_b)} 条")
    finally:
        if not args.keep:
            _purge([sid_a, sid_b])

    print()
    if failures:
        print("❌ 验收未通过：")
        for f in failures:
            print(f"   - {f}")
        return 1

    print("✅ 验收通过：连续 %d 轮上下文正确，窗口保留 ≥%d 轮，跨会话隔离正常" % (turns, MIN_TURNS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
