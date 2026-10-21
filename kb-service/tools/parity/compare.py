"""Python ↔ Node 解析/切片一致性比对（任务 1.2 / 1.3 验收工具）。

用法（在 kb-service 目录下）：
    python tools/parity/compare.py

可指定 Node 可执行文件：
    NODE_BIN=/path/to/node python tools/parity/compare.py

比对维度：blocks 数、chunks 数、blockTypes 序列、chunkChars 序列、frontmatter meta。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KB_SERVICE = HERE.parent.parent


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise SystemExit(f"命令失败：{' '.join(cmd)}\n{proc.stderr}")
    return proc.stdout


def main() -> int:
    node_bin = os.environ.get("NODE_BIN", "node")

    node_out = _run([node_bin, str(HERE / "node_probe.mjs")], KB_SERVICE)
    py_out = _run([sys.executable, str(HERE / "py_probe.py")], KB_SERVICE)

    node = {x["rel"]: x for x in json.loads(node_out)}
    py = {x["rel"]: x for x in json.loads(py_out)}

    only_node = set(node) - set(py)
    only_py = set(py) - set(node)
    if only_node:
        print("仅 Node 解析到：", sorted(only_node))
    if only_py:
        print("仅 Python 解析到：", sorted(only_py))

    keys = ["blocks", "chunks", "blockTypes", "chunkChars", "meta"]
    mismatch = 0
    print(f"{'文件':<50}{'blocks':>13}{'chunks':>13}  结果")
    for rel in sorted(set(node) & set(py)):
        a, b = node[rel], py[rel]
        diffs = [k for k in keys if a.get(k) != b.get(k)]
        status = "一致" if not diffs else "差异:" + ",".join(diffs)
        if diffs:
            mismatch += 1
        print(f"{rel:<50}{a['blocks']:>6}/{b['blocks']:<6}{a['chunks']:>6}/{b['chunks']:<6}  {status}")

    total = len(set(node) & set(py))
    print(f"\n合计 {total} 个文件，一致 {total - mismatch}，差异 {mismatch}")
    return 1 if (mismatch or only_node or only_py) else 0


if __name__ == "__main__":
    raise SystemExit(main())
