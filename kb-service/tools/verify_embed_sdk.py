"""嵌入 SDK 跨域浏览器验收（任务 3.9 / I-07）。

用**两个不同端口**模拟「第三方学校站点 → 能小智」的真实跨域：

1. SDK 自动初始化：``data-channel`` 一行代码生成浮窗，点击可问答；
2. 编程式 API：``init/open/close/setToken/destroy`` 与事件回调可用；
3. 身份透传：父页把 JWT 交给 SDK → iframe 内 ``/api/auth/me`` 已登录，
   ``setToken('')`` 立即回到访客；令牌不写 localStorage；
4. 跨域 CORS：第三方页面直接 ``fetch`` 健康检查、渠道配置与 ``/api/auth/me``
   （带 Authorization）均成功，问答请求的 ``Origin`` 为第三方站点；
5. 全程零 ``pageerror`` / ``console.error``（CORS 失败会在这里暴露）。

会创建并清理一个临时渠道；问答会留下会话日志（与其他 UI 验收一致）。

用法::

    python tools/verify_embed_sdk.py
    python tools/verify_embed_sdk.py --store mysql --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import string
import subprocess
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault("CORS_ALLOW_ORIGINS", "*")
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", r"C:\Users\wick\.workbuddy-ai\binaries\ms-playwright"
)

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


def _demo_html(base: str, channel: str, token: str) -> str:
    """模拟第三方校园站点：一行 SDK 自动初始化 + 编程式实例。"""
    code_js = json.dumps(channel)
    token_js = json.dumps(token)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>第三方站点 · SDK 验收</title></head>
<body>
  <h1 id="site-title">第三方校园站点</h1>
  <p>本页与能小智不同源（{html.escape(base)}）。</p>
  <script src="{html.escape(base)}/sdk/nxz.js"
          data-channel="{html.escape(channel)}"
          data-position="right"
          data-color="#22aa66"
          data-label="自动浮窗"></script>
  <script>
    window.__nxzEvents = [];
    window.__sdkState = null;
    window.addEventListener('load', function () {{
      window.__auto = window.NengXiaoZhi.get({code_js});
      window.__api = window.NengXiaoZhi.init({{
        channel: {code_js},
        instanceId: 'api',
        token: {token_js},
        label: '编程浮窗',
        position: 'left',
        color: '#8844dd',
        onReady: function () {{ window.__nxzEvents.push('ready'); }},
        onOpen: function () {{ window.__nxzEvents.push('open'); }},
        onState: function (state) {{
          window.__sdkState = state;
          window.__nxzEvents.push('state');
        }}
      }});
    }});
  </script>
</body>
</html>
"""


def _mint_admin_token() -> str:
    from app.security.auth import create_token

    return create_token("vf-sdk-admin", "管理员", extra={"mgr": True})


