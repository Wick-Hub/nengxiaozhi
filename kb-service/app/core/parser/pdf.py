"""PDF 解析（对齐 kb-engine/src/parse/pdf.js）。

仅抽取文本层；扫描件（图片型 PDF）需 OCR，见任务 1.10。
"""

from __future__ import annotations

import re
from io import BytesIO

from app.core.parser.block import Block


def pdf_to_blocks(data: bytes) -> list[Block]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)

    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        t = " ".join(para).strip()
        if t:
            blocks.append(Block(type="para", text=t))
        para.clear()

    for line in re.split(r"\r?\n", text):
        t = line.strip()
        if not t:
            flush()
            continue
        para.append(t)
    flush()
    return blocks
