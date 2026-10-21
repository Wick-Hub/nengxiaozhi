"""富文本解析与安全渲染（任务 1.9）。

大模型返回的是 Markdown 文本，前端需要**结构化组件数据**与**安全 HTML**。
本模块把回答正文解析为 blocks（组件数据），并渲染为 HTML。

支持的 Markdown 子集
--------------------

- 标题 ``#`` / ``##`` / ``###``（1–6 级）
- 无序列表 ``- `` / ``* `` / ``+ ``；有序列表 ``1. `` / ``1) ``
- 围栏代码块 ```` ```lang ````、行内代码 `` `x` ``
- 引用块 ``> ``
- 表格 ``| a | b |`` + 分隔行 ``|---|---|``（含 ``:---`` / ``---:`` / ``:---:`` 对齐）
- 链接 ``[文字](url)``、图片 ``![alt](src)``
- 加粗 ``**x**``、斜体 ``*x*``、删除线 ``~~x~~``
- 引用上标 ``[n]``（1–2 位数字，与来源面板编号对齐）
- 分隔线 ``---``

安全约定（防 XSS）
------------------

1. **一切文本先转义**（``& < > " '``），未识别语法按纯文本输出，不会被当成标签；
2. 链接 / 图片地址走 **协议白名单**：仅放行 ``http(s)`` / ``mailto`` / ``tel`` 与相对路径，
   ``javascript:`` / ``data:`` / ``vbscript:`` / ``file:`` / ``blob:`` 一律拒绝；
3. 校验前先剥离控制字符与空白，防 ``java\\tscript:`` 之类的混淆绕过；
4. 外链统一加 ``target="_blank" rel="noopener noreferrer"``。

原型 ``kb-engine`` 的 ``renderContent`` 只支持 ``**加粗**`` + ``[n]`` + 换行，
本模块为**能力增强**（无 Node 侧可做一致性回归）。
"""

from __future__ import annotations

import re
from typing import Any

# ------------------------------------------------------------------ 基础
_ESCAPE_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
_ESCAPE_RE = re.compile(r"[&<>\"']")

#: 显式拒绝的协议（先剥离空白/控制字符再比对，防混淆绕过）
_BLOCKED_SCHEMES = ("javascript:", "vbscript:", "data:", "file:", "blob:")
#: 放行的协议
_ALLOWED_SCHEMES = ("http://", "https://", "mailto:", "tel:")
_CTRL_RE = re.compile(r"[\x00-\x20\x7f]+")

_FENCE_RE = re.compile(r"^\s*```\s*([A-Za-z0-9_+.-]*)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_ULIST_RE = re.compile(r"^\s{0,3}[-*+]\s+(.+)$")
_OLIST_RE = re.compile(r"^\s{0,3}\d{1,3}[.)]\s+(.+)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)*\|?\s*$")

_INLINE_RE = re.compile(
    r"!\[(?P<img_alt>[^\]]*)\]\((?P<img_src>[^)]*)\)"
    r"|\[(?P<link_text>[^\]]*)\]\((?P<link_href>[^)]*)\)"
    r"|\[(?P<cite>\d{1,2})\]"
    r"|`(?P<code>[^`\n]+)`"
    r"|\*\*(?P<strong>[^*\n]+)\*\*"
    r"|~~(?P<del>[^~\n]+)~~"
    r"|\*(?P<em>[^*\n]+)\*"
)


def esc(text: Any) -> str:
    """HTML 转义（所有文本输出的唯一出口）。"""
    return _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP[m.group(0)], str(text))


def safe_url(raw: str | None) -> str:
    """链接 / 图片地址白名单校验；不安全返回空串。"""
    url = (raw or "").strip()
    if not url:
        return ""
    compact = _CTRL_RE.sub("", url).lower()
    if compact.startswith(_BLOCKED_SCHEMES):
        return ""
    # 相对路径 / 页内锚点
    if url.startswith(("/", "#", "./", "../")):
        return url
    if compact.startswith(_ALLOWED_SCHEMES):
        return url
    return ""


