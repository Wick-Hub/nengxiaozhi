"""模型目录解析（任务 1.4 / 1.6 共用）。

统一嵌入模型与精排模型的查找顺序，便于离线交付：

    显式目录 → 环境变量目录 → 服务自带 models/<name> → Node 原型缓存 → HF 镜像下载

Node 原型的 transformers.js 缓存位于
``kb-engine/node_modules/@huggingface/transformers/.cache/<org>/<model>/``，
可直接复用，无需二次下载。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import settings


def _node_cache_dir() -> Path:
    return (
        settings.base_dir.parent
        / "kb-engine"
        / "node_modules"
        / "@huggingface"
        / "transformers"
        / ".cache"
    )


def candidate_dirs(
    model_name: str,
    override: str | Path | None = None,
    extra_env: tuple[str, ...] = (),
) -> list[Path]:
    """按优先级列出可能的模型目录（去重且保序）。"""
    short = model_name.split("/")[-1]
    cands: list[Path] = []

    if override:
        cands.append(Path(override))
    for var in ("KB_MODEL_DIR", *extra_env):
        val = os.environ.get(var)
        if val:
            cands.append(Path(val))

    cands.append(settings.model_dir / short)
    cands.append(settings.model_dir / model_name)

    cache = _node_cache_dir()
    cands.append(cache / model_name)
    cands.append(cache / short)

    seen: set[str] = set()
    uniq: list[Path] = []
    for c in cands:
        key = str(c)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def onnx_file(d: Path, prefer_quantized: bool = False) -> Path | None:
    """定位目录下的 ONNX 权重文件。

    :param prefer_quantized: 精排模型与 Node 版 dtype=q8 对齐，优先取量化版；
        嵌入模型默认优先非量化版（与 Node 版 fp32 一致）。
    """
    onnx_dir = d / "onnx"
    if not onnx_dir.is_dir():
        return None
    files = sorted(onnx_dir.glob("*.onnx"))
    if not files:
        return None
    quant = [f for f in files if "quantized" in f.name]
    plain = [f for f in files if "quantized" not in f.name]
    if prefer_quantized:
        return (quant or plain)[0]
    return (plain or quant)[0]


def valid_dir(d: Path, prefer_quantized: bool = False) -> bool:
    """目录是否包含可用的 tokenizer 与 ONNX 权重。"""
    return d.is_dir() and (d / "tokenizer.json").is_file() and onnx_file(d, prefer_quantized) is not None
