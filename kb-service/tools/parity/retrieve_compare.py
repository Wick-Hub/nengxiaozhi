"""Python ↔ Node 检索一致性比对（任务 1.5 验收工具）。

在 kb-service 目录下运行::

    python tools/parity/retrieve_compare.py
    NODE_BIN=/path/to/node python tools/parity/retrieve_compare.py

比对内容：命中 chunkId 序列、余弦值、BM25 分数、RRF 分数。

``expectDiff`` 用例（role 为 null）用于验证**安全修复**：
原型在 role 为空时放行全部知识，本实现按「访客」过滤。
此时期望 Python 结果是 Node 结果的**子集**。
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

COS_TOL = 1e-5
BM25_TOL = 1e-6
RRF_TOL = 1e-9


def _node_results() -> list[dict] | None:
    node_bin = os.environ.get("NODE_BIN", "node")
    try:
        proc = subprocess.run(
            [node_bin, str(HERE / "node_retrieve_probe.mjs")],
            cwd=str(KB_ENGINE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"Node 侧不可用（{exc}），跳过比对。")
        return None
    if proc.returncode != 0:
        print("Node 侧检索失败：")
        print((proc.stderr or "").strip()[:1500])
        return None
    text = (proc.stdout or "").strip()
    start = text.find("[")
    if start < 0:
        print("Node 侧未输出结果：")
        print((proc.stderr or text).strip()[:1500])
        return None
    return json.loads(text[start:])


def _load_chunks() -> list:
    from app.core.chunker import Chunk

    store_path = Path(os.environ.get("KB_STORE_JSON", KB_ENGINE / "data" / "store.json"))
    store = json.loads(store_path.read_text(encoding="utf-8"))
    return [Chunk.from_dict(c) for c in store.get("chunks", [])]


def main() -> int:
    cases = json.loads((HERE / "retrieve_cases.json").read_text(encoding="utf-8"))["cases"]
    node_raw = _node_results()
    if node_raw is None:
        return 1

    from app.core.embedder import get_embedder
    from app.core.retriever import hybrid_search

    emb = get_embedder()
    chunks = _load_chunks()
    print(f"索引切片：{len(chunks)} 条 | 向量后端：{emb.backend}\n")

    bad = 0
    for case, node in zip(cases, node_raw):
        q, role, mode = case["question"], case.get("role"), case["mode"]
        vec = emb.encode_one(q)
        got = hybrid_search(chunks, vec, q, role=role, top_n=case["topK"], mode=mode)

        tag = f"[{mode}] role={role or 'None'} 「{q}」"
        if case.get("expectDiff"):
            node_ids = [h["chunkId"] for h in node["hits"]]
            got_ids = [r.chunk_id for r in got]
            subset = set(got_ids) <= set(node_ids)
            ok = subset and len(got_ids) > 0
            print(f"{tag}\n  Node({len(node_ids)})={node_ids}\n  Py  ({len(got_ids)})={got_ids}")
            print(f"  安全修复验证：{'通过（Py 为 Node 子集，越权切片已过滤）' if ok else '异常'}\n")
            if not ok:
                bad += 1
            continue

        node_ids = [h["chunkId"] for h in node["hits"]]
        got_ids = [r.chunk_id for r in got]
        diffs: list[str] = []
        if node_ids != got_ids:
            diffs.append("chunkId序列")
        else:
            for h, r in zip(node["hits"], got):
                if abs(h["cos"] - r.cos) > COS_TOL:
                    diffs.append("cos")
                if abs(h["bm25"] - r.bm25) > BM25_TOL:
                    diffs.append("bm25")
                if abs(h["rrf"] - r.rrf) > RRF_TOL:
                    diffs.append("rrf")
                if h["vRank"] != r.v_rank or h["bRank"] != r.b_rank:
                    diffs.append("rank")
        diffs = sorted(set(diffs))
        if diffs:
            bad += 1
        print(f"{tag}\n  Node({len(node_ids)})={node_ids}\n  Py  ({len(got_ids)})={got_ids}")
        print(f"  结果：{'一致' if not diffs else '差异 → ' + ','.join(diffs)}\n")

    print(f"合计 {len(cases)} 个用例，异常 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
