"""富文本渲染验收（任务 1.9）。

两件事：

1. **前后端一致性**：把 ``tools/richtext_cases.py`` 的同一批 Markdown 同时喂给
   后端 ``app.core.richtext.render_html()`` 与前端 ``static/app.js`` 的
   ``renderContent()``（经 Node 加载），要求**逐字符一致**。
2. **后端自检**：结构化 blocks 契约、``safe_url`` 协议白名单、XSS 转义、
   ``to_text`` 去标记、blocks 可 JSON 序列化。

用法::

    python tools/verify_richtext.py
    NODE_BIN=/path/to/node python tools/verify_richtext.py

未找到 Node 时只跑后端自检（并明确提示一致性部分被跳过）。
退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))
sys.path.insert(0, str(SERVICE_DIR / "tools"))

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from app.core import richtext as rt  # noqa: E402
from richtext_cases import CASES  # noqa: E402

PROBE = SERVICE_DIR / "tools" / "richtext_probe.mjs"
PASSED = 0
FAILED = 0

#: 渲染器允许输出的标签（用于「无白名单外标签」的精确校验）
_TAG_RE = re.compile(
    r"</?(?:h[1-6]|p|ul|ol|li|pre|code|blockquote|table|thead|tbody|tr|th|td|hr"
    r"|figure|figcaption|img|a|strong|em|del|sup|br)\b[^>]*>",
    re.IGNORECASE,
)


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


def node_bin() -> str | None:
    candidate = os.environ.get("NODE_BIN") or "node"
    if shutil.which(candidate) or Path(candidate).is_file():
        return candidate
    return None


def run_probe(binary: str) -> dict[str, str] | None:
    with tempfile.TemporaryDirectory() as tmp:
        cases_file = Path(tmp) / "cases.json"
        cases_file.write_text(
            json.dumps(CASES, ensure_ascii=False), encoding="utf-8"
        )
        proc = subprocess.run(
            [binary, str(PROBE), str(cases_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(SERVICE_DIR),
        )
    if proc.returncode != 0:
        print(f"  Node 探针失败（exit={proc.returncode}）：{(proc.stderr or '').strip()[:300]}")
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"  探针输出无法解析：{exc}")
        return None


def first_diff(a: str, b: str) -> str:
    for i, (ca, cb) in enumerate(zip(a, b)):
        if ca != cb:
            lo = max(0, i - 40)
            return f"位置 {i}：后端…{a[lo:i + 30]!r} / 前端…{b[lo:i + 30]!r}"
    return f"长度不同：后端 {len(a)} / 前端 {len(b)}"


def verify_parity() -> None:
    section("1. 前后端渲染一致性（逐字符）")
    binary = node_bin()
    if not binary:
        print("  [SKIP] 未找到 Node（可设 NODE_BIN 指定），跳过一致性比对")
        return
    print(f"  使用 Node：{binary}")
    frontend = run_probe(binary)
    if frontend is None:
        check("Node 探针可运行", False)
        return
    check("Node 探针可运行", True, f"{len(frontend)} 个用例")

    for name, md in CASES:
        if name not in frontend:
            check(f"一致性：{name}", False, "前端未返回该用例")
            continue
        py = rt.render_html(md)
        fe = frontend[name]
        check(f"一致性：{name}", py == fe, "" if py == fe else first_diff(py, fe))


def verify_blocks() -> None:
    section("2. 结构化 blocks 契约")
    md = (
        "## 标题\n\n段落[1]。\n\n- 甲\n- 乙\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "```py\nprint(1)\n```\n\n> 引用\n\n---\n\n![图](/x.png)"
    )
    blocks = rt.parse_blocks(md)
    kinds = [b["type"] for b in blocks]
    check(
        "块类型齐全",
        kinds == ["heading", "paragraph", "list", "table", "code", "quote", "hr", "image"],
        str(kinds),
    )
    check("blocks 可 JSON 序列化", isinstance(json.dumps(blocks, ensure_ascii=False), str))
    table = next(b for b in blocks if b["type"] == "table")
    check("表格表头", table["header"] == ["a", "b"], str(table["header"]))
    check("表格行", table["rows"] == [["1", "2"]], str(table["rows"]))
    lst = next(b for b in blocks if b["type"] == "list")
    check("列表项", [i["text"] for i in lst["items"]] == ["甲", "乙"])
    check("列表 ordered 标记", lst["ordered"] is False)
    para = next(b for b in blocks if b["type"] == "paragraph")
    check(
        "段落带行内节点",
        [n["type"] for n in para["inlines"]] == ["text", "cite", "text"],
        str([n["type"] for n in para["inlines"]]),
    )
    check("空文本 → 空 blocks", rt.parse_blocks("") == [] and rt.parse_blocks(None) == [])

    # 列数不齐的表格要补齐，避免前端下标越界
    uneven = rt.parse_blocks("| a | b | c |\n|---|---|---|\n| 1 |")
    check("表格列数补齐", uneven[0]["rows"] == [["1", "", ""]], str(uneven[0]["rows"]))


def verify_security() -> None:
    section("3. XSS 防护与协议白名单")

    # safe_url 白名单
    for url in (
        "https://a.edu.cn/x",
        "http://a.edu.cn",
        "mailto:a@b.cn",
        "tel:12345",
        "/api/x",
        "#anchor",
        "./rel",
        "../rel",
    ):
        check(f"放行 {url}", rt.safe_url(url) == url)
    for url in (
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "blob:http://x/y",
        "  javascript:alert(1)  ",
    ):
        check(f"拒绝 {url!r}", rt.safe_url(url) == "")

    # 渲染结果不得含可执行内容
    html = rt.render_html(
        "<script>alert(1)</script>\n\n"
        '[点我](javascript:alert(2))\n\n'
        "![x](javascript:alert(3))\n\n"
        '| a |\n|---|\n| <img src=x onerror=alert(4)> |\n\n'
        '" onmouseover="alert(5)'
    )
    for bad in ("<script", "<img src=x", 'href="javascript', 'src="javascript'):
        check(f"输出不含 {bad!r}", bad not in html)
    check("危险文本已转义为实体", "&lt;script&gt;" in html)
    check("引号已转义", "&quot;" in html)
    check("拒绝的链接退化为纯文本", "[点我](javascript:alert(2))" in html)

    # 精确校验：剥离白名单标签后不得残留任何 `<` / `>`，
    # 且标签内部不得出现事件属性（`onerror=` 只允许作为**已转义文本**出现）
    tags = _TAG_RE.findall(html)
    residue = _TAG_RE.sub("", html)
    check("无白名单外的标签", "<" not in residue and ">" not in residue, residue.strip()[:80])
    events = [t for t in tags if re.search(r"\son[a-z]+\s*=", t, re.IGNORECASE)]
    check("标签内无事件属性", not events, str(events[:2]))
    check("onerror 仅以转义文本出现", "onerror=" in html and "&lt;img" in html)

    # 允许的外链带安全属性
    ok_html = rt.render_html("[自助](https://a.edu.cn)")
    check("外链带 target/rel", 'target="_blank"' in ok_html and 'rel="noopener noreferrer"' in ok_html)


def verify_to_text() -> None:
    section("4. 纯文本降级")
    md = "## 标题\n\n- 甲 **粗**\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    text = rt.to_text(md)
    check("保留文字内容", "标题" in text and "甲" in text and "粗" in text)
    check("剥离标记", "##" not in text and "**" not in text and "|" not in text, repr(text[:60]))
    check("空文本 → 空串", rt.to_text("") == "" and rt.to_text(None) == "")


def write_preview(path: Path) -> Path:
    """生成可视化预览：用真实 ``static/style.css`` 渲染一份含表格/列表/链接/代码的回答。"""
    sample = """## 校园网开户与报修

