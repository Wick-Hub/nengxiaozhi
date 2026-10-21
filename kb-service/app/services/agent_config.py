"""助手配置服务（任务 2.3 / M-A-04·05·09）。

职责边界：

* **本任务**：知识文件夹清单（供「知识范围」勾选）、助手检索配置的读写、
  助手对话测试（用该助手的提示词 + 检索参数跑一次**不落库**的问答）。
* **不在本任务**：提示词本身的 CRUD（已由任务 2.2 的 ``PATCH /agents/{id}`` 提供，
  本任务只负责让它**在生成层真正生效**）。

设计要点：

* **检索配置的「缺省」是有语义的**：``0`` / ``[]`` 表示「沿用全局 / 不限定」，
  因此这里用**增量合并**（只覆盖显式传入的键），不做整体替换——管理页按页签
  分别保存，整体替换会让「只改召回条数」把知识范围一起清空。
* **对话测试绝不落库**（``save=False``）：运营调试助手时若写入会话日志与
  「未识别问题池」，会污染会话日志（2.1）与反哺闭环，事后无法分辨哪些是真实提问。
"""

from __future__ import annotations

import logging
from collections import defaultdict

from app.config import settings
from app.core import agent_runtime, llm_router
from app.core.index_store import get_index
from app.core.pipeline import answer_question
from app.db import agent_store
from app.db.agent_store import (
    RERANK_TOPN_RANGE,
    TOP_K_RANGE,
    merge_retrieval,
    normalize_retrieval,
    validate_retrieval,
)
from app.db.models import KTYPE_NAME

logger = logging.getLogger(__name__)

#: 允许通过检索配置接口写入的键（防越权写入 status 等其它字段）
_RETRIEVAL_KEYS = ("topK", "rerankTopn", "minScore", "depts", "ktypes")


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


# ------------------------------------------------------------------ 全局默认
def defaults() -> dict:
    """「沿用全局」时真正生效的数值，供管理页直接展示（避免运营靠猜）。"""
    return {
        "topK": settings.top_k,
        "rerankTopn": settings.rerank_topn,
        "rerankEnabled": bool(settings.rerank_enabled),
        "retrieveMode": settings.retrieve_mode,
    }


def ranges() -> dict:
    """各字段的合法区间与「0 表示什么」，与后端校验共用同一组常量。"""
    return {
        "topK": {"min": TOP_K_RANGE[0], "max": TOP_K_RANGE[1], "zeroMeans": "沿用全局"},
        "rerankTopn": {
            "min": RERANK_TOPN_RANGE[0],
            "max": RERANK_TOPN_RANGE[1],
            "zeroMeans": "沿用全局",
        },
        "minScore": {"min": 0.0, "max": 1.0, "zeroMeans": "不设阈值"},
    }


# ------------------------------------------------------------------ 模型与生成参数（任务 2.4）
def model_defaults() -> dict:
    """「留空」时真正生效的模型与生成参数（任务 2.4 / M-A-06）。

    前端**不写死第二份**默认值——否则改了 ``.env`` 管理页还在显示旧数字，
    运营会以为改动没生效。
    """
    route = llm_router.resolve(None)
    return {
        "modelCode": llm_router.DEFAULT_MODEL_CODE,
        "modelLabel": f"全局默认（{route.label}）",
        "remoteModel": route.remote_model,
        "baseUrl": route.base_url,
        "hasApiKey": bool(route.api_key),
        "temperature": settings.llm_temperature,
        # 平台默认**不下发 topP**（``None``），交给厂商默认值；前端据此显示「跟随厂商」
        "topP": None,
        "maxTokens": settings.llm_max_tokens,
    }


def model_ranges() -> dict:
    """生成参数的合法区间（与 ``agent_store`` 的校验共用常量）。"""
    return {
        "temperature": {"min": agent_store.TEMPERATURE_RANGE[0],
                        "max": agent_store.TEMPERATURE_RANGE[1],
                        "emptyMeans": "沿用全局"},
        "topP": {"min": agent_store.TOP_P_RANGE[0], "max": agent_store.TOP_P_RANGE[1],
                 "emptyMeans": "沿用厂商默认"},
        "maxTokens": {"min": agent_store.MAX_TOKENS_RANGE[0],
                      "max": agent_store.MAX_TOKENS_RANGE[1],
                      "emptyMeans": "沿用全局"},
    }


