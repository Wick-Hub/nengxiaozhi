"""把 JSON 索引灌入 MySQL（任务 1.14）。

数据源优先级：``KB_STORE_PATH`` → ``kb-service/data/store.json`` → ``kb-engine/data/store.json``。
全量重建 ``kb_doc`` / ``kb_chunk`` / ``kb_chunk_vec_proto`` 三表。

用法（在 kb-service 目录下）::

    python tools/ingest_mysql.py
    python tools/ingest_mysql.py --dry-run          # 只校验数据源，不写库
    python tools/ingest_mysql.py --store path.json  # 指定源文件

退出码：0 成功；1 失败。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import kb_repo  # noqa: E402
from app.db.session import ping  # noqa: E402

SERVICE_DIR = Path(__file__).resolve().parent.parent


def candidate_stores() -> list[Path]:
    return [
        Path(settings.store_path),
        SERVICE_DIR.parent / "kb-engine" / "data" / "store.json",
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 JSON 索引灌入 MySQL")
    ap.add_argument("--store", type=str, default=None, help="指定索引文件")
    ap.add_argument("--dry-run", action="store_true", help="只校验数据源，不写库")
    args = ap.parse_args(argv)

    if args.store:
        path = Path(args.store)
    else:
        path = next((p for p in candidate_stores() if p.is_file()), None)  # type: ignore[assignment]

    if not path or not Path(path).is_file():
        tried = "、".join(str(p) for p in candidate_stores())
        print(f"未找到索引文件，已尝试：{tried}")
        return 1

    print(f"数据源：{path}")
    store = json.loads(Path(path).read_text(encoding="utf-8"))
    docs = store.get("docs") or []
    chunks = store.get("chunks") or []
    print(f"  文档 {len(docs)} 篇 ｜ 切片 {len(chunks)} 条 ｜ 模型 {store.get('model') or '(未标注)'}")
    if not chunks:
        print("索引中无切片，终止。")
        return 1

    # 数据自检
    problems = []
    doc_ids = {d.get("docId") for d in docs}
    if any(not d.get("docId") for d in docs):
        problems.append("存在缺少 docId 的文档")
    orphan = [c.get("chunkId") for c in chunks if c.get("docId") not in doc_ids]
    if orphan:
        problems.append(f"{len(orphan)} 条切片无所属文档，例如 {orphan[:3]}")
    no_vec = [c.get("chunkId") for c in chunks if not c.get("embedding")]
    if no_vec:
        problems.append(f"{len(no_vec)} 条切片缺向量，例如 {no_vec[:3]}")
    dims = {len(c.get("embedding") or []) for c in chunks}
    if len(dims) > 1:
        problems.append(f"向量维度不一致：{sorted(dims)}")
    for p in problems:
        print(f"  ✘ {p}")
    if problems:
        return 1
    print(f"  自检通过（向量维度 {sorted(dims)}）")

    if args.dry_run:
        print("\n--dry-run：未写库。")
        return 0

    ok, msg = ping()
    print(f"\n数据库：{msg}")
    if not ok:
        print("数据库不可用，请先启动：docker compose -f ../database/docker-compose.yml up -d")
        return 1

    t0 = time.time()
    result = kb_repo.save_store(store)
    elapsed = time.time() - t0

    counts = kb_repo.count_knowledge()
    print("\n✅ 灌入完成")
    print(f"  写入：{result['docs']} 文档 / {result['chunks']} 切片 ｜ 耗时 {elapsed:.1f}s")
    print(f"  库中：{counts['docs']} 文档（已发布 {counts['published_docs']}）/ {counts['chunks']} 切片 / {counts['vectors']} 向量")
    if counts["chunks"] != result["chunks"]:
        print("  ✘ 切片数不一致，请检查")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