# ------------------------------------------------------------------ 行内解析
def parse_inlines(text: str) -> list[dict[str, Any]]:
    """把一行文本解析为行内节点列表。

    未匹配的字符原样落到 ``text`` 节点，渲染时统一转义——**未识别语法不会被当成标签**。
    """
    nodes: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            nodes.append({"type": "text", "text": text[pos : m.start()]})
        pos = m.end()
        groups = m.groupdict()
        if groups["img_alt"] is not None:
            nodes.append(
                {
                    "type": "image",
                    "alt": groups["img_alt"],
                    "src": safe_url(groups["img_src"]),
                }
            )
        elif groups["link_text"] is not None:
            href = safe_url(groups["link_href"])
            if href:
                nodes.append({"type": "link", "text": groups["link_text"], "href": href})
            else:
                # 不安全链接：退化为纯文本（保留可读性，绝不输出 href）
                nodes.append({"type": "text", "text": m.group(0)})
        elif groups["cite"] is not None:
            nodes.append({"type": "cite", "n": int(groups["cite"])})
        elif groups["code"] is not None:
            nodes.append({"type": "code", "text": groups["code"]})
        elif groups["strong"] is not None:
            nodes.append({"type": "strong", "text": groups["strong"]})
        elif groups["del"] is not None:
            nodes.append({"type": "del", "text": groups["del"]})
        elif groups["em"] is not None:
            nodes.append({"type": "em", "text": groups["em"]})
    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})
    return nodes


def inlines_to_text(nodes: list[dict[str, Any]]) -> str:
    """行内节点 → 纯文本（供搜索 / 摘要 / 兜底渲染）。"""
    parts: list[str] = []
    for node in nodes:
        kind = node.get("type")
        if kind == "cite":
            parts.append(f"[{node.get('n')}]")
        elif kind == "image":
            parts.append(f"![{node.get('alt', '')}]({node.get('src', '')})")
        elif kind == "link":
            parts.append(str(node.get("text", "")))
        else:
            parts.append(str(node.get("text", "")))
    return "".join(parts)


# ------------------------------------------------------------------ 块解析
def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _parse_align(sep_line: str) -> list[str | None]:
    aligns: list[str | None] = []
    for cell in _split_row(sep_line):
        cell = cell.strip()
        left, right = cell.startswith(":"), cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        elif left:
            aligns.append("left")
        else:
            aligns.append(None)
    return aligns


def _is_table_start(lines: list[str], i: int) -> bool:
    if i + 1 >= len(lines):
        return False
    head, sep = lines[i], lines[i + 1]
    if "|" not in head:
        return False
    if "-" not in sep:
        return False
    return bool(_TABLE_SEP_RE.match(sep))


def _is_block_start(lines: list[str], i: int) -> bool:
    line = lines[i]
    if not line.strip():
        return True
    if _FENCE_RE.match(line) or _HEADING_RE.match(line) or _QUOTE_RE.match(line):
        return True
    if _ULIST_RE.match(line) or _OLIST_RE.match(line):
        return True
    if _HR_RE.match(line):
        return True
    return _is_table_start(lines, i)


