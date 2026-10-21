"""MySQL 往返一致性校验（任务 1.14 验收）。

把 ``kb-engine/data/store.json``（Node 原型索引）灌入 MySQL，再用
``kb_repo.load_store()`` 读回，逐字段与原始 JSON 比对，量化验证 Python
数据层没有引入漂移。

用法::

    python tools/verify_store_roundtrip.py            # 读现有库数据比对
    python tools/verify_store_roundtrip.py --reingest # 先重新灌入再比对

已知且**有意**的差异（不视为失败）：
  * ``docs[].status``：源数据混用「已审核」/「已发布」，统一归一为「已发布」
    （``STATUS_MAP`` 两者都映射到 2）。
  * ``docs[].ktype``：源数据混用中文名与枚举名，读回统一为中文名。
  * ``docs[].updated``：MySQL 存 DATETIME，读回统一为 ``YYYY-MM-DD``。
  * ``docs[].source``：库中无对应列，读回为空串。
  * ``createdAt``：原型读回时用「当前时刻」，不参与比对。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.db import kb_repo  # noqa: E402
from app.config import settings  # noqa: E402

PROTO_STORE = BASE_DIR.parent / "kb-engine" / "data" / "store.json"

# 有意差异：这些字段不参与逐字段比对，只做「存在性 + 类型」检查
IGNORED_DOC_FIELDS = {"source", "createdAt"}
IGNORED_CHUNK_FIELDS: set[str] = set()


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, msg: str) -> bool:
        if ok:
            print(f"  \u2713 {msg}")
        else:
            print(f"  \u2717 {msg}")
            self.failures.append(msg)
        return ok

    def note(self, msg: str) -> None:
        print(f"  \u2022 {msg}")
        self.notes.append(msg)


def _load_proto() -> dict:
    if not PROTO_STORE.is_file():
        raise SystemExit(f"找不到原型索引：{PROTO_STORE}")
    return json.loads(PROTO_STORE.read_text(encoding="utf-8"))


def _cmp_scalar(rep: Report, label: str, want, got) -> None:
    if want != got:
        rep.check(False, f"{label}: 期望 {want!r}，实际 {got!r}")
    else:
        rep.check(True, f"{label} 一致")


def verify(proto: dict, actual: dict) -> Report:
    rep = Report()

    print("\n[1/4] 顶层结构")
    _cmp_scalar(rep, "version", proto.get("version"), actual.get("version"))
    _cmp_scalar(rep, "model", proto.get("model"), actual.get("model"))
    rep.check(
        bool(actual.get("createdAt")), f"createdAt 非空（{actual.get('createdAt')}）"
    )

    print("\n[2/4] 文档集")
    p_docs, a_docs = proto.get("docs", []), actual.get("docs", [])
    _cmp_scalar(rep, "文档数", len(p_docs), len(a_docs))

    a_by_id = {d.get("docId"): d for d in a_docs}
    for pd in p_docs:
        did = pd.get("docId")
        ad = a_by_id.get(did)
        if ad is None:
            rep.check(False, f"文档 {did} 缺失")
            continue
        for field in ("file", "title", "dept", "chunkCount"):
            _cmp_scalar(rep, f"{did}.{field}", pd.get(field), ad.get(field))
        # ktype：源可能是中文或枚举，比对归一后的中文名
        want_ktype = kb_repo.ktype_label(kb_repo.ktype_enum(pd.get("ktype")))
        _cmp_scalar(rep, f"{did}.ktype(归一)", want_ktype, ad.get("ktype"))
        # status：源「已审核」/「已发布」都归一为 2 → 「已发布」
        want_status = kb_repo.STATUS_NAME[kb_repo.status_code(pd.get("status"))]
        _cmp_scalar(rep, f"{did}.status(归一)", want_status, ad.get("status"))
        _cmp_scalar(rep, f"{did}.updated", pd.get("updated") or "", ad.get("updated"))

    print("\n[3/4] 切片集")
    p_chunks, a_chunks = proto.get("chunks", []), actual.get("chunks", [])
    _cmp_scalar(rep, "切片数", len(p_chunks), len(a_chunks))

    a_by_cid = {c.get("chunkId"): c for c in a_chunks}
    dims: set[int] = set()
    for pc in p_chunks:
        cid = pc.get("chunkId")
        ac = a_by_cid.get(cid)
        if ac is None:
            rep.check(False, f"切片 {cid} 缺失")
            continue
        for field in (
            "docId",
            "seqNo",
            "charCount",
            "dept",
            "titlePath",
            "blockType",
            "text",
            "embedText",
        ):
            if field in IGNORED_CHUNK_FIELDS:
                continue
            _cmp_scalar(rep, f"{cid}.{field}", pc.get(field), ac.get(field))
        # ktype 归一
        want_ktype = kb_repo.ktype_label(kb_repo.ktype_enum(pc.get("ktype")))
        _cmp_scalar(rep, f"{cid}.ktype(归一)", want_ktype, ac.get("ktype"))
        # visibleRoles 顺序敏感（原型按并集去重顺序写库）
        _cmp_scalar(
            rep,
            f"{cid}.visibleRoles",
            list(pc.get("visibleRoles") or []),
            list(ac.get("visibleRoles") or []),
        )
        # 向量维度 + 数值精度
        pe = list(pc.get("embedding") or [])
        ae = list(ac.get("embedding") or [])
        if len(pe) != len(ae):
            rep.check(False, f"{cid}.embedding 维度 {len(pe)} vs {len(ae)}")
        else:
            dims.add(len(ae))
            max_diff = max((abs(x - y) for x, y in zip(pe, ae)), default=0.0)
            if max_diff > 1e-6:
                rep.check(False, f"{cid}.embedding 数值漂移 max|Δ|={max_diff:.2e}")
            else:
                rep.check(True, f"{cid}.embedding {len(ae)} 维（max|Δ|={max_diff:.2e}）")

    print("\n[4/4] 向量维度一致性")
    rep.check(len(dims) == 1, f"全部切片同维度：{sorted(dims)}")

    # 编号连续性：D001..D00N / C001..C00M
    print("\n[附加] 编号连续性")
    want_doc_ids = [d.get("docId") for d in p_docs]
    got_doc_ids = [d.get("docId") for d in a_docs]
    _cmp_scalar(rep, "docId 序列", want_doc_ids, got_doc_ids)
    want_chunk_ids = [c.get("chunkId") for c in p_chunks]
    got_chunk_ids = [c.get("chunkId") for c in a_chunks]
    _cmp_scalar(rep, "chunkId 序列", want_chunk_ids, got_chunk_ids)

    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description="MySQL 往返一致性校验")
    ap.add_argument(
        "--reingest", action="store_true", help="先重新灌入原型索引，再读回比对"
    )
    args = ap.parse_args()

    print("=" * 68)
    print("MySQL 往返一致性校验（Python 数据层 vs Node 原型索引）")
    print("=" * 68)
    print(f"原型索引：{PROTO_STORE}")
    print(f"目标库　：MySQL {settings.db_host}:{settings.db_port}/{settings.db_name}")

    proto = _load_proto()

    if args.reingest:
        print("\n>>> 重新灌入 store.json ...")
        res = kb_repo.save_store(proto)
        print(f"    写入 {res['docs']} 文档 / {res['chunks']} 切片")

    print("\n>>> 从 MySQL 读回 ...")
    try:
        actual = kb_repo.load_store()
    except RuntimeError as exc:
        print(f"\n[失败] {exc}")
        return 2

    rep = verify(proto, actual)

    print("\n" + "=" * 68)
    if rep.failures:
        print(f"结果：失败（{len(rep.failures)} 项不一致）")
        for f in rep.failures[:20]:
            print(f"  - {f}")
        if len(rep.failures) > 20:
            print(f"  ... 另有 {len(rep.failures) - 20} 项")
        return 1
    print("结果：全部通过 ✅ MySQL 往返与原型索引一致")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
