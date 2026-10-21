"""Office 旧格式转换层（任务 1.10）。

``.doc`` / ``.ppt`` 是二进制旧格式，纯 Python 库无法解析，需要外部转换器：

* **LibreOffice**（推荐）—— ``soffice --headless --convert-to docx|pptx``，
  转换后用 python-docx / python-pptx 走正常解析链路（保留标题层级与表格结构）；
* 兜底 CLI —— ``antiword`` / ``catdoc`` 抽取 ``.doc`` 的**纯文本**（无结构）。

定位顺序：``SOFFICE_BIN`` 配置 → ``PATH`` → 常见安装路径（Windows / macOS / Linux）。

找不到转换器时抛 :class:`ConverterUnavailable`，由解析层转成**可执行的业务提示**
（而不是 500 或静默空文档）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

#: 常见安装位置（按平台）
_SOFFICE_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/snap/bin/libreoffice",
)

#: ``.doc`` 纯文本兜底工具（按优先级）
_DOC_TEXT_CLI: tuple[str, ...] = ("antiword", "catdoc")

SOFFICE_MISSING_HINT = (
    "未找到 LibreOffice（soffice），无法转换 .doc/.ppt。"
    "请安装 LibreOffice 并在 .env 设置 SOFFICE_BIN=<soffice 可执行文件路径>；"
    "或把文件另存为 .docx/.pptx 后上传"
)


class ConvertError(RuntimeError):
    """转换失败（格式损坏 / 超时 / 输出缺失）。"""


class ConverterUnavailable(ConvertError):
    """转换器不可用（未安装）。"""


def find_soffice() -> str | None:
    """定位 soffice 可执行文件。"""
    configured = (settings.soffice_bin or "").strip()
    if configured and Path(configured).is_file():
        return configured
    for name in ("soffice", "soffice.com", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _SOFFICE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def soffice_available() -> bool:
    return find_soffice() is not None


def find_doc_text_cli() -> str | None:
    for name in _DOC_TEXT_CLI:
        found = shutil.which(name)
        if found:
            return found
    return None


def convert_bytes(data: bytes, src_ext: str, target_ext: str) -> bytes:
    """用 LibreOffice 把 ``.doc/.ppt`` 转成 ``.docx/.pptx`` 并返回字节。

    :raises ConverterUnavailable: 未安装 LibreOffice
    :raises ConvertError: 转换超时 / 未产出目标文件
    """
    binary = find_soffice()
    if not binary:
        raise ConverterUnavailable(SOFFICE_MISSING_HINT)

    src_ext = src_ext if src_ext.startswith(".") else f".{src_ext}"
    target_ext = target_ext if target_ext.startswith(".") else f".{target_ext}"
    fmt = target_ext.lstrip(".")

    with tempfile.TemporaryDirectory(prefix="kb-convert-") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / f"input{src_ext}"
        src.write_bytes(data)
        out_dir = tmp_dir / "out"
        out_dir.mkdir()
        profile = tmp_dir / "profile"

        cmd = [
            binary,
            "--headless",
            "--norestore",
            "--invisible",
            "--nodefault",
            "--nolockcheck",
            f"-env:UserInstallation=file:///{profile.as_posix().lstrip('/')}",
            "--convert-to",
            fmt,
            "--outdir",
            str(out_dir),
            str(src),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.soffice_timeout,
                cwd=str(tmp_dir),
            )
        except subprocess.TimeoutExpired as exc:
            raise ConvertError(
                f"LibreOffice 转换超时（>{settings.soffice_timeout:.0f}s）：{src_ext} → {target_ext}"
            ) from exc
        except OSError as exc:
            raise ConvertError(f"LibreOffice 无法启动（{binary}）：{exc}") from exc

        produced = sorted(out_dir.glob(f"*.{fmt}"))
        if not produced:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise ConvertError(
                f"LibreOffice 未产出 {target_ext}（退出码 {proc.returncode}）：{detail or '无输出'}"
            )
        logger.info("已转换 %s → %s（%d 字节）", src_ext, target_ext, produced[0].stat().st_size)
        return produced[0].read_bytes()


def doc_text_via_cli(data: bytes) -> str | None:
    """``antiword`` / ``catdoc`` 兜底抽取 ``.doc`` 纯文本；不可用返回 ``None``。"""
    binary = find_doc_text_cli()
    if not binary:
        return None
    with tempfile.TemporaryDirectory(prefix="kb-doc-") as tmp:
        src = Path(tmp) / "input.doc"
        src.write_bytes(data)
        cmd = [binary, str(src)]
        if "catdoc" in Path(binary).name:
            cmd = [binary, "-d", "utf-8", str(src)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=settings.soffice_timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("%s 抽取失败：%s", binary, exc)
            return None
        if proc.returncode != 0 or not proc.stdout:
            logger.warning("%s 退出码 %s，无可用输出", binary, proc.returncode)
            return None
        for enc in ("utf-8", "gb18030"):
            try:
                return proc.stdout.decode(enc)
            except UnicodeDecodeError:
                continue
        return proc.stdout.decode("utf-8", errors="replace")


def stats() -> dict:
    """转换器可用性（供 ``/api/health`` 与验收工具展示）。"""
    return {
        "soffice": find_soffice() or "",
        "sofficeAvailable": soffice_available(),
        "docTextCli": find_doc_text_cli() or "",
        "timeout": settings.soffice_timeout,
    }


__all__ = [
    "SOFFICE_MISSING_HINT",
    "ConvertError",
    "ConverterUnavailable",
    "convert_bytes",
    "doc_text_via_cli",
    "find_doc_text_cli",
    "find_soffice",
    "soffice_available",
    "stats",
]