def parse_blocks(text: str | None) -> list[dict[str, Any]]:
    """Markdown 子集 → 结构化 blocks（组件数据）。

    blocks 类型：``heading`` / ``paragraph`` / ``list`` / ``code`` / ``quote`` /
    ``table`` / ``image`` / ``hr``。每个文本块同时给出 ``inlines``（行内节点）
    与 ``text``（纯文本），便于前端按需选择组件。
    """
    if not text:
        return []

    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    i = 0
    total = len(lines)

    while i < total:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        # 围栏代码块
        if (fence := _FENCE_RE.match(line)) is not None:
            lang = fence.group(1)
            i += 1
            buf: list[str] = []
            while i < total and not _FENCE_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过结束围栏（未闭合时 i 越界，循环自然结束）
            blocks.append({"type": "code", "lang": lang, "text": "\n".join(buf)})
            continue

        # 标题
        if (head := _HEADING_RE.match(line)) is not None:
            level = len(head.group(1))
            body = head.group(2).strip()
            blocks.append(
                {
                    "type": "heading",
                    "level": level,
                    "text": body,
                    "inlines": parse_inlines(body),
                }
            )
            i += 1
            continue

        # 分隔线
        if _HR_RE.match(line):
            blocks.append({"type": "hr"})
            i += 1
            continue

        # 表格
        if _is_table_start(lines, i):
            header = _split_row(lines[i])
            aligns = _parse_align(lines[i + 1])
            i += 2
            rows: list[list[str]] = []
            while i < total and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            width = len(header)
            rows = [(r + [""] * width)[:width] for r in rows]
            blocks.append(
                {
                    "type": "table",
                    "header": header,
                    "rows": rows,
                    "align": (aligns + [None] * width)[:width],
                }
            )
            continue

        # 引用块
        if _QUOTE_RE.match(line):
            buf = []
            while i < total and (q := _QUOTE_RE.match(lines[i])) is not None:
                buf.append(q.group(1))
                i += 1
            body = "\n".join(buf).strip()
            blocks.append({"type": "quote", "text": body, "inlines": parse_inlines(body)})
            continue

        # 列表
        if _ULIST_RE.match(line) or _OLIST_RE.match(line):
            ordered = _OLIST_RE.match(line) is not None
            items: list[dict[str, Any]] = []
            while i < total:
                m = _OLIST_RE.match(lines[i]) if ordered else _ULIST_RE.match(lines[i])
                if m is None:
                    break
                item_text = m.group(1).strip()
                items.append({"text": item_text, "inlines": parse_inlines(item_text)})
                i += 1
            blocks.append({"type": "list", "ordered": ordered, "items": items})
            continue

        # 段落（连续非空且非块起始行；保留内部换行 → <br>，与原型行为一致）
        buf = []
        while i < total and lines[i].strip() and not _is_block_start(lines, i):
            buf.append(lines[i].strip())
            i += 1
        if not buf:  # 防御：块起始判定与消费不一致时避免死循环
            buf.append(line.strip())
            i += 1
        body = "\n".join(buf)
        inlines = parse_inlines(body)
        # 整段只有一张图片 → 提升为块级图片（结构化返回）
        if len(inlines) == 1 and inlines[0].get("type") == "image":
            blocks.append({"type": "image", "alt": inlines[0]["alt"], "src": inlines[0]["src"]})
            continue
        blocks.append({"type": "paragraph", "text": body, "inlines": inlines})

    return blocks


# ------------------------------------------------------------------ 渲染
def render_inlines(nodes: list[dict[str, Any]]) -> str:
    """行内节点 → 安全 HTML。"""
    out: list[str] = []
    for node in nodes:
        kind = node.get("type")
        if kind == "text":
            # 段落内的换行转 <br>：与原型 renderContent 的 `\n → <br>` 行为一致
            out.append(esc(node.get("text", "")).replace("\n", "<br>"))
        elif kind == "strong":
            out.append(f"<strong>{esc(node.get('text', ''))}</strong>")
        elif kind == "em":
            out.append(f"<em>{esc(node.get('text', ''))}</em>")
        elif kind == "del":
            out.append(f"<del>{esc(node.get('text', ''))}</del>")
        elif kind == "code":
            out.append(f"<code>{esc(node.get('text', ''))}</code>")
        elif kind == "cite":
            out.append(f'<sup class="cite">[{int(node.get("n", 0))}]</sup>')
        elif kind == "link":
            href = safe_url(str(node.get("href", "")))
            label = esc(node.get("text", ""))
            if href:
                out.append(
                    f'<a href="{esc(href)}" target="_blank" rel="noopener noreferrer">{label}</a>'
                )
            else:
                out.append(label)
        elif kind == "image":
            src = safe_url(str(node.get("src", "")))
            alt = esc(node.get("alt", ""))
            if src:
                out.append(f'<img src="{esc(src)}" alt="{alt}" loading="lazy">')
            else:
                out.append(alt)
    return "".join(out)


