"""索引加载层测试（任务 1.14）。

覆盖：
- 默认 JSON 索引可加载，计数与文档一致（7 文档 / 35 切片）
- 缓存命中与 clear_index 失效
- 文件缺失 / 损坏时返回带 error 的空快照，且不抛异常
- MySQL 模式：可用时加载已发布知识；不可用时降级为带 error 的空快照（不抛异常）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import index_store
from app.core.chunker import Chunk
from app.core.index_store import (
    IndexSnapshot,
    clear_index,
    get_index,
    index_stats,
    load_index,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_index()
    yield
    clear_index()


def test_load_default_index() -> None:
    idx = get_index()
    assert idx.error is None
    assert idx.ok is True
    assert idx.source == "json"
    # 与 kb-engine/data/store.json 的既定规模一致
    assert len(idx.docs) == 7
    assert len(idx.chunks) == 35
    assert idx.model, "索引应记录生成向量所用的模型名"
    assert all(isinstance(c, Chunk) for c in idx.chunks)
    assert all(c.chunk_id for c in idx.chunks)


def test_index_stats_shape() -> None:
    stats = index_stats()
    assert stats["docs"] == 7
    assert stats["chunks"] == 35
    assert stats["source"] == "json"
    assert stats["error"] is None
    assert Path(stats["path"]).is_file()


def test_cache_hit_and_clear() -> None:
    first = get_index()
    second = get_index()
    assert first is second, "第二次读取应命中进程内缓存（同一对象）"

    clear_index()
    third = get_index()
    assert third is not first, "clear_index 后应重新加载"
    assert len(third.chunks) == len(first.chunks)


def test_missing_file_returns_error_snapshot(monkeypatch, tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(index_store.settings, "store_path", missing)
    monkeypatch.setattr(
        index_store,
        "candidate_paths",
        lambda: [missing],  # 同时屏蔽原型回退路径
    )
    clear_index()

    idx = load_index(force=True)
    assert idx.error is not None
    assert idx.ok is False
    assert idx.chunks == []


def test_corrupt_file_returns_error_snapshot(monkeypatch, tmp_path: Path) -> None:
    bad = tmp_path / "store.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(index_store.settings, "store_path", bad)
    monkeypatch.setattr(index_store, "candidate_paths", lambda: [bad])
    clear_index()

    idx = load_index(force=True)
    assert idx.error is not None
    assert "索引加载失败" in idx.error
    assert idx.chunks == []


def test_mysql_mode_loads_or_degrades(monkeypatch) -> None:
    """MySQL 模式：可用则加载已发布知识，不可用则降级为 error 快照（均不抛异常）。"""
    monkeypatch.setattr(index_store.settings, "kb_store", "mysql")
    clear_index()
    idx = load_index(force=True)

    assert idx.source == "mysql"
    if idx.ok:
        # 数据库可用：应读到已发布知识（与原型索引同规模）
        assert idx.chunks, "MySQL 可用时应加载到切片"
        assert idx.docs
        assert idx.path.startswith("mysql://")
        assert all(c.embedding for c in idx.chunks)
    else:
        # 数据库不可用 / 无数据：降级为空快照
        assert idx.chunks == []
        assert idx.docs == []
        assert idx.error


def test_mysql_mode_bad_credentials_degrades(monkeypatch) -> None:
    """凭据错误必须降级为 error 快照，绝不把异常抛给调用方。"""
    from app.db import session as db_session_mod

    monkeypatch.setattr(index_store.settings, "kb_store", "mysql")
    monkeypatch.setattr(index_store.settings, "db_password", "definitely-wrong-password")
    db_session_mod.dispose_engine()
    clear_index()
    try:
        idx = load_index(force=True)
        assert idx.source == "mysql"
        assert idx.ok is False
        assert idx.chunks == []
        assert "MySQL" in idx.error
    finally:
        db_session_mod.dispose_engine()
        clear_index()


def test_json_roundtrip_to_chunk(tmp_path: Path) -> None:
    """store.json 的一行能无损还原为 Chunk（供 MySQL 行复用同一契约）。"""
    data = {
        "chunkId": "d1::0",
        "docId": "d1",
        "seqNo": 0,
        "charCount": 12,
        "dept": "网络中心",
        "ktype": "办事指南",
        "visibleRoles": ["全部"],
        "titlePath": "网络 > 开通",
        "blockType": "para",
        "text": "校园网开通流程。",
        "embedText": "校园网开通流程。",
        "embedding": [0.1, 0.2, 0.3],
    }
    p = tmp_path / "store.json"
    p.write_text(json.dumps({"docs": [{"docId": "d1"}], "chunks": [data]}), encoding="utf-8")

    snap = index_store._load_json(p)
    assert snap.error is None
    assert len(snap.chunks) == 1
    c = snap.chunks[0]
    assert c.chunk_id == "d1::0"
    assert c.visible_roles == ["全部"]
    assert c.embedding == [0.1, 0.2, 0.3]
    assert c.to_dict()["titlePath"] == "网络 > 开通"


def test_empty_snapshot_ok_flag() -> None:
    assert IndexSnapshot().ok is True
    assert IndexSnapshot(error="x").ok is False
