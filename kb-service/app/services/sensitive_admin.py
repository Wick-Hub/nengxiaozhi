"""敏感词 / 关键词库管理服务（任务 2.10 / M-SYS-07）。

- ``KB_STORE=mysql``：词条是 ``sensitive_word`` 表的真实记录，支持增删改与启停；
  启用的 ``kind='sensitive'`` 词条即时进入风控检测（缓存失效后下一问即生效）。
- ``KB_STORE=json``：没有词库表，返回**当前实际生效**的内置/文件词表，``editable=False``，
  写操作返回 400 + 指引。

关键词（``kind='keyword'``）与敏感词共用一张表；R1 只做管理，暂不参与检测。
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import sensitive_repo
from app.db.session import DatabaseUnavailable, db_session, db_transaction
from app.security import sensitive

logger = logging.getLogger(__name__)

KINDS = sensitive_repo.KINDS
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


def _mysql_mode() -> bool:
    return settings.store_label == "mysql"


def _must_mysql() -> dict | None:
    if _mysql_mode():
        return None
    return _fail(
        "关键词库维护需要 MySQL 存储：请在 .env 中设置 KB_STORE=mysql 并确保数据库已启动"
        "（docker compose -f ../database/docker-compose.yml up -d）",
        status=400,
    )


def _page_args(page: int | None, page_size: int | None) -> tuple[int, int]:
    p = max(1, int(page or 1))
    size = min(MAX_PAGE_SIZE, max(1, int(page_size or DEFAULT_PAGE_SIZE)))
    return p, size


def _clean_word(raw: str | None) -> tuple[str | None, str | None]:
    word = (raw or "").strip()
    if not word:
        return None, "词条不能为空"
    if len(word) > 128:
        return None, "词条长度不能超过 128 个字符"
    if any(ch in word for ch in "\r\n\t"):
        return None, "词条不能包含换行或制表符"
    return word, None


def _clean_kind(raw: str | None) -> tuple[str | None, str | None]:
    kind = (raw or "sensitive").strip()
    if kind not in KINDS:
        return None, f"词条类型非法：{kind}（可选：{' / '.join(KINDS)}）"
    return kind, None


def _json_items() -> list[dict]:
    """JSON 模式：把当前生效的词表按只读词条返回（仅敏感词）。"""
    builtin = set(sensitive.DEFAULT_WORDS)
    return [
        {
            "wordId": None,
            "word": word,
            "kind": "sensitive",
            "category": "内置" if word in builtin else "文件",
            "status": 1,
            "remark": "内置意图词，不可删除" if word in builtin else "来自敏感词表文件",
            "createdBy": None,
            "createdAt": None,
            "updatedAt": None,
            "builtin": word in builtin,
        }
        for word in sensitive.load_words()
    ]


def list_words(
    *,
    kind: str | None = None,
    status: int | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """词条清单（分页）；JSON 模式为只读推导。"""
    page, page_size = _page_args(page, page_size)

    if not _mysql_mode():
        items = _json_items()
        if kind:
            items = [it for it in items if it["kind"] == kind]
        if status is not None:
            items = [it for it in items if it["status"] == status]
        if keyword:
            needle = keyword.lower()
            items = [it for it in items if needle in it["word"].lower()]
        total = len(items)
        page_items = items[(page - 1) * page_size : page * page_size]
        return _ok(
            {
                "items": page_items,
                "total": total,
                "page": page,
                "pageSize": page_size,
                "editable": False,
                "stats": {
                    "sensitive": {"enabled": total, "disabled": 0},
                    "keyword": {"enabled": 0, "disabled": 0},
                },
                "source": sensitive.stats().get("source"),
            }
        )

    try:
        with db_session() as conn:
            items, total = sensitive_repo.list_words(
                conn,
                kind=kind,
                status=status,
                keyword=keyword,
                page=page,
                page_size=page_size,
            )
            stats = sensitive_repo.count_by_kind(conn)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    except Exception as exc:  # noqa: BLE001 - 表缺失等给出可读错误
        logger.exception("敏感词库查询失败")
        return _fail(f"敏感词库查询失败：{exc}", status=503)

    return _ok(
        {
            "items": items,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "editable": True,
            "stats": stats,
            "source": "db",
        }
    )


def create_word(payload: dict, *, user_id: int | None = None) -> dict:
    """新增词条；同名词条已存在返回 409。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    word, err = _clean_word(payload.get("word"))
    if err:
        return _fail(err, status=400)
    kind, err = _clean_kind(payload.get("kind"))
    if err:
        return _fail(err, status=400)
    category = (payload.get("category") or "自定义").strip()[:32] or "自定义"
    remark = (payload.get("remark") or "").strip()[:255]
    status = 1 if int(payload.get("status", 1)) == 1 else 0

    try:
        with db_transaction() as conn:
            if sensitive_repo.find_by_word(conn, word) is not None:
                return _fail(f"词条已存在：{word}", status=409)
            word_id = sensitive_repo.create_word(
                conn,
                word=word,
                kind=kind,
                category=category,
                status=status,
                remark=remark,
                created_by=user_id,
            )
            item = sensitive_repo.get_word(conn, word_id)
    except IntegrityError:
        return _fail(f"词条已存在：{word}", status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)

    sensitive.invalidate()
    return _ok(item)


def update_word(word_id: int, payload: dict) -> dict:
    """更新词条（名称/类型/分类/启停/备注）；不存在返回 404。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    fields: dict = {}
    if "word" in payload and payload["word"] is not None:
        word, err = _clean_word(payload["word"])
        if err:
            return _fail(err, status=400)
        fields["word"] = word
    if "kind" in payload and payload["kind"] is not None:
        kind, err = _clean_kind(payload["kind"])
        if err:
            return _fail(err, status=400)
        fields["kind"] = kind
    if "category" in payload and payload["category"] is not None:
        fields["category"] = (payload["category"] or "").strip()[:32] or "自定义"
    if "remark" in payload and payload["remark"] is not None:
        fields["remark"] = (payload["remark"] or "").strip()[:255]
    if "status" in payload and payload["status"] is not None:
        fields["status"] = 1 if int(payload["status"]) == 1 else 0

    if not fields:
        return _fail("没有可更新的字段", status=400)

    try:
        with db_transaction() as conn:
            existing = sensitive_repo.get_word(conn, word_id)
            if existing is None:
                return _fail(f"词条不存在：wordId={word_id}", status=404)
            if "word" in fields:
                dup = sensitive_repo.find_by_word(conn, fields["word"], exclude_id=word_id)
                if dup is not None:
                    return _fail(f"词条已存在：{fields['word']}", status=409)
            sensitive_repo.update_word(conn, word_id, **fields)
            item = sensitive_repo.get_word(conn, word_id)
    except IntegrityError:
        return _fail("词条已存在，请勿重复添加", status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)

    sensitive.invalidate()
    return _ok(item)


def delete_word(word_id: int) -> dict:
    """删除词条；不存在返回 404。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    try:
        with db_transaction() as conn:
            existing = sensitive_repo.get_word(conn, word_id)
            if existing is None:
                return _fail(f"词条不存在：wordId={word_id}", status=404)
            sensitive_repo.delete_word(conn, word_id)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)

    sensitive.invalidate()
    return _ok({"wordId": word_id, "deleted": True})