def _render_table(block: dict[str, Any]) -> str:
    aligns: list[str | None] = list(block.get("align") or [])

    def cell(tag: str, text: str, idx: int) -> str:
        align = aligns[idx] if idx < len(aligns) else None
        style = f' style="text-align:{align}"' if align else ""
        return f"<{tag}{style}>{render_inlines(parse_inlines(text))}</{tag}>"

    head = "".join(cell("th", h, i) for i, h in enumerate(block.get("header") or []))
    body_rows = []
    for row in block.get("rows") or []:
        body_rows.append("".join(cell("td", c, i) for i, c in enumerate(row)))
    body = "".join(f"<tr>{r}</tr>" for r in body_rows)
    return (
        '<table class="rt-table">'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table>"
    )


def render_blocks(blocks: list[dict[str, Any]]) -> str:
    """结构化 blocks → 安全 HTML。"""
    out: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "heading":
            level = min(max(int(block.get("level", 1)), 1), 6)
            out.append(f"<h{level}>{render_inlines(block.get('inlines') or [])}</h{level}>")
        elif kind == "paragraph":
            out.append(f"<p>{render_inlines(block.get('inlines') or [])}</p>")
        elif kind == "list":
            tag = "ol" if block.get("ordered") else "ul"
            items = "".join(
                f"<li>{render_inlines(item.get('inlines') or [])}</li>"
                for item in block.get("items") or []
            )
            out.append(f'<{tag} class="rt-list">{items}</{tag}>')
        elif kind == "code":
            lang = esc(block.get("lang", "") or "")
            cls = f' class="language-{lang}"' if lang else ""
            out.append(f"<pre class=\"rt-code\"><code{cls}>{esc(block.get('text', ''))}</code></pre>")
        elif kind == "quote":
            out.append(
                f"<blockquote class=\"rt-quote\">{render_inlines(block.get('inlines') or [])}</blockquote>"
            )
        elif kind == "table":
            out.append(_render_table(block))
        elif kind == "image":
            src = safe_url(str(block.get("src", "")))
            alt = esc(block.get("alt", ""))
            if src:
                caption = f"<figcaption>{alt}</figcaption>" if alt else ""
                out.append(
                    f'<figure class="rt-figure"><img src="{esc(src)}" alt="{alt}" '
                    f'loading="lazy">{caption}</figure>'
                )
            else:
                out.append(f"<p>{alt}</p>")
        elif kind == "hr":
            out.append('<hr class="rt-hr">')
    return "".join(out)


def render_html(text: str | None) -> str:
    """Markdown 子集 → 安全 HTML（``parse_blocks`` + ``render_blocks``）。"""
    return render_blocks(parse_blocks(text))


def to_text(text: str | None) -> str:
    """Markdown 子集 → 纯文本（去掉标记，保留引用编号与图片说明）。"""
    parts: list[str] = []
    for block in parse_blocks(text):
        kind = block.get("type")
        if kind in ("heading", "paragraph", "quote"):
            parts.append(inlines_to_text(block.get("inlines") or []))
        elif kind == "code":
            parts.append(str(block.get("text", "")))
        elif kind == "list":
            for item in block.get("items") or []:
                parts.append(inlines_to_text(item.get("inlines") or []))
        elif kind == "table":
            parts.append(" ".join(block.get("header") or []))
            for row in block.get("rows") or []:
                parts.append(" ".join(str(c) for c in row))
        elif kind == "image":
            parts.append(str(block.get("alt", "")))
    return "\n".join(p for p in parts if p)