def _create_channel(base: str, token: str, code: str) -> tuple[int, str]:
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=20, trust_env=False, base_url=base, headers=headers) as cli:
        agents = cli.get("/api/admin/agents").json()["data"]["items"]
        aid = agents[0]["agentId"]
        resp = cli.post(
            "/api/admin/channels",
            json={
                "channelCode": code,
                "channelName": "SDK 跨域验收",
                "defaultAgentId": aid,
                "welcomeText": "SDK 验收欢迎语",
                "needAuth": 0,
                "status": 1,
                "themeConfig": {
                    "primaryColor": "#22aa66",
                    "position": "right",
                    "launcherText": "SDK验收",
                },
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"创建验收渠道失败：{resp.status_code} {resp.text[:200]}")
        return int(resp.json()["data"]["channelId"]), str(aid)


def _delete_channel(base: str, token: str, cid: int) -> None:
    import httpx

    try:
        with httpx.Client(timeout=10, trust_env=False, base_url=base) as cli:
            cli.delete(
                f"/api/admin/channels/{cid}",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:  # noqa: BLE001 - 清理失败不掩盖验收结果
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="嵌入 SDK 跨域浏览器验收（任务 3.9）")
    parser.add_argument("--port", type=int, default=8037, help="能小智服务端口")
    parser.add_argument("--static-port", type=int, default=8038, help="第三方站点端口")
    parser.add_argument("--store", default="json", choices=["json", "mysql"])
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store
    os.environ["AUTH_ENABLED"] = "true"
    os.environ["CORS_ALLOW_ORIGINS"] = "*"
    os.environ["RATE_LIMIT_ENABLED"] = "false"
    os.environ["LLM_MOCK"] = "1"

    from playwright.sync_api import sync_playwright

    from tools._server import read_log, spawn_server, stop_server, wait_port

    base = f"http://127.0.0.1:{args.port}"
    static_base = f"http://127.0.0.1:{args.static_port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)
    suffix = "".join(random.choices(string.ascii_lowercase, k=5))
    channel = f"vf-sdk-{suffix}"
    token = _mint_admin_token()

    print(f"启动服务 {base}（KB_STORE={args.store}, AUTH_ENABLED=true）…")
    env = dict(os.environ)
    proc, log_path = spawn_server(args.port, env, SERVICE_DIR)
    static_proc = None
    cid = None
    tmp = tempfile.TemporaryDirectory(prefix="nxz-sdk-")
    try:
        if not wait_port(args.port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        cid, aid = _create_channel(base, token, channel)
        print(f"验收渠道 {channel}（id={cid}, agentId={aid}）已创建")

        demo = Path(tmp.name) / "index.html"
        demo.write_text(_demo_html(base, channel, token), encoding="utf-8")
        static_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "http.server",
                str(args.static_port),
                "--bind",
                "127.0.0.1",
                "--directory",
                tmp.name,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not wait_port(args.static_port, timeout=20):
            print("第三方站点启动失败")
            return 1

        errors: list[str] = []
        chat_origins: list[str] = []
        cors_origins: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            page = browser.new_page(viewport={"width": 1360, "height": 900})
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    errors.append(f"console.error: {msg.text}")

            def origin_of(req) -> str:  # noqa: ANN001
                # request.headers 可能不包含 Origin，all_headers() 才完整
                try:
                    headers = req.all_headers()
                except Exception:  # noqa: BLE001 - 个别版本无此方法时降级
                    headers = req.headers
                for key, value in headers.items():
                    if key.lower() == "origin" and value:
                        return value
                return ""

            def on_request(req) -> None:  # noqa: ANN001
                if "/api/chat" in req.url:
                    origin = origin_of(req)
                    if origin:
                        chat_origins.append(origin)
                if any(p in req.url for p in ("/api/health", "/api/channels/", "/api/auth/me")):
                    origin = origin_of(req)
                    if origin:
                        cors_origins.append(origin)

            page.on("console", on_console)
            page.on("request", on_request)
            page.on("dialog", lambda d: d.accept())

            try:
                section("1. 自动初始化与跨域浮窗问答")
                page.goto(f"{static_base}/index.html", wait_until="networkidle")
                check("第三方页面已加载", page.locator("#site-title").count() == 1)
                launchers = page.locator("[data-nxz-sdk-launcher]")
                check("自动 + 编程实例各生成一个入口", launchers.count() == 2,
                      str(launchers.count()))
                check("自动入口文案来自 data-label",
                      launchers.first.inner_text().strip() == "自动浮窗",
                      launchers.first.inner_text())
                launchers.first.click()
                auto_panel = page.locator("#nxz-sdk-panel-default")
                check("点击后浮窗可见", auto_panel.is_visible())
                frame = page.frame_locator("#nxz-sdk-panel-default iframe")
                frame.locator("#input").wait_for(timeout=30000)
                frame_url = page.locator("#nxz-sdk-panel-default iframe").get_attribute("src") or ""
                check("iframe 携带 channel/embed/sdk",
                      f"channel={channel}" in frame_url
                      and "embed=1" in frame_url and "sdk=1" in frame_url, frame_url)

                frame.locator("#input").fill("校园卡怎么补办？")
                frame.locator("#send").click()
                # 流式过程先出现「正在检索…」占位气泡，AI 标识在终态才追加
                frame.locator(".msg.ai .ai-label").first.wait_for(timeout=90000)
                answer = frame.locator(".msg.ai:not(.thinking) .bubble").last.inner_text()
                check("浮窗内收到 AI 回答", len(answer.strip()) > 0, answer[:60])
                check("回答带 AI 生成标识",
                      frame.locator(".msg.ai .ai-label").count() > 0)
                check("iframe 内问答请求同源于应用（无二次跨域）",
                      bool(chat_origins) and all(o == base for o in chat_origins),
                      str(chat_origins[:2]))
                page.screenshot(path=str(shots / "sdk-cross-origin.png"), full_page=True)

                section("2. 编程式 API 与事件")
                page.evaluate("window.__auto.close()")
                check("close() 收起自动浮窗", not auto_panel.is_visible())
                page.evaluate("window.__api.open()")
                api_panel = page.locator("#nxz-sdk-panel-api")
                check("open() 打开编程浮窗", api_panel.is_visible())
                check("open 事件已回调",
                      page.evaluate("window.__nxzEvents.indexOf('open') >= 0"))
                page.evaluate("window.__api.close()")
                check("编程浮窗可关闭", not api_panel.is_visible())
                check("SDK 版本可读",
                      page.evaluate("window.NengXiaoZhi.version") == "1.0.0",
                      str(page.evaluate("window.NengXiaoZhi.version")))

                section("3. 身份透传（setToken 双向）")

                def sdk_wait(authenticated: bool, timeout: int = 30000) -> bool:
                    try:
                        page.wait_for_function(
                            "flag => window.__sdkState && window.__sdkState.authenticated === flag",
                            arg=authenticated,
                            timeout=timeout,
                        )
                        return True
                    except Exception:  # noqa: BLE001 - 失败交由 check 汇总
                        return False

                def sdk_diag() -> str:
                    try:
                        diag = page.evaluate(
                            "() => ({state: window.__sdkState, events: window.__nxzEvents,"
                            " src: (window.__api && window.__api.frame &&"
                            " window.__api.frame.getAttribute('src')) || null})"
                        )
                        return json.dumps(diag, ensure_ascii=False)
                    except Exception as exc:  # noqa: BLE001
                        return f"诊断失败: {exc}"

                wait_authed = sdk_wait(True)
                state = page.evaluate("window.__sdkState") or {}
                check("带 token 的 SDK 身份已登录", wait_authed, sdk_diag())
                check("iframe 身份为管理员", state.get("role") == "管理员", str(state.get("role")))
                check("ready 事件已回调",
                      page.evaluate("window.__nxzEvents.indexOf('ready') >= 0"))

                page.evaluate("window.__api.setToken('')")
                check("setToken('') 立即回到访客", sdk_wait(False), sdk_diag())

                page.evaluate(f"window.__api.setToken({json.dumps(token)})")
                check("重新 setToken 恢复登录", sdk_wait(True), sdk_diag())
                check("令牌不写入宿主页 localStorage",
                      page.evaluate("localStorage.getItem('nxz_token')") is None)

                section("4. 跨域 CORS")
                health = page.evaluate(
                    f"fetch({json.dumps(base + '/api/health')}).then(r => r.status)"
                )
                check("第三方页 fetch /api/health 成功", health == 200, str(health))
                channel_status = page.evaluate(
                    "fetch(" + json.dumps(base + "/api/channels/" + channel)
                    + ").then(r => r.status)"
                )
                check("第三方页 fetch 渠道配置成功", channel_status == 200, str(channel_status))
                me = page.evaluate(
                    "fetch(" + json.dumps(base + "/api/auth/me")
                    + ", {headers: {Authorization: 'Bearer ' + "
                    + json.dumps(token) + "}}).then(r => r.json())"
                )
                check("跨域 Authorization 头生效（/auth/me 已登录）",
                      me.get("data", {}).get("authenticated") is True,
                      str(me.get("data", {}).get("role")))
                check("宿主页跨域请求携带第三方 Origin",
                      static_base in cors_origins, str(sorted(set(cors_origins))[:3]))
                check("零 pageerror / console.error", not errors, "; ".join(errors[:3]))

                import httpx

                with httpx.Client(timeout=10, trust_env=False) as cli:
                    pf = cli.options(
                        f"{base}/api/chat",
                        headers={
                            "Origin": static_base,
                            "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "authorization,content-type",
                        },
                    )
                check("跨域预检允许 POST + Authorization",
                      pf.status_code in (200, 204)
                      and pf.headers.get("access-control-allow-origin") in ("*", static_base)
                      and (pf.headers.get("access-control-allow-headers", "") == "*"
                           or "authorization" in pf.headers.get(
                               "access-control-allow-headers", "").lower()),
                      f"{pf.status_code} {pf.headers.get('access-control-allow-origin')}")
            finally:
                browser.close()
    finally:
        if static_proc is not None:
            static_proc.terminate()
            try:
                static_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                static_proc.kill()
        if cid is not None:
            _delete_channel(base, token, cid)
            print(f"验收渠道 {channel} 已清理")
        stop_server(proc)
        tmp.cleanup()

    print(f"\n{'=' * 58}\nPASS {PASSED} / FAIL {FAILED}\n{'=' * 58}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
