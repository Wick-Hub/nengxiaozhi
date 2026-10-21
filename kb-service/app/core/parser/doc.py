"""旧版 Office 二进制格式解析（任务 1.10）：``.doc`` / ``.ppt``。

这两种格式是 OLE 复合文档，纯 Python 库无法直接解析，因此**统一走转换**：

* ``.doc`` → LibreOffice 转 ``.docx`` → :func:`docx_to_blocks`（保留标题层级与表格）；
  转换器不可用时兜底 ``antiword`` / ``catdoc`` 抽**纯文本**（无结构）。
* ``.ppt`` → LibreOffice 转 ``.pptx`` → :func:`pptx_to_blocks`（无纯文本兜底工具）。

转换器都不可用时抛 :class:`~app.core.parser.office_convert.ConverterUnavailable`
（附可执行安装提示），由 :mod:`app.services.kb_admin` 转成业务错误返回给前端。

    .. note::
       原型 ``kb-engine`` 的 ``SUPPORTED`` 只有 8 种格式，本模块是任务 1.10 的
       **新增能力**，无 Node 侧可回归。
"""

from __future__ import annotations

import logging

from app.core.parser import office_convert
from app.core.parser.block import Block
from app.core.parser.docx import docx_to_blocks
from app.core.parser.markdown import md_to_blocks
from app.core.parser.pptx import ImageSink, pptx_to_blocks

logger = logging.getLogger(__name__)


def doc_to_blocks(data: bytes) -> list[Block]:
    """``.doc`` → 解析块（LibreOffice 优先，antiword/catdoc 兜底）。"""
    errors: list[str] = []

    if office_convert.soffice_available():
        try:
            docx_bytes = office_convert.convert_bytes(data, ".doc", ".docx")
            blocks = docx_to_blocks(docx_bytes)
            if blocks:
                return blocks
            errors.append("LibreOffice 转换成功但内容为空")
        except office_convert.ConvertError as exc:
            logger.warning(".doc 经 LibreOffice 转换失败，尝试纯文本兜底：%s", exc)
            errors.append(str(exc))
    else:
        errors.append(office_convert.SOFFICE_MISSING_HINT)

    text = office_convert.doc_text_via_cli(data)
    if text and text.strip():
        logger.info(".doc 已由命令行工具抽取纯文本（%d 字）", len(text))
        return md_to_blocks(text)

    errors.append("未找到 antiword/catdoc，无法抽取 .doc 纯文本")
    raise office_convert.ConverterUnavailable("；".join(errors))


def ppt_to_blocks(data: bytes, *, image_sink: ImageSink | None = None) -> list[Block]:
    """``.ppt`` → 解析块（仅 LibreOffice 一条路径，无纯文本兜底）。"""
    if not office_convert.soffice_available():
        raise office_convert.ConverterUnavailable(office_convert.SOFFICE_MISSING_HINT)

    pptx_bytes = office_convert.convert_bytes(data, ".ppt", ".pptx")
    return pptx_to_blocks(pptx_bytes, image_sink=image_sink)


__all__ = ["doc_to_blocks", "ppt_to_blocks"]
