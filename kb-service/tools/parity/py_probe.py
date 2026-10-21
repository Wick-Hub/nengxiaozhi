"""解析/切片一致性探针（Python 侧）。

用法：
    python tools/parity/py_probe.py > py_parse.json
输出与 node_probe.mjs 相同的结构，供 compare.py 比对。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import settings
from app.core.chunker import blocks_to_chunks
from app.core.parser import SUPPORTED_EXT, parse_file


def main() -> None:
    root = Path(settings.knowledge_dir).resolve()
    out = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.suffix.lower() not in SUPPORTED_EXT:
            continue
        rel = f.relative_to(root).as_posix()
        if rel.startswith("00-") or "/" not in rel:
            continue
        doc = parse_file(f)
        chunks = blocks_to_chunks(doc.blocks, doc.meta, f)
        out.append(
            {
                "rel": rel,
                "blocks": len(doc.blocks),
                "chunks": len(chunks),
                "meta": doc.meta,
                "blockTypes": [b.type for b in doc.blocks],
                "chunkChars": [c.char_count for c in chunks],
            }
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
