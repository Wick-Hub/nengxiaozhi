"""助手提示词 / 检索配置 / 对话测试（任务 2.3 / M-A-04·05·09）。

覆盖四层：

1. **存储层**：``retrieval_config`` 的归一化、增量合并、校验（``agent_store``）；
2. **运行时解析**（``app/core/agent_runtime.py``）：空 ID / 非法 ID / 不存在 / 已下架
   的四种分支，以及「未配置 = 沿用全局」；
3. **服务层**：知识文件夹聚合、检索配置读写、对话测试（不落库）；
4. **接口层**：``/api/admin/kb-folders``、``/agents/{id}/retrieval``、
   ``/agents/{id}/test`` 的状态码与响应封装，以及 ``/api/chat`` 的 ``agentId`` 契约。

**核心不变量**：未配置检索参数的助手，其检索行为必须与批1 **逐项一致**
（``test_unconfigured_agent_matches_legacy_retrieval``）——这是不破坏一致性回归的前提。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import agent_runtime
from app.core.generator import SYSTEM_PROMPT, build_messages
from app.db import agent_store
from app.main import app
from app.services import agent_admin, agent_config

client = TestClient(app)


# ------------------------------------------------------------------ 夹具
@pytest.fixture()
def base_agent() -> dict:
    return agent_store.list_agents()[0]


@pytest.fixture()
def prompt_agent() -> dict:
    result = agent_admin.create_agent(
        {
            "agentCode": "t-prompt",
            "agentName": "提示词助手",
            "systemPrompt": "你是测试助手，只回答「是」或「否」。",
            "visibleRoles": ["全部"],
        }
    )
    assert result["ok"], result["error"]
    return result["data"]


# ================================================================== 1. 存储层
def test_default_retrieval_is_all_inherit() -> None:
    cfg = agent_store.normalize_retrieval(None)
    assert cfg == {"topK": 0, "rerankTopn": 0, "minScore": 0.0, "depts": [], "ktypes": []}


def test_normalize_retrieval_tolerates_garbage() -> None:
    """坏输入一律降级为默认值，绝不抛异常（检索配置不该让问答整体失败）。"""
    for bad in (None, 123, "not-json", "{bad json", ["a"], object()):
        assert agent_store.normalize_retrieval(bad) == agent_store.default_retrieval()
    # 越界数值被夹到合法区间
    assert agent_store.normalize_retrieval({"minScore": 9})["minScore"] == 1.0
    assert agent_store.normalize_retrieval({"minScore": -3})["minScore"] == 0.0
    assert agent_store.normalize_retrieval({"topK": "abc"})["topK"] == 0


def test_normalize_retrieval_parses_json_string() -> None:
    cfg = agent_store.normalize_retrieval('{"topK": 8, "depts": ["财务处", "财务处"]}')
    assert cfg["topK"] == 8
    assert cfg["depts"] == ["财务处"], "重复项应去重"


def test_merge_retrieval_only_overwrites_present_keys() -> None:
    base = {"topK": 3, "rerankTopn": 0, "minScore": 0.2, "depts": ["财务处"], "ktypes": []}
    merged = agent_store.merge_retrieval(base, {"rerankTopn": 12})
    assert merged["rerankTopn"] == 12
    assert merged["topK"] == 3, "未传的键必须保持原值"
    assert merged["depts"] == ["财务处"], "未传 depts 不得被清空"
    # 显式传 0 / [] 才是重置
    reset = agent_store.merge_retrieval(base, {"topK": 0, "depts": []})
    assert reset["topK"] == 0 and reset["depts"] == []


def test_validate_retrieval_reports_all_errors() -> None:
    errors = agent_store.validate_retrieval(
        {"topK": 999, "rerankTopn": -1, "minScore": 5, "depts": "x" * 100}
    )
    assert len(errors) == 4, errors
    assert agent_store.validate_retrieval({"topK": 0, "depts": []}) == [], "0 / [] 是合法值"


def test_retrieval_config_roundtrip_in_store(base_agent) -> None:
    cfg = {"topK": 7, "rerankTopn": 15, "minScore": 0.35, "depts": ["财务处"], "ktypes": ["办事指南"]}
    updated = agent_store.update_agent(base_agent["agentId"], {"retrievalConfig": cfg})
    assert updated is not None
    assert updated["retrievalConfig"] == cfg
    assert agent_store.get_agent(base_agent["agentId"])["retrievalConfig"] == cfg


def test_legacy_record_without_retrieval_config_reads_as_defaults() -> None:
    """批2 早期建的记录没有该字段，读取时必须等价于「全默认」，不能报错。"""
    created = agent_admin.create_agent({"agentCode": "t-legacy", "agentName": "旧记录"})["data"]
    data = agent_store._read_all()  # noqa: SLF001 - 直接构造「旧记录」场景
    data["agents"][str(created["agentId"])].pop("retrievalConfig", None)
    agent_store._write_all(data)  # noqa: SLF001
    got = agent_store.get_agent(created["agentId"])
    assert got["retrievalConfig"] == agent_store.default_retrieval()


# ================================================================== 2. 运行时解析
def test_resolve_empty_agent_id_means_global() -> None:
    for empty in (None, "", "  ", 0, "0"):
        rt = agent_runtime.resolve(empty)
        assert rt.found is False and rt.error is None
        assert rt.top_k == 0 and rt.system_prompt == ""


def test_resolve_missing_and_invalid_and_offline() -> None:
    assert "非法" in agent_runtime.resolve("abc").error
    assert "不存在" in agent_runtime.resolve(999999).error

    off = agent_admin.create_agent({"agentCode": "t-off", "agentName": "下架助手"})["data"]
    agent_admin.set_status(off["agentId"], 0)
    assert "已下架" in agent_runtime.resolve(off["agentId"]).error
    with pytest.raises(agent_runtime.AgentUnavailable):
        agent_runtime.resolve(off["agentId"], strict=True)


def test_resolve_reads_config_and_priority(prompt_agent) -> None:
    agent_store.update_agent(
        prompt_agent["agentId"],
        {"retrievalConfig": {"topK": 4, "rerankTopn": 9, "minScore": 0.1, "depts": ["财务处"]}},
    )
    rt = agent_runtime.resolve(prompt_agent["agentId"])
    assert rt.found and rt.system_prompt.startswith("你是测试助手")
    assert rt.top_k == 4 and rt.rerank_topn == 9 and rt.min_score == 0.1
    assert rt.depts == ("财务处",)
    # 优先级：显式请求 > 助手配置
    assert rt.effective_top_k() == 4
    assert rt.effective_top_k(20) == 20
    assert rt.effective_rerank_topn() == 9


def test_unconfigured_agent_falls_back_to_global(base_agent) -> None:
    from app.config import settings

    rt = agent_runtime.resolve(base_agent["agentId"])
    assert rt.effective_top_k() == settings.top_k
    assert rt.effective_rerank_topn() == settings.rerank_topn
    assert rt.min_score == 0.0 and rt.depts == () and rt.ktypes == ()
    assert rt.system_prompt == "", "默认助手没配提示词 → 生成层应回退 SYSTEM_PROMPT"


# ================================================================== 3. 提示词生效
def test_build_messages_uses_custom_prompt_then_falls_back() -> None:
    msgs = build_messages("问题", [], system_prompt="自定义提示词")
    assert msgs[0] == {"role": "system", "content": "自定义提示词"}
    msgs = build_messages("问题", [])
    assert msgs[0]["content"] == SYSTEM_PROMPT, "未传提示词必须回退平台默认"
    msgs = build_messages("问题", [], system_prompt="   ")
    assert msgs[0]["content"] == "   ", "空串是「显式传了」而非「未传」，由调用方负责归一"


def test_pipeline_passes_agent_prompt_to_llm(monkeypatch, prompt_agent) -> None:
    """端到端：带 agentId 时，送到大模型的系统消息必须是该助手的提示词。"""
    from app.core import pipeline
    from app.core.generator import Generator
    from app.core.llm_client import LLMResponse

    captured: dict = {}

    class _Client:
        def chat(self, messages, **kw):
            captured["messages"] = messages
            return LLMResponse(content="是[1]", usage={}, latency_ms=1, model="stub")

        def chat_stream(self, messages, on_delta=None, **kw):
            captured["messages"] = messages
            if on_delta:
                on_delta("是")
            return LLMResponse(content="是[1]", usage={}, latency_ms=1, model="stub")

    monkeypatch.setattr(pipeline, "get_generator", lambda: Generator(_Client()))
    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: [_fake_hit()])

    ans = pipeline.answer_question("这是测试吗", agent_id=prompt_agent["agentId"], save=False)
    assert captured["messages"][0]["content"] == "你是测试助手，只回答「是」或「否」。"
    assert ans.text == "是[1]"


def test_pipeline_without_agent_uses_default_prompt(monkeypatch) -> None:
    from app.core import pipeline
    from app.core.generator import Generator
    from app.core.llm_client import LLMResponse

    captured: dict = {}

    class _Client:
        def chat(self, messages, **kw):
            captured["messages"] = messages
            return LLMResponse(content="好[1]", usage={}, latency_ms=1, model="stub")

    monkeypatch.setattr(pipeline, "get_generator", lambda: Generator(_Client()))
    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: [_fake_hit()])
    pipeline.answer_question("随便问问", save=False)
    assert captured["messages"][0]["content"] == SYSTEM_PROMPT


def _fake_hit():
    """一个最小可用的 Retrieved（供 monkeypatch 掉检索层时使用）。"""
    from app.core.chunker import Chunk
    from app.core.retriever import Retrieved

    return Retrieved(chunk=Chunk(chunk_id="D001-C001", doc_id="D001", text="校园网开户流程", dept="信息网络中心", ktype="办事指南"), cos=0.8)


# ================================================================== 4. 检索参数生效
def test_unconfigured_agent_matches_legacy_retrieval() -> None:
    """核心不变量：未配置检索参数的助手，召回结果与「完全不传 runtime」逐项一致。"""
    from app.core.pipeline import retrieve

    question = "校园网怎么开户"
    legacy = retrieve(question, role="本科生")
    rt = agent_runtime.AgentRuntime(agent_id=1)  # 全默认
    configured = retrieve(question, role="本科生", runtime=rt)
    assert [r.chunk.chunk_id for r in legacy] == [r.chunk.chunk_id for r in configured]


def test_agent_top_k_and_dept_scope_applied() -> None:
    from app.core.pipeline import retrieve

    question = "报到需要带什么材料"
    rt = agent_runtime.AgentRuntime(agent_id=1, top_k=2, depts=("财务处",))
    hits = retrieve(question, role="本科生", runtime=rt)
    assert len(hits) <= 2, "topK 必须生效"
    assert all(h.chunk.dept == "财务处" for h in hits), "部门范围必须在召回前过滤"


def test_request_top_k_overrides_agent() -> None:
    from app.core.pipeline import retrieve

    rt = agent_runtime.AgentRuntime(agent_id=1, top_k=1)
    assert len(retrieve("校园网怎么开户", role="本科生", top_k=4, runtime=rt)) <= 4
    assert len(retrieve("校园网怎么开户", role="本科生", runtime=rt)) <= 1


def test_min_score_filters_results() -> None:
    from app.core.pipeline import retrieve

    loose = retrieve("校园网怎么开户", role="本科生")
    assert loose, "前置条件：该问题应能召回到内容"
    strict = retrieve("校园网怎么开户", role="本科生", runtime=agent_runtime.AgentRuntime(min_score=0.99))
    assert all(h.cos >= 0.99 for h in strict)
    assert len(strict) <= len(loose)


def test_scope_filter_runs_before_recall() -> None:
    """范围过滤若放在召回之后，其它部门的切片会先占满名额 —— 这里锁死「之前」。"""
    from app.core.pipeline import retrieve

    rt = agent_runtime.AgentRuntime(depts=("国有资产管理处",), top_k=5)
    hits = retrieve("采购流程是什么", role="教职工", runtime=rt)
    assert hits, "限定部门后仍应能召回该部门的切片"
    assert {h.chunk.dept for h in hits} == {"国有资产管理处"}


def test_unknown_dept_yields_empty() -> None:
    from app.core.pipeline import retrieve

    rt = agent_runtime.AgentRuntime(depts=("不存在的部门",))
    assert retrieve("校园网怎么开户", role="本科生", runtime=rt) == []


# ================================================================== 5. 服务层
def test_list_kb_folders_aggregates_index() -> None:
    result = agent_config.list_kb_folders()
    assert result["ok"], result["error"]
    data = result["data"]
    assert data["folders"] and data["chunkTotal"] > 0
    total = sum(f["chunkCount"] for f in data["folders"])
    assert total == data["chunkTotal"], "文件夹切片数之和必须等于索引切片总数"
    assert all(f["dept"] and f["ktype"] for f in data["folders"])
    assert data["depts"] and data["ktypes"]


def test_get_retrieval_exposes_defaults_and_ranges(base_agent) -> None:
    result = agent_config.get_retrieval(base_agent["agentId"])
    assert result["ok"]
    assert result["data"]["retrievalConfig"] == agent_store.default_retrieval()
    assert result["data"]["defaults"]["topK"] > 0
    assert result["data"]["ranges"]["topK"]["max"] == agent_store.TOP_K_RANGE[1]


def test_get_retrieval_missing_agent() -> None:
    assert agent_config.get_retrieval(999999) == {
        "ok": False,
        "data": None,
        "error": "助手不存在：999999",
        "status": 404,
    }


def test_update_retrieval_merges_and_rejects_bad(base_agent) -> None:
    aid = base_agent["agentId"]
    assert agent_config.update_retrieval(aid, {"topK": 6})["ok"]
    assert agent_config.update_retrieval(aid, {"depts": ["财务处"]})["ok"]
    cfg = agent_store.get_agent(aid)["retrievalConfig"]
    assert cfg["topK"] == 6 and cfg["depts"] == ["财务处"], "两次增量更新都要保留"

    bad = agent_config.update_retrieval(aid, {"topK": 999})
    assert not bad["ok"] and bad["status"] == 400 and "召回条数" in bad["error"]
    empty = agent_config.update_retrieval(aid, {"agentName": "想偷改名称"})
    assert not empty["ok"], "检索接口只接受检索字段"


def test_test_chat_does_not_persist(prompt_agent) -> None:
    from app.db import chatlog

    before = len(chatlog.read_all_sessions())
    result = agent_config.test_chat(prompt_agent["agentId"], "校园网怎么开户", role="本科生")
    assert result["ok"], result["error"]
    data = result["data"]
    assert data["saved"] is False
    assert data["retrieval"]["topK"] > 0
    assert data["agent"]["hasSystemPrompt"] is True
    assert len(chatlog.read_all_sessions()) == before, "对话测试不得写入会话日志"


def test_test_chat_validation(prompt_agent) -> None:
    assert not agent_config.test_chat(prompt_agent["agentId"], "   ")["ok"]
    assert not agent_config.test_chat(prompt_agent["agentId"], "问" * 501)["ok"]
    assert agent_config.test_chat(999999, "任意问题")["status"] == 404


def test_test_chat_reports_effective_params(prompt_agent) -> None:
    agent_config.update_retrieval(prompt_agent["agentId"], {"topK": 3, "depts": ["财务处"]})
    data = agent_config.test_chat(prompt_agent["agentId"], "学费怎么交")["data"]
    assert data["retrieval"]["topK"] == 3
    assert data["retrieval"]["topKSource"] == "助手配置"
    assert data["retrieval"]["depts"] == ["财务处"]
    assert all(s["dept"] == "财务处" for s in data["sources"])


# ================================================================== 6. 接口层
def test_api_kb_folders() -> None:
    resp = client.get("/api/admin/kb-folders")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["data"]["folders"]


def test_api_retrieval_crud(base_agent) -> None:
    aid = base_agent["agentId"]
    resp = client.get(f"/api/admin/agents/{aid}/retrieval")
    assert resp.status_code == 200
    assert resp.json()["data"]["agentId"] == aid

    resp = client.put(f"/api/admin/agents/{aid}/retrieval", json={"topK": 9, "depts": ["财务处"]})
    assert resp.status_code == 200
    cfg = resp.json()["data"]["retrievalConfig"]
    assert cfg["topK"] == 9 and cfg["depts"] == ["财务处"]

    assert client.put(f"/api/admin/agents/{aid}/retrieval", json={"topK": 999}).status_code == 400
    assert client.get("/api/admin/agents/999999/retrieval").status_code == 404


def test_api_test_endpoint(prompt_agent) -> None:
    resp = client.post(
        f"/api/admin/agents/{prompt_agent['agentId']}/test",
        json={"question": "校园网怎么开户", "role": "本科生"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["saved"] is False and data["retrieval"]["topK"] > 0
    # 参数校验：空问题由 Pydantic 拦成 422
    assert client.post(f"/api/admin/agents/{prompt_agent['agentId']}/test", json={"question": ""}).status_code == 422


def test_api_list_agents_exposes_editor_defaults() -> None:
    data = client.get("/api/admin/agents").json()["data"]
    assert data["defaultSystemPrompt"] == SYSTEM_PROMPT
    assert data["retrievalDefaults"]["topK"] > 0
    assert data["retrievalRanges"]["minScore"]["max"] == 1.0
    # 任务 2.11 起风控/兜底话术也已接线，可存储字段全部生效
    assert data["pendingFields"] == []
    # 模型路由的默认值也要下发，让管理页显示「留空到底等于什么」（前端不写死第二份）
    assert data["modelDefaults"]["modelCode"] == "default"
    assert data["modelDefaults"]["temperature"] == settings.llm_temperature
    assert data["modelDefaults"]["maxTokens"] == settings.llm_max_tokens
    assert data["modelDefaults"]["topP"] is None  # 平台默认不下发 topP，交给厂商默认
    # 架构变更：模型下拉的**唯一来源是网关**，因此不再下发厂商模板（`vendors`）。
    # 这里只断言「下拉数据在位且结构正确」——具体有哪些模型取决于网关，
    # 不该由单测写死（写了就会在网关换模型时误报）。
    choices = data["modelChoices"]
    assert isinstance(choices, list) and choices, "模型下拉不应为空"
    assert choices[0]["modelCode"] == "default", "default 必须永远在第一项"
    assert all("modelCode" in c and "available" in c for c in choices)
    assert "vendors" not in data, "厂商模板已随模型管理页下线，改由网关控制面维护"


def test_api_chat_with_agent_id(prompt_agent) -> None:
    resp = client.post(
        "/api/chat",
        json={"question": "校园网怎么开户", "role": "本科生", "agentId": str(prompt_agent["agentId"])},
    )
    assert resp.status_code == 200 and resp.json()["ok"]


def test_api_chat_accepts_numeric_agent_id(prompt_agent) -> None:
    """渠道分流前端传的是数字 agentId；不得因 str 类型校验被 422 拦下。"""
    resp = client.post(
        "/api/chat",
        json={"question": "校园网怎么开户", "role": "本科生", "agentId": prompt_agent["agentId"]},
    )
    assert resp.status_code == 200 and resp.json()["ok"]


def test_api_chat_with_bad_agent_id_is_400() -> None:
    """选了一个不存在/已下架的助手时**必须报错**，不能静默回落成通用回答。"""
    resp = client.post("/api/chat", json={"question": "校园网怎么开户", "agentId": "999999"})
    assert resp.status_code == 400
    assert "不存在" in resp.json()["error"]

    off = agent_admin.create_agent({"agentCode": "t-off2", "agentName": "下架助手2"})["data"]
    agent_admin.set_status(off["agentId"], 0)
    resp = client.post("/api/chat", json={"question": "校园网怎么开户", "agentId": str(off["agentId"])})
    assert resp.status_code == 400 and "已下架" in resp.json()["error"]


def test_api_chat_without_agent_id_unchanged() -> None:
    """不传 agentId 时行为与批1 完全一致（这是向后兼容的底线）。"""
    resp = client.post("/api/chat", json={"question": "校园网怎么开户", "role": "本科生"})
    assert resp.status_code == 200 and resp.json()["ok"]


def test_api_chat_stream_with_bad_agent_id_is_400() -> None:
    resp = client.post("/api/chat/stream", json={"question": "校园网怎么开户", "agentId": "999999"})
    assert resp.status_code == 400
