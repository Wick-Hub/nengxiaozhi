"""敏感词双向检测（任务 1.12）。

在**提问**与**回答**两个方向做检测，命中即返回风控话术。原型无此能力，属新增。

词表策略（任务 2.10 起）：

- 内置 :data:`DEFAULT_WORDS` 只覆盖两类**意图**——「提示词注入 / 越狱」与
  「批量导出个人信息」，不含具体违规词条，**始终生效**；
- ``KB_STORE=mysql`` 时**优先**加载 ``sensitive_word`` 表中启用的 ``kind='sensitive'`` 词条；
- 数据库不可用或词表为空时，回退到 ``SENSITIVE_WORDS_FILE``（每行一词，``#`` 开头为注释）；
- 文件也缺失时只用内置默认，**不会导致启动失败**。

词表缓存 30 秒；管理端增删改后调用 :func:`invalidate` 立即重载，做到「入库即生效」。
匹配为大小写不敏感的子串查找；词表规模 <1 万时性能足够，无需 Aho–Corasick。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from app.config import settings
from app.core import replies

logger = logging.getLogger(__name__)

#: 风控话术（提问侧直接返回；回答侧替换整段）。任务 2.11 起统一取自平台默认。
RISK_REPLY = replies.DEFAULT_RISK_REPLY

#: 内置默认词表（意图类，不含具体违规词条）
DEFAULT_WORDS: tuple[str, ...] = (
    # 提示词注入 / 越狱
    "忽略以上",
    "忽略之前",
    "忽略上述",
    "无视以上指令",
    "ignore previous",
    "ignore above",
    "ignore all previous",
    "jailbreak",
    "越狱模式",
    "开发者模式",
    "系统提示词",
    "system prompt",
    # 批量导出个人信息意图
    "导出全部用户",
    "导出所有学号",
    "列出所有手机号",
    "所有学生的身份证",
)

_words: tuple[str, ...] | None = None
_lock = threading.Lock()
#: 缓存有效期；多进程部署时其他进程最迟 30 秒感知到词库变更
_CACHE_TTL = 30.0
_loaded_at = 0.0
_source = "builtin"


def _read_file(path: Path) -> tuple[str, ...]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    words: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words.append(line)
    return tuple(words)


def _load_db_words() -> tuple[str, ...]:
    """从 MySQL 词库读取启用的敏感词；不可用或非 MySQL 模式返回空。"""
    if settings.store_label != "mysql":
        return ()
    try:
        from app.db import sensitive_repo
        from app.db.session import db_session

        with db_session() as conn:
            return tuple(sensitive_repo.enabled_words(conn))
    except Exception as exc:  # noqa: BLE001 - 词库不可用不能拖垮问答
        logger.warning("敏感词库读取失败，回退文件/内置词表：%s", exc)
        return ()


def load_words(force: bool = False) -> tuple[str, ...]:
    """加载词表，结果缓存 30 秒；``force=True`` 强制重载。

    来源优先级：MySQL 词库 → 外部文件 → 内置默认；内置默认始终并入。
    """
    global _words, _loaded_at, _source
    with _lock:
        if _words is not None and not force and (time.monotonic() - _loaded_at) < _CACHE_TTL:
            return _words

        db_words = _load_db_words()
        if db_words:
            extra, source = db_words, "db"
        else:
            raw_path = (settings.sensitive_words_file or "").strip()
            extra = _read_file(Path(raw_path)) if raw_path else ()
            source = "file" if extra else "builtin"

        merged = tuple(dict.fromkeys(DEFAULT_WORDS + tuple(extra)))
        _words = merged
        _source = source
        _loaded_at = time.monotonic()
        logger.info(
            "敏感词表已加载：内置 %d 条 + %s %d 条 = %d 条",
            len(DEFAULT_WORDS),
            source,
            len(extra),
            len(merged),
        )
        return _words


def reload() -> tuple[str, ...]:
    """重载词表（文件变更或服务启动后手工刷新时调用）。"""
    return load_words(force=True)


def invalidate() -> None:
    """词库变更后失效缓存（管理端 CRUD 成功后调用）。

    只清缓存不立即查库：下一次问答会重新加载，避免写接口被词库查询拖慢。
    """
    global _words, _loaded_at
    with _lock:
        _words = None
        _loaded_at = 0.0


def check(text: str | None, enabled: bool | None = None) -> str | None:
    """检测文本，命中返回命中的词（便于审计），未命中返回 ``None``。"""
    if not text:
        return None
    if enabled is None:
        enabled = settings.sensitive_check_enabled
    if not enabled:
        return None

    lowered = text.lower()
    for word in load_words():
        if word.lower() in lowered:
            return word
    return None


def is_blocked(text: str | None) -> bool:
    return check(text) is not None


def check_question(text: str | None) -> str | None:
    """提问侧检测（命中应直接返回风控话术，不进入检索与生成）。"""
    return check(text)


def check_answer(text: str | None) -> str | None:
    """回答侧检测（命中应替换为风控话术，避免不当内容外泄）。"""
    return check(text)


def stats() -> dict:
    return {
        "enabled": settings.sensitive_check_enabled,
        "words": len(load_words()),
        "builtin": len(DEFAULT_WORDS),
        "file": settings.sensitive_words_file or "",
        "source": _source,
        "cacheTtl": _CACHE_TTL,
    }
