"""集成中心存储（任务 4.8）：本地模型 / MCP 服务 / 第三方智能体。

两种后端，对外返回同一份驼峰结构：

* **MySQL**：``integration_local_model`` / ``integration_mcp_server`` /
  ``integration_agent_app``；
* **JSON**：``data/integrations.json``（进程内锁 + 原子写）。

密文列（API Key / Token / MCP Headers）在两种后端中都只存 AES-GCM 密文：
MySQL 为 VARBINARY，JSON 为 base64 字符串。**本模块不打印、不返回明文**，
明文只由服务层在「测试连通性」或「解析本地模型路由」时短暂解密。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, insert, select, update

from app.config import settings
from app.db import models as M
from app.db.session import db_session, db_transaction

logger = logging.getLogger(__name__)

INTEGRATION_PATH: Path = settings.base_dir / "data" / "integrations.json"
_lock = threading.RLock()


def _mysql() -> bool:
    return settings.store_label == "mysql"


def _now() -> datetime:
    return datetime.now()


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _b64(blob: bytes | None) -> str | None:
    return base64.b64encode(blob).decode("ascii") if blob else None


def _unb64(text) -> bytes | None:
    if not text:
        return None
    try:
        return base64.b64decode(text)
    except Exception:  # noqa: BLE001
        return None


def _empty_json() -> dict:
    return {
        "localModels": [],
        "mcpServers": [],
        "agentApps": [],
        "nextLocalId": 1,
        "nextMcpId": 1,
        "nextAgentId": 1,
    }


def _read_json() -> dict:
    if not INTEGRATION_PATH.is_file():
        data = _empty_json()
    else:
        try:
            data = json.loads(INTEGRATION_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 文件损坏时重建，避免整页不可用
            logger.warning("集成配置文件损坏，已重建：%s", INTEGRATION_PATH)
            data = _empty_json()
    for key, value in _empty_json().items():
        data.setdefault(key, value)
    for key in ("localModels", "mcpServers", "agentApps"):
        if not isinstance(data.get(key), list):
            data[key] = []
    return data


def _write_json(data: dict) -> None:
    INTEGRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INTEGRATION_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INTEGRATION_PATH)


# ================================================================== 本地模型
def _local_view(row) -> dict:
    return {
        "localId": int(row.local_id),
        "code": row.model_code,
        "name": row.model_name,
        "baseUrl": row.base_url,
        "remoteModel": row.remote_model,
        "timeout": float(row.timeout or 30),
        "status": int(row.status or 0),
        "active": int(row.is_active or 0),
        "remark": row.remark or "",
        "lastTestAt": _iso(row.last_test_at),
        "lastTestState": row.last_test_state or "",
        "lastTestError": row.last_test_error or "",
        "lastLatencyMs": int(row.last_latency_ms or 0),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
        "_secret": bytes(row.api_key_enc) if row.api_key_enc else None,
    }


def _json_local(item: dict) -> dict:
    record = dict(item)
    record["_secret"] = _unb64(item.get("_secretB64"))
    record.pop("_secretB64", None)
    return record


def _to_json_local(item: dict) -> dict:
    record = {k: v for k, v in item.items() if k != "_secret"}
    record["_secretB64"] = _b64(item.get("_secret"))
    return record


def list_local_models() -> list[dict]:
    if _mysql():
        with db_session() as conn:
            rows = conn.execute(
                select(M.IntegrationLocalModel).order_by(
                    M.IntegrationLocalModel.is_active.desc(),
                    M.IntegrationLocalModel.local_id,
                )
            ).all()
        return [_local_view(r) for r in rows]
    with _lock:
        data = _read_json()
    items = [_json_local(i) for i in data["localModels"]]
    items.sort(key=lambda r: (-int(r.get("active") or 0), int(r.get("localId") or 0)))
    return items


def get_local_model(local_id: int) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.IntegrationLocalModel).where(
                    M.IntegrationLocalModel.local_id == int(local_id)
                )
            ).first()
        return _local_view(row) if row else None
    with _lock:
        data = _read_json()
    for item in data["localModels"]:
        if int(item.get("localId") or 0) == int(local_id):
            return _json_local(item)
    return None


def find_local_model_by_code(code: str) -> dict | None:
    code = str(code or "").strip()
    if not code:
        return None
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.IntegrationLocalModel).where(
                    M.IntegrationLocalModel.model_code == code
                )
            ).first()
        return _local_view(row) if row else None
    with _lock:
        data = _read_json()
    for item in data["localModels"]:
        if item.get("code") == code:
            return _json_local(item)
    return None


def active_local_model() -> dict | None:
    """当前启用的本地模型（用于 ``LLM_PROVIDER=local`` 的路由）。"""
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.IntegrationLocalModel)
                .where(M.IntegrationLocalModel.is_active == 1)
                .order_by(M.IntegrationLocalModel.local_id)
            ).first()
        return _local_view(row) if row else None
    with _lock:
        data = _read_json()
    for item in data["localModels"]:
        if int(item.get("active") or 0) == 1:
            return _json_local(item)
    return None


def create_local_model(item: dict) -> dict:
    now = _now()
    if _mysql():
        with db_transaction() as conn:
            res = conn.execute(
                insert(M.IntegrationLocalModel).values(
                    model_code=item["code"],
                    model_name=item["name"],
                    base_url=item["baseUrl"],
                    remote_model=item["remoteModel"],
                    api_key_enc=item.get("_secret"),
                    timeout=float(item.get("timeout") or 30),
                    status=int(item.get("status", 1)),
                    is_active=int(item.get("active") or 0),
                    remark=str(item.get("remark") or "")[:255],
                    created_at=now,
                    updated_at=now,
                )
            )
            local_id = int(res.inserted_primary_key[0])
        if item.get("active"):
            activate_local_model(local_id)
        return get_local_model(local_id) or {}
    with _lock:
        data = _read_json()
        local_id = int(data.get("nextLocalId") or 1)
        data["nextLocalId"] = local_id + 1
        record = {
            "localId": local_id,
            "code": item["code"],
            "name": item["name"],
            "baseUrl": item["baseUrl"],
            "remoteModel": item["remoteModel"],
            "timeout": float(item.get("timeout") or 30),
            "status": int(item.get("status", 1)),
            "active": 0,
            "remark": str(item.get("remark") or "")[:255],
            "lastTestAt": None,
            "lastTestState": "",
            "lastTestError": "",
            "lastLatencyMs": 0,
            "createdAt": _iso(now),
            "updatedAt": _iso(now),
            "_secret": item.get("_secret"),
        }
        data["localModels"].append(_to_json_local(record))
        _write_json(data)
        if item.get("active"):
            activate_local_model(local_id)
        return get_local_model(local_id) or record


def update_local_model(local_id: int, fields: dict) -> dict | None:
    mapping = {
        "code": "model_code",
        "name": "model_name",
        "baseUrl": "base_url",
        "remoteModel": "remote_model",
        "timeout": "timeout",
        "status": "status",
        "remark": "remark",
        "_secret": "api_key_enc",
    }
    if _mysql():
        values = {
            mapping[k]: v for k, v in fields.items() if k in mapping
        }
        values["updated_at"] = _now()
        with db_transaction() as conn:
            conn.execute(
                update(M.IntegrationLocalModel)
                .where(M.IntegrationLocalModel.local_id == int(local_id))
                .values(**values)
            )
        return get_local_model(local_id)
    with _lock:
        data = _read_json()
        for item in data["localModels"]:
            if int(item.get("localId") or 0) != int(local_id):
                continue
            for key, value in fields.items():
                if key == "_secret":
                    item["_secretB64"] = _b64(value)
                elif key in {"code", "name", "baseUrl", "remoteModel", "timeout",
                             "status", "remark"}:
                    item[key] = value
            item["updatedAt"] = _iso(_now())
            _write_json(data)
            return _json_local(item)
    return None


def delete_local_model(local_id: int) -> bool:
    if _mysql():
        with db_transaction() as conn:
            res = conn.execute(
                delete(M.IntegrationLocalModel).where(
                    M.IntegrationLocalModel.local_id == int(local_id)
                )
            )
            return bool(res.rowcount)
    with _lock:
        data = _read_json()
        before = len(data["localModels"])
        data["localModels"] = [
            i for i in data["localModels"] if int(i.get("localId") or 0) != int(local_id)
        ]
        if len(data["localModels"]) == before:
            return False
        _write_json(data)
        return True


def activate_local_model(local_id: int) -> bool:
    """把指定本地模型设为当前模型；全表至多一条 ``is_active=1``。"""
    if _mysql():
        with db_transaction() as conn:
            exists = conn.execute(
                select(M.IntegrationLocalModel.local_id).where(
                    M.IntegrationLocalModel.local_id == int(local_id)
                )
            ).first()
            if not exists:
                return False
            conn.execute(update(M.IntegrationLocalModel).values(is_active=0))
            conn.execute(
                update(M.IntegrationLocalModel)
                .where(M.IntegrationLocalModel.local_id == int(local_id))
                .values(is_active=1, updated_at=_now())
            )
        return True
    with _lock:
        data = _read_json()
        found = False
        for item in data["localModels"]:
            if int(item.get("localId") or 0) == int(local_id):
                found = True
            item["active"] = int(int(item.get("localId") or 0) == int(local_id))
        if not found:
            return False
        _write_json(data)
        return True


def record_local_test(local_id: int, state: str, latency_ms: int, error: str = "") -> None:
    if _mysql():
        with db_transaction() as conn:
            conn.execute(
                update(M.IntegrationLocalModel)
                .where(M.IntegrationLocalModel.local_id == int(local_id))
                .values(
                    last_test_at=_now(),
                    last_test_state=state[:16],
                    last_test_error=error[:512],
                    last_latency_ms=int(latency_ms or 0),
                )
            )
        return
    with _lock:
        data = _read_json()
        for item in data["localModels"]:
            if int(item.get("localId") or 0) == int(local_id):
                item.update({
                    "lastTestAt": _iso(_now()),
                    "lastTestState": state[:16],
                    "lastTestError": error[:512],
                    "lastLatencyMs": int(latency_ms or 0),
                })
                _write_json(data)
                return


# ================================================================== MCP
def _mcp_view(row) -> dict:
    args = row.args_json if isinstance(row.args_json, list) else []
    tools = row.tools_json if isinstance(row.tools_json, list) else []
    return {
        "serverId": int(row.server_id),
        "code": row.server_code,
        "name": row.server_name,
        "transport": row.transport,
        "endpoint": row.endpoint or "",
        "command": row.command or "",
        "args": [str(a) for a in args],
        "status": int(row.status or 0),
        "remark": row.remark or "",
        "lastTestAt": _iso(row.last_test_at),
        "lastTestState": row.last_test_state or "",
        "lastTestError": row.last_test_error or "",
        "lastLatencyMs": int(row.last_latency_ms or 0),
        "tools": tools,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
        "_secret": bytes(row.headers_enc) if row.headers_enc else None,
    }


def _json_mcp(item: dict) -> dict:
    record = dict(item)
    record["_secret"] = _unb64(item.get("_secretB64"))
    record.pop("_secretB64", None)
    return record


def _to_json_mcp(item: dict) -> dict:
    record = {k: v for k, v in item.items() if k != "_secret"}
    record["_secretB64"] = _b64(item.get("_secret"))
    return record


def list_mcp_servers() -> list[dict]:
    if _mysql():
        with db_session() as conn:
            rows = conn.execute(
                select(M.IntegrationMcpServer).order_by(M.IntegrationMcpServer.server_id)
            ).all()
        return [_mcp_view(r) for r in rows]
    with _lock:
        data = _read_json()
    return [_json_mcp(i) for i in data["mcpServers"]]


def get_mcp_server(server_id: int) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.IntegrationMcpServer).where(
                    M.IntegrationMcpServer.server_id == int(server_id)
                )
            ).first()
        return _mcp_view(row) if row else None
    with _lock:
        data = _read_json()
    for item in data["mcpServers"]:
        if int(item.get("serverId") or 0) == int(server_id):
            return _json_mcp(item)
    return None


def create_mcp_server(item: dict) -> dict:
    now = _now()
    if _mysql():
        with db_transaction() as conn:
            res = conn.execute(
                insert(M.IntegrationMcpServer).values(
                    server_code=item["code"],
                    server_name=item["name"],
                    transport=item["transport"],
                    endpoint=item.get("endpoint") or "",
                    command=item.get("command") or "",
                    args_json=item.get("args") or [],
                    headers_enc=item.get("_secret"),
                    status=int(item.get("status", 1)),
                    remark=str(item.get("remark") or "")[:255],
                    created_at=now,
                    updated_at=now,
                )
            )
            server_id = int(res.inserted_primary_key[0])
        return get_mcp_server(server_id) or {}
    with _lock:
        data = _read_json()
        server_id = int(data.get("nextMcpId") or 1)
        data["nextMcpId"] = server_id + 1
        record = {
            "serverId": server_id,
            "code": item["code"],
            "name": item["name"],
            "transport": item["transport"],
            "endpoint": item.get("endpoint") or "",
            "command": item.get("command") or "",
            "args": item.get("args") or [],
            "status": int(item.get("status", 1)),
            "remark": str(item.get("remark") or "")[:255],
            "lastTestAt": None,
            "lastTestState": "",
            "lastTestError": "",
            "lastLatencyMs": 0,
            "tools": [],
            "createdAt": _iso(now),
            "updatedAt": _iso(now),
            "_secret": item.get("_secret"),
        }
        data["mcpServers"].append(_to_json_mcp(record))
        _write_json(data)
        return get_mcp_server(server_id) or record


def update_mcp_server(server_id: int, fields: dict) -> dict | None:
    mapping = {
        "code": "server_code",
        "name": "server_name",
        "transport": "transport",
        "endpoint": "endpoint",
        "command": "command",
        "args": "args_json",
        "status": "status",
        "remark": "remark",
        "_secret": "headers_enc",
    }
    if _mysql():
        values = {mapping[k]: v for k, v in fields.items() if k in mapping}
        values["updated_at"] = _now()
        with db_transaction() as conn:
            conn.execute(
                update(M.IntegrationMcpServer)
                .where(M.IntegrationMcpServer.server_id == int(server_id))
                .values(**values)
            )
        return get_mcp_server(server_id)
    with _lock:
        data = _read_json()
        for item in data["mcpServers"]:
            if int(item.get("serverId") or 0) != int(server_id):
                continue
            for key, value in fields.items():
                if key == "_secret":
                    item["_secretB64"] = _b64(value)
                elif key in {"code", "name", "transport", "endpoint", "command",
                             "args", "status", "remark"}:
                    item[key] = value
            item["updatedAt"] = _iso(_now())
            _write_json(data)
            return _json_mcp(item)
    return None


def delete_mcp_server(server_id: int) -> bool:
    if _mysql():
        with db_transaction() as conn:
            res = conn.execute(
                delete(M.IntegrationMcpServer).where(
                    M.IntegrationMcpServer.server_id == int(server_id)
                )
            )
            return bool(res.rowcount)
    with _lock:
        data = _read_json()
        before = len(data["mcpServers"])
        data["mcpServers"] = [
            i for i in data["mcpServers"] if int(i.get("serverId") or 0) != int(server_id)
        ]
        if len(data["mcpServers"]) == before:
            return False
        _write_json(data)
        return True


def record_mcp_test(
    server_id: int,
    state: str,
    latency_ms: int,
    error: str = "",
    tools: list | None = None,
) -> None:
    if _mysql():
        values = {
            "last_test_at": _now(),
            "last_test_state": state[:16],
            "last_test_error": error[:512],
            "last_latency_ms": int(latency_ms or 0),
        }
        if tools is not None:
            values["tools_json"] = tools
        with db_transaction() as conn:
            conn.execute(
                update(M.IntegrationMcpServer)
                .where(M.IntegrationMcpServer.server_id == int(server_id))
                .values(**values)
            )
        return
    with _lock:
        data = _read_json()
        for item in data["mcpServers"]:
            if int(item.get("serverId") or 0) == int(server_id):
                item.update({
                    "lastTestAt": _iso(_now()),
                    "lastTestState": state[:16],
                    "lastTestError": error[:512],
                    "lastLatencyMs": int(latency_ms or 0),
                })
                if tools is not None:
                    item["tools"] = tools
                _write_json(data)
                return


# ================================================================== 第三方智能体
def _agent_view(row) -> dict:
    return {
        "appId": int(row.app_id),
        "code": row.app_code,
        "name": row.app_name,
        "kind": row.kind,
        "baseUrl": row.base_url,
        "remoteAppId": row.remote_app_id or "",
        "status": int(row.status or 0),
        "remark": row.remark or "",
        "lastTestAt": _iso(row.last_test_at),
        "lastTestState": row.last_test_state or "",
        "lastTestError": row.last_test_error or "",
        "lastLatencyMs": int(row.last_latency_ms or 0),
        "lastReply": row.last_reply or "",
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
        "_secret": bytes(row.token_enc) if row.token_enc else None,
    }


def _json_agent(item: dict) -> dict:
    record = dict(item)
    record["_secret"] = _unb64(item.get("_secretB64"))
    record.pop("_secretB64", None)
    return record


def _to_json_agent(item: dict) -> dict:
    record = {k: v for k, v in item.items() if k != "_secret"}
    record["_secretB64"] = _b64(item.get("_secret"))
    return record


def list_agent_apps() -> list[dict]:
    if _mysql():
        with db_session() as conn:
            rows = conn.execute(
                select(M.IntegrationAgentApp).order_by(M.IntegrationAgentApp.app_id)
            ).all()
        return [_agent_view(r) for r in rows]
    with _lock:
        data = _read_json()
    return [_json_agent(i) for i in data["agentApps"]]


def get_agent_app(app_id: int) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.IntegrationAgentApp).where(
                    M.IntegrationAgentApp.app_id == int(app_id)
                )
            ).first()
        return _agent_view(row) if row else None
    with _lock:
        data = _read_json()
    for item in data["agentApps"]:
        if int(item.get("appId") or 0) == int(app_id):
            return _json_agent(item)
    return None


def create_agent_app(item: dict) -> dict:
    now = _now()
    if _mysql():
        with db_transaction() as conn:
            res = conn.execute(
                insert(M.IntegrationAgentApp).values(
                    app_code=item["code"],
                    app_name=item["name"],
                    kind=item["kind"],
                    base_url=item["baseUrl"],
                    remote_app_id=item.get("remoteAppId") or "",
                    token_enc=item.get("_secret"),
                    status=int(item.get("status", 1)),
                    remark=str(item.get("remark") or "")[:255],
                    created_at=now,
                    updated_at=now,
                )
            )
            app_id = int(res.inserted_primary_key[0])
        return get_agent_app(app_id) or {}
    with _lock:
        data = _read_json()
        app_id = int(data.get("nextAgentId") or 1)
        data["nextAgentId"] = app_id + 1
        record = {
            "appId": app_id,
            "code": item["code"],
            "name": item["name"],
            "kind": item["kind"],
            "baseUrl": item["baseUrl"],
            "remoteAppId": item.get("remoteAppId") or "",
            "status": int(item.get("status", 1)),
            "remark": str(item.get("remark") or "")[:255],
            "lastTestAt": None,
            "lastTestState": "",
            "lastTestError": "",
            "lastLatencyMs": 0,
            "lastReply": "",
            "createdAt": _iso(now),
            "updatedAt": _iso(now),
            "_secret": item.get("_secret"),
        }
        data["agentApps"].append(_to_json_agent(record))
        _write_json(data)
        return get_agent_app(app_id) or record


def update_agent_app(app_id: int, fields: dict) -> dict | None:
    mapping = {
        "code": "app_code",
        "name": "app_name",
        "kind": "kind",
        "baseUrl": "base_url",
        "remoteAppId": "remote_app_id",
        "status": "status",
        "remark": "remark",
        "_secret": "token_enc",
    }
    if _mysql():
        values = {mapping[k]: v for k, v in fields.items() if k in mapping}
        values["updated_at"] = _now()
        with db_transaction() as conn:
            conn.execute(
                update(M.IntegrationAgentApp)
                .where(M.IntegrationAgentApp.app_id == int(app_id))
                .values(**values)
            )
        return get_agent_app(app_id)
    with _lock:
        data = _read_json()
        for item in data["agentApps"]:
            if int(item.get("appId") or 0) != int(app_id):
                continue
            for key, value in fields.items():
                if key == "_secret":
                    item["_secretB64"] = _b64(value)
                elif key in {"code", "name", "kind", "baseUrl", "remoteAppId",
                             "status", "remark"}:
                    item[key] = value
            item["updatedAt"] = _iso(_now())
            _write_json(data)
            return _json_agent(item)
    return None


def delete_agent_app(app_id: int) -> bool:
    if _mysql():
        with db_transaction() as conn:
            res = conn.execute(
                delete(M.IntegrationAgentApp).where(
                    M.IntegrationAgentApp.app_id == int(app_id)
                )
            )
            return bool(res.rowcount)
    with _lock:
        data = _read_json()
        before = len(data["agentApps"])
        data["agentApps"] = [
            i for i in data["agentApps"] if int(i.get("appId") or 0) != int(app_id)
        ]
        if len(data["agentApps"]) == before:
            return False
        _write_json(data)
        return True


def record_agent_test(
    app_id: int,
    state: str,
    latency_ms: int,
    error: str = "",
    reply: str = "",
) -> None:
    if _mysql():
        with db_transaction() as conn:
            conn.execute(
                update(M.IntegrationAgentApp)
                .where(M.IntegrationAgentApp.app_id == int(app_id))
                .values(
                    last_test_at=_now(),
                    last_test_state=state[:16],
                    last_test_error=error[:512],
                    last_latency_ms=int(latency_ms or 0),
                    last_reply=reply[:512],
                )
            )
        return
    with _lock:
        data = _read_json()
        for item in data["agentApps"]:
            if int(item.get("appId") or 0) == int(app_id):
                item.update({
                    "lastTestAt": _iso(_now()),
                    "lastTestState": state[:16],
                    "lastTestError": error[:512],
                    "lastLatencyMs": int(latency_ms or 0),
                    "lastReply": reply[:512],
                })
                _write_json(data)
                return


def reset_store() -> None:
    """测试用：清空三类集成配置。"""
    if _mysql():
        with db_transaction() as conn:
            conn.execute(delete(M.IntegrationLocalModel))
            conn.execute(delete(M.IntegrationMcpServer))
            conn.execute(delete(M.IntegrationAgentApp))
        return
    with _lock:
        _write_json(_empty_json())
