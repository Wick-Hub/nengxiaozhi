"""角色权限前端验收（任务 2.9 / M-SYS-01·03）。

真实 Chromium + ``AUTH_ENABLED=true`` + MySQL，覆盖：

1. **登录**：未登录弹出登录框；已设密码账号必须密码登录；
2. **菜单可见性**：受限角色只看到有权限的页签（docs.view / points.view）；
3. **按钮可用性**：上传、审核、删除、导出等按钮按权限点隐藏；
4. **越权 403**：浏览器内直调无权限接口返回 403，并写入 ACCESS_DENIED 日志；
5. **角色矩阵 UI**：新建角色（勾选权限点）→ 卡片出现 → 删除；
6. **管理员 UI**：账号清单可见；系统管理页显示词库/日志/角色/账号四块；
7. **零 JS 错误**：全程无 ``pageerror`` / ``console.error``。

用法::

    python tools/verify_rbac_ui.py
    python tools/verify_rbac_ui.py --port 8039 --headful

前置：MySQL 已启动，已执行 ``tools/migrate_rbac_2_9.py`` 与 ``tools/seed_auth.py``。
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "mysql")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", r"C:\Users\wick\.workbuddy-ai\binaries\ms-playwright"
)

try:  # pragma: no cover
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


def _login_ui(page, user_no: str, password: str = "") -> None:
    page.wait_for_selector(".nxz-auth-box", state="visible", timeout=20000)
    page.fill("#nxz-auth-user", user_no)
    if password:
        page.fill("#nxz-auth-pass", password)
    page.click('.nxz-auth-box button[type="submit"]')
    page.wait_for_function(
        "(u) => (document.querySelector('#nxz-auth-badge')?.textContent || '').includes(u)",
        arg=user_no,
        timeout=25000,
    )
    page.wait_for_load_state("networkidle")


def main() -> int:
    parser = argparse.ArgumentParser(description="角色权限前端验收（任务 2.9）")
    parser.add_argument("--port", type=int, default=8039, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["KB_STORE"] = "mysql"
    env["AUTH_ENABLED"] = "true"
    env["LLM_MOCK"] = "1"
    env["RATE_LIMIT_ENABLED"] = "false"

    print(f"启动服务 127.0.0.1:{port}（AUTH_ENABLED=true）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1
        return _run(base, shots, headful=args.headful)
    finally:
        stop_server(proc)


def _run(base: str, shots: Path, *, headful: bool) -> int:
    import httpx
    from playwright.sync_api import sync_playwright

    suffix = "".join(random.choices(string.ascii_lowercase, k=5))
    role_code = f"ui_{suffix}"
    role_name = f"UI验收角色{suffix}"
    ui_role_code = f"uirole_{suffix}"
    ui_role_name = f"UI矩阵角色{suffix}"
    user_no = f"U{suffix}".upper()
    password = "Verify#2026"

    with httpx.Client(base_url=base, timeout=15, trust_env=False) as cli:
        login = cli.post("/api/auth/login", json={"userNo": "admin"})
        if login.status_code != 200:
            print(f"管理员登录失败：{login.status_code} {login.text[:200]}")
            return 1
        admin_token = login.json()["data"]["token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        role = cli.post(
            "/api/admin/roles",
            headers=admin_headers,
            json={
                "roleCode": role_code,
                "roleName": role_name,
                "remark": "UI 验收临时角色",
                "permissions": ["docs.view", "points.view"],
            },
        )
        if role.status_code != 200:
            print(f"创建角色失败：{role.status_code} {role.text[:300]}")
            cli.delete(f"/api/admin/roles/{role.json()['data']['roleId']}", headers=admin_headers)
            return 1
        role_id = role.json()["data"]["roleId"]

        account = cli.post(
            "/api/admin/admins",
            headers=admin_headers,
            json={
                "userNo": user_no,
                "realName": f"UI验收账号{suffix}",
                "password": password,
                "roleIds": [role_id],
                "status": 1,
            },
        )
        if account.status_code != 200:
            print(f"创建管理员失败：{account.status_code} {account.text[:300]}")
            cli.delete(f"/api/admin/roles/{role_id}", headers=admin_headers)
            return 1
        user_id = account.json()["data"]["userId"]

    js_errors: list[str] = []
    # 第 2 步故意用无权限 token 调接口，Chromium 会把 403 响应记成
    # console.error；只在那一小段白名单它，其他错误照旧严格校验。
    expect_403 = {"on": False}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not headful)
            page = browser.new_page(viewport={"width": 1600, "height": 1000})

            page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type != "error":
                    return
                text = msg.text
                if expect_403["on"] and "status of 403" in text:
                    return
                js_errors.append(f"console.error: {text}")

            page.on("console", on_console)
            page.on("dialog", lambda d: d.accept())

            try:
                section("1. 受限角色登录与菜单可见性")
                page.goto(f"{base}/admin", wait_until="domcontentloaded")
                page.wait_for_selector(".nxz-auth-box", state="visible", timeout=20000)
                check("未登录访问 /admin 弹出登录框", True)
                check("登录框标注管理员密码说明",
                      "管理员" in page.locator(".nxz-auth-hint").inner_text())

                _login_ui(page, user_no, password)
                check("登录后顶栏显示学工号与角色",
                      user_no in page.locator("#nxz-auth-badge").inner_text(),
                      page.locator("#nxz-auth-badge").inner_text())
                page.wait_for_function(
                    "() => getComputedStyle(document.querySelector('#tabs .tab[data-tab=\"agents\"]')).display === 'none'",
                    timeout=15000,
                )
                check("受限角色只看到 1 个页签", page.locator("#tabs .tab:visible").count() == 1,
                      str(page.locator("#tabs .tab:visible").count()))
                check("系统管理页签不可见",
                      not page.locator('#tabs .tab[data-tab="system"]').is_visible())
                check("上传卡片按 docs.upload 隐藏", not page.locator("#upload").is_visible())

                page.wait_for_function(
                    "() => document.querySelectorAll('#tbody tr').length > 0 || !document.querySelector('#empty').hidden",
                    timeout=20000,
                )
                rows = page.locator("#tbody tr").count()
                check("文档列表按 docs.view 正常加载", rows > 0, f"rows={rows}")
                if rows:
                    first = page.locator("#tbody tr").first
                    check("预览按钮可见（docs.view）",
                          first.locator('a.op-btn:has-text("预览")').is_visible())
                    check("发布按钮隐藏（docs.audit）",
                          page.locator('#tbody [data-op="publish"]:visible').count() == 0)
                    check("删除按钮隐藏（docs.delete）",
                          page.locator('#tbody [data-op="delete"]:visible').count() == 0)

                section("2. 越权接口 403 与留痕")
                expect_403["on"] = True
                try:
                    status = page.evaluate(
                        """async () => {
                            const t = localStorage.getItem('nxz_token') || '';
                            const r = await fetch('/api/admin/operation-logs',
                                { headers: { Authorization: 'Bearer ' + t } });
                            return r.status;
                        }"""
                    )
                    page.wait_for_timeout(300)  # 等 console 事件派发完成再关白名单
                finally:
                    expect_403["on"] = False
                check("无 system.logs 直调接口 403", status == 403, str(status))

                section("3. 知识点页按钮权限")
                page.goto(f"{base}/points", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => document.querySelectorAll('#pt-body tr').length > 0 || !document.querySelector('#pt-empty').hidden",
                    timeout=20000,
                )
                check("/points 可打开（points.view）", page.locator(".topbar").is_visible())
                check("新建知识点隐藏（points.edit）", not page.locator("#pt-new").is_visible())
                check("导出按钮隐藏（points.export）", not page.locator("#pt-export-csv").is_visible())
                if page.locator("#pt-body tr").count():
                    check("行内编辑按钮隐藏（points.edit）",
                          page.locator("#pt-body [data-edit]:visible").count() == 0)

                section("4. 管理员登录与系统管理")
                page.goto(f"{base}/admin", wait_until="domcontentloaded")
                page.wait_for_function(
                    "(u) => (document.querySelector('#nxz-auth-badge')?.textContent || '').includes(u)",
                    arg=user_no,
                    timeout=20000,
                )
                page.click('#nxz-auth-badge button:has-text("退出")')
                page.wait_for_selector(".nxz-auth-box", state="visible", timeout=20000)
                _login_ui(page, "admin")
                check("管理员看到全部 5 个页签", page.locator("#tabs .tab:visible").count() == 5,
                      str(page.locator("#tabs .tab:visible").count()))

                page.click('#tabs .tab[data-tab="system"]')
                page.wait_for_function(
                    "() => (document.querySelector('#rbac-note')?.textContent || '').trim().length > 0",
                    timeout=20000,
                )
                check("系统管理页四块卡片均可见",
                      page.locator("#sw-card").is_visible()
                      and page.locator("#op-card").is_visible()
                      and page.locator("#rbac-card").is_visible()
                      and page.locator("#rbac-admin-card").is_visible())
                check("角色矩阵显示内置角色",
                      page.locator('#rbac-roles .rbac-card:has-text("部门联络员")').count() == 1)
                check("角色矩阵显示本次测试角色",
                      page.locator(f'#rbac-roles .rbac-card:has-text("{role_name}")').count() == 1)
                check("管理员清单显示本次测试账号",
                      page.locator(f'#rbac-admin-tbody tr:has-text("{user_no}")').count() == 1)

                section("5. 角色矩阵 UI 增删")
                page.click("#rbac-role-new")
                page.wait_for_selector("#rbac-role-modal", state="visible")
                page.fill("#rbac-role-code", ui_role_code)
                page.fill("#rbac-role-name", ui_role_name)
                page.locator('#rbac-role-perms input[value="docs.view"]').check()
                page.locator('#rbac-role-perms input[value="chatlogs.view"]').check()
                page.click("#rbac-role-save")
                page.wait_for_selector(
                    f'#rbac-roles .rbac-card:has-text("{ui_role_name}")', timeout=20000
                )
                ui_card = page.locator(f'#rbac-roles .rbac-card:has-text("{ui_role_name}")')
                check("新建角色后卡片出现", ui_card.count() == 1)
                check("卡片显示所勾选权限点",
                      ui_card.locator(".rbac-chip").filter(has_text="docs.view").count() == 1)
                ui_card.locator("button[data-role-del]").click()
                page.wait_for_selector(
                    f'#rbac-roles .rbac-card:has-text("{ui_role_name}")',
                    state="detached",
                    timeout=20000,
                )
                check("删除角色后卡片消失",
                      page.locator(f'#rbac-roles .rbac-card:has-text("{ui_role_name}")').count() == 0)

                section("6. 越权日志可查询")
                page.select_option("#op-action", "ACCESS_DENIED")
                page.click("#op-search")
                page.wait_for_selector(
                    '#op-tbody tr:has-text("/api/admin/operation-logs")', timeout=20000
                )
                denied_row = page.locator('#op-tbody tr:has-text("/api/admin/operation-logs")').first
                check("操作日志可筛出越权拦截", denied_row.count() == 1,
                      denied_row.inner_text().replace("\t", " ")[:120])

                page.screenshot(path=str(shots / "rbac-admin.png"), full_page=True)
                page.click('#tabs .tab[data-tab="docs"]')
                page.screenshot(path=str(shots / "rbac-admin-docs.png"))

                section("7. 零 JS 运行时错误")
                check("全程无 pageerror / console.error", not js_errors,
                      " | ".join(js_errors[:3]) if js_errors else "clean")
            finally:
                browser.close()
    finally:
        # 清理：测试账号与角色（UI 新建的角色一般已在页面删除，这里兜底）
        with httpx.Client(base_url=base, timeout=15, trust_env=False) as cli:
            headers = {"Authorization": f"Bearer {admin_token}"}
            cli.delete(f"/api/admin/admins/{user_id}", headers=headers)
            cli.delete(f"/api/admin/roles/{role_id}", headers=headers)
            roles = cli.get("/api/admin/roles", headers=headers)
            if roles.status_code == 200:
                for item in roles.json()["data"]["items"]:
                    if item["roleCode"] == ui_role_code:
                        cli.delete(f"/api/admin/roles/{item['roleId']}", headers=headers)

    print(f"\n截图目录：{shots}")
    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
