"""模型准备脚本（任务 1.4 嵌入 / 1.6 精排）。

把模型准备到 ``kb-service/models/<name>/``，使服务可**完全离线**加载。
解析顺序：Node 原型缓存复制（最快）→ HuggingFace 镜像下载。

用法::

    # 嵌入模型（默认）
    python tools/fetch_model.py
    # 精排模型
    python tools/fetch_model.py --model onnx-community/bge-reranker-v2-m3-ONNX

    python tools/fetch_model.py --force              # 已存在也重新准备
    python tools/fetch_model.py --endpoint ""        # 禁用镜像下载（仅允许本地复制）

准备完成后目录形如::

    models/bge-small-zh-v1.5/
    ├─ config.json
    ├─ tokenizer.json
    ├─ tokenizer_config.json
    └─ onnx/model.onnx

    models/bge-reranker-v2-m3-ONNX/
    ├─ config.json
    ├─ tokenizer.json
    ├─ tokenizer_config.json
    └─ onnx/model_quantized.onnx
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SERVICE_DIR.parent

DEFAULT_MODEL = "Xenova/bge-small-zh-v1.5"
DEFAULT_ENDPOINT = "https://hf-mirror.com"

# 必需的非权重文件（相对模型仓库根）
REQUIRED = ["config.json", "tokenizer.json", "tokenizer_config.json"]


def _node_cache_dir(model: str) -> Path:
    return (
        PROJECT_DIR
        / "kb-engine"
        / "node_modules"
        / "@huggingface"
        / "transformers"
        / ".cache"
        / model
    )


def _onnx_files(d: Path) -> list[Path]:
    onnx_dir = d / "onnx"
    return sorted(onnx_dir.glob("*.onnx")) if onnx_dir.is_dir() else []


def _is_ready(dest: Path) -> bool:
    return all((dest / rel).is_file() for rel in REQUIRED) and bool(_onnx_files(dest))


def copy_from_node_cache(model: str, dest: Path) -> bool:
    src = _node_cache_dir(model)
    weights = _onnx_files(src)
    if not all((src / rel).is_file() for rel in REQUIRED) or not weights:
        return False
    for rel in REQUIRED:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target)
    for w in weights:
        target = dest / "onnx" / w.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(w, target)
    return True


def download(model: str, dest: Path, endpoint: str | None) -> bool:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  未安装 huggingface-hub，跳过下载（pip install huggingface-hub）")
        return False
    dest.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            repo_id=model,
            local_dir=str(dest),
            endpoint=endpoint or None,
            allow_patterns=[
                "onnx/*.onnx",
                "tokenizer.json",
                "tokenizer_config.json",
                "config.json",
                "vocab.txt",
                "special_tokens_map.json",
            ],
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  下载失败：{exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="准备 kb-service 嵌入模型")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="HF 模型 id")
    ap.add_argument("--dest", default=None, help="目标目录（默认 models/<short>）")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="HF 镜像；空字符串禁用下载")
    ap.add_argument("--force", action="store_true", help="已存在时也重新准备")
    args = ap.parse_args(argv)

    short = args.model.split("/")[-1]
    dest = Path(args.dest) if args.dest else SERVICE_DIR / "models" / short

    print(f"模型：{args.model}")
    print(f"目标：{dest}")

    if _is_ready(dest) and not args.force:
        print("已就绪，无需处理（--force 可强制重建）。")
        return 0

    if copy_from_node_cache(args.model, dest):
        print("来源：Node 原型缓存（已复制）")
    elif args.endpoint and download(args.model, dest, args.endpoint):
        print(f"来源：{args.endpoint}（已下载）")
    else:
        print("失败：未能从本地缓存复制，也未成功下载。", file=sys.stderr)
        return 1

    if _is_ready(dest):
        size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        print(f"完成：{dest}（{size / 1024 / 1024:.1f} MB）")
        return 0

    print("失败：文件不完整。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
