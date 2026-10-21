"""解析层（任务 1.2 / 1.10）。

统一入口 ``parse_file(path, *, image_sink=None, self_image_key=None)``
→ ``ParsedDocument(meta, blocks)``，行为对齐 ``kb-engine/src/parse/index.js``。

**12 类格式**（任务 1.10 补齐）：

| # | 格式 | 实现 | 备注 |
| --- | --- | --- | --- |
| 1 | ``.md`` | :mod:`frontmatter` + :mod:`markdown` | 含 YAML frontmatter |
| 2 | ``.txt`` | :mod:`markdown` | 按 Markdown 子集切块 |
| 3 | ``.docx`` | :mod:`docx`（python-docx） | 与原型逐块一致 |
| 4 | ``.doc`` | :mod:`doc` | 经 LibreOffice 转 docx；antiword/catdoc 兜底 |
| 5 | ``.pdf`` | :mod:`pdf`（pypdf） | 仅文本层 |
| 6 | ``.xlsx`` | :mod:`xlsx`（openpyxl） | 工作表 → 表格 |
| 7 | ``.xls`` | :mod:`xlsx`（xlrd） | 同上 |
| 8 | ``.csv`` | :mod:`xlsx` | 单个表格 |
| 9 | ``.json`` | :mod:`jsonfile` | 问答对 / 结构化 |
| 10 | ``.pptx`` | :mod:`pptx`（python-pptx） | 逐页标题/正文/表格/图片/备注 |
| 11 | ``.ppt`` | :mod:`doc` | 经 LibreOffice 转 pptx |
| 12 | 图片 | :mod:`image` | png/jpg/jpeg 等 → OCR 文本 + 原图引用 |

``image_sink`` 为 ``(data, ext) -> key`` 回调，用于把**内嵌图片**落盘并拿到对象键；
不传时 docx/pptx **不抽取**内嵌图片（保持与原型一致的块输出，parity 回归走这条路径）。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.parser import image as image_parser
from app.core.parser.block import Block, ParsedDocument
from app.core.parser.doc import doc_to_blocks, ppt_to_blocks
from app.core.parser.docx import docx_to_blocks
from app.core.parser.frontmatter import parse_frontmatter
from app.core.parser.jsonfile import json_to_blocks
from app.core.parser.markdown import md_to_blocks, split_row, to_md_table
from app.core.parser.pdf import pdf_to_blocks
from app.core.parser.pptx import ImageSink, pptx_to_blocks
from app.core.parser.xlsx import csv_to_blocks, xlsx_to_blocks

#: 图片类格式（第 12 类）
IMAGE_EXT = image_parser.IMAGE_EXT

#: 全部受支持的扩展名
SUPPORTED_EXT: frozenset[str] = frozenset(
    {
        ".md",
        ".txt",
        ".docx",
        ".doc",
        ".pdf",
        ".xlsx",
        ".xls",
        ".csv",
        ".json",
        ".pptx",
        ".ppt",
    }
) | IMAGE_EXT

#: 对外宣称的格式类数（图片类归一，供文档与验收工具引用）
FORMAT_COUNT = 12

#: 任务 1.10 已把原先的「待扩展」全部落地，此常量保留为空集以免破坏既有引用
PLANNED_EXT: frozenset[str] = frozenset()

__all__ = [
    "FORMAT_COUNT",
    "IMAGE_EXT",
    "PLANNED_EXT",
    "SUPPORTED_EXT",
    "Block",
    "ImageSink",
    "ParsedDocument",
    "csv_to_blocks",
    "doc_to_blocks",
    "docx_to_blocks",
    "image_parser",
    "is_supported",
    "md_to_blocks",
    "parse_file",
    "ppt_to_blocks",
    "pptx_to_blocks",
    "split_row",
    "to_md_table",
]


def is_supported(path: str | Path) -> bool:
    """扩展名是否受支持。"""
    return Path(path).suffix.lower() in SUPPORTED_EXT


def parse_file(
    path: str | Path,
    *,
    image_sink: ImageSink | None = None,
    self_image_key: str | None = None,
    display_name: str | None = None,
) -> ParsedDocument:
    """按扩展名分发解析，返回 ``(meta, blocks)``。

    :param image_sink: 内嵌图片落盘回调（``None`` 则不抽取，保持原型行为）
    :param self_image_key: 图片型文件**自身**的对象键（上传件为 ``uploads/<safe_name>``）；
        为空时按内容哈希落到 ``uploads/original/``
    :param display_name: 图片型文件的**展示名**（用作标题）。默认取物理文件名；
        内容寻址的原图（``<hash16>.png``）应传文档标题，否则标题会是一串哈希
    :raises ValueError: 不支持的格式
    :raises ConverterUnavailable: ``.doc/.ppt`` 且无可用转换器
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"不支持的格式：{ext}")
    data = p.read_bytes()
    meta: dict = {}
    blocks: list[Block] = []

    if ext == ".md":
        meta, body = parse_frontmatter(data.decode("utf-8"))
        blocks = md_to_blocks(body)
    elif ext == ".txt":
        blocks = md_to_blocks(data.decode("utf-8"))
    elif ext == ".docx":
        blocks = docx_to_blocks(data, image_sink=image_sink)
    elif ext == ".doc":
        blocks = doc_to_blocks(data)
    elif ext == ".pdf":
        blocks = pdf_to_blocks(data)
    elif ext in (".xlsx", ".xls"):
        blocks = xlsx_to_blocks(data, ext)
    elif ext == ".csv":
        blocks = csv_to_blocks(data)
    elif ext == ".json":
        blocks = json_to_blocks(json.loads(data.decode("utf-8")))
    elif ext == ".pptx":
        blocks = pptx_to_blocks(data, image_sink=image_sink)
    elif ext == ".ppt":
        blocks = ppt_to_blocks(data, image_sink=image_sink)
    elif ext in IMAGE_EXT:
        blocks, image_meta = image_parser.image_to_blocks(
            data, source_name=display_name or p.name, image_key=self_image_key
        )
        # 标题交由调用方决定（上传件的 staging 文件名带时间戳前缀，不适合当标题）
        meta = {"image": image_meta}

    return ParsedDocument(meta=meta, blocks=blocks)
