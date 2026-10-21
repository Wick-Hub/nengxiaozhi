"""API 冒烟测试：启动服务并逐个探测接口。

用法（在 kb-service 目录下）::

    python tools/smoke_api.py            # 默认端口 8013，默认 LLM_MOCK=1
    python tools/smoke_api.py --no-mock  # 使用 .env 中的真实 LLM 配置
    python tools/smoke_api.py --keep     # 探测后保持运行，便于手工调试

脚本会自动启动 uvicorn、等待端口就绪、逐个请求接口、最后关闭服务。
退出码：0 表示全部探测完成（不校验业务语义），1 表示启动失败。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR / "tools"))

from _server import read_log, spawn_server, stop_server, wait_port  # noqa: E402


def probe(client: httpx.Client, with_sync: bool = False) -> None:
    print("=== 接口实测 ===")

    r = client.get("/api/health")
    print(f"GET  /api/health          -> {r.status_code} {r.json()}")

    r = client.get("/api/meta")
    meta = r.json().get("data", {})
    print(f"GET  /api/meta            -> {r.status_code} 部门 {len(meta.get('depts', []))} 个 / 类型 {len(meta.get('ktypes', []))} 个")

    r = client.get("/")
    print(f"GET  /                    -> {r.status_code} ({len(r.text)} bytes)")

    # ---- 认证（任务 1.13）----
    r = client.get("/api/auth/me")
    me = r.json().get("data", {})
    print(
        f"GET  /api/auth/me         -> {r.status_code} role={me.get('role')} "
        f"authenticated={me.get('authenticated')} authEnabled={me.get('authEnabled')}"
    )

    r = client.post("/api/auth/login", json={"userNo": "admin"})
    if r.status_code == 200:
        tok = r.json()["data"]
        print(
            f"POST /api/auth/login      -> {r.status_code} userNo={tok.get('userNo')} "
            f"role={tok.get('role')} isAdmin={tok.get('isAdmin')} expiresIn={tok.get('expiresIn')}s"
        )
        rr = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok['token']}"})
        print(f"GET  /api/auth/me (token) -> {rr.status_code} role={rr.json()['data'].get('role')}")
    else:
        # 登录要读 sys_user，必须连 MySQL；JSON 模式下会 500 —— 这是预期降级，不算冒烟失败
        hint = "（登录依赖 MySQL，当前不可用；如需验证请启动数据库后加 --store mysql）"
        print(f"POST /api/auth/login      -> {r.status_code} {hint}")

    for path in ("/app.js", "/style.css", "/admin", "/admin.js", "/admin.css",
                 "/preview", "/preview.js", "/preview.css"):
        rr = client.get(path)
        print(f"GET  {path:<21} -> {rr.status_code} ({len(rr.content)} bytes)")

    r = client.get("/api/docs")
    docs = r.json().get("data") or []
    print(f"GET  /api/docs            -> {r.status_code} {len(docs)} 篇文档")
    if docs:
        d0 = docs[0]
        print(f"     首条: {d0.get('docId')} {d0.get('title')} | {d0.get('dept')}/{d0.get('ktype')} | status={d0.get('status')}({d0.get('statusName')}) | 切片 {d0.get('chunkCount')}")

        # ---- 预览与对比（任务 1.11）----
        did = d0.get("docId")
        pv = client.get(f"/api/docs/{did}/preview")
        pv_data = pv.json().get("data") or {}
        print(
            f"GET  /api/docs/{did}/preview -> {pv.status_code} 切片 {pv_data.get('total')} 条 | "
            f"原文 {pv_data.get('textMeta', {}).get('chars')} 字 | 原件 {pv_data.get('original', {}).get('fileName')}"
        )
        ck = client.get(f"/api/docs/{did}/chunks")
        n = len(ck.json().get("data") or [])
        print(f"GET  /api/docs/{did}/chunks  -> {ck.status_code} {n} 条（列表，向后兼容）")
        hits = client.get(f"/api/docs/{did}/chunks", params={"q": "校园网"})
        print(f"GET  /api/docs/{did}/chunks?q=校园网 -> {hits.status_code} 命中 {len(hits.json().get('data') or [])} 条")
        txt = client.get(f"/api/docs/{did}/text")
        print(f"GET  /api/docs/{did}/text    -> {txt.status_code} {txt.json().get('data', {}).get('chars')} 字")
        raw = client.get(f"/api/docs/{did}/raw")
        print(
            f"GET  /api/docs/{did}/raw     -> {raw.status_code} {len(raw.content)} bytes | "
            f"{raw.headers.get('content-type')} | {raw.headers.get('content-disposition', '')[:48]}"
        )

        # ---- 原图（任务 1.10）----
        imgs = client.get(f"/api/docs/{did}/images")
        img_data = imgs.json().get("data") or {}
        img_list = img_data.get("images") or []
        print(
            f"GET  /api/docs/{did}/images  -> {imgs.status_code} {img_data.get('total', 0)} 张"
            f"（图片型文档={img_data.get('isImageDoc')}）"
        )
        if img_list:
            key = img_list[0]["key"]
            one = client.get(f"/api/docs/{did}/image", params={"key": key})
            print(
                f"GET  /api/docs/{did}/image   -> {one.status_code} {len(one.content)} bytes | "
                f"{one.headers.get('content-type')} | {one.headers.get('cache-control', '')}"
            )
        # 越权 key 必须被拒（不能用任意路径把服务器文件当图片读出来）
        bad = client.get(f"/api/docs/{did}/image", params={"key": "uploads/../etc/passwd"})
        print(f"GET  /api/docs/{did}/image?key=<穿越> -> {bad.status_code}（期望 400/404）")

    # ---- 问答（真实链路）----
    payload = {"question": "一卡通丢失了怎么办", "role": "本科生"}
    r = client.post("/api/chat", json=payload)
    body = r.json()
    print(f"POST /api/chat            -> {r.status_code} ok={body.get('ok')}")
    if body.get("ok") is False:
        # LLM 未配置等可预期问题：应 503 + ok=false + error，而非 500
        print(f"     error      : {body.get('error')}")
    data = body.get("data") or {}
    print(f"     answer     : {str(data.get('answer'))[:80]}")
    print(f"     refused    : {data.get('refused')} | confidence: {data.get('confidence')}")
    print(f"     citations  : {[c.get('n') for c in data.get('citations') or []]}")
    print(f"     sessionId  : {data.get('sessionId')} | msgId: {data.get('msgId')}")

    r = client.post("/api/chat/stream", json=payload)
    text = r.text
    deltas = text.count('"type": "delta"')
    has_error = '"type": "error"' in text
    note = " | error 事件" if has_error else ""
    print(f"POST /api/chat/stream     -> {r.status_code} | delta 事件 {deltas} 个 | 含 done: {'\"type\": \"done\"' in text}{note}")
    if has_error:
        for line in text.splitlines():
            if '"type": "error"' in line:
                print(f"     {line.strip()[:140]}")
                break

    # ---- 多轮：用上一个会话继续追问 ----
    sid = data.get("sessionId")
    if sid:
        client.post("/api/chat", json={"question": "需要带什么材料", "role": "本科生", "sessionId": sid})
        r3 = client.get(f"/api/sessions/{sid}/messages")
        msgs = r3.json().get("data") or []
        print(f"GET  /api/sessions/{{id}}/messages -> {r3.status_code} 会话内 {len(msgs)} 条消息（多轮）")

    # ---- 权限：不传角色应只看到公共切片 ----
    r = client.post("/api/chat", json={"question": "教职工内部上网资费"})
    d = r.json().get("data") or {}
    print(f"POST /api/chat (无角色)    -> {r.status_code} citations {[c.get('n') for c in d.get('citations') or []]}")

    # ---- 增量同步（会写库：默认只探测，加 --with-sync 才真正执行）----
    if with_sync:
        for method, path in [("post", "/api/sync")]:
            try:
                rr = getattr(client, method)(path)
                ctype = rr.headers.get("content-type", "")
                out = str(rr.json())[:120] if "json" in ctype else rr.text[:120]
                print(f"{method.upper():4} {path:<24} -> {rr.status_code} {out}")
            except Exception as exc:  # noqa: BLE001 - 冒烟需报告任何异常
                print(f"{method.upper():4} {path:<24} -> ERR {type(exc).__name__}: {str(exc)[:120]}")
    else:
        print("POST /api/sync                -> 已跳过（会写库；需要时加 --with-sync）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="kb-service API 冒烟测试")
    ap.add_argument("--port", type=int, default=8013)
    ap.add_argument("--keep", action="store_true", help="探测后保持服务运行（便于手工调试）")
    ap.add_argument("--no-mock", action="store_true", help="使用 .env 真实 LLM 配置（默认置 LLM_MOCK=1）")
    ap.add_argument(
        "--store",
        default="json",
        choices=["json", "mysql"],
        help="子进程存储模式（默认 json，可在未启动 MySQL 时跑通）；验证 MySQL 全链路时传 mysql",
    )
    ap.add_argument(
        "--with-sync",
        action="store_true",
        help="额外调用 POST /api/sync（会真实写库：把知识目录里的新文件补进库）",
    )
    args = ap.parse_args(argv)

    env = os.environ.copy()
    if not args.no_mock:
        env["LLM_MOCK"] = "1"
    # 默认走 JSON：MySQL 未启动时 /api/health 会阻塞数十秒并触发读超时，
    # 而例行冒烟不该依赖数据库。需要验证 MySQL 全链路时显式 --store mysql。
    env["KB_STORE"] = args.store

    proc, log_path = spawn_server(args.port, env, SERVICE_DIR)
    try:
        if not wait_port(args.port):
            print("服务启动超时，输出如下：")
            stop_server(proc)
            print(read_log(log_path, 2000))
            return 1
        print(f"服务已就绪：http://127.0.0.1:{args.port}\n")
        # trust_env=False：只探测本地服务，忽略 HTTP(S)_PROXY 等环境变量
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=30, trust_env=False) as client:
            probe(client, with_sync=args.with_sync)
        if args.keep:
            print("\n服务保持运行（Ctrl+C 结束）…")
            proc.wait()
        return 0
    finally:
        if not args.keep:
            stop_server(proc)
            print("\n已停止服务")


if __name__ == "__main__":
    sys.exit(main())
