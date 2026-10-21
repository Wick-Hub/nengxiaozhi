"""统一身份认证验收（任务 4.1）。

真实 HTTP 走完内置 mock CAS 的「登录 → ticket → 回调 → 交换码 → JWT」全链路，
并覆盖一次性交换码、开放重定向、ticket 复用与鉴权开关下的身份落地。

用法::

    python tools/verify_sso.py
    python tools/verify_sso.py --port 8040

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    parser = argparse.ArgumentParser(description="统一身份认证验收（任务 4.1）")
    parser.add_argument("--port", type=int, default=8040, help="验收用服务端口")
    args = parser.parse_args()

    import httpx

    from tools._server import read_log, spawn_server, stop_server, wait_port

    port = args.port
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "true"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=true）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        with httpx.Client(timeout=20, trust_env=False, base_url=base) as cli:
            section("1. 身份与 SSO 配置")
            me0 = cli.get("/api/auth/me").json()["data"]
            check("访客 /auth/me 可访问", me0["authenticated"] is False)
            check("SSO 已开启", me0["ssoEnabled"] is True)
            check("使用内置 mock CAS", me0["ssoProvider"] == "mock-cas", me0["ssoProvider"])
            check("鉴权开关已生效", me0["authEnabled"] is True)

            section("2. CAS 登录 → 回调 → 交换码")
            entry = cli.get("/api/auth/cas/login", params={"redirect": "/admin"},
                            follow_redirects=False)
            check("登录入口 302", entry.status_code == 302, str(entry.status_code))
            mock_url = entry.headers.get("location", "")
            check("跳到 mock CAS 登录", mock_url.startswith("/api/auth/cas/mock/login?"), mock_url[:60])
            service = parse_qs(urlsplit(mock_url).query).get("service", [""])[0]
            check("service 指向本站回调", "/api/auth/cas/callback" in service, service[:80])

            issued = cli.get(f"{mock_url}&userNo=20230001", follow_redirects=False)
            check("mock CAS 签发 ticket", issued.status_code == 302, str(issued.status_code))
            callback_url = issued.headers.get("location", "")
            check("携带 ST- ticket", "ticket=ST-" in callback_url, callback_url[:100])

            callback = cli.get(callback_url, follow_redirects=False)
            location = callback.headers.get("location", "")
            check("回调 302 回站内", callback.status_code == 302 and location.startswith("/admin#cas="),
                  location[:60])
            code = location.split("#cas=", 1)[1] if "#cas=" in location else ""
            check("下发一次性交换码", len(code) > 40)

            exchanged = cli.post("/api/auth/cas/exchange", json={"code": code})
            data = exchanged.json().get("data") or {}
            check("交换码换正式 token", exchanged.status_code == 200 and bool(data.get("token")),
                  str(exchanged.status_code))
            check("身份带出学工号", data.get("userNo") == "20230001", data.get("userNo", ""))
            check("身份带出角色", data.get("role") == "本科生", data.get("role", ""))
            check("访客默认无管理权限", data.get("isAdmin") is False)

            token = data.get("token", "")
            authed_me = cli.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["data"]
            check("token 可用于 /auth/me", authed_me["authenticated"] is True
                  and authed_me["userNo"] == "20230001")

            replay = cli.post("/api/auth/cas/exchange", json={"code": code})
            check("交换码不可重放", replay.status_code == 401, str(replay.status_code))

            section("3. 鉴权与异常路径")
            no_token = cli.get("/api/admin/channels")
            check("无 token 访问管理接口 401", no_token.status_code == 401, str(no_token.status_code))
            with_token = cli.get("/api/admin/channels", headers={"Authorization": f"Bearer {token}"})
            check("普通角色访问管理接口 403", with_token.status_code == 403, str(with_token.status_code))

            bad = cli.get("/api/auth/cas/callback", params={"ticket": "ST-bogus", "redirect": "/admin"},
                          follow_redirects=False)
            check("非法 ticket 回跳带错误", "#cas_error=" in bad.headers.get("location", ""),
                  bad.headers.get("location", "")[:60])

            escaped = cli.get("/api/auth/cas/login", params={"redirect": "//evil.com/x"},
                              follow_redirects=False)
            svc = parse_qs(urlsplit(escaped.headers.get("location", "")).query).get("service", [""])[0]
            target = parse_qs(urlsplit(svc).query).get("redirect", [""])[0]
            check("开放重定向被收敛为站内", target == "/", target)

            external = cli.get("/api/auth/cas/mock/login",
                               params={"service": "http://evil.com/steal", "userNo": "20230001"},
                               follow_redirects=False)
            check("mock CAS 拒绝外部 service", external.status_code == 400, str(external.status_code))

            teacher = cli.get("/api/auth/cas/login", params={"redirect": "/"}, follow_redirects=False)
            teacher_mock = teacher.headers.get("location", "")
            t_issued = cli.get(f"{teacher_mock}&userNo=20990001&role={quote('teacher')}",
                               follow_redirects=False)
            t_cb = cli.get(t_issued.headers.get("location", ""), follow_redirects=False)
            t_code = t_cb.headers.get("location", "").split("#cas=", 1)[-1]
            t_data = cli.post("/api/auth/cas/exchange", json={"code": t_code}).json()["data"]
            check("上游角色编码映射为本地角色", t_data.get("role") == "教职工", t_data.get("role", ""))

        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())
