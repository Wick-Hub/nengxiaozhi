"""向量层测试（任务 1.4）。

分两类：
    - 与模型无关：维度、L2 归一化、确定性、空串、批次一致性
      （哈希降级后端同样满足，故任何环境都应通过）
    - 依赖真实模型：语义区分度、与 Node 版数值一致性
      （模型缺失时 skip，并明确原因）
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.embedder import Embedder, get_embedder, reset_embedder


@pytest.fixture(scope="module")
def emb() -> Embedder:
    e = Embedder()
    e.load()
    return e


def test_dim(emb: Embedder) -> None:
    vecs = emb.encode(["一卡通丢失了怎么办", "食堂菜谱"])
    assert len(vecs) == 2
    assert all(len(v) == 512 for v in vecs)


def test_l2_normalized(emb: Embedder) -> None:
    vecs = np.asarray(emb.encode(["校园网账号开通", "宿舍报修"]), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_deterministic(emb: Embedder) -> None:
    a = emb.encode_one("学生证补办流程")
    b = emb.encode_one("学生证补办流程")
    assert a == b


def test_batch_size_invariance(emb: Embedder) -> None:
    """不同 batch_size 不应改变结果（padding 不参与池化）。"""
    texts = ["一卡通", "校园网", "宿舍报修", "图书馆开放时间"]
    one = np.asarray(emb.encode(texts, batch_size=1), dtype=np.float32)
    many = np.asarray(emb.encode(texts, batch_size=8), dtype=np.float32)
    assert np.allclose(one, many, atol=1e-6)


def test_empty_and_blank(emb: Embedder) -> None:
    vecs = emb.encode(["", "   "])
    assert len(vecs) == 2
    assert all(len(v) == 512 for v in vecs)


def test_empty_input(emb: Embedder) -> None:
    assert emb.encode([]) == []


def test_status_shape(emb: Embedder) -> None:
    st = emb.status()
    assert st["dim"] == 512
    assert st["backend"] in {"onnx", "hash"}
    assert isinstance(st["is_fallback"], bool)


def test_singleton() -> None:
    reset_embedder()
    assert get_embedder() is get_embedder()
    reset_embedder()


# ---------------------------------------------------------------- 依赖真实模型

def test_semantic_separation(emb: Embedder) -> None:
    if emb.is_fallback:
        pytest.skip("嵌入模型不可用（哈希降级），跳过语义验证")
    v = np.asarray(
        emb.encode(["一卡通丢失了怎么办", "校园卡丢了如何补办", "食堂今天中午的菜谱"]),
        dtype=np.float32,
    )
    same = float(v[0] @ v[1])
    other = float(v[0] @ v[2])
    assert same > other, f"同义相似度应高于无关：{same:.4f} vs {other:.4f}"
    assert same > 0.5
    assert other < 0.5


def test_parity_with_node(emb: Embedder) -> None:
    """与 Node 原型（transformers.js）逐值一致。"""
    if emb.is_fallback:
        pytest.skip("嵌入模型不可用（哈希降级），跳过一致性验证")

    import json
    import os
    import subprocess
    from pathlib import Path

    here = Path(__file__).resolve().parent.parent / "tools" / "parity"
    node_bin = os.environ.get("NODE_BIN", "node")
    kb_engine = here.parent.parent.parent / "kb-engine"
    try:
        proc = subprocess.run(
            [node_bin, str(here / "node_embed_probe.mjs")],
            cwd=str(kb_engine),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"Node 侧不可用：{exc}")

    text = (proc.stdout or "").strip()
    start = text.find("[")
    if proc.returncode != 0 or start < 0:
        pytest.skip(f"Node 侧未产出向量：{(proc.stderr or '')[:300]}")

    node = np.asarray(json.loads(text[start:]), dtype=np.float32)
    texts = json.loads((here / "embed_texts.json").read_text(encoding="utf-8"))
    py = np.asarray(emb.encode(texts, batch_size=8), dtype=np.float32)

    assert node.shape == py.shape
    cos = np.sum(node * py, axis=1)
    assert np.all(cos >= 0.9999), f"余弦相似度过低：{cos}"
    assert np.max(np.abs(node - py)) <= 1e-4
