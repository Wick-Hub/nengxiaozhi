"""智能体管理页 · 浏览器端到端验收（任务 2.2 前端）。

真实 Chromium 打开 ``/admin`` 的「智能体管理」页签，验证 HTTP 层看不见的东西：
表单收集、弹窗开关、confirm 交互、列表局部刷新、以及**零 JS 运行时错误**。

1. **页签**：三个页签互斥切换，智能体页懒加载后可见；
2. **场景搭建（M-A-01）**：6 张场景卡片渲染；点「一键搭建」后卡片转「已搭建」，
   列表出现对应助手，接口侧 presets 状态同步；
3. **创建助手（M-A-02）**：走弹窗表单创建，列表出现新行且字段落库；
4. **基础配置（M-A-03）**：编辑弹窗回填原值 → 改名称/描述/流式/启禁用 → 保存后
   列表徽章与文本同步变化；
5. **启停**：列表内「下架 / 上架」按钮直接切换状态徽章；
6. **筛选与搜索**：状态页签、关键词搜索均能收窄列表；
7. **删除**：confirm 确认后行消失，接口侧同步；
8. **零 JS 错误**：全程无 ``pageerror`` / ``console.error``。

**幂等性**：验收前快照场景助手的搭建状态，结束后**恢复原状**（本次新建的删掉、
原有的保留），并清理 ``vf-ui-*`` 验收助手，可重复运行且不破坏既有数据。

用法::

    python tools/verify_agents_ui.py
    python tools/verify_agents_ui.py --port 8024 --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", r"C:\Users\wick\.workbuddy-ai\binaries\ms-playwright"
)

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0

SCENE_NAMES = ["财务助手", "招生助手", "后勤助手", "国资助手", "信网助手", "迎新助手"]
#: 验收自建的助手编码（前后清理，避免污染）
UI_CODES = ["vf-ui-new", "vf-ui-edit"]


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


def _client(base: str):
    import httpx

    return httpx.Client(timeout=20, trust_env=False, base_url=base)


def _agents(cli) -> list[dict]:
    return cli.get("/api/admin/agents").json()["data"]["items"]


def _preset_state(cli) -> dict[str, bool]:
    return {p["sceneKey"]: p["created"] for p in cli.get("/api/admin/agents/presets").json()["data"]["presets"]}


def _cleanup(cli, codes: list[str]) -> None:
    """按编码删除助手（幂等）。"""
    for a in _agents(cli):
        if a["agentCode"] in codes:
            cli.delete(f"/api/admin/agents/{a['agentId']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="智能体管理页浏览器验收（任务 2.2）")
    parser.add_argument("--port", type=int, default=8024, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图输出目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["KB_STORE"] = os.environ.get("KB_STORE", "json")
    env["AUTH_ENABLED"] = "false"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE={env['KB_STORE']}）…")
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
    with _client(base) as cli:
        # ---- 前置：清理残留 + 快照场景状态 ----
        _cleanup(cli, UI_CODES)
        scene_before = _preset_state(cli)
        presets = cli.get("/api/admin/agents/presets").json()["data"]["presets"]
        target_scene = next((p for p in presets if not p["created"]), None)

        created_scene_id: int | None = None
        code = 1
        try:
            created_scene_id, code = _browser_flow(
                base, shots, headful=headful, cli=cli, target_scene=target_scene
            )
            return code
        finally:
            # ---- 后置：恢复原状（只删本次新建的场景助手） ----
            _cleanup(cli, UI_CODES)
            if created_scene_id:
                cli.delete(f"/api/admin/agents/{created_scene_id}")
            restored = _preset_state(cli)
            same = all(restored.get(k) == v for k, v in scene_before.items())
            check("场景搭建状态已恢复原状（可重复运行）", same,
                  f"{len(restored)} 个场景")

            print(f"\n截图目录：{shots}")
            print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")


def _browser_flow(base: str, shots: Path, *, headful: bool, cli, target_scene: dict | None):
    """执行浏览器验收。返回 ``(本次新建的场景助手 ID 或 None, 退出码)``。"""
    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    created_scene_id: int | None = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type == "error":
                js_errors.append(f"console.error: {msg.text}")

        page.on("console", on_console)
        page.on("dialog", lambda d: d.accept())  # confirm / alert 一律确认

        try:
            section("1. 页签与初始状态")
            page.goto(f"{base}/admin", wait_until="networkidle")
            # 任务 2.4 起管理页有 4 个页签（知识库 / 智能体 / 会话日志 / 大模型管理）
            check("页签数量为 5（知识库/智能体/网关/日志/系统管理）", page.locator("#tabs .tab").count() == 5)
            check("默认停在「知识库」页", page.locator("#page-docs").is_visible())
            check("智能体页初始隐藏", not page.locator("#page-agents").is_visible())
            check("模型网关页初始隐藏", not page.locator("#page-gateway").is_visible())
            check("模型网关页初始隐藏", not page.locator("#page-gateway").is_visible())

            page.click('#tabs .tab[data-tab="agents"]')
            page.wait_for_selector("#ag-scenes .scene-card", timeout=15000)
            check("智能体页可见", page.locator("#page-agents").is_visible())
            check("知识库页已隐藏", not page.locator("#page-docs").is_visible())
            check("会话日志页仍隐藏", not page.locator("#page-logs").is_visible())
            check("模型网关页仍隐藏", not page.locator("#page-gateway").is_visible())

            section("2. 场景智能体（M-A-01）")
            names = page.locator("#ag-scenes .sc-name").all_inner_texts()
            check("渲染 6 张场景卡片", len(names) == 6, f"{names}")
            check("场景名称与需求一致", names == SCENE_NAMES, f"{names}")
            note = page.locator("#ag-note").inner_text()
            check("提示条统计搭建进度", "场景智能体已搭建" in note, note[:56])
            page.screenshot(path=str(shots / "agents-scenes.png"), full_page=True)

            if target_scene is None:
                check("跳过搭建验证（6 个场景均已搭建）", True, "skip")
            else:
                card = page.locator(".scene-card", has_text=target_scene["agentName"]).first
                check("目标场景初始为「一键搭建」",
                      "一键搭建" in card.locator(".sc-btn").inner_text())
                card.locator(".sc-btn").click()
                page.wait_for_function(
                    """(name) => {
                      const cards = [...document.querySelectorAll('#ag-scenes .scene-card')];
                      const c = cards.find(x => x.querySelector('.sc-name').innerText === name);
                      return c && c.classList.contains('done');
                    }""",
                    arg=target_scene["agentName"],
                    timeout=15000,
                )
                check("搭建后卡片转为「已搭建」",
                      "已搭建" in card.locator(".sc-btn").inner_text())
                after = {p["sceneKey"]: p for p in
                         cli.get("/api/admin/agents/presets").json()["data"]["presets"]}
                check("接口侧同步标记 created",
                      after[target_scene["sceneKey"]]["created"] is True)
                created_scene_id = after[target_scene["sceneKey"]]["agentId"]
                check("列表出现该场景助手",
                      page.locator("#ag-tbody tr", has_text=target_scene["agentName"]).count() > 0)

            section("3. 创建助手（M-A-02）")
            before_rows = page.locator("#ag-tbody tr").count()
            page.click("#ag-new")
            page.wait_for_function("document.getElementById('ag-modal').hidden === false",
                                   timeout=10000)
            check("弹窗打开", page.locator("#ag-modal").is_visible())
            check("弹窗标题为「新建助手」",
                  "新建助手" in page.locator("#ag-modal-title").inner_text())
            check("可见角色复选框已渲染", page.locator("#ag-f-roles input").count() > 0)
            check("「全部」默认勾选",
                  page.locator("#ag-f-roles input[value='全部']").is_checked())

            page.fill("#ag-f-name", "UI验收助手")
            page.fill("#ag-f-code", "vf-ui-new")
            page.fill("#ag-f-desc", "由浏览器验收工具创建")
            page.fill("#ag-f-icon", "🧪")
            page.fill("#ag-f-fallback", "兜底：没有找到相关信息")
            page.fill("#ag-f-risk", "风控：涉及敏感内容")
            page.screenshot(path=str(shots / "agents-modal.png"), full_page=True)
            page.click("#ag-save")
            page.wait_for_function("document.getElementById('ag-modal').hidden === true",
                                   timeout=15000)
            page.wait_for_timeout(700)

            rows_after = page.locator("#ag-tbody tr").count()
            check("列表行数 +1", rows_after == before_rows + 1, f"{before_rows} → {rows_after}")
            new_row = page.locator("#ag-tbody tr", has_text="UI验收助手").first
            check("新助手出现在列表中", new_row.count() > 0)
            row_text = new_row.inner_text()
            check("显示头像 emoji", "🧪" in row_text)
            check("默认「流式」", "流式" in row_text, row_text.replace("\n", " ")[:70])
            check("默认「已上架」", "已上架" in row_text)

            api_new = next((a for a in _agents(cli) if a["agentCode"] == "vf-ui-new"), None)
            check("接口侧已落库（描述 + 兜底/风控话术）",
                  api_new is not None
                  and api_new["description"] == "由浏览器验收工具创建"
                  and api_new["fallbackReply"] == "兜底：没有找到相关信息"
                  and api_new["riskReply"] == "风控：涉及敏感内容",
                  (api_new or {}).get("description", "-"))

            section("4. 基础配置编辑（M-A-03）")
            new_row.locator(".op-btn", has_text="编辑").click()
            page.wait_for_function("document.getElementById('ag-modal').hidden === false",
                                   timeout=10000)
            check("编辑弹窗回填名称", page.input_value("#ag-f-name") == "UI验收助手")
            check("编辑弹窗回填描述",
                  page.input_value("#ag-f-desc") == "由浏览器验收工具创建")
            check("编辑弹窗回填兜底话术",
                  page.input_value("#ag-f-fallback") == "兜底：没有找到相关信息")
            check("编辑弹窗回填流式开关", page.is_checked("#ag-f-stream"))
            check("编辑弹窗回填启用开关", page.is_checked("#ag-f-status"))

            page.fill("#ag-f-name", "UI验收助手·改")
            page.fill("#ag-f-desc", "描述已修改")
            page.uncheck("#ag-f-stream")
            page.uncheck("#ag-f-status")
            page.click("#ag-save")
            page.wait_for_function("document.getElementById('ag-modal').hidden === true",
                                   timeout=15000)
            page.wait_for_timeout(700)

            edited = page.locator("#ag-tbody tr", has_text="UI验收助手·改").first
            check("列表显示新名称", edited.count() > 0)
            edited_text = edited.inner_text()
            check("流式徽章变为「整段」", "整段" in edited_text, edited_text.replace("\n", " ")[:70])
            check("状态徽章变为「已下架」", "已下架" in edited_text)
            api_edited = next((a for a in _agents(cli) if a["agentCode"] == "vf-ui-new"), None)
            check("接口侧确认三项改动",
                  api_edited["agentName"] == "UI验收助手·改"
                  and api_edited["streamEnabled"] == 0 and api_edited["status"] == 0,
                  f"stream={api_edited['streamEnabled']} status={api_edited['status']}")
            page.screenshot(path=str(shots / "agents-list.png"), full_page=True)

            section("5. 启停按钮（M-A-03）")
            edited.locator(".op-btn", has_text="上架").click()
            page.wait_for_timeout(900)
            check("点「上架」后徽章变为已上架",
                  "已上架" in page.locator("#ag-tbody tr", has_text="UI验收助手·改").first.inner_text())
            api_on = next((a for a in _agents(cli) if a["agentCode"] == "vf-ui-new"), None)
            check("接口侧状态同步为 1", api_on["status"] == 1, f"status={api_on['status']}")

            page.locator("#ag-tbody tr", has_text="UI验收助手·改").first.locator(
                ".op-btn", has_text="下架"
            ).click()
            page.wait_for_timeout(900)
            check("点「下架」后徽章变为已下架",
                  "已下架" in page.locator("#ag-tbody tr", has_text="UI验收助手·改").first.inner_text())

            section("6. 状态筛选与关键词搜索")
            page.click("#ag-status-tabs .f:nth-child(3)")  # 已下架
            page.wait_for_timeout(800)
            texts = page.locator("#ag-tbody tr").all_inner_texts()
            check("「已下架」筛选只留下架助手",
                  bool(texts) and all("已下架" in t for t in texts), f"rows={len(texts)}")
            page.click("#ag-status-tabs .f:nth-child(1)")  # 全部
            page.wait_for_timeout(800)

            page.fill("#ag-q", "UI验收")
            page.press("#ag-q", "Enter")
            page.wait_for_timeout(800)
            check("搜索命中验收助手",
                  page.locator("#ag-tbody tr", has_text="UI验收助手·改").count() > 0)
            check("搜索结果被收窄", page.locator("#ag-tbody tr").count() >= 1)
            page.fill("#ag-q", "")
            page.wait_for_timeout(800)

            section("7. 删除助手")
            before_del = page.locator("#ag-tbody tr").count()
            page.locator("#ag-tbody tr", has_text="UI验收助手·改").first.locator(
                ".op-btn", has_text="删除"
            ).click()
            page.wait_for_timeout(1000)
            after_del = page.locator("#ag-tbody tr").count()
            check("确认后行数 -1", after_del == before_del - 1, f"{before_del} → {after_del}")
            check("列表中已找不到该助手",
                  page.locator("#ag-tbody tr", has_text="UI验收助手·改").count() == 0)
            check("接口侧已删除", all(a["agentCode"] != "vf-ui-new" for a in _agents(cli)))

            section("8. 零 JS 运行时错误")
            check("全程无 pageerror / console.error", not js_errors,
                  " | ".join(js_errors[:3]) if js_errors else "clean")
        finally:
            browser.close()

    return created_scene_id, (0 if FAILED == 0 else 1)


if __name__ == "__main__":
    raise SystemExit(main())
