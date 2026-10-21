"""富文本渲染测试（任务 1.9）。

覆盖三层：

1. **后端解析/渲染**：Markdown 子集 → blocks 组件数据 → 安全 HTML；
2. **XSS 防护**：脚本标签、协议注入（含大小写与制表符混淆）、属性注入；
3. **前后端一致性**：同一批用例同时喂给 ``app.core.richtext.render_html()`` 与
   前端 ``static/app.js`` 的 ``renderContent()``（经 Node 加载），要求逐字符一致
   ——未找到 Node 时跳过该组用例。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import richtext as rt
from app.main import app

SERVICE_DIR = Path(__file__).resolve().parent.parent
PROBE = SERVICE_DIR / "tools" / "richtext_probe.mjs"

sys.path.insert(0, str(SERVICE_DIR / "tools"))
from richtext_cases import CASES  # noqa: E402

client = TestClient(app)


# ------------------------------------------------------------------ 1. 解析与渲染
def test_heading_levels() -> None:
    blocks = rt.parse_blocks("# 一\n\n## 二\n\n### 三")
    assert [b["type"] for b in blocks] == ["heading"] * 3
    assert [b["level"] for b in blocks] == [1, 2, 3]
    assert blocks[1]["text"] == "二"


def test_paragraph_keeps_newline_as_br() -> None:
    html = rt.render_html("第一行\n第二行")
    assert html == "<p>第一行<br>第二行</p>"


def test_unordered_and_ordered_lists() -> None:
    ul = rt.parse_blocks("- 甲\n- 乙")[0]
    assert ul["type"] == "list" and ul["ordered"] is False
    assert [i["text"] for i in ul["items"]] == ["甲", "乙"]

    ol = rt.parse_blocks("1. 甲\n2. 乙")[0]
    assert ol["type"] == "list" and ol["ordered"] is True

    assert rt.render_html("- 甲") == '<ul class="rt-list"><li>甲</li></ul>'
    assert rt.render_html("1. 甲") == '<ol class="rt-list"><li>甲</li></ol>'


def test_code_block_and_inline_code() -> None:
    html = rt.render_html("```bash\ncurl a\n```")
    assert html == '<pre class="rt-code"><code class="language-bash">curl a</code></pre>'
    assert rt.render_html("`x`") == "<p><code>x</code></p>"


def test_unclosed_code_block_is_tolerated() -> None:
    """未闭合的围栏按「读到文末」处理（保留尾随空行，与前端一致）。"""
    blocks = rt.parse_blocks("```bash\ncurl a\n")
    assert blocks == [{"type": "code", "lang": "bash", "text": "curl a\n"}]
    assert rt.parse_blocks("```bash\ncurl a")[0]["text"] == "curl a"


def test_quote_and_hr() -> None:
    assert rt.render_html("> 注意") == '<blockquote class="rt-quote">注意</blockquote>'
    assert rt.render_html("---") == '<hr class="rt-hr">'


def test_table_with_alignment() -> None:
    md = "| 项目 | 说明 | 费用 |\n|:---|:---:|---:|\n| 开户 | 首次开通 | 0 元 |"
    blocks = rt.parse_blocks(md)
    assert blocks[0]["type"] == "table"
    assert blocks[0]["header"] == ["项目", "说明", "费用"]
    assert blocks[0]["rows"] == [["开户", "首次开通", "0 元"]]
    assert blocks[0]["align"] == ["left", "center", "right"]

    html = rt.render_html(md)
    assert '<th style="text-align:left">项目</th>' in html
    assert '<th style="text-align:center">说明</th>' in html
    assert '<td style="text-align:right">0 元</td>' in html


def test_table_ragged_rows_padded() -> None:
    """列数不齐的行要补齐，避免前端下标越界。"""
    blocks = rt.parse_blocks("| a | b | c |\n|---|---|---|\n| 1 |")
    assert blocks[0]["rows"] == [["1", "", ""]]


def test_lone_image_promoted_to_block() -> None:
    blocks = rt.parse_blocks("![拓扑图](/static/topo.png)")
    assert blocks[0]["type"] == "image"
    assert blocks[0]["alt"] == "拓扑图"
    assert blocks[0]["src"] == "/static/topo.png"
    html = rt.render_html("![拓扑图](/static/topo.png)")
    assert '<figure class="rt-figure">' in html
    assert '<figcaption>拓扑图</figcaption>' in html


def test_inline_marks() -> None:
    html = rt.render_html("**粗** *斜* ~~删~~ `码`[1]")
    assert "<strong>粗</strong>" in html
    assert "<em>斜</em>" in html
    assert "<del>删</del>" in html
    assert "<code>码</code>" in html
    assert '<sup class="cite">[1]</sup>' in html


def test_blocks_json_serializable() -> None:
    blocks = rt.parse_blocks("## 标题\n\n段落[1]\n\n| a |\n|---|\n| 1 |")
    assert json.loads(json.dumps(blocks, ensure_ascii=False)) == blocks


def test_empty_inputs() -> None:
    assert rt.parse_blocks("") == []
    assert rt.parse_blocks(None) == []
    assert rt.parse_blocks("   \n  ") == []
    assert rt.render_html("") == ""
    assert rt.render_html(None) == ""


# ------------------------------------------------------------------ 2. XSS 防护
@pytest.mark.parametrize(
    "url",
    [
        "https://a.edu.cn/x",
        "http://a.edu.cn",
        "mailto:a@b.cn",
        "tel:12345",
        "/api/x",
        "#anchor",
        "./rel",
        "../rel",
    ],
)
def test_safe_url_allows(url: str) -> None:
    assert rt.safe_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "java script:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "blob:http://x/y",
        "  javascript:alert(1)  ",
    ],
)
def test_safe_url_blocks(url: str) -> None:
    assert rt.safe_url(url) == ""


def test_script_tag_is_escaped() -> None:
    html = rt.render_html("<script>alert(1)</script>")
    assert "<script" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_dangerous_link_degrades_to_text() -> None:
    html = rt.render_html("[点我](javascript:alert(1))")
    assert "href" not in html
    assert "[点我](javascript:alert(1))" in html


def test_dangerous_image_degrades_to_alt() -> None:
    html = rt.render_html("![x](javascript:alert(1))")
    assert "src=" not in html
    assert "<img" not in html


def test_quote_escaped_prevents_attribute_injection() -> None:
    html = rt.render_html('" onmouseover="alert(1)')
    assert "&quot;" in html
    assert 'onmouseover="' not in html


def test_only_whitelisted_tags_in_output() -> None:
    """剥离白名单标签后不得残留任何尖括号。"""
    import re

    tag_re = re.compile(
        r"</?(?:h[1-6]|p|ul|ol|li|pre|code|blockquote|table|thead|tbody|tr|th|td|hr"
        r"|figure|figcaption|img|a|strong|em|del|sup|br)\b[^>]*>",
        re.IGNORECASE,
    )
    html = rt.render_html(
        "<script>alert(1)</script>\n\n[点我](javascript:alert(2))\n\n"
        '| a |\n|---|\n| <img src=x onerror=alert(3)> |'
    )
    residue = tag_re.sub("", html)
    assert "<" not in residue and ">" not in residue, residue


def test_external_link_has_safe_attrs() -> None:
    html = rt.render_html("[自助](https://a.edu.cn)")
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


def test_table_cell_is_escaped() -> None:
    html = rt.render_html("| a |\n|---|\n| <script>alert(1)</script> |")
    assert "<script" not in html
    assert "&lt;script&gt;" in html


# ------------------------------------------------------------------ 3. 纯文本降级
def test_to_text_strips_markup() -> None:
    text = rt.to_text("## 标题\n\n- 甲 **粗**\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "标题" in text and "甲" in text and "粗" in text
    assert "##" not in text and "**" not in text and "|" not in text
    assert rt.to_text("") == ""


# ------------------------------------------------------------------ 4. 前后端一致性
def _node_bin() -> str | None:
    candidate = os.environ.get("NODE_BIN") or "node"
    if shutil.which(candidate) or Path(candidate).is_file():
        return candidate
    return None


@pytest.fixture(scope="module")
def frontend_html() -> dict[str, str]:
    binary = _node_bin()
    if not binary:
        pytest.skip("未找到 Node，跳过前后端一致性比对（可用 NODE_BIN 指定）")
    with tempfile.TemporaryDirectory() as tmp:
        cases_file = Path(tmp) / "cases.json"
        cases_file.write_text(json.dumps(CASES, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [binary, str(PROBE), str(cases_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(SERVICE_DIR),
        )
    assert proc.returncode == 0, f"Node 探针失败：{proc.stderr[:400]}"
    return json.loads(proc.stdout)


@pytest.mark.parametrize("name,md", CASES, ids=[c[0] for c in CASES])
def test_frontend_backend_render_identical(
    name: str, md: str, frontend_html: dict[str, str]
) -> None:
    """同一份 Markdown，前端 renderContent 与后端 render_html 必须逐字符一致。"""
    assert name in frontend_html, f"前端未返回用例 {name}"
    assert rt.render_html(md) == frontend_html[name], f"用例「{name}」前后端渲染不一致"


# ------------------------------------------------------------------ 5. 接口层
def test_chat_response_carries_blocks_and_html() -> None:
    resp = client.post("/api/chat", json={"question": "校园网怎么开通？", "role": "访客"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert isinstance(data["blocks"], list) and data["blocks"]
    assert isinstance(data["answerHtml"], str) and data["answerHtml"]
    # 向后兼容：answer 原样保留
    assert data["answer"]
    # blocks 与 answerHtml 同源
    assert rt.render_blocks(data["blocks"]) == data["answerHtml"]


def test_chat_response_html_is_safe() -> None:
    resp = client.post("/api/chat", json={"question": "校园网怎么开通？", "role": "访客"})
    html = resp.json()["data"]["answerHtml"]
    assert "<script" not in html


def test_stream_done_carries_blocks_and_html() -> None:
    with client.stream(
        "POST", "/api/chat/stream", json={"question": "校园网怎么开通？", "role": "访客"}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    done = None
    for line in body.splitlines():
        if line.startswith("data:"):
            obj = json.loads(line[5:].strip())
            if obj.get("type") == "done":
                done = obj
    assert done is not None, "未收到 done 事件"
    assert isinstance(done["blocks"], list) and done["blocks"]
    assert isinstance(done["answerHtml"], str) and done["answerHtml"]
    assert done["answer"]


def test_history_messages_carry_blocks() -> None:
    """历史会话恢复时也应带结构化数据，刷新前后渲染一致。"""
    first = client.post("/api/chat", json={"question": "一卡通丢了怎么办？", "role": "访客"}).json()
    sid = first["data"]["sessionId"]
    msgs = client.get(f"/api/sessions/{sid}/messages").json()["data"]
    ai_msgs = [m for m in msgs if m["role"] == "ai"]
    assert ai_msgs
    for m in ai_msgs:
        assert isinstance(m["blocks"], list) and m["blocks"]
        assert isinstance(m["answerHtml"], str) and m["answerHtml"]
    user_msgs = [m for m in msgs if m["role"] == "user"]
    for m in user_msgs:
        assert m["blocks"] == [] and m["answerHtml"] is None
