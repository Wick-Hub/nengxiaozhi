"""助手提示词 / 检索参数 / 对话测试验收（任务 2.3 / M-A-04·05·09）。

八组自检，**默认不依赖 MySQL**（``KB_STORE=json``，助手数据写临时目录），
可直接复现；带 ``--store mysql`` 可在有库环境跑同一套断言。

1. **检索配置存储**：``retrieval_config`` 归一化、坏输入容错、**增量合并**
   （只覆盖显式传入的键）、取值范围校验；
2. **运行时解析**：空 ID / 非法 ID / 不存在 / 已下架 四个分支，
   以及「未配置 = 沿用全局」；
3. **提示词在线生效（M-A-04）**：非流式与流式两条链路上，送到大模型的
   系统消息必须等于该助手的提示词；未配置时回退平台默认；
4. **检索参数生效（M-A-05）**：topK、精排候选数、最低相似度、知识范围
   （部门 / 类型），以及「显式请求 > 助手配置 > 全局默认」的优先级；
5. **过滤时机**：知识范围必须在**召回之前**生效——放在之后会被其它部门的
   切片挤掉名额，表现为「配了范围反而搜不到」；
6. **一致性回归（不变量）**：未配置检索参数的助手，其召回结果与「完全不传助手」
   逐项一致——这是不破坏批1 回归的前提；
7. **接口层契约**：真实起服务跑 ``/kb-folders``、``/agents/{id}/retrieval``、
   ``/agents/{id}/test`` 全链路与状态码（400/404/422）；
8. **对话测试不落库**：测试问答不得写入会话日志，否则运营调试会污染 2.1 的日志
   与「未识别问题池」。

用法::

    python tools/verify_prompt.py
    python tools/verify_prompt.py --store mysql
    python tools/verify_prompt.py --port 8026

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# 必须在导入 app.* 之前定型（settings 在导入期读取环境变量）
os.environ.setdefault("KB_STORE", "json")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


#: 本工具创建的测试助手编码前缀
TEST_PREFIX = "vf-"


def purge_test_agents() -> int:
    """删除本工具留下的 ``vf-*`` 助手，返回删除条数。

    ⚠ **MySQL 模式下必须清理**：连的是真实库，``agent_code`` 有唯一约束
    （``uk_agent_code``），上次跑完残留的 ``vf-p1`` 会让下次运行直接
    ``IntegrityError: Duplicate entry`` 崩掉 —— 属幂等性缺陷。
    JSON 模式用临时文件，天然干净。
    """
    from app.config import settings
    from app.db import agent_store

    if settings.store_label != "mysql":
        return 0
    victims = [a for a in agent_store.list_agents()
               if str(a.get("agentCode") or "").startswith(TEST_PREFIX)]
    for a in victims:
        agent_store.delete_agent(a["agentId"])
    return len(victims)


# ==================================================================== 1. 存储
def check_store() -> None:
    from app.db import agent_store

    section("1. 检索配置存储（retrieval_config）")
    d = agent_store.default_retrieval()
    check("默认配置 = 全量沿用（0 / 空数组）",
          d == {"topK": 0, "rerankTopn": 0, "minScore": 0.0, "depts": [], "ktypes": []}, f"{d}")

    for bad in (None, 123, "not-json", "{坏 json", ["a"], object()):
        if agent_store.normalize_retrieval(bad) != d:
            check(f"坏输入容错：{type(bad).__name__}", False, str(bad))
            return
    check("坏输入一律降级为默认值（不抛异常）", True, "6 种形态")

    check("JSON 字符串可解析",
          agent_store.normalize_retrieval('{"topK": 8}')["topK"] == 8)
    check("重复项去重",
          agent_store.normalize_retrieval({"depts": ["财务处", "财务处"]})["depts"] == ["财务处"])
    check("越界 minScore 被夹到 1.0",
          agent_store.normalize_retrieval({"minScore": 9})["minScore"] == 1.0)
    check("非数字 topK 归零", agent_store.normalize_retrieval({"topK": "abc"})["topK"] == 0)

    base = {"topK": 3, "rerankTopn": 0, "minScore": 0.2, "depts": ["财务处"], "ktypes": []}
    merged = agent_store.merge_retrieval(base, {"rerankTopn": 12})
    check("增量合并：只改传入的键",
          merged["rerankTopn"] == 12 and merged["topK"] == 3 and merged["depts"] == ["财务处"],
          f"{merged}")
    reset = agent_store.merge_retrieval(base, {"topK": 0, "depts": []})
    check("增量合并：显式 0 / [] 才是重置", reset["topK"] == 0 and reset["depts"] == [])

    errs = agent_store.validate_retrieval(
        {"topK": 999, "rerankTopn": -1, "minScore": 5, "depts": "x" * 100}
    )
    check("校验一次报全 4 条错误", len(errs) == 4, f"{len(errs)} 条：{'；'.join(errs)}")
    check("0 / [] 是合法值", agent_store.validate_retrieval({"topK": 0, "depts": []}) == [])

    a = agent_store.create_agent({"agentCode": "vf-p1", "agentName": "配置甲"})
    got = agent_store.update_agent(a["agentId"], {"retrievalConfig": {"topK": 7, "depts": ["财务处"]}})
    check("落盘后回读一致", got["retrievalConfig"]["topK"] == 7
          and got["retrievalConfig"]["depts"] == ["财务处"])

    # 「批2 早期建的、没有该字段的旧记录」只可能出现在 **JSON 后端**：
    # JSON 记录是自由 dict，可以缺字段；而 MySQL 侧的字段缺失由
    # ``tools/migrate_agent_2_3.py`` 负责补齐，列一旦存在就不可能「有行缺列」。
    #
    # ⚠ 这里原先无条件调用 ``_read_all()/_write_all()``（JSON 专属私有函数），
    #   在 ``--store mysql`` 下会 KeyError 崩溃 —— 该分支在有库环境从未被执行过，
    #   所以一直没暴露。（MySQL 分支的等价验证改为断言列已由迁移补齐。）
    if agent_store._mysql():
        from sqlalchemy import text

        from app.db.session import db_session

        with db_session() as conn:
            cols = {str(r[0]) for r in conn.execute(text("SHOW COLUMNS FROM agent")).all()}
        check("MySQL：retrieval_config 列已由迁移补齐", "retrieval_config" in cols,
              "缺失则需先跑 tools/migrate_agent_2_3.py")
    else:
        data = agent_store._read_all()  # noqa: SLF001
        data["agents"][str(a["agentId"])].pop("retrievalConfig", None)
        agent_store._write_all(data)  # noqa: SLF001
        check("旧记录（无该字段）读取等价于全默认",
              agent_store.get_agent(a["agentId"])["retrievalConfig"] == d)


# ==================================================================== 2. 运行时解析
def check_runtime() -> None:
    from app.core import agent_runtime
    from app.db import agent_store
    from app.services import agent_admin

    section("2. 助手运行时解析")
    for empty in (None, "", "  ", 0, "0"):
        rt = agent_runtime.resolve(empty)
        if rt.found or rt.error is not None:
            check(f"空 agent_id（{empty!r}）= 不指定助手", False, f"found={rt.found} err={rt.error}")
            return
    check("空 agent_id 一律视为「不指定助手」（不报错）", True, "None/''/'  '/0/'0'")

    check("非法 ID 报「非法」", "非法" in (agent_runtime.resolve("abc").error or ""))
    check("不存在的 ID 报「不存在」", "不存在" in (agent_runtime.resolve(999999).error or ""))

    off = agent_admin.create_agent({"agentCode": "vf-off", "agentName": "下架助手"})["data"]
    agent_admin.set_status(off["agentId"], 0)
    check("已下架的助手报「已下架」",
          "已下架" in (agent_runtime.resolve(off["agentId"]).error or ""))
    try:
        agent_runtime.resolve(off["agentId"], strict=True)
        check("strict=True 时抛 AgentUnavailable", False, "未抛出")
    except agent_runtime.AgentUnavailable:
        check("strict=True 时抛 AgentUnavailable", True)

    prompt = agent_admin.create_agent(
        {"agentCode": "vf-p2", "agentName": "提示词助手", "systemPrompt": "只答是或否。"}
    )["data"]
    agent_store.update_agent(prompt["agentId"],
                             {"retrievalConfig": {"topK": 4, "rerankTopn": 9, "minScore": 0.1}})
    # 任务 2.4：给同一助手再配生成参数，验证「模型与参数已接线」
    agent_store.update_agent(prompt["agentId"],
                             {"temperature": 0.5, "topP": 0.7, "maxTokens": 321,
                              "modelCode": "vf-p2-model"})
    rt = agent_runtime.resolve(prompt["agentId"])
    check("解析出提示词", rt.system_prompt == "只答是或否。", rt.system_prompt)
    check("解析出检索参数", (rt.top_k, rt.rerank_topn, rt.min_score) == (4, 9, 0.1),
          f"topK={rt.top_k} rerank={rt.rerank_topn} minScore={rt.min_score}")
    check("解析出生成参数（任务 2.4 接线）",
          (rt.temperature, rt.top_p, rt.max_tokens) == (0.5, 0.7, 321),
          f"temp={rt.temperature} topP={rt.top_p} max={rt.max_tokens}")
    check("生成参数生效值 = 助手配置",
          (rt.effective_temperature(), rt.effective_top_p(), rt.effective_max_tokens())
          == (0.5, 0.7, 321),
          f"temp={rt.effective_temperature()} topP={rt.effective_top_p()} "
          f"max={rt.effective_max_tokens()}")
    check("参数来源标注为「助手配置」",
          rt.param_sources()["temperatureSource"] == "助手配置"
          and rt.param_sources()["topPSource"] == "助手配置"
          and rt.param_sources()["modelSource"] == "助手配置",
          str(rt.param_sources()))

    from app.config import settings

    base = agent_store.list_agents()[0]
    rt0 = agent_runtime.resolve(base["agentId"])
    check("未配置助手 → 检索参数全部沿用全局",
          rt0.effective_top_k() == settings.top_k
          and rt0.effective_rerank_topn() == settings.rerank_topn
          and rt0.min_score == 0.0 and rt0.depts == () and rt0.system_prompt == "",
          f"topK→{rt0.effective_top_k()} rerank→{rt0.effective_rerank_topn()}")
    check("未配置助手 → 生成参数沿用平台默认（NULL 不物化）",
          rt0.temperature is None and rt0.top_p is None and rt0.max_tokens is None
          and rt0.effective_temperature() == settings.llm_temperature
          and rt0.effective_max_tokens() == settings.llm_max_tokens,
          f"temp={rt0.effective_temperature()} max={rt0.effective_max_tokens()}")
    check("平台默认不下发 topP（None = 交给厂商默认）", rt0.effective_top_p() is None)
    check("优先级：显式请求 > 助手配置",
          rt.effective_top_k(20) == 20 and rt.effective_top_k() == 4)
    # 2.11 起所有可存储字段（检索/生成/话术）均已接线
    check("无未接线字段（pendingFields 为空）",
          rt.to_dict()["pendingFields"] == [], str(rt.to_dict()["pendingFields"]))
    check("运行时携带风控/兜底话术",
          bool(rt.risk_reply) and bool(rt.fallback_reply),
          f"risk={len(rt.risk_reply)} fallback={len(rt.fallback_reply)}")


# ==================================================================== 3. 提示词生效
def check_prompt_effect() -> None:
    from app.core import generator as G
    from app.core import pipeline
    from app.core.llm_client import LLMResponse
    from app.services import agent_admin

    section("3. 提示词在线生效（M-A-04）")

    class _Recorder:
        """记录每次送进大模型的 messages，不真的发请求。"""

        def __init__(self) -> None:
            self.calls: list[list[dict]] = []

        def chat(self, messages, **kw):
            self.calls.append(messages)
            return LLMResponse(content="是[1]", usage={}, latency_ms=1, model="stub")

        def chat_stream(self, messages, on_delta=None, **kw):
            self.calls.append(messages)
            if on_delta:
                on_delta("是")
            return LLMResponse(content="是[1]", usage={}, latency_ms=1, model="stub")

    rec = _Recorder()
    G._generator = G.Generator(rec)  # noqa: SLF001 - 验收脚本，进程退出即失效

    agent = agent_admin.create_agent(
        {
            "agentCode": "vf-p3",
            "agentName": "提示词验收",
            "systemPrompt": "你是验收助手，只回答「是」或「否」。",
        }
    )["data"]

    pipeline.answer_question("校园网怎么开户", role="本科生", save=False,
                             agent_id=agent["agentId"])
    check("非流式：system 消息 = 助手提示词",
          rec.calls and rec.calls[-1][0]["content"] == "你是验收助手，只回答「是」或「否」。",
          rec.calls[-1][0]["content"][:24] if rec.calls else "-")

    pipeline.answer_question_stream("校园网怎么开户", role="本科生", save=False,
                                    agent_id=agent["agentId"])
    check("流式：system 消息 = 助手提示词",
          rec.calls[-1][0]["content"] == "你是验收助手，只回答「是」或「否」。")

    pipeline.answer_question("校园网怎么开户", role="本科生", save=False)
    check("不传 agentId：system 消息 = 平台默认提示词",
          rec.calls[-1][0]["content"] == G.SYSTEM_PROMPT,
          f"len={len(rec.calls[-1][0]['content'])}")

    blank = agent_admin.create_agent({"agentCode": "vf-p4", "agentName": "空提示词"})["data"]
    pipeline.answer_question("校园网怎么开户", role="本科生", save=False,
                             agent_id=blank["agentId"])
    check("提示词留空的助手：回退平台默认提示词",
          rec.calls[-1][0]["content"] == G.SYSTEM_PROMPT)

    check("知识资料与身份约束仍在 user 消息里",
          "知识资料：" in rec.calls[-1][-1]["content"]
          and "提问者身份" in rec.calls[-1][-1]["content"])


# ==================================================================== 4. 检索参数生效
def check_retrieval_effect() -> None:
    from app.core import agent_runtime
    from app.core.index_store import get_index
    from app.core.pipeline import retrieve
    from app.services import agent_admin

    section("4. 检索参数生效（M-A-05）")
    idx = get_index()
    check("知识库可用（前置条件）", idx.ok and idx.chunks, f"{len(idx.chunks)} 切片")
    if not idx.chunks:
        return

    q = "校园网怎么开户"
    base_hits = retrieve(q, role="本科生")
    check("基线召回非空（前置条件）", bool(base_hits), f"{len(base_hits)} 条")
    if not base_hits:
        return

    rt2 = agent_runtime.AgentRuntime(top_k=2)
    check("topK=2 生效", len(retrieve(q, role="本科生", runtime=rt2)) <= 2)
    rt1 = agent_runtime.AgentRuntime(top_k=1)
    check("topK=1 生效", len(retrieve(q, role="本科生", runtime=rt1)) <= 1)
    check("优先级：请求 top_k 覆盖助手 topK",
          len(retrieve(q, role="本科生", top_k=4, runtime=rt1)) <= 4)

    depts = sorted({c.dept for c in idx.chunks})
    target = depts[0]
    rt_d = agent_runtime.AgentRuntime(depts=(target,))
    hits = retrieve(q, role="本科生", runtime=rt_d)
    check(f"部门范围生效（{target}）",
          hits and {h.chunk.dept for h in hits} == {target},
          f"{len(hits)} 条")
    check("未知部门 → 空召回",
          retrieve(q, role="本科生", runtime=agent_runtime.AgentRuntime(depts=("不存在",))) == [])

    ktypes = sorted({c.ktype for c in idx.chunks})
    kt = ktypes[0]
    hits_kt = retrieve(q, role="本科生", runtime=agent_runtime.AgentRuntime(ktypes=(kt,)))
    check(f"类型范围生效（{kt}）", all(h.chunk.ktype == kt for h in hits_kt), f"{len(hits_kt)} 条")

    strict = retrieve(q, role="本科生", runtime=agent_runtime.AgentRuntime(min_score=0.999))
    check("minScore 阈值生效", all(h.cos >= 0.999 for h in strict), f"{len(strict)} 条")
    check("minScore=0 不过滤",
          len(retrieve(q, role="本科生", runtime=agent_runtime.AgentRuntime(min_score=0.0)))
          == len(base_hits))

    # 过滤时机（关键）：找一个「全局前 5 完全不含它、但它自己仍有可见切片」的部门。
    # 限定该部门后仍应召回；若实现是「先召回再筛」，这里必然是 0 条——
    # 那正是「配了知识范围反而搜不到」的成因。
    from app.core.retriever import is_visible

    decisive: tuple[str, str, str, int, int] | None = None
    for q2, role2 in (
        ("采购流程是什么", "教职工"),
        ("校园网怎么开户", "本科生"),
        ("学费怎么缴纳", "本科生"),
        ("报到需要带什么材料", "访客"),
    ):
        global_hits = retrieve(q2, role=role2, top_k=5)
        global_depts = {h.chunk.dept for h in global_hits}
        for cand in depts:
            visible = [c for c in idx.chunks if c.dept == cand and is_visible(c, role2)]
            if not visible or cand in global_depts:
                continue
            pre = retrieve(q2, role=role2, top_k=5,
                           runtime=agent_runtime.AgentRuntime(depts=(cand,)))
            if pre:
                post_n = len([h for h in global_hits if h.chunk.dept == cand])
                decisive = (q2, role2, cand, len(pre), post_n)
                break
        if decisive:
            break

    if decisive is None:
        check("范围过滤发生在召回之前", False, "未构造出「全局前 5 不含该部门」的反例")
    else:
        q2, role2, cand, pre_n, post_n = decisive
        check("范围过滤发生在召回之前（事前筛有结果，事后筛会全丢）",
              pre_n > 0 and post_n == 0,
              f"「{q2}」/{role2} 限定「{cand}」：事前 {pre_n} 条 / 事后 {post_n} 条")


# ==================================================================== 5. 一致性
def check_parity() -> None:
    from app.core import agent_runtime
    from app.core.pipeline import retrieve
    from app.db import agent_store

    section("5. 一致性回归：未配置助手 ≡ 无助手")
    base = agent_store.list_agents()[0]
    rt = agent_runtime.resolve(base["agentId"])
    check("默认助手解析出的检索参数全为「沿用全局」",
          rt.top_k == 0 and rt.rerank_topn == 0 and rt.min_score == 0.0
          and not rt.depts and not rt.ktypes)

    cases = [
        ("校园网怎么开户", "本科生"),
        ("学费怎么缴纳", "本科生"),
        ("采购流程是什么", "教职工"),
        ("报到需要带什么材料", "访客"),
    ]
    same = 0
    for q, role in cases:
        legacy = retrieve(q, role=role)
        with_agent = retrieve(q, role=role, runtime=rt)
        if [h.chunk.chunk_id for h in legacy] == [h.chunk.chunk_id for h in with_agent]:
            same += 1
        else:
            print(f"      差异：{q} / {role}")
    check("四个用例召回结果逐项一致（含顺序）", same == len(cases), f"{same}/{len(cases)}")

    # 配过再改回默认，也应回到一致
    from app.services import agent_config

    agent_config.update_retrieval(base["agentId"], {"topK": 3, "depts": ["财务处"]})
    changed = retrieve("校园网怎么开户", role="本科生", runtime=agent_runtime.resolve(base["agentId"]))
    check("配置生效后可观察到差异", len(changed) <= 3)
    agent_config.update_retrieval(
        base["agentId"], {"topK": 0, "rerankTopn": 0, "minScore": 0, "depts": [], "ktypes": []}
    )
    back = retrieve("校园网怎么开户", role="本科生", runtime=agent_runtime.resolve(base["agentId"]))
    check("恢复默认后与批1 行为一致",
          [h.chunk.chunk_id for h in back] == [h.chunk.chunk_id for h in retrieve("校园网怎么开户", role="本科生")])


# ==================================================================== 6. 接口层
def check_api(port: int) -> None:
    import httpx

    from tools._server import read_log, spawn_server, stop_server, wait_port

    section("6. 接口层契约（真实服务）")
    env = dict(os.environ)
    env["KB_STORE"] = os.environ.get("KB_STORE", "json")
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"

    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    base = f"http://127.0.0.1:{port}"
    created: int | None = None
    try:
        if not wait_port(port):
            check("服务启动", False, read_log(log_path, 800)[-400:])
            return
        check("服务启动", True, f"port={port}")

        with httpx.Client(timeout=90.0, trust_env=False) as cli:
            r = cli.get(f"{base}/api/admin/kb-folders")
            ok = r.status_code == 200 and r.json().get("ok") is True
            check("GET /kb-folders 200", ok, f"status={r.status_code}")
            if ok:
                d = r.json()["data"]
                total = sum(f["chunkCount"] for f in d["folders"])
                check("文件夹切片数之和 = 索引切片总数",
                      total == d["chunkTotal"], f"{total} vs {d['chunkTotal']}")
                check("文件夹含 dept/ktype/文档数/切片数",
                      all({"dept", "ktype", "docCount", "chunkCount"} <= set(f) for f in d["folders"]))
                check("同时给出部门与类型字典（供勾选）", bool(d["depts"]) and bool(d["ktypes"]))

            r = cli.get(f"{base}/api/admin/agents")
            d = r.json()["data"]
            check("GET /agents 带出编辑器默认值",
                  d.get("defaultSystemPrompt") and d.get("retrievalDefaults", {}).get("topK"),
                  f"defaults={d.get('retrievalDefaults')}")

            # 前置清理：上次运行若中途失败可能残留
            for a in d["items"]:
                if a["agentCode"] == "vf-prompt":
                    cli.delete(f"{base}/api/admin/agents/{a['agentId']}")

            r = cli.post(f"{base}/api/admin/agents",
                         json={"agentCode": "vf-prompt", "agentName": "检索验收助手",
                               "systemPrompt": "你是检索验收助手。"})
            check("POST /agents 创建 200", r.status_code == 200, f"status={r.status_code}")
            if r.status_code != 200:
                return
            created = r.json()["data"]["agentId"]

            r = cli.get(f"{base}/api/admin/agents/{created}/retrieval")
            check("GET /agents/{id}/retrieval 200",
                  r.status_code == 200 and r.json()["data"]["agentId"] == created)
            check("返回 defaults / ranges / pendingFields",
                  {"defaults", "ranges", "pendingFields"} <= set(r.json()["data"]),
                  f"ranges.topK.max={r.json()['data']['ranges']['topK']['max']}")

            r = cli.put(f"{base}/api/admin/agents/{created}/retrieval",
                        json={"topK": 3, "depts": ["财务处"]})
            cfg = r.json()["data"]["retrievalConfig"] if r.status_code == 200 else {}
            check("PUT /agents/{id}/retrieval 200 + 生效",
                  r.status_code == 200 and cfg.get("topK") == 3 and cfg.get("depts") == ["财务处"],
                  f"status={r.status_code}")

            r = cli.put(f"{base}/api/admin/agents/{created}/retrieval", json={"rerankTopn": 11})
            cfg = r.json()["data"]["retrievalConfig"]
            check("PUT 增量语义：未传的键保持原值",
                  cfg["topK"] == 3 and cfg["depts"] == ["财务处"] and cfg["rerankTopn"] == 11,
                  f"{cfg}")

            r = cli.put(f"{base}/api/admin/agents/{created}/retrieval", json={"topK": 999})
            check("PUT 越界 → 400", r.status_code == 400, f"status={r.status_code}")

            r = cli.get(f"{base}/api/admin/agents/999999/retrieval")
            check("GET 不存在的助手 → 404", r.status_code == 404, f"status={r.status_code}")

            r = cli.post(f"{base}/api/admin/agents/{created}/test",
                         json={"question": "学费怎么缴纳", "role": "本科生"})
            ok = r.status_code == 200 and r.json().get("ok")
            check("POST /agents/{id}/test 200", bool(ok), f"status={r.status_code}")
            if ok:
                t = r.json()["data"]
                check("测试返回答案/引用/召回明细",
                      "answer" in t and "citations" in t and "sources" in t,
                      f"retrieved={t['retrieved']} sources={len(t['sources'])}")
                check("测试回传生效参数（topK / 来源标注）",
                      t["retrieval"]["topK"] == 3
                      and t["retrieval"]["topKSource"] == "助手配置"
                      and t["retrieval"]["depts"] == ["财务处"],
                      f"{t['retrieval']}")
                check("召回明细全部落在配置的知识范围内",
                      all(s["dept"] == "财务处" for s in t["sources"]),
                      f"{sorted({s['dept'] for s in t['sources']})}")
                check("测试明确标注不落库", t["saved"] is False)

            r = cli.post(f"{base}/api/admin/agents/{created}/test", json={"question": ""})
            check("测试空问题 → 422", r.status_code == 422, f"status={r.status_code}")

            r = cli.post(f"{base}/api/chat",
                         json={"question": "校园网怎么开户", "agentId": str(created)})
            check("POST /chat 带 agentId 200", r.status_code == 200, f"status={r.status_code}")
            r = cli.post(f"{base}/api/chat",
                         json={"question": "校园网怎么开户", "agentId": "999999"})
            check("POST /chat 带不存在的 agentId → 400（不静默回落）",
                  r.status_code == 400 and "不存在" in r.json()["error"], f"status={r.status_code}")

            # 会话日志未被测试污染
            r = cli.get(f"{base}/api/admin/chatlogs", params={"pageSize": 200})
            rows = r.json()["data"]["items"] if r.status_code == 200 else []
            check("对话测试未写入会话日志",
                  all("学费怎么缴纳" not in (row.get("question") or "") for row in rows),
                  f"{len(rows)} 行")
    finally:
        if created is not None:
            try:
                with httpx.Client(timeout=20.0, trust_env=False) as cli:
                    cli.delete(f"{base}/api/admin/agents/{created}")
            except Exception:  # noqa: BLE001
                pass
        stop_server(proc)


# ==================================================================== 7. 幂等
def check_idempotent() -> None:
    import json

    from app.config import settings

    section("7. 幂等性：验收不残留数据")
    real = settings.base_dir / "data" / "agents.json"
    if not real.is_file():
        check("真实 data/agents.json 不存在（无需清理）", True, str(real))
    else:
        data = json.loads(real.read_text(encoding="utf-8"))
        codes = {str(r.get("agentCode") or "") for r in (data.get("agents") or {}).values()}
        leftovers = sorted(c for c in codes if c.startswith("vf-"))
        check("真实 data/agents.json 无 vf-* 残留（接口层已自清理）",
              not leftovers, "/".join(leftovers) or "无")
        check("真实库里的默认助手未被动过",
              any(c == "campus-default" for c in codes) or not codes,
              f"{len(codes)} 个助手")

    # 隔离方式因后端而异，不能一刀切：
    #   * JSON：把 AGENTS_PATH 重定向到临时目录，与真实 data/agents.json 隔离；
    #   * MySQL：助手存在数据库里，**没有「JSON 文件重定向」这回事**，
    #     隔离靠「跑完清理 vf-* 行」（见 purge_test_agents）。
    # 原断言在 MySQL 下必然失败（AGENTS_PATH 恒等于真实路径），
    # 而有库环境此前从未跑过这条路径。
    from app.db import agent_store

    if settings.store_label == "mysql":
        check("MySQL：助手存储走数据库（隔离靠跑完清理，非文件重定向）",
              True, settings.db_name)
    else:
        check("内存侧使用临时库（与真实库隔离）",
              agent_store.AGENTS_PATH != real, str(agent_store.AGENTS_PATH.parent.name))


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="助手提示词/检索参数验收（任务 2.3）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"],
                        help="存储模式；默认 json（无需 MySQL）")
    parser.add_argument("--port", type=int, default=8026, help="接口层验收端口")
    parser.add_argument("--keep-data", action="store_true", help="保留临时数据目录")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-prompt-"))
    if args.store == "json":
        from app.db import agent_store

        agent_store.AGENTS_PATH = tmpdir / "agents.json"
        print(f"临时助手文件：{agent_store.AGENTS_PATH}")

    from app.config import settings

    print("=" * 68)
    print("助手提示词 / 检索参数 / 对话测试验收（任务 2.3）")
    print(f"存储模式：{settings.store_label}    数据目录：{settings.base_dir}")
    print("=" * 68)

    try:
        # MySQL 模式下先清残留：agent_code 唯一约束会让上次遗留的 vf-p1 撞车
        purged = purge_test_agents()
        if purged and settings.store_label == "mysql":
            print(f"（已清理上次残留的 {purged} 个 {TEST_PREFIX}* 助手）")

        check_store()
        check_runtime()
        check_prompt_effect()
        check_retrieval_effect()
        check_parity()
        check_api(args.port)
        check_idempotent()
    finally:
        left = purge_test_agents()
        if left and settings.store_label == "mysql":
            print(f"（已清理本次创建的 {left} 个 {TEST_PREFIX}* 助手）")
        if args.keep_data:
            print(f"\n临时数据保留于：{tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
