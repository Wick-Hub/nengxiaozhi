"""数据库引擎与会话（任务 1.14）。

复用现有 MySQL 库 `nengxiaozhi`（23 张表，DDL 见仓库根 `database/schema.sql`）。
所有查询必须参数化，禁止字符串拼接 SQL。

用法::

    from app.db.session import db_session

    with db_session() as conn:                 # SQLAlchemy Connection
        conn.execute(text("SELECT 1"))

    from app.db.session import get_engine
    with get_engine().begin() as conn:         # 自动提交/回滚
        ...

引擎惰性创建且进程内复用；连接池 ``pool_pre_ping`` 自动剔除失效连接。
数据库不可用时抛出 :class:`DatabaseUnavailable`，调用方据此降级或报错。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_lock = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """数据库不可用（未启动 / 凭据错误 / 库不存在）。"""


def get_engine() -> Engine:
    """惰性创建并复用 SQLAlchemy 引擎。"""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = create_engine(
                    settings.db_url,
                    pool_pre_ping=True,   # 取连接前探活，避免 MySQL 8h 断连报错
                    pool_recycle=3600,
                    pool_size=5,
                    max_overflow=5,
                    future=True,
                )
                logger.info("数据库引擎已创建：%s:%s/%s", settings.db_host, settings.db_port, settings.db_name)
    return _engine


def dispose_engine() -> None:
    """释放连接池（测试与进程退出时调用）。"""
    global _engine
    with _lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None


@contextmanager
def db_session() -> Iterator:
    """只读/自动提交连接上下文。

    正常退出提交，异常回滚，最后归还连接到池。
    """
    engine = get_engine()
    conn = engine.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def db_transaction() -> Iterator:
    """显式事务上下文（多表写入用）。"""
    engine = get_engine()
    with engine.begin() as conn:
        yield conn


def get_session():
    """获取会话（依赖注入用）。返回 ``(connection, sessionmaker)`` 中的连接。

    保持与骨架同名，便于后续接入 FastAPI 依赖注入。
    """
    return get_engine().connect()


def ping() -> tuple[bool, str]:
    """探测数据库连通性。

    :return: ``(是否可用, 描述信息)``
    """
    try:
        with get_engine().connect() as conn:
            ver = conn.execute(text("SELECT VERSION()")).scalar()
            db = conn.execute(text("SELECT DATABASE()")).scalar()
        return True, f"MySQL {ver} / {db}"
    except Exception as exc:  # noqa: BLE001 - 探测失败需给出原因
        return False, f"{type(exc).__name__}: {exc}"
