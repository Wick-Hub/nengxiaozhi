"""前端契约联调校验：严格按 static/app.js 的消费逻辑跑一遍真实链路。

用途：迁入前端后，验证后端返回的字段名/结构与 app.js 的读取方式完全对得上，
而不是只靠「接口 200」这种弱断言。

用法（在 kb-service 目录下）::

    python tools/verify_frontend_contract.py
    python tools/verify_frontend_contract.py --port 8050

校验点（逐条对应 app.js 的代码位置）：
    1. ask()      请求体 {question, role, sessionId}
    2. 流式解析   delta 事件的 text 字段（acc += obj.text）
    3. finalize() done 事件的 sessionId/answer/citations/model/latencyMs/confidence/refused
    4. addCitations() citation 的 n / chunkId
    5. loadHistory() /api/sessions/{id}/messages 的 data 为**数组**，元素含 role/content
    6. 错误路径   未配置 LLM 时返回 503（app.js 据 resp.ok 直接展示 error）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR / "tools"))

from _server import read_log, spawn_server, stop_server, wait_port  # noqa: E402

DONE_FIELDS = [
    "sessionId",
    "answer",
    "citations",
    "model",
    "latencyMs",
    "confidence",
    "refused",
    # 任务 1.9 富文本：done 同时给出结构化 blocks 与安全 HTML
    "blocks",
    "answerHtml",
]


def check_llm_unavailable(port: int, store: str = "json") -> None:
    """未配置 LLM 时应 503，且带 error 文案（app.js 会直接展示）。"""
    env = os.environ.copy()
    env["LLM_MOCK"] = "0"
    env["LLM_API_KEY"] = ""
    env["KB_STORE"] = store
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port):
            print("  [跳过] 无 LLM 场景服务未就绪")
            return
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20, trust_env=False) as c:
            r = c.post("/api/chat", json={"question": "校园网怎么开通", "role": "本科生"})
            body = r.json()
            ok = r.status_code == 503 and body.get("ok") is False and "LLM_API_KEY" in (body.get("error") or "")
            print(f"  7. 无 LLM 降级        -> {r.status_code} ok={body.get('ok')} {'✔' if ok else '✘'}")
            assert ok, f"应为 503 + ok=false + error，实际 {r.status_code} {body}"
    finally:
        stop_server(proc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="前端契约联调校验")
    ap.add_argument("--port", type=int, default=8049)
    ap.add_argument(
        "--store",
        default="json",
        choices=["json", "mysql"],
        help=(
            "子进程使用的存储模式（默认 json）。字段契约与存储无关，"
            "默认走 JSON 可在未启动 MySQL 时也能跑通；"
            "MySQL 不可达时 /api/health 会阻塞数十秒并触发读超时。"
        ),
    )
    args = ap.parse_args(argv)

    env = os.environ.copy()
    env["LLM_MOCK"] = "1"
    env["KB_STORE"] = args.store
    proc, log_path = spawn_server(args.port, env, SERVICE_DIR)
    try:
        if not wait_port(args.port):
            print("服务启动超时：")
            stop_server(proc)
            print(read_log(log_path, 2000))
            return 1

        # trust_env=False：只与本地服务通信，忽略 HTTP(S)_PROXY 等环境变量
        # （否则本机存在代理时，127.0.0.1 的请求会被转发出去并超时）
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=30, trust_env=False) as c:
            health = c.get("/api/health").json()
            print(f"服务已就绪 | llm={health['llm']} store={health['store']} docs={health['docs']} chunks={health['chunks']}\n")

            # 1) ask() 的请求体（app.js 原样：sessionId 可能为 null）
            payload = {"question": "校园网怎么开通？", "role": "本科生", "sessionId": None}
            print(f"  1. 请求体            -> {payload}")

            acc, started, final = "", False, None
            with c.stream("POST", "/api/chat/stream", json=payload) as resp:
                assert resp.status_code == 200, f"流式接口应 200，实际 {resp.status_code}"
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        obj = json.loads(line[5:].strip())
                        if obj["type"] == "delta":
                            assert "text" in obj, f"delta 缺 text：{obj}"
                            started = True
                            acc += obj["text"]
                        elif obj["type"] == "done":
                            final = obj
                        elif obj["type"] == "error":
                            raise AssertionError(f"收到 error 事件：{obj}")

            assert final is not None, "未收到 done 事件"
            print(f"  2. delta.text 累积    -> {len(acc)} 字，started={started} ✔")

            missing = [k for k in DONE_FIELDS if k not in final]
            assert not missing, f"done 缺字段：{missing}"
            assert final["answer"] == acc, "done.answer 应等于累积增量"
            print(f"  3. done 字段齐备      -> {DONE_FIELDS} ✔（answer 与累积一致）")
            print(f"     sessionId={final['sessionId']} msgId={final.get('msgId')} latencyMs={final['latencyMs']}")

            c0 = final["citations"][0]
            for k in ("n", "chunkId"):
                assert k in c0, f"citation 缺 {k}"
            print(f"  4. citation 字段      -> n={c0['n']} chunkId={c0['chunkId']} ✔")

            # 任务 1.9：done.answerHtml 必须与本地渲染器同源（blocks → HTML）
            from app.core.richtext import parse_blocks, render_html

            assert isinstance(final["blocks"], list) and final["blocks"], "done.blocks 不能为空"
            assert render_html(final["answer"]) == final["answerHtml"], "answerHtml 与 answer 渲染不一致"
            assert parse_blocks(final["answer"]) == final["blocks"], "blocks 与 answer 解析不一致"
            print(
                f"  5. 富文本字段         -> blocks={[b['type'] for b in final['blocks']]} "
                f"answerHtml={len(final['answerHtml'])} 字 ✔（与 answer 同源）"
            )

            hist = c.get(f"/api/sessions/{final['sessionId']}/messages").json()
            assert hist["ok"] is True and isinstance(hist["data"], list), "data 必须是数组"
            roles = [m["role"] for m in hist["data"]]
            ai_msgs = [m for m in hist["data"] if m["role"] == "ai"]
            for m in ai_msgs:
                assert m.get("answerHtml"), "历史 ai 消息应带 answerHtml（刷新前后渲染一致）"
            print(f"  6. 历史 data 为数组   -> {len(hist['data'])} 条 {roles} ✔（ai 消息带 answerHtml）")

        print("\n>>> app.js 的全部消费点均通过 <<<\n")

        check_llm_unavailable(args.port + 1, args.store)
        return 0
    finally:
        stop_server(proc)


if __name__ == "__main__":
    sys.exit(main())
