"""检索一致性回归：JSON 模式 vs MySQL 模式（任务 1.14 验收）。

同一批问题、同一个嵌入模型、同一套参数，分别在 JSON 索引与 MySQL 索引上
跑完整检索链路（角色过滤 → 向量 + BM25 → RRF → 精排），逐项比对：

  * 召回切片 ID 序列（top-k）
  * 每条结果的分项得分（cos / bm25 / rrf）
  * 精排后的最终序列与分数

这是「每一层都用 Node 原型做一致性回归」原则在数据层的落地：只要 MySQL
往返没有引入漂移，两种存储的检索结果就必须**逐字节一致**。

用法::

    python tools/verify_retrieval_consistency.py
    python tools/verify_retrieval_consistency.py --mode vector --top-k 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.config import settings  # noqa: E402
from app.core import index_store  # noqa: E402
from app.core.chunker import Chunk  # noqa: E402
from app.core.embedder import get_embedder  # noqa: E402
from app.core.reranker import get_reranker  # noqa: E402
from app.core.retriever import hybrid_search  # noqa: E402

PROTO_STORE = BASE_DIR.parent / "kb-engine" / "data" / "store.json"

# 覆盖各知识类型与角色可见性的问题集
QUERIES: list[tuple[str, str | None]] = [
    ("校园网怎么开户", "本科生"),
    ("校园网开户与报修流程", None),
    ("图书馆借书能借多久", "本科生"),
    ("研究生国家助学金标准是多少", "研究生"),
    ("补办学生证需要什么材料", "本科生"),
    ("教职工住房补贴怎么申请", "教职工"),
    ("本科招生咨询电话", None),
    ("挂科了怎么重修", "本科生"),
    ("离退休人员体检安排", "离退休人员"),
    ("校医院门诊时间", None),
]


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def check(self, ok: bool, msg: str, *, quiet: bool = False) -> bool:
        self.checks += 1
        if not ok:
            print(f"  \u2717 {msg}")
            self.failures.append(msg)
        elif not quiet:
            print(f"  \u2713 {msg}")
        return ok


def _snap_json() -> index_store.IndexSnapshot:
    return index_store._load_json(PROTO_STORE)


def _snap_mysql() -> index_store.IndexSnapshot:
    return index_store._load_mysql()


def _fmt(results) -> str:
    return " > ".join(r.chunk.chunk_id for r in results)


def compare_retrieval(
    rep: Report,
    json_chunks: list[Chunk],
    mysql_chunks: list[Chunk],
    top_k: int,
    mode: str,
) -> None:
    embedder = get_embedder()
    print(f"\n嵌入模型：{settings.embed_model} ｜ 降级哈希：{embedder.is_fallback}")
    print(f"精排启用：{settings.rerank_enabled} ｜ 模式：{mode} ｜ top_k：{top_k}")

    print("\n[1/2] 混合检索（向量 + BM25 → RRF）")
    for q, role in QUERIES:
        vec = embedder.encode_one(q)
        a = hybrid_search(json_chunks, vec, q, role=role, top_n=top_k, mode=mode)
        b = hybrid_search(mysql_chunks, vec, q, role=role, top_n=top_k, mode=mode)

        tag = f"[{role or '缺省'}] {q}"
        if not rep.check(len(a) == len(b), f"{tag} 召回条数 {len(a)} vs {len(b)}"):
            continue
        ids_a = [r.chunk.chunk_id for r in a]
        ids_b = [r.chunk.chunk_id for r in b]
        if not rep.check(ids_a == ids_b, f"{tag} 切片序列\n      JSON : {_fmt(a)}\n      MySQL: {_fmt(b)}"):
            continue
        max_d = 0.0
        for ra, rb in zip(a, b):
            max_d = max(max_d, abs(ra.cos - rb.cos), abs(ra.bm25 - rb.bm25), abs(ra.rrf - rb.rrf))
        rep.check(
            max_d < 1e-6,
            f"{tag} 得分一致（max|Δ|={max_d:.2e}）｜ {_fmt(a)}",
        )

    print("\n[2/2] 精排后序列（若启用）")
    if not settings.rerank_enabled:
        print("  \u2022 精排未启用，跳过")
        return
    reranker = get_reranker()
    print(f"  \u2022 精排模型可用：{getattr(reranker, 'available', '未知')}")
    for q, role in QUERIES:
        vec = embedder.encode_one(q)
        recall_k = max(settings.rerank_topn, top_k)
        a = hybrid_search(json_chunks, vec, q, role=role, top_n=recall_k, mode=mode)
        b = hybrid_search(mysql_chunks, vec, q, role=role, top_n=recall_k, mode=mode)
        if len(a) <= top_k:
            continue
        ra = reranker.rerank(q, a, top_k)
        rb = reranker.rerank(q, b, top_k)
        ids_a = [r.chunk.chunk_id for r in ra]
        ids_b = [r.chunk.chunk_id for r in rb]
        rep.check(
            ids_a == ids_b,
            f"[{role or '缺省'}] {q} 精排序列\n      JSON : {' > '.join(ids_a)}\n      MySQL: {' > '.join(ids_b)}",
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="检索一致性回归（JSON vs MySQL）")
    ap.add_argument("--mode", default=settings.retrieve_mode, choices=["hybrid", "vector", "bm25"])
    ap.add_argument("--top-k", type=int, default=settings.top_k)
    ap.add_argument("--dump", action="store_true", help="输出首个问题的完整得分明细")
    args = ap.parse_args()

    print("=" * 68)
    print("检索一致性回归：JSON 索引 vs MySQL 索引")
    print("=" * 68)
    print(f"JSON 源 ：{PROTO_STORE}")
    print(f"MySQL 源：{settings.db_host}:{settings.db_port}/{settings.db_name}")

    snap_json = _snap_json()
    snap_mysql = _snap_mysql()

    if snap_mysql.error:
        print(f"\n[失败] MySQL 索引不可用：{snap_mysql.error}")
        return 2

    print(f"\nJSON  ：{len(snap_json.docs)} 文档 / {len(snap_json.chunks)} 切片 / {snap_json.model}")
    print(f"MySQL ：{len(snap_mysql.docs)} 文档 / {len(snap_mysql.chunks)} 切片 / {snap_mysql.model}")

    rep = Report()
    print("\n[0/2] 索引规模与模型")
    rep.check(len(snap_json.chunks) == len(snap_mysql.chunks), "切片数一致")
    rep.check(len(snap_json.docs) == len(snap_mysql.docs), "文档数一致")
    rep.check(snap_json.model == snap_mysql.model, "嵌入模型标识一致")
    rep.check(
        [c.chunk_id for c in snap_json.chunks] == [c.chunk_id for c in snap_mysql.chunks],
        "切片 ID 序列一致",
    )

    compare_retrieval(rep, snap_json.chunks, snap_mysql.chunks, args.top_k, args.mode)

    if args.dump:
        print("\n--- 明细（首个问题）---")
        q, role = QUERIES[0]
        vec = get_embedder().encode_one(q)
        for src, chunks in (("JSON", snap_json.chunks), ("MySQL", snap_mysql.chunks)):
            print(f"\n{src}: [{role}] {q}")
            for r in hybrid_search(chunks, vec, q, role=role, top_n=args.top_k, mode=args.mode):
                print(
                    f"  {r.chunk.chunk_id}  score={r.score:.6f} cos={r.cos:.6f} "
                    f"bm25={r.bm25:.4f} v_rank={r.v_rank} b_rank={r.b_rank}"
                )

    print("\n" + "=" * 68)
    if rep.failures:
        print(f"结果：失败（{len(rep.failures)}/{rep.checks} 项不一致）")
        for f in rep.failures[:10]:
            print(f"  - {f.splitlines()[0]}")
        return 1
    print(f"结果：全部通过 ✅ {rep.checks} 项检查，两种存储检索结果完全一致")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
