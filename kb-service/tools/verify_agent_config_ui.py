"""助手配置页 · 浏览器端到端验收（任务 2.3 前端 / M-A-04·05·09）。

真实 Chromium 打开 ``/admin`` 的「智能体管理」页签，验证 HTTP 层看不见的东西：
弹窗四页签互斥、提示词回填与字数提示、知识文件夹异步渲染、检索参数联动提示、
对话测试面板渲染，以及**零 JS 运行时错误**。

1. **弹窗页签**：四个页签（基础信息 / 提示词 / 检索配置 / 对话测试），互斥切换；
2. **提示词（M-A-04）**：新建时留空 → 显示「使用平台默认提示词」与默认提示词长度；
   点「查看平台默认提示词」展开默认文本；填入自定义提示词后字数提示同步；
3. **检索配置（M-A-05）**：知识文件夹（部门 / 类型）异步渲染出可勾选项；
   留 0 时提示「沿用全局」并在输入后变为「生效：N 条（助手配置）」；
   勾选部门 + topK → 保存；
4. **保存回填**：再次打开编辑，提示词与检索配置都应原样回填（含勾选状态）；
5. **列表标记**：配过提示词 / 检索的助手在列表里有「提示词 / 检索」标记；
6. **对话测试（M-A-09）**：列表「测试」按钮直达测试页签；跑一次测试，
   结果面板显示答案、引用、召回明细与**生效参数**（topK 来源标注、部门范围）；
7. **不落库**：测试前后会话日志条数不变；
8. **零 JS 错误**：全程无 ``pageerror`` / ``console.error``。

**幂等性**：验收自建的 ``vf-uic`` 助手在前后各清理一次，可重复运行。

用法::

    python tools/verify_agent_config_ui.py
    python tools/verify_agent_config_ui.py --port 8025 --headful

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

#: 验收自建助手编码（前后各清理一次）
UI_CODE = "vf-uic"
CUSTOM_PROMPT = "你是验收专用助手，只回答「已收到」，不要展开解释。"


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

    return httpx.Client(timeout=90, trust_env=False, base_url=base)


def _agents(cli) -> list[dict]:
    return cli.get("/api/admin/agents").json()["data"]["items"]


def _find(cli, code: str) -> dict | None:
    return next((a for a in _agents(cli) if a["agentCode"] == code), None)


def _cleanup(cli, code: str) -> None:
    found = _find(cli, code)
    if found:
        cli.delete(f"/api/admin/agents/{found['agentId']}")


def _log_count(cli) -> int:
    return cli.get("/api/admin/chatlogs", params={"pageSize": 1}).json()["data"]["total"]


def main() -> int:
    parser = argparse.ArgumentParser(description="助手配置页浏览器验收（任务 2.3）")
    parser.add_argument("--port", type=int, default=8025, help="验收用服务端口")
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
    env["RATE_LIMIT_ENABLED"] = "false"

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
    from playwright.sync_api import sync_playwright

    with _client(base) as cli:
        _cleanup(cli, UI_CODE)  # 前置清理：上次运行中途失败可能残留
        logs_before = _log_count(cli)
        js_errors: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not headful)
            page = browser.new_page(viewport={"width": 1600, "height": 1040})
            page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    js_errors.append(f"console.error: {msg.text}")

            page.on("console", on_console)
            page.on("dialog", lambda d: d.accept())

            try:
                section("1. 打开助手配置弹窗")
                page.goto(f"{base}/admin", wait_until="networkidle")
                page.click('#tabs .tab[data-tab="agents"]')
                page.wait_for_selector("#ag-scenes .scene-card", timeout=20000)

                page.click("#ag-new")
                page.wait_for_function(
                    "document.getElementById('ag-modal').hidden === false", timeout=10000
                )
                check("弹窗打开", page.locator("#ag-modal").is_visible())
                check("弹窗为宽版（多页签需要）",
                      "modal-wide" in (page.get_attribute("#ag-modal .modal-box", "class") or ""))
                check("弹窗有 4 个页签", page.locator("#ag-tabs .ag-tab").count() == 4,
                      f"{page.locator('#ag-tabs .ag-tab').all_inner_texts()}")
                check("默认停在「基础信息」", page.locator("#ag-pane-basic").is_visible())
                check("提示词页签初始隐藏", not page.locator("#ag-pane-prompt").is_visible())
                check("检索配置页签初始隐藏", not page.locator("#ag-pane-retrieval").is_visible())
                check("对话测试页签初始隐藏", not page.locator("#ag-pane-test").is_visible())

                section("2. 提示词页签（M-A-04）")
                page.click('#ag-tabs .ag-tab[data-agt="prompt"]')
                check("切到提示词页签", page.locator("#ag-pane-prompt").is_visible())
                check("基础信息页签已隐藏", not page.locator("#ag-pane-basic").is_visible())
                count_txt = page.locator("#ag-f-prompt-count").inner_text()
                check("留空时提示「使用平台默认提示词」",
                      "使用平台默认提示词" in count_txt, count_txt[:60])
                check("提示词输入框可编辑", page.locator("#ag-f-prompt").is_editable())

                check("默认提示词初始折叠", page.locator("#ag-f-prompt-default").is_hidden())
                page.click("#ag-prompt-default-toggle")
                page.wait_for_timeout(200)
                default_txt = page.locator("#ag-f-prompt-default").inner_text()
                check("展开平台默认提示词且内容非空", len(default_txt) > 50, f"{len(default_txt)} 字符")
                check("默认提示词含 5 条硬约束之一", "不得编造" in default_txt)

                page.fill("#ag-f-prompt", CUSTOM_PROMPT)
                page.wait_for_timeout(200)
                count_txt = page.locator("#ag-f-prompt-count").inner_text()
                check("填入后字数提示同步",
                      str(len(CUSTOM_PROMPT)) in count_txt and "使用平台默认" not in count_txt,
                      count_txt)

                section("3. 检索配置页签（M-A-05）")
                page.click('#ag-tabs .ag-tab[data-agt="retrieval"]')
                page.wait_for_selector("#ag-f-depts input", timeout=15000)
                check("切到检索配置页签", page.locator("#ag-pane-retrieval").is_visible())
                n_depts = page.locator("#ag-f-depts input").count()
                n_ktypes = page.locator("#ag-f-ktypes input").count()
                check("部门可选项已渲染", n_depts > 0, f"{n_depts} 项")
                check("知识类型可选项已渲染", n_ktypes > 0, f"{n_ktypes} 项")
                note = page.locator("#ag-f-ret-note").inner_text()
                check("提示条说明「沿用平台默认」的数值", "沿用平台默认" in note, note[:64])
                check("提示条说明范围过滤在召回之前", "召回之前" in note)

                hint0 = page.locator("#ag-f-topk-hint").inner_text()
                check("topK 留 0 时提示沿用全局", "沿用全局" in hint0, hint0)
                page.fill("#ag-f-topk", "3")
                page.wait_for_timeout(200)
                hint1 = page.locator("#ag-f-topk-hint").inner_text()
                check("topK 填值后提示「生效 N 条（助手配置）」",
                      "生效：3 条" in hint1, hint1)

                first_dept = page.locator("#ag-f-depts input").first
                dept_name = first_dept.get_attribute("value")
                first_dept.check()
                # 本工具验证 UI 接线而非召回质量：缺少 ml extra 时向量后端降级为哈希，
                # 余弦普遍低于 0.1，非零阈值会把召回全部滤掉；阈值过滤由 test_min_score_filters_results 覆盖。
                page.fill("#ag-f-minscore", "0")
                page.screenshot(path=str(shots / "agent-config-retrieval.png"), full_page=True)
                check("已勾选一个部门", first_dept.is_checked(), dept_name)

                section("4. 保存并回填")
                # 名称/编码在「基础信息」页签，先切回去（多页签互斥，隐藏页无法 fill）
                page.click('#ag-tabs .ag-tab[data-agt="basic"]')
                page.wait_for_timeout(200)
                page.fill("#ag-f-name", "UI配置验收助手")
                page.fill("#ag-f-code", UI_CODE)
                page.click("#ag-save")
                page.wait_for_function(
                    "document.getElementById('ag-modal').hidden === true", timeout=20000
                )
                page.wait_for_timeout(800)

                row = page.locator("#ag-tbody tr", has_text="UI配置验收助手").first
                check("新助手出现在列表", row.count() > 0)
                row_text = row.inner_text()
                check("列表出现「提示词」标记", "提示词" in row_text, row_text.replace("\n", " ")[:80])
                check("列表出现「检索」标记", "检索" in row_text)

                api_agent = _find(cli, UI_CODE)
                check("接口侧已落库提示词", api_agent and api_agent["systemPrompt"] == CUSTOM_PROMPT,
                      (api_agent or {}).get("systemPrompt", "-")[:24])
                check("接口侧已落库检索配置",
                      api_agent and api_agent["retrievalConfig"]["topK"] == 3
                      and api_agent["retrievalConfig"]["depts"] == [dept_name],
                      str((api_agent or {}).get("retrievalConfig")))

                row.locator(".op-btn", has_text="编辑").click()
                page.wait_for_function(
                    "document.getElementById('ag-modal').hidden === false", timeout=10000
                )
                page.click('#ag-tabs .ag-tab[data-agt="prompt"]')
                page.wait_for_timeout(200)
                check("重新打开：提示词已回填",
                      page.input_value("#ag-f-prompt") == CUSTOM_PROMPT)
                page.click('#ag-tabs .ag-tab[data-agt="retrieval"]')
                page.wait_for_selector("#ag-f-depts input", timeout=15000)
                page.wait_for_timeout(300)
                check("重新打开：topK 已回填", page.input_value("#ag-f-topk") == "3")
                checked = [c.get_attribute("value")
                           for c in page.locator("#ag-f-depts input:checked").all()]
                check("重新打开：部门勾选状态已回填", checked == [dept_name], f"{checked}")

                section("5. 对话测试（M-A-09）")
                page.click('#ag-tabs .ag-tab[data-agt="test"]')
                page.wait_for_timeout(200)
                check("切到对话测试页签", page.locator("#ag-pane-test").is_visible())
                check("测试结果面板初始隐藏", page.locator("#ag-test-result").is_hidden())
                check("身份下拉已渲染", page.locator("#ag-test-role option").count() > 1)

                page.fill("#ag-test-q", "校园网怎么开户")
                page.click("#ag-test-run")
                page.wait_for_selector("#ag-test-result:not([hidden])", timeout=90000)
                page.wait_for_timeout(300)

                answer_txt = page.locator("#ag-test-result .tr-answer").inner_text()
                check("结果面板渲染出答案正文", len(answer_txt.strip()) > 0, answer_txt[:40])
                result_txt = page.locator("#ag-test-result").inner_text()
                check("结果含召回条数与耗时徽章",
                      "召回" in result_txt and "耗时" in result_txt, result_txt[:60].replace("\n", " "))
                # 任务 2.4 起测试面板有两行 .tr-meta（检索参数 / 生成参数），
                # 用文本定位而非 .tr-meta 单数选择器，否则 strict 模式会因命中两个元素而报错。
                retrieval_meta = page.locator(
                    "#ag-test-result .tr-meta", has_text="生效检索参数").inner_text()
                model_meta = page.locator(
                    "#ag-test-result .tr-meta", has_text="生效模型参数").inner_text()
                check("生效参数标注 topK 来源为「助手配置」",
                      "topK=3（助手配置）" in retrieval_meta, retrieval_meta[:110])
                check("生效参数标注知识范围",
                      dept_name in retrieval_meta, retrieval_meta[:160])
                check("提示词来源标注为「自定义」", "自定义" in retrieval_meta)
                check("生效模型参数显示走了哪个模型（任务 2.4）",
                      "模型" in model_meta and ("默认" in model_meta or "全局" in model_meta),
                      model_meta[:120])
                check("召回明细表已渲染",
                      page.locator("#ag-test-result .tr-table tbody tr").count() >= 1)
                src_depts = page.locator("#ag-test-result .tr-table tbody tr td:nth-child(3)").all_inner_texts()
                check("召回明细全部落在配置的部门内",
                      bool(src_depts) and all(d.strip() == dept_name for d in src_depts),
                      f"{sorted(set(src_depts))}")
                msg = page.locator("#ag-test-msg").inner_text()
                check("提示测试不写入会话日志", "未写入会话日志" in msg, msg[:60])
                page.screenshot(path=str(shots / "agent-config-test.png"), full_page=True)

                check("测试未污染会话日志", _log_count(cli) == logs_before,
                      f"{logs_before} → {_log_count(cli)}")

                page.click("#ag-modal .modal-x")
                page.wait_for_function(
                    "document.getElementById('ag-modal').hidden === true", timeout=10000
                )

                section("6. 列表「测试」按钮直达测试页签")
                page.locator("#ag-tbody tr", has_text="UI配置验收助手").first.locator(
                    ".op-btn", has_text="测试"
                ).click()
                page.wait_for_function(
                    "document.getElementById('ag-modal').hidden === false", timeout=10000
                )
                check("点「测试」直接打开对话测试页签",
                      page.locator("#ag-pane-test").is_visible())
                check("测试页签下保存按钮仍可用", page.locator("#ag-save").is_enabled())
                page.click("#ag-modal .modal-x")
                page.wait_for_timeout(300)

                section("7. 零 JS 运行时错误")
                check("全程无 pageerror / console.error", not js_errors,
                      " | ".join(js_errors[:3]) if js_errors else "clean")
            finally:
                browser.close()

        section("8. 清理与幂等")
        _cleanup(cli, UI_CODE)
        check("验收助手已清理", _find(cli, UI_CODE) is None)
        check("会话日志条数未被验收改变", _log_count(cli) == logs_before)

    print(f"\n截图目录：{shots}")
    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
