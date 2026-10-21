# LLM_MODEL 可省略 —— 变更说明（2026-10-19）

> 背景：用户目标是 **能小智侧完全不配置大模型，所有模型配置都放网关**。
> 已确认：能小智不持有任何模型台账 / 厂商 Key，模型清单 / 路由 / 限流 / 白名单 100% 在网关。
> 本次唯一的额外改动：把 `.env` 里唯一的模型相关项 `LLM_MODEL`（仅作「无助手绑定」时的兜底）
> 改成 **可省略**。省略时默认路由自动取网关 `/v1/models` 清单的第一个模型。
>
> 附带的正确性修正：几处过期文案「模型管理」→「模型网关」。

---

## 一、前提确认（本次没有推到 GitHub 的原因）

1. `campus-ai-gateway/` 是 `Jiusheng01/campus-ai-gateway` 的**干净 clone**：
   `main == origin/main == 91531ae`，工作区零改动、零未推送提交 → **无任何可 PR 的网关代码**。
2. 整个工作区与 `kb-service/` **都不是 git 仓库**，无 GitHub remote → 我们的改动无版本库、无从 push。
3. 环境无 `gh` CLI；网关 remote 只指向上游（无 fork / 无凭据）。

因此本文件作为**可携带到目标仓库再决定是否提交**的说明文档。

---

## 二、改动清单（文件 → 具体改动）

### 1. `app/config.py`
- `llm_model: str = "deepseek-chat"` → `""`（不再有占位默认，`LLM_MODEL` 现在可省略）。
- `llm_label` 属性：
  - 改前：`return self.llm_model if self.llm_api_key else "unconfigured"`
  - 改后：`if not self.llm_api_key: return "unconfigured"; return self.llm_model or "（跟随网关默认）"`

### 2. `app/core/llm_router.py`
- **新增** `default_remote_model() -> str`：
  - `LLM_MODEL` 非空 → 返回它（显式优先，兼容既有部署）；
  - 空 → 懒加载 `from app.services import gateway_admin`，取 `list_remote_models()["items"][0]["id"]`；
    - 懒加载是为避免与 `gateway_admin` 形成模块级循环依赖（它只在 `choices()` 里懒 import llm_router）。
- `_from_env()`：`label` 与 `remote_model` 由 `settings.llm_model` 改为 `default_remote_model()`。
- 模块 docstring 中「空 / `default`」一行的路由说明同步更新。

### 3. `app/core/llm_client.py`
- `get_client()` 单例构建：`LLMClient(model=settings.llm_model)` → 当 `settings.llm_model` 为空时，
  `try: from app.core.llm_router import default_remote_model; model = default_remote_model()`。
- 过期文案：某模型缺 Key 的报错里「模型管理」→「模型网关」，并改成确认白名单/业务 Key 已配置。

### 4. `app/services/gateway_admin.py`
- `choices()` 绑定下拉第一项：`跟随全局默认（{settings.llm_model or '未配置'}）` →
  `LLM_MODEL` 非空显示 `跟随网关默认（{settings.llm_model}）`，空则显示 `跟随网关默认（自动取网关第一个模型）`。

### 5. `app/api/chat.py`
- 503 的 `hint`：「在管理后台「模型管理」新增模型并绑定到助手」→「在管理后台「模型网关」绑定助手到网关模型」。

### 6. `tools/verify_gateway.py`
- 组 1：`check("已配置 LLM_MODEL（网关要求 model 非空）", bool(settings.llm_model))` → 改为
  `LLM_MODEL` 非空判「已配置」，空则 `info("未配置 LLM_MODEL —— 将自动取网关清单的第一个模型（见组 2 核对）")`。
- 组 2：清单非空且 `LLM_MODEL` 为空时 `check("LLM_MODEL 省略 → 自动取网关默认", got["items"][0]["id"])`；
  非空时保留原「`.env` 的 LLM_MODEL 在网关清单内」检查。

### 7. 文档（非代码）
- `README.md`：架构说明 +「怎么接」两处改为 `LLM_MODEL` 可省略；页签清单去掉已下线的「模型管理」。

---

## 三、验证结果

- **离线逻辑**（monkeypatch `gateway_admin.list_remote_models` 返回 `["gw-model-a","gw-model-b"]`）：
  - `llm_router.route_of(None).remote_model == "gw-model-a"`；`llm_client.get_client()._model == "gw-model-a"`；
  - 显式配置路径：`setattr(settings,"llm_model","explicit-model")` → `default_remote_model()=="explicit-model"`。
- `pytest tests/test_gateway_integration.py` → **13 passed**（无回归）。
- `tools/verify_gateway.py --self-test` → **13 PASS / 0 FAIL / 1 WARN**。
- 所有改动模块 `py_compile` 通过。

---

## 四、边界与已知限制

- 能小智 **永远需要** `LLM_BASE_URL`（网关地址）+ `LLM_API_KEY`（业务 Key）——这是连接/鉴权，
  任何 OpenAI 兼容客户端都省不掉；网关契约也要求 `model` 字段非空（否则 400）。
  即"零模型配置"的实现是 **默认模型自动从网关清单取**，不是"不发模型"。
- **网关 model 清单决定默认**：`default_remote_model()` 取清单第一个。若网关首建时不可达，
  清单为空 → 默认为 `""`，后续网关恢复也**不会自动切换**（`get_client()` 单例已固化）。
  与既有的「改 `.env` 需重启 / `reset_client()`」语义一致，正常部署（网关可达）无此问题。
- 若想**固定**某个默认模型，把 `.env` 的 `LLM_MODEL` 取消注释填别名即可（显式优先）。

---

## 五、在目标仓库落地的方法

由于 kb-service 无 git，上述改动需按文件逐项合入。建议在你的仓库里：
1. 新建分支 `feat/llm-model-optional`；
2. 按 §二 的列表逐文件合入（多数是定点替换，风险低）；
3. 跑一遍 §三 的验证（`pytest tests/test_gateway_integration.py` + `tools/verify_gateway.py --self-test`）；
4. 推送并开 PR，PR 描述可复用本文件 §二/§四。
