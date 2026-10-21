"""可插拔 OCR 层（任务 1.10）。

**本地优先、零系统依赖优先**：

* ``rapidocr``（默认）—— RapidOCR + onnxruntime，**模型随 wheel 分发**，
  不需要装任何系统软件，也无需联网下载；中文识别开箱可用；
* ``tesseract`` —— ``pytesseract`` + tesseract 可执行文件（需自行安装，
  中文还需 ``chi_sim`` 语言包）；
* ``none`` —— 显式关闭 OCR。

``OCR_ENGINE=auto``（默认）按 ``rapidocr → tesseract`` 顺序探测；
两者都不可用时返回 ``ok=False`` 的**降级结果**并给出可执行的安装提示，
绝不抛异常拖垮入库链路（图片型文档仍可入库，只是没有文字）。

用法::

    from app.core.parser import ocr

    ocr.available_engines()        # ['rapidocr']
    ocr.engine_name()              # 'rapidocr'
    result = ocr.ocr_image(data)   # OcrResult(ok=True, text='...', lines=[...])
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

ENGINE_AUTO = "auto"
ENGINE_RAPIDOCR = "rapidocr"
ENGINE_TESSERACT = "tesseract"
ENGINE_NONE = "none"

#: 自动探测顺序（前者优先）
_AUTO_ORDER: tuple[str, ...] = (ENGINE_RAPIDOCR, ENGINE_TESSERACT)

RAPIDOCR_MISSING_HINT = (
    "未安装 OCR 引擎。推荐（纯 pip、模型内置、无需系统软件）："
    "pip install rapidocr-onnxruntime；"
    "或安装 tesseract：winget install UB-Mannheim.TesseractOCR（中文另需 chi_sim 语言包）"
    "并在 .env 设 TESSERACT_BIN=<tesseract.exe 路径>"
)


# ------------------------------------------------------------------ 数据结构
@dataclass
class OcrLine:
    """一行识别结果。``box`` 为四点多边形 ``[[x,y], ...]``（左上起顺时针）。"""

    text: str
    score: float = 0.0
    box: list[list[float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"text": self.text, "score": self.score, "box": self.box}


@dataclass
class OcrResult:
    ok: bool
    engine: str = ENGINE_NONE
    text: str = ""
    lines: list[OcrLine] = field(default_factory=list)
    elapsed: float = 0.0
    error: str | None = None

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "text": self.text,
            "lineCount": self.line_count,
            "elapsed": round(self.elapsed, 3),
            "error": self.error,
        }


# ------------------------------------------------------------------ 引擎探测
_ENGINE_LOCK = threading.Lock()
_INSTANCES: dict[str, object] = {}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # 父包缺失 / 路径异常
        return False


def tesseract_bin() -> str | None:
    """tesseract 可执行文件路径（``TESSERACT_BIN`` 优先，其次 PATH）。"""
    configured = (settings.tesseract_bin or "").strip()
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("tesseract")


def rapidocr_available() -> bool:
    return _module_available("rapidocr_onnxruntime") or _module_available("rapidocr")


def tesseract_available() -> bool:
    return _module_available("pytesseract") and tesseract_bin() is not None


def available_engines() -> list[str]:
    """当前环境**实际可用**的引擎（按自动探测顺序）。"""
    out = []
    if rapidocr_available():
        out.append(ENGINE_RAPIDOCR)
    if tesseract_available():
        out.append(ENGINE_TESSERACT)
    return out


def resolve_engine() -> str:
    """把 ``OCR_ENGINE`` 配置解析为实际引擎名（不可用时返回 ``none``）。"""
    configured = (settings.ocr_engine or ENGINE_AUTO).strip().lower()
    if configured == ENGINE_NONE:
        return ENGINE_NONE
    if configured in (ENGINE_RAPIDOCR, ENGINE_TESSERACT):
        ok = rapidocr_available() if configured == ENGINE_RAPIDOCR else tesseract_available()
        return configured if ok else ENGINE_NONE
    # auto（含无法识别的取值，按 auto 处理并告警）
    if configured != ENGINE_AUTO:
        logger.warning("未知的 OCR_ENGINE=%r，按 auto 处理", configured)
    for name in _AUTO_ORDER:
        if name in available_engines():
            return name
    return ENGINE_NONE


def engine_name() -> str:
    return resolve_engine()


def reset_engine_cache() -> None:
    """释放引擎单例（测试与配置变更后调用）。"""
    with _ENGINE_LOCK:
        _INSTANCES.clear()


# ------------------------------------------------------------------ 图像准备
def _load_image(data: bytes):
    """bytes → RGB ``PIL.Image``（超长边按 ``OCR_MAX_SIDE`` 等比缩放）。"""
    from PIL import Image

    img = Image.open(BytesIO(data))
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    limit = max(0, int(settings.ocr_max_side))
    longest = max(img.size) if img.size else 0
    if limit and longest > limit:
        scale = limit / longest
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    return img


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _normalize_box(box) -> list[list[float]]:
    out: list[list[float]] = []
    for point in box or []:
        try:
            out.append([float(point[0]), float(point[1])])
        except (TypeError, IndexError, ValueError):
            continue
    return out


def lines_to_text(lines: list[OcrLine]) -> str:
    """按识别顺序拼成文本（每行一条，保留原始换行语义）。"""
    return "\n".join(x.text for x in lines if x.text)


# ------------------------------------------------------------------ 引擎实现
def _rapidocr_instance():
    with _ENGINE_LOCK:
        inst = _INSTANCES.get(ENGINE_RAPIDOCR)
        if inst is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:  # v2 起的包名
                from rapidocr import RapidOCR  # type: ignore[no-redef]
            inst = RapidOCR()
            _INSTANCES[ENGINE_RAPIDOCR] = inst
        return inst


def _run_rapidocr(img) -> list[OcrLine]:
    import numpy as np

    engine = _rapidocr_instance()
    result = engine(np.asarray(img))
    raw = result[0] if isinstance(result, tuple) else result
    lines: list[OcrLine] = []
    for item in raw or []:
        if len(item) < 2:
            continue
        text = str(item[1]).strip()
        if not text:
            continue
        lines.append(
            OcrLine(
                text=text,
                score=_to_float(item[2] if len(item) > 2 else 0.0),
                box=_normalize_box(item[0]),
            )
        )
    return lines


def _run_tesseract(img) -> list[OcrLine]:
    import pytesseract

    binary = tesseract_bin()
    if binary:
        pytesseract.pytesseract.tesseract_cmd = binary
    data = pytesseract.image_to_data(
        img, lang=settings.ocr_lang or "eng", output_type=pytesseract.Output.DICT
    )
    lines: list[OcrLine] = []
    # image_to_data 是**词**级输出，按 (block, par, line) 归并成行
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for i in range(len(data.get("text", []))):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        buckets.setdefault(key, []).append(i)
    for key in sorted(buckets):
        idxs = buckets[key]
        words = [(data["text"][i] or "").strip() for i in idxs]
        confs = [_to_float(data["conf"][i]) for i in idxs if _to_float(data["conf"][i]) >= 0]
        left = min(data["left"][i] for i in idxs)
        top = min(data["top"][i] for i in idxs)
        right = max(data["left"][i] + data["width"][i] for i in idxs)
        bottom = max(data["top"][i] + data["height"][i] for i in idxs)
        lines.append(
            OcrLine(
                text=" ".join(words),
                score=(sum(confs) / len(confs) / 100.0) if confs else 0.0,
                box=[[left, top], [right, top], [right, bottom], [left, bottom]],
            )
        )
    return lines


# ------------------------------------------------------------------ 入口
def ocr_image(data: bytes) -> OcrResult:
    """对图片字节做 OCR。

    :return: :class:`OcrResult`。**引擎不可用/识别失败都返回 ``ok=False``**，
             由调用方决定降级策略（绝不抛异常）。
    """
    engine = resolve_engine()
    if engine == ENGINE_NONE:
        configured = (settings.ocr_engine or ENGINE_AUTO).strip().lower()
        if configured == ENGINE_NONE:
            return OcrResult(ok=False, engine=ENGINE_NONE, error="OCR 已按配置关闭（OCR_ENGINE=none）")
        return OcrResult(ok=False, engine=ENGINE_NONE, error=RAPIDOCR_MISSING_HINT)

    started = time.perf_counter()
    try:
        img = _load_image(data)
    except Exception as exc:  # noqa: BLE001 - 损坏/非图片要以业务错误返回
        return OcrResult(ok=False, engine=engine, error=f"图片无法解码：{exc}")

    try:
        lines = _run_rapidocr(img) if engine == ENGINE_RAPIDOCR else _run_tesseract(img)
    except Exception as exc:  # noqa: BLE001 - 引擎异常不得拖垮入库
        logger.warning("OCR 失败（engine=%s）：%s", engine, exc)
        return OcrResult(ok=False, engine=engine, error=f"OCR 识别失败：{exc}")

    return OcrResult(
        ok=True,
        engine=engine,
        text=lines_to_text(lines),
        lines=lines,
        elapsed=time.perf_counter() - started,
    )


def ocr_file(path: str | Path) -> OcrResult:
    try:
        return ocr_image(Path(path).read_bytes())
    except OSError as exc:
        return OcrResult(ok=False, error=f"图片读取失败：{exc}")


def stats() -> dict:
    """引擎状态（供 ``/api/health`` 与验收工具展示）。"""
    return {
        "configured": (settings.ocr_engine or ENGINE_AUTO).strip().lower(),
        "engine": resolve_engine(),
        "available": available_engines(),
        "lang": settings.ocr_lang,
        "tesseractBin": tesseract_bin() or "",
        "maxSide": settings.ocr_max_side,
    }


__all__ = [
    "ENGINE_AUTO",
    "ENGINE_NONE",
    "ENGINE_RAPIDOCR",
    "ENGINE_TESSERACT",
    "RAPIDOCR_MISSING_HINT",
    "OcrLine",
    "OcrResult",
    "available_engines",
    "engine_name",
    "lines_to_text",
    "ocr_file",
    "ocr_image",
    "rapidocr_available",
    "reset_engine_cache",
    "resolve_engine",
    "stats",
    "tesseract_available",
    "tesseract_bin",
]
