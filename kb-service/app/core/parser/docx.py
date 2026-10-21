"""DOCX 解析（对齐 kb-engine/src/parse/docx.js）。

原型用 mammoth 转 HTML 再遍历；Python 用 python-docx 按文档顺序遍历段落与表格。

任务 1.10 追加：``image_sink`` 非空时把**内嵌图片**落盘，并把对象键写进
``Block.meta["image_key"]``。**块的数量与文本不受影响**——不传 ``image_sink`` 时
行为与原型完全一致，parity 回归工具走的正是这条路径。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from io import BytesIO

from app.core.parser.block import Block
from app.core.parser.markdown import to_md_table

_HEADING_STYLE_RE = re.compile(r"^(?:Heading|标题)\s*(\d)")

ImageSink = Callable[[bytes, str], str]


def _heading_level(style: str) -> int | None:
    m = _HEADING_STYLE_RE.match(style or "")
    return int(m.group(1)) if m else None


def _is_list_style(style: str) -> bool:
    s = (style or "").lower()
    return s.startswith("list") or (style or "").startswith("列表")


def _extract_images(paragraph, image_sink: ImageSink) -> list[str]:
    """抽取段落里的内嵌图片，返回对象键列表（失败静默跳过，不影响整篇）。"""
    keys: list[str] = []
    try:
        from docx.oxml.ns import qn
    except ImportError:  # pragma: no cover
        return keys
    for blip in paragraph._p.findall(".//" + qn("a:blip")):
        rid = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
        if not rid:
            continue
        try:
            part = paragraph.part.related_parts[rid]
            blob = part.blob
            ext = "." + str(part.partname).rsplit(".", 1)[-1].lower()
            keys.append(image_sink(blob, ext))
        except Exception:  # noqa: BLE001 - 单张图失败不拖垮整篇
            continue
    return keys


def docx_to_blocks(data: bytes, *, image_sink: ImageSink | None = None) -> list[Block]:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(BytesIO(data))
    blocks: list[Block] = []
    list_buf: list[str] = []

    def flush_list() -> None:
        if list_buf:
            blocks.append(Block(type="para", text="\n".join(list_buf)))
            list_buf.clear()

    for child in doc.element.body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            para = Paragraph(child, doc)
            text = (para.text or "").strip()
            try:
                style = para.style.name or ""
            except Exception:
                style = ""
            level = _heading_level(style)
            has_img = bool(child.findall(".//" + qn("w:drawing"))) or bool(
                child.findall(".//" + qn("w:pict"))
            )
            if level is not None:
                flush_list()
                if text:
                    blocks.append(Block(type="heading", level=level, text=text))
            elif _is_list_style(style):
                if text:
                    list_buf.append("• " + text)
            else:
                flush_list()
                if has_img:
                    meta: dict = {}
                    if image_sink is not None:
                        keys = _extract_images(para, image_sink)
                        if keys:
                            meta["image_key"] = keys[0]
                            meta["image_keys"] = keys
                    blocks.append(Block(type="image", text=text or "文档内嵌图片", meta=meta))
                elif text:
                    blocks.append(Block(type="para", text=text))
        elif tag == qn("w:tbl"):
            flush_list()
            table = Table(child, doc)
            rows: list[list[str]] = []
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if cells:
                    rows.append(cells)
            if rows:
                blocks.append(Block(type="table", text=to_md_table(rows), rows=rows))

    flush_list()
    return blocks