def model_choices() -> list[dict]:
    """助手可绑定的模型下拉项：``default``（全局默认）在前，其余**全部来自网关**。

    架构变更后能小智不再有自己的模型台账 —— 可用模型由网关下发
    （``GET {LLM_BASE_URL}/models``，受业务 Key 白名单过滤）。
    所以这里不再读本地模型表，也不会出现「本地启用但网关不认」的状态。
    """
    from app.services import gateway_admin

    route = llm_router.resolve(None)
    options: list[dict] = [
        {
            "modelCode": llm_router.DEFAULT_MODEL_CODE,
            "modelName": f"全局默认（{route.label}）",
            "vendor": "",
            "vendorLabel": "系统",
            "remoteModel": route.remote_model,
            "isLocal": 0,
            "status": 1,
            "available": route.available or bool(settings.llm_mock),
            "isDefault": True,
        }
    ]
    for m in gateway_admin.list_remote_models()["items"]:
        options.append(
            {
                "modelCode": m["id"],
                "modelName": m["id"],
                "vendor": "",
                # 厂商信息在网关控制面，「owned_by」由网关回传（通常是网关自己）
                "vendorLabel": m.get("ownedBy") or "网关",
                "remoteModel": m["id"],
                "isLocal": 0,
                "status": 1,
                "available": bool(settings.llm_api_key) or bool(settings.llm_mock),
                "isDefault": False,
            }
        )
    return options


# ------------------------------------------------------------------ 知识文件夹
def _ktype_name(code: str) -> str:
    """知识类型显示名：索引里既有中文（``办事指南``）也有枚举（``policy``），两种都要兜住。"""
    return KTYPE_NAME.get(code) or code or "未分类"