请按以下流程办理，具体见 **信息网络中心** 通知[1]。

1. 登录 [自助服务门户](https://net.example.edu.cn) 提交申请
2. 携带 **学生证** 到服务大厅办理

- 材料：学生证、身份证
- 费用：免费

> 提醒：账号密码请通过学校官方渠道办理，勿轻信第三方。

| 项目 | 办理地点 | 时限 |
|:---|:---:|---:|
| 开户 | 服务大厅 | 1 个工作日 |
| 报修 | 线上工单 | 24 小时 |

```bash
ping net.example.edu.cn
```

---

普通段落，含行内 `code`、*强调*、~~删除线~~ 与引用[2]。"""

    xss_input = (
        '<script>alert(1)</script>\n\n'
        '[点我](javascript:alert(2))\n\n'
        '![图](data:text/html;base64,PHNjcmlwdD4=)\n\n'
        '" onmouseover="alert(3)'
    )
    css = (SERVICE_DIR / "static" / "style.css").read_text(encoding="utf-8")

    rows = []
    binary = node_bin()
    frontend = run_probe(binary) if binary else None
    for name, md in CASES:
        py = rt.render_html(md)
        if frontend is None:
            mark, note = "—", "未运行 Node 探针"
        elif frontend.get(name) == py:
            mark, note = "✅", "一致"
        else:
            mark, note = "❌", "不一致"
        rows.append(
            f"<tr><td>{rt.esc(name)}</td><td class='mark'>{mark}</td><td>{rt.esc(note)}</td></tr>"
        )

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>富文本渲染预览（任务 1.9）</title>
<style>
{css}
body {{ padding: 0; }}
.wrap {{ max-width: 900px; margin: 0 auto; padding: 24px; }}
h2.sec {{ margin: 28px 0 12px; font-size: 17px; }}
.note {{ color: var(--muted); font-size: 13px; margin-bottom: 10px; }}
.parity {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
.parity th, .parity td {{ border: 1px solid var(--border); padding: 5px 10px; text-align: left; }}
.parity th {{ background: #f1f5f9; }}
.parity td.mark {{ text-align: center; width: 48px; }}
pre.raw {{ background: #f8fafc; border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; font-size: 13px; overflow-x: auto; white-space: pre-wrap; }}
.bubble {{ max-width: 100%; }}
</style>
</head>
<body>
<div class="wrap">
  <h2 class="sec">1. 渲染效果（真实 static/style.css）</h2>
  <div class="note">以下 HTML 由后端 <code>app.core.richtext.render_html()</code> 生成，
  前端 <code>renderContent()</code> 输出与其逐字符一致（见第 3 节）。</div>
  <div class="msg ai"><div class="bubble">{rt.render_html(sample)}</div></div>

  <h2 class="sec">2. XSS 防护</h2>
  <div class="note">左侧为原始输入，右侧为渲染结果——脚本标签、危险协议、属性注入均被转义或丢弃。</div>
  <pre class="raw">输入：
{rt.esc(xss_input)}</pre>
  <div class="msg ai"><div class="bubble">{rt.render_html(xss_input)}</div></div>

  <h2 class="sec">3. 前后端一致性（{len(CASES)} 条用例）</h2>
  <table class="parity">
    <thead><tr><th>用例</th><th>结果</th><th>说明</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="富文本渲染验收（任务 1.9）")
    parser.add_argument(
        "--preview",
        metavar="PATH",
        help="额外输出一份可视化预览 HTML（用真实 style.css 渲染样例回答）",
    )
    args = parser.parse_args()

    print("=" * 64)
    print("kb-service 富文本渲染验收（任务 1.9）")
    print(f"用例 {len(CASES)} 条 | 后端 app/core/richtext.py | 前端 static/app.js")
    print("=" * 64)

    verify_parity()
    verify_blocks()
    verify_security()
    verify_to_text()

    if args.preview:
        section("5. 可视化预览")
        out = write_preview(Path(args.preview))
        check("预览已生成", out.is_file(), str(out))

    print("\n" + "=" * 64)
    print(f"汇总：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 64)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
