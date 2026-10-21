"""Python ↔ Node 生成层一致性比对（任务 1.7 验收工具）。

在 kb-service 目录下运行::

    python tools/parity/generate_compare.py
    NODE_BIN=/path/to/node python tools/parity/generate_compare.py

Node 侧置 ``LLM_MOCK=1``，mock 回答是确定性的，因此可逐字比对：

    1. 回答正文（answer）
    2. 引用列表（chunkId / docId / similarity / titlePath / dept / ktype）
    3. 拒答判定（refused）与置信度（confidence）
    4. 引用解析对越界编号（[9]/[99]）与重复编号的处理

**有意差异**：本实现在引用中额外返回 ``n``（正文原始编号）。原型不返回编号，
来源面板只能按数组位置显示，导致「正文引用 [3]、面板显示 [1]」的错位。
比对时先剔除 ``n`` 校验其余字段一致，再单独校验 ``n`` 与 chunkId 的对应关系。
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


def _strip_n(cites: list[dict]) -> list[dict]:
    """剔除本实现新增的 n 字段，用于与原型的引用结构对比。"""
    return [{k: v for k, v in c.items() if k != "n"} for c in cites]


def _node_results() -> dict | None:
    node_bin = os.environ.get("NODE_BIN", "node")
    try:
        proc = subprocess.run(
            [node_bin, str(HERE / "node_generate_probe.mjs")],
            cwd=str(KB_ENGINE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"Node 侧不可用（{exc}），跳过比对。")
        return None
    if proc.returncode != 0:
        print("Node 侧生成失败：")
        print((proc.stderr or "").strip()[:1500])
        return None
    text = (proc.stdout or "").strip()
    start = text.find("{")
    if start < 0:
        print("Node 侧未输出结果：")
        print((proc.stderr or text).strip()[:1500])
        return None
    return json.loads(text[start:])


def main() -> int:
    cfg = json.loads((HERE / "generate_cases.json").read_text(encoding="utf-8"))
    node = _node_results()
    if node is None:
        return 1

    from app.config import settings
    from app.core.chunker import Chunk
    from app.core.generator import Generator, parse_citations
    from app.core.llm_client import LLMClient
    from app.core.retriever import Retrieved

    # 强制 mock，保证确定性
    settings.llm_mock = True

    store_path = Path(os.environ.get("KB_STORE_JSON", KB_ENGINE / "data" / "store.json"))
    store = json.loads(store_path.read_text(encoding="utf-8"))
    cos = cfg["cos"]
    results = [
        Retrieved(chunk=Chunk.from_dict(c), cos=cos[i])
        for i, c in enumerate(store["chunks"][: len(cos)])
    ]

    gen = Generator(LLMClient(mock=True))
    print("生成层一致性比对（LLM_MOCK=1，mock 回答确定性）\n")

    bad = 0
    for case, exp in zip(cfg["cases"], node["cases"]):
        got = gen.generate(case["question"], results, role=case.get("role"), history=case.get("history"))
        tag = f"「{case['question']}」 role={case.get('role') or 'None'}"
        diffs: list[str] = []
        if got.answer != exp["answer"]:
            diffs.append("answer")
        if got.refused != exp["refused"]:
            diffs.append("refused")
        if abs(got.confidence - exp["confidence"]) > 1e-9:
            diffs.append("confidence")
        if _strip_n(got.citations) != exp["citations"]:
            diffs.append("citations")
        if diffs:
            bad += 1
        print(f"{tag}\n  answer    : {got.answer[:60]}…")
        print(f"  refused   : {got.refused} | confidence: {got.confidence}")
        print(f"  引用编号 n : {[c['n'] for c in got.citations]}  ← 本实现保留正文原始编号")
        print(f"  结果：{'一致' if not diffs else '差异 → ' + ','.join(diffs)}\n")

    # 引用解析边界
    py_cites = parse_citations(cfg["citationAnswer"], results)
    node_cites = node["citation"]["citations"]
    fields_ok = _strip_n(py_cites) == node_cites
    n_ok = all(c["chunkId"] == results[c["n"] - 1].chunk_id for c in py_cites)
    if not (fields_ok and n_ok):
        bad += 1
    print("引用解析边界（正文含越界 [9]/[99]、重复 [2]）")
    print(f"  Node chunkId : {[c['chunkId'] for c in node_cites]}")
    print(f"  Py   chunkId : {[c['chunkId'] for c in py_cites]}")
    print(f"  Py   编号 n  : {[c['n'] for c in py_cites]}")
    print(f"  字段一致：{'是' if fields_ok else '否'} | 编号与切片对应：{'是' if n_ok else '否'}\n")

    total = len(cfg["cases"]) + 1
    print(f"合计 {total} 项，差异 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