def list_kb_folders() -> dict:
    """知识文件夹清单 = 「部门 × 知识类型」组合 + 各自的文档数/切片数。

    与 2.3 的「知识范围」勾选一一对应：勾了哪些文件夹，助手就只看这些切片
    （过滤发生在混合召回**之前**，见 :func:`app.core.pipeline.retrieve`）。
    """
    idx = get_index()
    if not idx.ok:
        return _fail(f"知识库不可用（存储模式 {settings.store_label}）：{idx.error}", 503)

    chunk_count: dict[tuple[str, str], int] = defaultdict(int)
    docs_of: dict[tuple[str, str], set[str]] = defaultdict(set)
    dept_count: dict[str, int] = defaultdict(int)
    ktype_count: dict[str, int] = defaultdict(int)

    for c in idx.chunks:
        key = (c.dept or "未分类", c.ktype or "未分类")
        chunk_count[key] += 1
        if c.doc_id:
            docs_of[key].add(str(c.doc_id))
        dept_count[key[0]] += 1
        ktype_count[key[1]] += 1

    folders = [
        {
            "dept": dept,
            "ktype": ktype,
            "ktypeName": _ktype_name(ktype),
            "docCount": len(docs_of.get((dept, ktype), ())),
            "chunkCount": chunk_count[(dept, ktype)],
        }
        for dept, ktype in sorted(chunk_count)
    ]
    return _ok(
        {
            "folders": folders,
            "depts": [
                {"name": name, "chunkCount": n}
                for name, n in sorted(dept_count.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
            "ktypes": [
                {"code": code, "name": _ktype_name(code), "chunkCount": n}
                for code, n in sorted(ktype_count.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
            "chunkTotal": len(idx.chunks),
            "docTotal": len(idx.docs),
        }
    )


# ------------------------------------------------------------------ 检索配置
def get_retrieval(agent_id: int) -> dict:
    """读取某助手的检索配置 + 全局默认值（未配置的项由前端显示为「沿用全局」）。"""
    found = agent_store.get_agent(agent_id)
    if found is None:
        return _fail(f"助手不存在：{agent_id}", 404)
    return _ok(
        {
            "agentId": found["agentId"],
            "agentCode": found["agentCode"],
            "agentName": found["agentName"],
            "retrievalConfig": normalize_retrieval(found.get("retrievalConfig")),
            "defaults": defaults(),
            "ranges": ranges(),
            # 2.11 起配置字段全部接线，保留空数组维持前端契约
            "pendingFields": [],
        }
    )


def update_retrieval(agent_id: int, payload: dict) -> dict:
    """增量更新检索配置（未传的键保持原值）。"""
    found = agent_store.get_agent(agent_id)
    if found is None:
        return _fail(f"助手不存在：{agent_id}", 404)

    clean = {k: v for k, v in (payload or {}).items() if k in _RETRIEVAL_KEYS}
    if not clean:
        return _fail("没有可更新的检索配置项（可选：topK/rerankTopn/minScore/depts/ktypes）")

    errors = validate_retrieval(clean)
    if errors:
        return _fail("；".join(errors), 400)
    merged = merge_retrieval(found.get("retrievalConfig"), clean)
    updated = agent_store.update_agent(agent_id, {"retrievalConfig": merged})
    if updated is None:
        return _fail(f"助手不存在：{agent_id}", 404)
    return _ok(
        {
            "agentId": updated["agentId"],
            "retrievalConfig": normalize_retrieval(updated.get("retrievalConfig")),
            "defaults": defaults(),
        }
    )


# ------------------------------------------------------------------ 对话测试
def test_chat(
    agent_id: int,
    question: str,
    role: str | None = None,
    top_k: int | None = None,
) -> dict:
    """用该助手的提示词 + 检索配置跑一次问答，**不落库**。

    返回体刻意把「生效参数」一并回传：运营人员在管理页能看到
    ``topK=5（沿用全局）``、``rerankTopn=30`` 这类信息，才能判断
    「回答不好」到底是提示词问题还是检索问题。
    """
    found = agent_store.get_agent(agent_id)
    if found is None:
        return _fail(f"助手不存在：{agent_id}", 404)

    rt = agent_runtime.resolve(agent_id, strict=True)
    if not rt.found:
        return _fail(rt.error or f"助手不可用：{agent_id}", 404)

    question = (question or "").strip()
    if not question:
        return _fail("请输入测试问题")
    if len(question) > 500:
        return _fail("测试问题最长 500 字")

    ans = answer_question(question, role=role, top_k=top_k, save=False, runtime=rt)

    eff_top_k = rt.effective_top_k(top_k)
    eff_rerank = rt.effective_rerank_topn()
    recall_k = max(eff_rerank, eff_top_k) if settings.rerank_enabled else eff_top_k

    return _ok(
        {
            "question": question,
            "role": role,
            "answer": ans.text,
            "refused": ans.refused,
            "confidence": ans.confidence,
            "citations": ans.citations,
            "retrieved": ans.retrieved,
            "model": ans.model,
            "latencyMs": ans.latency_ms,
            "firstTokenMs": ans.first_token_ms,
            "usage": ans.usage or {},
            "saved": False,
            "agent": {
                "agentId": rt.agent_id,
                "agentCode": rt.agent_code,
                "agentName": rt.agent_name,
                "hasSystemPrompt": bool(rt.system_prompt),
            },
            "retrieval": {
                "topK": eff_top_k,
                "topKSource": "助手配置" if rt.top_k else "全局默认",
                "rerankTopn": eff_rerank,
                "rerankTopnSource": "助手配置" if rt.rerank_topn else "全局默认",
                "rerankEnabled": bool(settings.rerank_enabled),
                "recallK": recall_k,
                "minScore": rt.min_score,
                "depts": list(rt.depts),
                "ktypes": list(rt.ktypes),
            },
            # 任务 2.4（M-A-06）：模型路由与生成参数的**生效值 + 来源**。
            # 与 retrieval 同形，让运营一眼看出「回答不好」是提示词、检索还是模型的问题。
            "model": {
                "modelCode": ans.model_code or llm_router.DEFAULT_MODEL_CODE,
                "modelSource": ans.model_source or llm_router.SOURCE_ENV,
                "sourceLabel": llm_router.SOURCE_LABELS.get(
                    ans.model_source or llm_router.SOURCE_ENV, ""
                ),
                "remoteModel": ans.model,
                "temperature": rt.effective_temperature(),
                "temperatureSource": "助手配置" if rt.temperature is not None else "全局默认",
                "topP": rt.effective_top_p(),
                "topPSource": "助手配置" if rt.top_p is not None else "厂商默认",
                "maxTokens": rt.effective_max_tokens(),
                "maxTokensSource": "助手配置" if rt.max_tokens is not None else "全局默认",
            },
            # 召回明细：让「检索到了什么」可见，而不是只看最终答案
            "sources": [
                {
                    "n": i + 1,
                    "chunkId": h.chunk.chunk_id,
                    "docId": h.chunk.doc_id,
                    "titlePath": h.chunk.title_path,
                    "dept": h.chunk.dept,
                    "ktype": h.chunk.ktype,
                    "cos": round(h.cos, 4),
                    "rrf": round(h.rrf, 6),
                }
                for i, h in enumerate(ans.hits)
            ],
        }
    )
