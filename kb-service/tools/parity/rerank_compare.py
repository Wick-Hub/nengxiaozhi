"""Python ↔ Node 精排一致性比对（任务 1.6 验收工具）。

在 kb-service 目录下运行::

    python tools/parity/rerank_compare.py
    NODE_BIN=/path/to/node python tools/parity/rerank_compare.py

**验收口径说明**

精排模型是量化（q8）权重，Node 侧为 onnxruntime-node 1.21.0、Python 侧为
onnxruntime 1.31.0，两者量化算子实现存在差异，logits 绝对值会有 0.5~1.3 的
系统性偏移（已确认 tokenizer 输出逐值一致，差异来自算子而非分词）。因此：

    - 核心验收项：**排序一致性**（top-1 必须一致，统计 top-3 / 全序列一致率）
    - 参考信息：分数绝对差（用于判断差异是否发生在"接近翻转"的位置）

同时校验「候选数 ≤ topK 时跳过精排、原序返回」与原型一致。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KB_SERVICE = HERE.parent.parent
KB_ENGINE = Path(os.environ.get("KB_ENGINE", KB_SERVICE.parent / "kb-engine"))


def _node_results() -> list[dict] | None:
    node_bin = os.environ.get("NODE_BIN", "node")
    try:
        proc = subprocess.run(
            [node_bin, str(HERE / "node_rerank_probe.mjs")],
            cwd=str(KB_ENGINE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=1800,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"Node 侧不可用（{exc}），跳过比对。")
        return None
    if proc.returncode != 0:
        print("Node 侧精排失败：")
        print((proc.stderr or "").strip()[:1500])
        return None
    text = (proc.stdout or "").strip()
    start = text.find("[")
    if start < 0:
        print("Node 侧未输出结果：")
        print((proc.stderr or text).strip()[:1500])
        return None
    return json.loads(text[start:])


def main() -> int:
    cases = json.loads((HERE / "rerank_cases.json").read_text(encoding="utf-8"))["cases"]
    node_raw = _node_results()
    if node_raw is None:
        return 1

    from app.core.chunker import Chunk
    from app.core.reranker import get_reranker

    store_path = Path(os.environ.get("KB_STORE_JSON", KB_ENGINE / "data" / "store.json"))
    store = json.loads(store_path.read_text(encoding="utf-8"))
    all_chunks = [Chunk.from_dict(c) for c in store.get("chunks", [])]

    rr = get_reranker()
    if not rr.load():
        print(f"精排模型不可用：{rr.status()['error']}")
        return 1
    print(f"精排模型：{rr.model_name} | 目录：{rr.status()['model_dir']}")
    print("（分数差异来自量化算子的跨运行时实现，验收以排序一致性为准）\n")

    n_top1 = n_top3 = n_full = 0
    graded = 0
    for case, node in zip(cases, node_raw):
        q, first, top_k = case["query"], case["first"], case["topK"]
        cands = all_chunks[:first]
        py_scores = rr.score(q, [c.text for c in cands])
        py_order = [
            cands[i].chunk_id
            for i in sorted(range(len(cands)), key=lambda i: -py_scores[i])[:top_k]
        ]
        exp = node["after"]
        node_scores = {s["chunkId"]: s["score"] for s in node["scores"]}
        maxdiff = max(abs(py_scores[i] - node_scores[c.chunk_id]) for i, c in enumerate(cands))

        if first <= top_k:
            ok = py_order == exp
            print(f"「{q}」 候选 {first} ≤ topK {top_k} → 应跳过精排")
            print(f"  Node={exp}\n  Py  ={py_order}\n  结果：{'一致' if ok else '差异'}\n")
            n_top1 += int(ok)
            n_top3 += int(ok)
            n_full += int(ok)
            graded += 1
            continue

        graded += 1
        t1 = exp[0] == py_order[0]
        t3 = exp[:3] == py_order[:3]
        full = exp == py_order
        n_top1 += t1
        n_top3 += t3
        n_full += full
        print(f"「{q}」 候选 {first} → topK {top_k} | 分数最大绝对差 {maxdiff:.2f}")
        print(f"  Node={exp}")
        print(f"  Py  ={py_order}")
        print(f"  top1 {'✓' if t1 else '✗'}  top3 {'✓' if t3 else '✗'}  全序列 {'✓' if full else '✗'}\n")

    print("=" * 60)
    print(f"用例数 {graded} | top-1 一致 {n_top1}/{graded} | top-3 一致 {n_top3}/{graded} | 全序列一致 {n_full}/{graded}")
    # 验收门槛：top-1 必须全部一致
    return 0 if n_top1 == graded else 1


if __name__ == "__main__":
    sys.exit(main())
