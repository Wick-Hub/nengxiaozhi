"""图片解析（任务 1.10）：图片 → OCR 文本 + 原图引用。

两块职责：

* :func:`save_original` —— 按**内容哈希**把原图落盘到 ``UPLOAD_DIR/original/``，
  返回 ``file_key`` 风格的对象键（``uploads/original/<hash16>.<ext>``）。
  内容寻址天然去重，且同一张图在多个文档里只存一份。
* :func:`image_to_blocks` —— 图片 → 解析块（标题 + ``[图片] 文件名`` + OCR 文本）。
  OCR 不可用时**不抛错**：仍产出图片块，另加一条显式说明，并把原因放进 meta。

    .. note::
       原型 ``kb-engine`` **没有**图片/OCR 支持（``SUPPORTED`` 只有 8 种格式），
       本模块是任务 1.10 的**新增能力**，无 Node 侧可回归。
"""

from __future__ import annotations

import hashlib
import logging
from io import BytesIO
from pathlib import Path

from app.config import settings
from app.core.parser.block import Block
from app.core.parser.ocr import OcrResult, ocr_image

logger = logging.getLogger(__name__)

#: 图片扩展名（第 12 类格式：png/jpg/jpeg 视为一类）
IMAGE_EXT: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"})

#: 原图子目录（相对 ``UPLOAD_DIR``）
ORIGINAL_SUBDIR = "original"

#: 原图对象键前缀（与 ``kb_admin`` 的 ``uploads/…`` 口径一致，供原文/原图接口复用）
ORIGINAL_KEY_PREFIX = f"uploads/{ORIGINAL_SUBDIR}/"

_EXT_ALIAS = {".jpe": ".jpg", ".tif": ".tiff"}


def normalize_ext(ext: str) -> str:
    ext = (ext or "").strip().lower()
    if not ext:
        return ".bin"
    if not ext.startswith("."):
        ext = f".{ext}"
    return _EXT_ALIAS.get(ext, ext)


def original_dir() -> Path:
    return Path(settings.upload_dir) / ORIGINAL_SUBDIR


def original_key(name: str) -> str:
    return f"{ORIGINAL_KEY_PREFIX}{name}"


def save_original(data: bytes, ext: str) -> str:
    """按内容哈希落盘原图，返回对象键。已存在则直接复用（幂等）。"""
    ext = normalize_ext(ext)
    digest = hashlib.sha256(data).hexdigest()
    name = f"{digest[:16]}{ext}"
    target = original_dir() / name
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：并发上传同一张图时不会读到半截内容
        tmp = target.with_name(f"{name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    return original_key(name)


def image_meta(data: bytes) -> dict:
    """图片基础元信息（宽/高/格式/模式）；解码失败返回 ``{"error": ...}``。"""
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            return {
                "width": img.width,
                "height": img.height,
                "format": (img.format or "").lower(),
                "mode": img.mode,
            }
    except Exception as exc:  # noqa: BLE001 - 非图片/损坏图片要给业务提示
        return {"error": f"图片无法解码：{exc}"}


def image_to_blocks(
    data: bytes,
    *,
    source_name: str = "",
    image_key: str | None = None,
    ocr_result: OcrResult | None = None,
) -> tuple[list[Block], dict]:
    """图片 → 解析块。

    :param image_key: 原图对象键；为空时自动 :func:`save_original`
    :param ocr_result: 可注入的 OCR 结果（测试用；为空时真实调用 OCR）

    :return: ``(blocks, meta)``，``meta`` 含宽高、OCR 引擎、行数与告警
    """
    name = Path(source_name or "image").name
    stem = Path(name).stem or "图片"
    key = image_key or save_original(data, Path(name).suffix)
    info = image_meta(data)

    blocks: list[Block] = [Block(type="heading", level=1, text=stem)]
    blocks.append(
        Block(
            type="image",
            text=name,
            meta={"image_key": key, "image_name": name, **{k: v for k, v in info.items() if k != "error"}},
        )
    )

    result = ocr_result if ocr_result is not None else ocr_image(data)
    meta: dict = {
        "imageKey": key,
        "imageName": name,
        "ocrOk": result.ok,
        "ocrEngine": result.engine,
        "ocrLineCount": result.line_count,
        "ocrElapsed": round(result.elapsed, 3),
        **{k: v for k, v in info.items()},
    }

    if result.ok and result.text.strip():
        blocks.append(
            Block(
                type="para",
                text=result.text.strip(),
                meta={"ocr": True, "ocr_engine": result.engine, "image_key": key},
            )
        )
    else:
        reason = result.error or "未识别到文字"
        meta["warning"] = f"图片「{name}」未提取到文字：{reason}"
        blocks.append(
            Block(
                type="para",
                text=f"（未提取到图片文字：{reason}）",
                meta={"ocr": False, "image_key": key},
            )
        )

    if "error" in info:
        meta["warning"] = f"图片「{name}」{info['error']}"
    return blocks, meta


def stats() -> dict:
    """原图存储状态（验收工具用）。"""
    directory = original_dir()
    files = sorted(p for p in directory.glob("*") if p.is_file()) if directory.is_dir() else []
    return {
        "dir": str(directory),
        "count": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "keyPrefix": ORIGINAL_KEY_PREFIX,
    }


__all__ = [
    "IMAGE_EXT",
    "ORIGINAL_KEY_PREFIX",
    "ORIGINAL_SUBDIR",
    "image_meta",
    "image_to_blocks",
    "normalize_ext",
    "original_dir",
    "original_key",
    "save_original",
    "stats",
]
