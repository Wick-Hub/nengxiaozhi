"""敏感词 / 关键词库仓储（任务 2.10 / M-SYS-07）。

仅 MySQL 模式使用；JSON 模式由服务层直接从内置词表推导只读清单。
"""

from __future__ import annotations

from sqlalchemy import delete, func, insert, select, update

from app.db import models as M

#: 词条类型：sensitive 参与风控检测；keyword 仅作业务词库管理（暂不参与检测）
KINDS = ("sensitive", "keyword")

_COLUMNS = (
    M.SensitiveWord.word_id,
    M.SensitiveWord.word,
    M.SensitiveWord.kind,
    M.SensitiveWord.category,
    M.SensitiveWord.status,
    M.SensitiveWord.remark,
    M.SensitiveWord.created_by,
    M.SensitiveWord.created_at,
    M.SensitiveWord.updated_at,
)


def _to_item(row) -> dict:
    return {
        "wordId": int(row[0]),
        "word": row[1],
        "kind": row[2],
        "category": row[3],
        "status": int(row[4]),
        "remark": row[5] or "",
        "createdBy": row[6],
        "createdAt": row[7].isoformat() if row[7] else None,
        "updatedAt": row[8].isoformat() if row[8] else None,
    }


def list_words(
    conn,
    *,
    kind: str | None = None,
    status: int | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """分页查询词条，返回 ``(items, total)``；筛选条件下推到 SQL。"""
    conds = []
    if kind:
        conds.append(M.SensitiveWord.kind == kind)
    if status is not None:
        conds.append(M.SensitiveWord.status == status)
    if keyword:
        conds.append(M.SensitiveWord.word.like(f"%{keyword}%"))

    total = int(
        conn.execute(select(func.count()).select_from(M.SensitiveWord).where(*conds)).scalar()
        or 0
    )
    rows = conn.execute(
        select(*_COLUMNS)
        .where(*conds)
        .order_by(M.SensitiveWord.word_id.desc())
        .offset((max(1, page) - 1) * page_size)
        .limit(page_size)
    ).all()
    return [_to_item(r) for r in rows], total


def count_by_kind(conn) -> dict[str, int]:
    """各类型的启用/停用数量（管理页统计条）。"""
    rows = conn.execute(
        select(M.SensitiveWord.kind, M.SensitiveWord.status, func.count())
        .group_by(M.SensitiveWord.kind, M.SensitiveWord.status)
    ).all()
    out = {k: {"enabled": 0, "disabled": 0} for k in KINDS}
    for kind, status, n in rows:
        bucket = out.setdefault(kind, {"enabled": 0, "disabled": 0})
        bucket["enabled" if int(status) == 1 else "disabled"] = int(n)
    return out


def get_word(conn, word_id: int) -> dict | None:
    row = conn.execute(select(*_COLUMNS).where(M.SensitiveWord.word_id == word_id)).first()
    return _to_item(row) if row else None


def find_by_word(conn, word: str, *, exclude_id: int | None = None) -> dict | None:
    stmt = select(*_COLUMNS).where(M.SensitiveWord.word == word)
    if exclude_id is not None:
        stmt = stmt.where(M.SensitiveWord.word_id != exclude_id)
    row = conn.execute(stmt).first()
    return _to_item(row) if row else None


def create_word(
    conn,
    *,
    word: str,
    kind: str = "sensitive",
    category: str = "自定义",
    status: int = 1,
    remark: str = "",
    created_by: int | None = None,
) -> int:
    result = conn.execute(
        insert(M.SensitiveWord).values(
            word=word,
            kind=kind,
            category=category,
            status=status,
            remark=remark,
            created_by=created_by,
        )
    )
    return int(result.inserted_primary_key[0])


def update_word(conn, word_id: int, **fields) -> int:
    """按需更新字段，返回受影响行数。"""
    allowed = {"word", "kind", "category", "status", "remark"}
    values = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not values:
        return 0
    result = conn.execute(
        update(M.SensitiveWord).where(M.SensitiveWord.word_id == word_id).values(**values)
    )
    return int(result.rowcount or 0)


def delete_word(conn, word_id: int) -> int:
    result = conn.execute(delete(M.SensitiveWord).where(M.SensitiveWord.word_id == word_id))
    return int(result.rowcount or 0)


def enabled_words(conn, kind: str = "sensitive") -> list[str]:
    """当前启用词条（供风控检测加载）。"""
    rows = conn.execute(
        select(M.SensitiveWord.word)
        .where(M.SensitiveWord.kind == kind, M.SensitiveWord.status == 1)
        .order_by(M.SensitiveWord.word_id)
    ).all()
    return [r[0] for r in rows]
