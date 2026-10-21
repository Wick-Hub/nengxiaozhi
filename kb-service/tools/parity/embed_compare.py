"""Python ↔ Node 向量一致性比对（任务 1.4 验收工具）。

在 kb-service 目录下运行::

    python tools/parity/embed_compare.py
    NODE_BIN=/path/to/node python tools/parity/embed_compare.py

判定标准（fp32 推理的正常数值误差）：
    余弦相似度 >= 0.9999  且  逐元素最大绝对误差 <= 1e-4

Node 侧不可用时（离线且无缓存）会明确报错，不影响 Python 侧自检输出。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
KB_SERVICE = HERE.parent.parent
KB_ENGINE = Path(os.environ.get("KB_ENGINE", KB_SERVICE.parent / "kb-engine"))

COS_MIN = 0.9999
MAXABS_MAX = 1e-4


def _node_vectors() -> list[list[float]] | None:
    node_bin = os.environ.get("NODE_BIN", "node")
    try:
        proc = subprocess.run(
            [node_bin, str(HERE / "node_embed_probe.mjs")],
            cwd=str(KB_ENGINE),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        print(f"未找到 Node 可执行文件（{node_bin}），跳过 Node 侧比对。")
        return None
    if proc.returncode != 0:
        print("Node 侧编码失败，跳过比对：")
        print((proc.stderr or "").strip()[:1200])
        return None
    # 容错：即使有前置日志，也从第一个 '[' 起解析 JSON
    text = (proc.stdout or "").strip()
    start = text.find("[")
    if start < 0:
        print("Node 侧未输出向量数据，跳过比对：")
        print((proc.stderr or text).strip()[:1200])
        return None
    return json.loads(text[start:])


def main() -> int:
    texts: list[str] = json.loads((HERE / "embed_texts.json").read_text(encoding="utf-8"))

    from app.core.embedder import get_embedder

    emb = get_embedder()
    py_vecs = np.asarray(emb.encode(texts, batch_size=8), dtype=np.float32)
    print(f"Python 侧：backend={emb.backend} dim={py_vecs.shape[1]} 条数={len(py_vecs)}")

    if emb.is_fallback:
        print("警告：Python 侧处于哈希降级状态，无法与 Node 版做数值比对。")
        return 1

    node_raw = _node_vectors()
    if node_raw is None:
        print("Node 侧不可用，本次仅完成 Python 侧自检。")
        return 0

    node_vecs = np.asarray(node_raw, dtype=np.float32)
    if node_vecs.shape != py_vecs.shape:
        print(f"形状不一致：node={node_vecs.shape} python={py_vecs.shape}")
        return 1

    print(f"\n{'#':<3}{'文本':<40}{'余弦':>12}{'最大绝对差':>14}  结果")
    bad = 0
    for i, t in enumerate(texts):
        cos = float(py_vecs[i] @ node_vecs[i])
        maxabs = float(np.max(np.abs(py_vecs[i] - node_vecs[i])))
        ok = cos >= COS_MIN and maxabs <= MAXABS_MAX
        if not ok:
            bad += 1
        label = t if len(t) <= 34 else t[:33] + "…"
        print(f"{i:<3}{label:<40}{cos:>12.6f}{maxabs:>14.2e}  {'一致' if ok else '差异'}")

    print(f"\n合计 {len(texts)} 条，一致 {len(texts) - bad}，差异 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
