# kb-service —— 能小智校园 AI 助手平台 · Python 服务层

生产交付主体（Python + FastAPI）。Node 版 `kb-engine` 降为参考原型，不再作为生产运行单元。

## 目录结构

```text
kb-service/
├─ app/
│  ├─ main.py            FastAPI 入口（uvicorn app.main:app）
│  ├─ config.py          环境变量配置（pydantic-settings）
│  ├─ api/               REST + SSE 路由
│  │  ├─ router.py       汇总，前缀 /api
│  │  ├─ health.py       GET /api/health
│  │  ├─ chat.py         POST /api/chat · /api/chat/stream（SSE）
│  │  ├─ docs.py         文档 CRUD / 切片 / 原文
│  │  ├─ sessions.py     历史会话
│  │  ├─ meta.py         部门/类型/状态/角色字典
│  │  ├─ sync.py         增量同步
│  │  ├─ admin.py        管理端：会话日志（2.1）/ 智能体助手（2.2·2.3）/ 操作日志
│  │  └─ integrations.py 集成中心：本地模型 / MCP / 第三方智能体（任务 4.8）
│  ├─ core/              引擎：parser（12 格式 + OCR）/ chunker / embedder / retriever / reranker / generator / pipeline
│  │                     agent_runtime（助手运行时配置解析，任务 2.3）
│  ├─ db/                session / models / kb_repo / vector_repository / chatlog / agent_store
│  │                     integration_store（本地模型/MCP/智能体注册表，双存储 + 字段加密，4.8）
│  ├─ services/          kb_admin（解析→切片→向量化→入库）· kb_sync（增量同步）
│  │                     chatlog_admin（会话日志查询/导出，任务 2.1）· agent_admin（助手管理，任务 2.2）
│  │                     agent_config（知识文件夹/检索配置/对话测试，任务 2.3）
│  │                     integration_admin（集成配置校验/加密/适配器/本地路由，任务 4.8）
│  ├─ session/           context（多轮上下文组装，任务 1.8）
│  ├─ models/            Pydantic 请求响应模型
│  └─ security/          auth（HS256 JWT）· rbac（角色）· deps（FastAPI 守卫）
├─ static/               前端静态资源（任务 1.15 已迁入，与原型的 public/ 字节级一致）
│  ├─ index.html/app.js/style.css     对话页（SSE 流式 + 引用面板）
│  ├─ admin.html/admin.js/admin.css   管理后台（/admin 路由，五页签；助手弹窗四页签）
│  ├─ chatlog.js                      会话日志页（任务 2.1，新增文件）
│  ├─ agent.js                        智能体管理页（任务 2.2，新增文件）
│  ├─ gateway.js                      模型网关页（C-01 接入入口 + 网关下发的模型清单）
│  ├─ sdk/nxz.js                      嵌入 SDK（任务 3.9：一行接入 / 编程 API / 身份桥）
│  ├─ sdk-demo.html                   第三方站点接入示例（任务 3.9，跨域浮窗演示）
│  ├─ share.html/share.js/share.css   公开问答分享页（任务 4.9，/share/<id>）
│  ├─ feedback.html/feedback.js/feedback.css  问答反馈管理页（任务 4.9，/feedback）
│  ├─ access.html/access.js/access.css  通行分析管理页（任务 4.4，/access）
│  ├─ support.html/support.js/support.css  客服工作台（任务 4.6，/support）
│  ├─ support-widget.js               门户转人工面板（任务 4.6，新增文件）
│  ├─ data-center.html/js/css         数据中台（任务 4.2，/data-center）
│  ├─ integrations.html/js/css        集成中心（任务 4.8，/integrations）
│  └─ preview.html/preview.js/preview.css  原文-切片对比页（任务 1.11，新增文件）
├─ tools/parity/         Python↔Node 解析/切片一致性比对（node_probe.mjs / py_probe.py / compare.py）
├─ tools/                smoke_api.py（接口冒烟）· check_db.py（连通性验收）
│                        ingest_mysql.py（JSON→MySQL 灌数）· seed_auth.py（角色/账号）
│                        make_samples.py（生成 12 格式验收样例）
│                        verify_store_roundtrip.py（MySQL 往返逐字段比对）
│                        verify_retrieval_consistency.py（JSON/MySQL 检索一致性）
│                        verify_multiturn.py（多轮 ≥10 轮验收）
│                        verify_frontend_contract.py（前端契约联调）
│                        verify_preview.py（1.11 预览与对比验收）
│                        verify_formats.py（1.10 12 格式 + OCR + 原图验收）
│                        verify_chatlogs.py（2.1 会话日志验收）· verify_chatlog_ui.py（2.1 浏览器验收）
│                        verify_agents.py（2.2 助手管理验收）· verify_agents_ui.py（2.2 浏览器验收）
│                        verify_prompt.py（2.3 提示词/检索参数验收）· verify_agent_config_ui.py（2.3 浏览器验收）
│                        verify_gateway.py（LLM 网关对接自检）· verify_gateway_e2e.py（↔真实网关端到端 34 项）
│                        verify_gateway_ui.py（模型网关页浏览器验收 43 项）
│                        verify_mysql_branch.py（2.1/2.2 MySQL 分支桩验收，无需数据库）
│                        setup_gateway_demo.py（在网关铺一套可用配置，幂等）
│                        _fake_gateway.py（按网关契约实现的假端点，供 --self-test）
│                        _mock_twcore.py（mock ThinkWatch Core —— 网关 4b87b6a 起 twcore 已退役，仅存档）
│                        migrate_agent_2_2.py（agent 表补 4 列，幂等）
│                        migrate_agent_2_3.py（agent 表补 retrieval_config 列，幂等）
│                        migrate_agent_2_4.py（llm_model 建表 + agent 生成参数列改可空，幂等）
│                        migrate_route_3_4.py（channel 表补 route_config 列，幂等）
│                        verify_routes.py（3.4 角色路由验收）· verify_routes_ui.py（3.4 浏览器验收）
│                        verify_portal_ui.py（3.5 门户三端/历史/登录验收）
│                        migrate_agent_guide_3_8.py（agent 表补 guide_questions 列，幂等）
│                        verify_switcher.py（3.8 多智能体切换验收）· verify_switcher_ui.py（3.8 浏览器验收）
│                        migrate_session_device_3_10.py（chat_session 补 device 列，幂等）
│                        migrate_message_refused_3_10.py（chat_message 补 refused 列，幂等）
│                        verify_stats.py（3.10 统计口径/导出/设备验收）· verify_stats_ui.py（3.10 浏览器验收）
│                        verify_embed_sdk.py（3.9 跨域 SDK 浏览器验收）
│                        migrate_unrecognized_3_11.py（未识别问题表补 9 列 + 3 索引，幂等）
│                        verify_unrecognized.py（3.11 入池/聚合/批量/导出）· verify_unrecognized_ui.py（3.11 浏览器验收）
│                        verify_sso.py（4.1 CAS 登录/交换码/重定向/鉴权）· verify_sso_ui.py（4.1 浏览器验收）
│                        verify_asr.py（4.7 语音状态/转写/限制/问答链路）· verify_asr_ui.py（4.7 浏览器验收）
│                        verify_gov.py（4.5 导办/票据）· verify_gov_ui.py（4.5 浏览器验收）
│                        verify_data_query.py（4.3 课表/成绩/一卡通）· verify_data_query_ui.py（4.3 浏览器验收）
│                        migrate_access_4_4.py（access_event 建表 / mock 灌数，幂等）
│                        verify_access.py（4.4 通行同步/看板/下钻/报告）· verify_access_ui.py（4.4 浏览器验收）
│                        migrate_share_feedback_4_9.py（qa_share / qa_feedback 建表，幂等）
│                        verify_share_feedback.py（4.9 分享/反馈/后台/CSV）· verify_share_feedback_ui.py（4.9 浏览器验收）
│                        migrate_support_4_6.py（support_group 建表 + 工单/坐席扩列，幂等）
│                        verify_support.py（4.6 工单/坐席/WebSocket/转人工）· verify_support_ui.py（4.6 浏览器验收）
│                        migrate_data_center_4_2.py（任务/批次/水位建表，幂等）
│                        verify_data_center.py（4.2 上报/同步/定时）· verify_data_center_ui.py（4.2 浏览器验收）
│                        migrate_integrations_4_8.py（本地模型/MCP/智能体三表，幂等）
│                        verify_integrations.py（4.8 配置/加密/适配器/路由）· verify_integrations_ui.py（4.8 浏览器验收）
│                        verify_deploy.py（5.1 生产部署：配置/证书/守护重启/HTTPS 端到端）
│                        build_offline_bundle.py（5.2 离线包构建）· offline_db_init.py（5.2 建库/迁移/种子）
│                        verify_offline_bundle.py（5.2 安装/升级/回滚/数据留存验收）
│                        perf_load.py（5.3 检索微基准 + 闭环 HTTP 压测 + 8C 绑定 + 容量报告）
│                        verify_acceptance.py（5.4 验收文档校验 + 接口彩排，输出 rehearsal.md）
│                        verify_data_init.py（5.5 样例标记/PII/引用回溯审计，支持仅源文件预检）
│                        verify_security_5_6.py（5.6 鉴权/限流/加密/审计/RBAC/生产基线自查）
├─ deploy/               生产部署（任务 5.1）：守护进程、Nginx TLS 模板、自签证书、
│                        Windows 计划任务脚本、Linux systemd、生产 env 模板（见 deploy/README.md）
│  └─ offline/           离线交付（任务 5.2）：Windows/Linux 安装·升级·回滚脚本、
│                        运行时依赖清单、OFFLINE-INSTALL.md 手册
├─ tests/                骨架 + 引擎各层 + 索引/会话/编排 + API 契约 + 前端静态资源 + MySQL 数据层与分支桩
├─ pyproject.toml
└─ .env.example
```

## 快速开始

```bash
# 1) 依赖（核心：含 FastAPI 与数据层 sqlalchemy/pymysql）
python -m pip install -e .

# 2) 按需安装可选依赖（解析 / OCR / 向量精排 / 生成 / 测试）
python -m pip install -e ".[parse,ocr,ml,llm,dev]"
#    ocr extra = rapidocr-onnxruntime（模型随 wheel 分发、无需系统软件，中文开箱可用）
#    若改用 tesseract：系统装 Tesseract + chi_sim 语言包，并在 .env 设 TESSERACT_BIN
#    .doc/.ppt 旧格式需 LibreOffice，并在 .env 设 SOFFICE_BIN（缺失时给出可执行提示）

# 3) 配置
cp .env.example .env    # 填写 DB_* / LLM_API_KEY / AES_KEY / JWT_SECRET
#    注意把 KB_STORE 设为 mysql（生产路径），详见「数据层」一节
# 4) 启动
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 或
python -m app.main
```

> 生产部署（HTTPS、常驻、自动重启、MySQL 健康检查）见 [`deploy/README.md`](deploy/README.md)，
> 验收命令 `python tools/verify_deploy.py`。

访问：
- `http://127.0.0.1:8000/` 对话页（SSE 流式问答 + 引用来源面板）
- `http://127.0.0.1:8000/admin` 知识管理后台
- `http://127.0.0.1:8000/preview` 文档预览与对比（原文 ↔ 切片 + 关键词检索）
- `http://127.0.0.1:8000/unrecognized` 未识别问题池（聚合 / 聚类 / 批量处理 / 反哺）
- `http://127.0.0.1:8000/feedback` 问答反馈管理（点赞/点踩记录、处理与导出）
- `http://127.0.0.1:8000/gov` 导办事项管理（同步 / 启停 / 角色授权 / 生效时段）
- `http://127.0.0.1:8000/support` 客服工作台（工单接入 / 回复 / 转交 / 结束 / 分组坐席）
- `http://127.0.0.1:8000/data-center` 数据中台（上报批次 / 组织同步 / 定时水位）
- `http://127.0.0.1:8000/integrations` 集成中心（本地模型 / MCP 服务 / 第三方智能体）
- `http://127.0.0.1:8000/hall` 一网通办大厅（免登录票据兑换与身份核验）
- `http://127.0.0.1:8000/share/<shareId>` 公开问答分享卡片（无需登录）
- `http://127.0.0.1:8000/sdk-demo` 嵌入 SDK 第三方站点示例（3.9）
- `http://127.0.0.1:8000/docs` 交互式 API 文档
- `http://127.0.0.1:8000/api/health` 健康检查

## 当前进度

> 批1（引擎与服务层，1.1–1.15）已全部完成；**批2 管理端核心已全部完成**
> （2.1–2.11）；批3 已完成 3.1 渠道接入、3.2 门户个性化、3.3 应用广场与角色授权、
> 3.4 渠道×角色会话路由、3.5 用户端门户、3.6 敏感词切库、3.7 AI 生成内容标识、
> 3.8 多智能体切换、3.9 嵌入 SDK、3.10 统计与运营分析、
> 3.11 未识别问题闭环（**批3 11/11 完成**）；
> 批4 已完成 **4.1 CAS 单点登录**、**4.2 数据中台对接**、**4.3 问数服务**、
> **4.4 通行查询智能体**、**4.5 导办事项与票据**、**4.6 客服体系**、
> **4.7 ASR 语音输入**、**4.8 本地模型/MCP/第三方智能体**与
> **4.9 分享问答与用户反馈**（**批4 9/9 完成**）；
> **批5 5.1 生产部署与 HTTPS、5.2 离线交付包、5.3 性能与容量验证、5.4 验收用例与演示脚本、5.6 安全加固验收已完成**。
> 5.4 交付物：`artifacts/acceptance/验收用例清单.md`（99 条需求 + AC-01～AC-19 + 负偏差）、
> `演示脚本.md`（15 分钟六幕）、`佐证材料清单.md`（E-01～E-59 / S-01～S-06）、
> `rehearsal.md`（接口彩排 **PASS 9 / WARN 0 / FAIL 0**）、
> `交付前检查清单.md`（软件交付/上线/验收三阶段 Go-No-Go、阻断项与复核命令）；运行
> `python tools/verify_acceptance.py [--rehearse]` 复核。
> 5.5 数据初始化审计：`tools/verify_data_init.py`（`--sources-only` 用于真实文档入库前预检），
> 报告 `artifacts/acceptance/data-init-report.md`；替换流程与回滚预案见
> `artifacts/acceptance/数据初始化与真实文档替换方案.md`（真实文档待校方提供）。
> 5.6 安全加固验收：`tools/verify_security_5_6.py`（`AUTH_ENABLED=true` / 限流开启 / MySQL），
> **PASS 50 / WARN 0 / FAIL 0**，报告 `artifacts/acceptance/security-report.md`；覆盖 401/403
> 三态、AES/JWT 轮换、管理/登录/问答限流、敏感词、操作日志脱敏、RBAC 与生产配置基线。
> 最新测试基线（2026-10-21）：`pytest` **1251 passed / 75 skipped**（共 1326 条；
> tokenizers 就绪后原先跳过的 7 项嵌入/精排用例转为真实执行）；
> `tools/verify_*.py` **70 个**；5.2 离线包验收 **57 PASS / 0 FAIL**
> （安装 → 升级 → 回滚 → 数据留存，含 BGE 512 维加载）；5.3 压测报告
> `artifacts/perf-report.md`（8C 绑定：精排关 16 并发 P95 275ms / 精排开 8 并发超 3s）；4.1 SSO 验收 HTTP **23 PASS** + 浏览器
> **17 PASS**；4.2 数据中台验收 HTTP JSON **55** / MySQL **53 PASS** + 浏览器
> **28 PASS**；4.3 问数验收 HTTP **56 PASS** + 浏览器 **31 PASS**；
> 4.4 通行验收 HTTP **72 PASS** + 浏览器 **55 PASS**；
> 4.5 导办验收 HTTP **82 PASS** + 浏览器 **37 PASS**；
> 4.6 客服验收 HTTP JSON **64** / MySQL **71 PASS** + 浏览器 **17 PASS**；
> 4.7 ASR 验收 HTTP **33 PASS** + 浏览器 **20 PASS**；
> 4.8 集成中心验收 HTTP JSON **99** / MySQL **99 PASS** + 浏览器 **45 PASS**；
> 4.9 分享/反馈验收 HTTP **61 PASS** + 浏览器 **42 PASS**；
> 3.9 跨域浏览器验收 JSON **25 PASS**；
> 统计验收 JSON **88 PASS** / MySQL **83 PASS** + 浏览器各 **27 PASS**；
> 未识别池验收 HTTP **44 PASS** + 浏览器 **18 PASS**。

### 批2 · 管理端核心（已完成）

- **任务 2.1 会话日志管理页** ✅ 关联 M-S-01。原型 `kb-engine` 无日志管理页，本项为**新增能力**：
  - `app/services/chatlog_admin.py`（新增）：**双视图**（问答明细 = 一问一答 / 咨询用户 = 按会话聚合）
    \+ 筛选（智能体 / 渠道 / 角色 / 时间范围）+ 关键词检索（问题与回答）+ 分页 + CSV 导出。
    **分页与导出共用 `_collect()`**，从机制上保证「导出的就是页面所见」，避免两套口径漂移。
  - **双后端语义一致**：`KB_STORE=json` 读 `data/sessions.json`；`mysql` 查
    `chat_session`/`chat_message`/`answer_citation`/`token_usage`，且把**会话级筛选与时间范围
    下推到 SQL**（扫描窗口只覆盖相关消息，而非全库最近消息）。两条分支归一为同一份「消息行」结构。
  - **排序必须全序**：时间倒序 → `msgId` → `sessionId` 三级 tiebreaker。
    （踩坑实录：`msgId` 只在会话内自增、**并非全局唯一**，缺末位兜底会让同刻记录顺序不稳定，
    表现为翻页重复/漏项。）
  - **已知边界如实暴露**：响应带 `scanned`/`scanLimit`/`store`，前端提示条直接显示
    「本次扫描消息 N / 上限 2000」；JSON 模式**没有** agent/channel/model/token 落库，
    对应字段返回 `None` 并显示「—」，**不用假数据填充**。
  - `app/api/admin.py`（重写，26 行占位 → 实装）：5 个接口，统一挂
    `require_admin` + `rate_limit("admin")`；导出带 **UTF-8 BOM**（Excel 不乱码）+
    RFC 5987 中文文件名 + `nosniff`，命中 `EXPORT_LIMIT` 时附 `X-Export-Truncated`（不静默截断）。
  - `app/security/oplog.py`：新增 `LOG_EXPORT` 动作码；`list_operations` 在 JSON 模式**早返回空**，
    避免管理端在无库环境 500。
  - **前端**：`static/chatlog.js`（新增）+ 管理页页签化（`#page-docs` / `#page-logs`），
    按需懒加载。
  - **验收**：`tools/verify_chatlogs.py`（服务层+接口层+鉴权，**60 PASS**）、
    `tools/verify_chatlog_ui.py`（真实 Chromium 端到端，**30 PASS**，零 JS 错误）、
    `tests/test_chatlog_admin.py`（**20 项**）。

- **任务 2.2 智能体 / 助手管理** ✅ 关联 M-A-01/02/03。原型同样无此能力：
  - `app/db/agent_store.py`（新增）：双后端（JSON `data/agents.json` / MySQL `agent` 表），
    `visible_roles` 在库内是逗号串、对外**一律归一为数组**，避免前端两套解析。
    JSON 模式首次访问自动 seed 默认助手（编码与 `chatlog.py` 的 seed 常量一致，不产生第二个「默认助手」）。
  - `app/services/agent_admin.py`（新增）：
    - **场景智能体预设 6 个**（M-A-01）：财务 / 招生 / 后勤 / 国资 / 信网 / 迎新，
      每个带**角色化系统提示词** + 描述 + 头像 + 可见角色；
      「一键搭建」按 `agent_code` 判重——**已存在则复用原记录**，绝不建重复行。
    - **校验一次报全**（而非遇错即停），错误分级映射 **400 参数 / 404 不存在 / 409 冲突**，
      便于管理页一次性提示。
  - **数据模型扩展**（M-A-03 助手基础配置）：`agent` 表新增四列
    `description` / `stream_enabled` / `risk_reply` / `fallback_reply`。
    `database/schema.sql` 已同步（新建库直接带列）；**已有库**用
    `tools/migrate_agent_2_2.py` 幂等补列（MySQL 8 无 `ADD COLUMN IF NOT EXISTS`，
    故先 `SHOW COLUMNS` 再按差集 ALTER，支持 `--dry-run`）。
  - **接口 8 个**：列表（含预设状态，省一次往返）/ 详情 / 预设 / 创建 / 一键搭建 /
    更新 / 启停 / 删除。**`/agents/presets` 必须注册在 `/agents/{agent_id}` 之前**，
    否则 `presets` 会被当作 id 转 int，直接 422（已加测试守卫）。
  - **前端**：`static/agent.js`（新增）+ 场景卡片区 + 助手列表 + 新建/编辑弹窗
    （名称 / 编码 / 类型 / 描述 / 头像 / 可见角色 / 输出方式 / 启禁用 / 风控话术 / 兜底话术）。
  - **验收**：`tools/verify_agents.py`（**87 PASS**，含「不同助手有独立 agent 行」口径）、
    `tools/verify_agents_ui.py`（真实 Chromium 端到端，**45 PASS**，零 JS 错误，
    且**跑完自动恢复场景搭建状态**，可重复运行）、`tests/test_agent_admin.py`（**41 项**）。

- **任务 2.3 提示词 / 检索参数 / 对话测试** ✅ 关联 M-A-04/05/09。
  2.2 只做到「可存储可读取」，本任务把它**真正接线**——配置不再只是躺在库里。
  - **`app/core/agent_runtime.py`（新增）**：把 `agent` 表里的配置翻译成检索/生成两层
    能用的参数，并固化优先级 **显式请求 > 助手配置 > 全局默认（.env）**。
    空 ID / 非法 ID / 不存在 / 已下架四个分支显式区分：`strict=True` 抛
    `AgentUnavailable`，非严格模式返回带 `error` 的空配置。
  - **提示词在线生效（M-A-04）**：`generator.build_messages/generate/generate_stream`
    增加 `system_prompt` 入参，为空回退 `SYSTEM_PROMPT`；
    `/api/chat` 与 `/api/chat/stream` 支持 `agentId`，**选了不存在/已下架的助手直接 400，
    不静默回落**（否则「选了财务助手却拿到通用回答」在日志里完全不可见）。
  - **检索参数与知识范围（M-A-05）**：`pipeline.retrieve` 支持
    `topK` / `rerankTopn` / `minScore` / `depts` / `ktypes`。
    **范围过滤在 `hybrid_search` 之前执行**——放到结果之后会被其它部门的切片占满
    `recall_k` 名额，表现为「配了知识范围反而搜不到」（`verify_prompt.py` 用
    「全局前 5 完全不含该部门」的反例把这条锁死）。`minScore` 作用在**余弦相似度**上
    （三种检索模式都算 cos，量纲统一 0–1，可解释）。
  - **数据模型扩展**：`agent` 表新增 `retrieval_config JSON`（**允许 NULL**，
    MySQL 的 JSON 列不支持字面量默认；`NULL` 与 `{}` 等价，正是「缺省即沿用全局」的语义）。
    `database/schema.sql` 已同步；已有库用 `tools/migrate_agent_2_3.py` 幂等补列。
  - **`retrieval_config` 用可辨识零值而非物化全局值**：`0` / `[]` 表示「沿用全局 / 不限定」。
    物化会让改 `.env` 后已建助手不跟随，也会让「未配置」与「显式配成同值」无法区分。
    `PUT /agents/{id}/retrieval` 是**增量语义**（`merge_retrieval` 只覆盖显式传入的键）——
    管理页按页签分别保存，整体替换会让「只改召回条数」把知识范围一起清空。
  - **对话测试（M-A-09）**：`POST /agents/{id}/test` 用该助手的提示词 + 检索配置跑一次
    **不落库**的问答（`save=False`），返回答案/引用/召回明细 + **生效参数**
    （`topK=3（助手配置）`、`rerankTopn=30（全局默认）`、部门范围、提示词是否自定义）。
    运营据此能判断「回答不好」是提示词问题还是检索问题。
    测试**不留操作日志**，否则调试会把审计日志刷满。
  - **接口 4 个**：`GET /kb-folders`（部门×类型的文件夹清单 + 文档数/切片数）、
    `GET`/`PUT /agents/{id}/retrieval`、`POST /agents/{id}/test`。
    `GET /agents` 额外下发 `defaultSystemPrompt` / `retrievalDefaults` / `retrievalRanges`，
    让管理页能显示「留空到底等于什么」，**前端不写死第二份默认值**。
  - **前端**：编辑弹窗拆为四页签（基础信息 / 提示词 / 检索配置 / 对话测试）；
    提示词页签可展开平台默认提示词并显示字数；检索配置页签渲染知识文件夹多选网格、
    实时提示「生效 N 条（助手配置）/ 沿用全局（5 条）」；测试页签渲染答案、引用、
    召回明细表与生效参数。列表新增「测试」按钮直达测试页签，并给配过的助手打
    「提示词 / 检索」标记。
  - **未接线字段显式登记**：`modelCode` / `temperature` / `topP` / `maxTokens` /
    `riskReply` / `fallbackReply` 仍为「可存储、未接线」，由 `agent_runtime.PENDING_FIELDS`
    统一声明并经接口下发到管理页，**不假装已生效**。前者依赖任务 2.4 的 LLM 统一网关
    （模型路由），后者需要先统一「平台级默认话术」的唯一来源（与 1.12 的
    `sensitive.RISK_REPLY` 重叠），一并在 2.4 / 2.11 处理。
  - **一致性不变量**：未配置检索参数的助手，召回结果与「完全不传 runtime」**逐项一致**
    （`tests/test_agent_config.py::test_unconfigured_agent_matches_legacy_retrieval` +
    `verify_prompt.py` 的四用例比对）——这是不破坏批1 回归的前提。
  - **验收**：`tools/verify_prompt.py`（**73 PASS**）、
    `tools/verify_agent_config_ui.py`（真实 Chromium 端到端，**49 PASS**，零 JS 错误，
    且跑完自清理）、`tests/test_agent_config.py`（**35 项**）。

- **任务 2.4 大模型与模型管理** ✅ 关联 M-A-06 / M-SYS-06 / C-01。
  2.3 留在 `PENDING_FIELDS` 里的四项（`modelCode` / `temperature` / `topP` / `maxTokens`）
  在本任务**全部接线**——从「配置只是躺在库里」变成「问答真的走它」。

> ⚠ **架构变更（2026-10，后续修订）**：**模型管理已下线，模型唯一来源是网关**。
> - 能小智**不再有自己的模型台账**：`app/db/model_store.py`、`app/services/model_admin.py`、
>   `static/model.js`、「模型管理」页签与相关 5 个写接口**均已删除**。
> - 可用模型清单由网关下发：`GET {LLM_BASE_URL}/models`（受业务 Key 的 `AllowedModels`
>   白名单过滤），见 `app/services/gateway_admin.py::list_remote_models`（30s 缓存 +
>   可强制刷新）。助手的「绑定模型」下拉即取自此。
> - `model_code` **就是网关的模型名**，能小智不做本地映射 → 路由只剩两支：
>   空/`default` → `.env`（`source=env`）；其余 → 全局网关连接 + 该编码（`source=gateway`）。
> - `.env` 的 `LLM_MODEL` **可省略**：省略时默认路由（`default`）自动取网关 `/v1/models` 清单的
>   第一个模型——能小智侧连「默认用哪个模型」都不用配，真正只持网关地址 + 业务 Key。
> - 因此原「库内查不到就回退」的分支**消失了**——本地没有第二份台账，
>   也就没有「本地登记但网关不认」这种状态。绑定校验改为提示性检查
>   （`gateway_admin.unknown_binding`），且**清单拉不到时不判非法**（网关不可达 ≠ 模型不存在）。
> - 原「厂商 Key 加密落库」能力一并移除：能小智现在**只持有网关业务 Key**，
>   厂商凭据只在网关侧。这比"把厂商 Key 加密存在能小智"更小的暴露面。
> - 下方关于 `model_store` / 路由三分支 / Key 加密的历史实现说明**已过时**，
>   保留作为演进记录；当前实现以上面这段为准。

  历史实现（已随架构变更移除，仅作演进记录）：

  - **为什么必须先做网关层**：批1 的 `llm_client.get_client()` 是**全局单例**，
    所有助手共用同一个模型——「按助手绑定模型」在结构上无法实现。
    新增 `app/core/llm_router.py` 按 `model_code` 缓存多实例客户端；
    空 / `default` 仍复用单例，使 `reset_client()` 对默认路径依旧有效、批1 行为不变。
  - **路由三分支**（全部可观测，且**绝不抛异常**）：
    空 / `default` → `.env`（`source=env`）；库中命中且启用 → DB（`source=db`）；
    查不到或已停用 → **回退 `.env` 并记 `source=fallback` + `error`**。
    回退而非报错的理由：模型配置是运营数据，删错一个模型不该让线上问答整体 500，
    而静默回退才是最危险的——所以错误信息会一路带到管理页与日志。
    （**降级链不在本任务**：`token_usage.call_state` / `fallback_from` 两列已建好但未接线，
    C-01 的「主模型失败自动切备用」留给后续任务。）
  - **`app/db/model_store.py`（已删除）**：双后端（JSON `data/models.json` / MySQL `llm_model`
    第 23 张表）。三条约定：① **`default` 是保留编码、不落库**，因此「未配置模型」与批1 的行为
    由**机制**保证一致；② **API Key 只进不出**——明文经 AES-256-GCM 加密落库，
    出参只有掩码，`with_secret=True` 才给明文且**只有路由层可用**（不解密时掩码恒为 `****`，
    **不泄露 Key 长度**）；③ 模型表**不做模型级默认参数**（温度/topP/最大输出只存在于 `agent` 表），
    否则同一个数值会出现两处定义。
  - **生成参数用「可辨识 NULL」而非「物化全局值」**：`agent.temperature` / `top_p` /
    `max_tokens` 改为**可空**，`NULL` = 沿用平台默认（与 2.3 的 `retrievalConfig` 同一思路）。
    物化会让改 `.env` 后已建助手不跟随，也让「未配置」与「显式配成同值」无法区分。
    **`0.0` 是合法温度，不能当哨兵值**（已用假 LLM 端点实测断言）。
    **迁移 `tools/migrate_agent_2_4.py` 会把 2.4 前物化的 `0.3/1.0/2000` 置为 NULL**——
    依据是这三列在此之前**从未被读取过**（见 `PENDING_FIELDS` 的登记），置空是如实反映现状。
    `topP` 平台默认为 `None`：**请求体里不下发该字段**，各厂商默认值并不一致，硬填反而改变行为。
  - **接口 6 个**：`GET /models`（含厂商模板/取值范围/Key 加密状态）、`POST /models`、
    `GET/PATCH/DELETE /models/{model_code}`、`POST /models/{code}/probe`，
    外加 `GET /models/choices`（助手可绑定下拉，只含启用中）与 `GET /models/route-preview`。
    ⚠ **`choices` 与 `route-preview` 必须注册在 `/models/{model_code}` 之前**——与 2.2 的
    `/agents/presets` 同类，但更阴险：`model_code` 是字符串不会 422，而是**静默返回
    「模型不存在」**（已加守卫测试）。
  - **错误分级与删除保护**：400 参数 / 404 不存在 / 409 冲突；**被助手引用的模型不许删**
    （409 并列出引用者）——否则那些助手会静默回退全局默认，运营完全看不出问题出在哪。
    `probe` 失败也返回 **HTTP 200 + `ok:false`**：失败是运营需要看到的结果，不是 500。
    `POST /models` 与 `probe` **写 / 不写操作日志**分别有明确取舍（增删改要留痕，
    自测不留，否则调试会把审计日志刷满；detail 里**只记 `hasApiKey`，绝不记 Key 本身**）。
  - **跨字段校验**：启用中的非本地模型必须有 Key（否则一定调用失败）。**PATCH 不带 `apiKey`
    表示「不改」，不能因此误报缺 Key**——所以校验在合并后做，并用「库里有没有 Key」
    代替「请求里有没有 Key」。
  - **前端**：`static/model.js`（新增）+ 管理页第 4 个页签（模型表格 / 全局默认卡片 /
    新增编辑弹窗 / 连通性自测）；助手编辑弹窗新增「绑定模型」下拉与三个生成参数输入框
    （留空 = 沿用平台默认），模型增删改后经 `window.refreshModelChoices` 同步助手页下拉。
  - **顺手修掉的三个前端缺陷**（均由 `tools/verify_model_ui.py` 的浏览器验收抓出）：
    ① 厂商下拉在 `initModelPage` 里渲染，而那时 `vendors` 还是空数组 →
    下拉恒空、**「选厂商自动预填」整个失效**（改为在 `load()` 内渲染，并加静态契约测试锁死）；
    ② 列表行「自测」的结果落在**弹窗内**节点，而弹窗是关着的 → **用户点了没反应**
    （改为页面常驻节点 `#md-probe-page`）；
    ③ 注释声称有「清除」按钮但页面没有 → 用户**无法清空已保存的 Key**（补上按钮 + 显式提交 `apiKey:""`）。
  - **还修掉一个与代理相关的真实缺陷**：`llm_client` 用默认 `trust_env=True` 的 `httpx.post`，
    校园网的 `HTTP_PROXY` 会把 `127.0.0.1` 的请求也发给代理 → **校内本地模型（C-05）
    无论如何都连不上**（报的是代理的错，更难排查）。改为按 host 判断：本地地址绕过代理，
    云端地址仍走代理（部分校园网必须经代理出网）。
  - **`modelCode` 也纳入可显式置空**：`AgentUpdateRequest.NULLABLE_KEYS` 原本只放行
    三个生成参数，导致**接口层无法解绑模型**（存储层可以，schema 层不行）——能力不对称。
    仍**不**对所有 null 放开：前端误传 `systemPrompt: null` 会静默清空提示词。
  - **一致性回归**：`tools/verify_model.py` 起一个**本地假 LLM 端点**真实捕获
    `/chat/completions` 请求体（`LLM_MOCK=1` 时生成层直接短路，看不到请求体，
    「参数是否透传」就无从验证），逐项断言 `model` / `temperature` / `max_tokens` / `top_p`
    与 `Authorization` 头；并验证**未绑定模型的助手**在引入模型管理前后响应**逐项不变**。
  - **验收**：`tools/verify_model.py`（**123 PASS**，八组含假端点参数透传与一致性回归）、
    `tools/verify_model_ui.py`（真实 Chromium 端到端，**43 PASS**，零 JS 错误，跑完恢复原状）、
    `tests/test_model_admin.py`（**33 项**）。

- **任务 2.4 延伸 · 接入 campus-ai-gateway（C-01 LLM 网关）** ✅

  对接 <https://github.com/Jiusheng01/campus-ai-gateway>（Go 写的校园大模型统一接入网关）。
  克隆在项目根目录 `campus-ai-gateway/`（**交付打包时须排除**，该仓库无 LICENSE）。

  - **结论：配置级接入，`LLMClient` 一行不用改。** 依据（读其源码而非 README）：
    数据面是 17 行透明反向代理（`FlushInterval=-1` + `X-Accel-Buffering: no`，SSE 不缓冲）；
    响应体**原样透传**（审计只写 1MB 上限的旁路副本）；请求体只读
    `model`/`stream`/`max_tokens` 用于白名单与 TPM 预占，其余字段原样转发；
    错误体 `{"error":{"code","message","request_id"}}` 与 OpenAI 同构。
  - **怎么接（三处配置，二选一）**：
    管理后台「模型网关」新建模型——`apiBase=http://<gateway>:8080/v1`、
    `remoteModel=<网关虚拟模型别名>`、`apiKey=sk-campus-*`，再让助手 `modelCode` 指向它；
    或直接改 `.env` 的 `LLM_BASE_URL` / `LLM_API_KEY`（业务 Key）；`LLM_MODEL` 可省略——省略时自动取网关第一个模型。
  - **厂商 Key 不需要交给能小智**：网关的 `authMiddleware` 会删掉客户端 `Authorization`，
    由数据面按 `provider.credential_env` 从**容器环境变量**注入渠道凭据。
    能小智只持有网关签发的 `sk-campus-*`。
  - **错误不再是一坨字符串**：`LLMError` 现携带 `statusCode` / `code`（网关错误码）/
    `requestId`（可到网关 `gateway_request_log` 定位）/ `hint`（中文处置建议），
    并向下贯穿到三处：
    ①`POST /api/chat` → **502** + `errorDetail`（**接入前这里是 500**：`pipeline` 不捕获
    `LLMError`，用户只看到「Internal Server Error」）；
    ②`POST /api/chat/stream` → `error` 事件追加 `errorDetail`；
    ③管理页「自测」→ 直接显示处置建议与 `requestId`。
  - **不透传上游状态码**：网关的 401/403 说的是「网关不认我们这把 Key」，
    对前端而言不是「需要登录」，透传会把前端引到错误的处置方向；真实状态码放在
    `errorDetail.status` 里，运维照样看得到。
  - **四个必须知道的坑**：
    ①`remoteModel` 必须与业务 Key 的 `AllowedModels` **逐字一致**，否则 403
    `model_not_allowed`（现在会明确提示）；
    ②能小智按 IP 限流 + 网关按 Key 限流（RPM/TPM/并发）→ 注意双重拦截；
    ③只跑 gateway 不跑 twcore 会退化成纯限流+审计代理（**网关自身不实现路由/降级**）；
    ④网关自述未最终验收的能力（业务数据面 Failover、30,000 RPM、真实 IdP）
    **不能当作能小智的验收结论**，须在本校环境重跑。
  - **验收**：`tools/verify_gateway.py`（静态 5 组 + 在线 11 断言 + `--self-test` 假端点自测）、
    `tools/_fake_gateway.py`（按网关契约实现的假端点）、
    `tests/test_gateway_integration.py`（**11 项**：默认路径与库内模型两条成功链路、
    403/401/连接失败三条失败链路都必须 502、流式 delta/done/error 三态、契约键集合守卫）。
  - **实测状态**：✅ **已在真实网关上线验证**（Docker 起 `deploy/docker-compose.yml`
    的 postgres17 + redis7 + migrate + gateway-server + nginx，数据面 `DATA_PLANE=direct` 直连厂商）。
    `tools/verify_gateway_e2e.py` → **34 PASS / 0 FAIL**；`verify_gateway.py` 在线断言
    → **11 PASS / 0 FAIL**；SSE 首块 4ms / 全程 457ms（三跳都不攒批）。
  - **网关版本**：本地 = 上游 **`4b87b6a`**（2026-10-20 升级，原 `91531ae`）。
    该版本**移除 ThinkWatch**、把加权路由与熔断下沉到进程内、限流/配额上移到 Tenant；
    迁移 11 → **13**。⚠ 升级后问答链路仍不可用——`API_KEY_HMAC_SECRET` 与
    `INTERN_DISCOVERY_API_KEY` 两个密钥丢失（业务 Key 一律 401 `invalid_api_key`），
    须由网关侧补齐密钥并重签业务 Key。
    **仍未实测**：真实校园 IdP、多渠道路由/熔断场景 —— 须在本校环境跑。

- **管理后台「模型网关」页** ✅（C-01 接入的前端入口）

  后台页签（知识库 / 智能体管理 / **模型网关** / 会话日志）。

  - **只做外链，不做内嵌**：网关控制台带 `X-Frame-Options: DENY` +
    CSP `frame-ancestors 'none'`，**明确禁止被嵌入**——其源码注释写明这是为保护
    「控制台在内存中持有访问令牌」。绕过它（改源码或反代剥离安全头）会削弱上游的
    安全边界，所以入口做成 `target="_blank"` 新标签页。
  - **不持有网关管理员凭据**：后端只探**无需鉴权**的 `/healthz`，
    因此能小智不需要为「看个状态」而多保存一份高权限凭据。
    代价是看不到配额/用量类指标（那些在 `/admin/status`），**页面如实说明**，
    而不是显示一堆「—」让人以为功能坏了。
  - **探测三态，刻意不合并**：`online`（`/healthz` 200）/
    `unknown`（有响应但非 200 —— 本地推理服务 vLLM/Ollama 就长这样，
    **报成"离线"会让运维去查一个其实活着的服务**）/ `offline`（连接失败）。
  - **端点来源自动汇总**：`.env` 的 `LLM_BASE_URL` + 库内各模型的 `apiBase`，
    按 origin 去重；厂商域名（DeepSeek/百炼/方舟等）标为「厂商直连」且**不做探测**
    （它们可能因未配 Key 返回 401，那不代表离线）。
  - **错误码对照表**：11 条网关错误码 → 可执行的中文处置建议，按 `errorDetail.code` 定位。
    与管理页自测、问答错误提示共用同一批语义。
  - **验收**：`tools/verify_gateway_ui.py`（真实 Chromium，**43 PASS**，零 JS 错误）+
    `tests/test_gateway_admin.py`（**27 项**，含真实 socket 验证探测三态与代理绕过）。

- **前端打磨（随 2.4 延伸一并做）**
  - **键盘焦点可见性**：原先只有 `input/select/textarea` 有 focus 样式，
    **按钮与页签完全没有** —— 深色主题下 Tab 导航看不出焦点在哪（可访问性缺陷）。
    用 `:focus-visible` 补齐（鼠标点击不显示焦点环，仅键盘导航显示）。
    ⚠ 验证时**不能只看 `outline`**：输入框的焦点靠「border 变色 + 3px 光晕」表达，
    只读 outline 会误判为"不可见"——这个坑在自检时踩过一次，已在工具注释中记明。
  - **懒加载提示**：切页签时显示「加载中…」，**延迟 250ms 才出现** ——
    本地库通常几十毫秒返回，立刻显示会让提示闪一下，比不显示更糟。
    各页 `init*Page()` 统一返回 Promise 供其判断（`agent.js`/`chatlog.js`/`gateway.js`
    补了 return，`model.js` 本就是 async）。
    ps：提示只在**慢请求**时出现，验收工具用延迟 1.2s 逼出该状态来断言，
    否则「永不出现」也能通过。

- **任务 2.5 知识库分类管理** ✅ 关联 M-K-01 / M-K-02。分类 = 「部门 × 知识类型」，
  是 2.6 授权与 2.7 批量移动的组织维度。
  - **双后端同形**：MySQL（`kb_category` 真实表）支持完整 CRUD；JSON 模式是**索引只读推导**
    （`editable=false`，写操作 400 + 可执行指引），页面上给只读提示而不是一排禁用的按钮。
  - **删除保护**：分类下**有文档**或**有子分类**时 409 并说明原因；重命名/排序走 PATCH。
  - **接口 4 个**：`GET`/`POST` `/api/admin/kb-categories`、`PATCH`/`DELETE` `/{id}`；
    操作留痕 `CATEGORY_CREATE/UPDATE/DELETE`。
  - **前端**：知识库页新增「知识分类管理」卡片，按部门分组渲染树，附文档数徽章。
  - **验收**：`tools/verify_kb_category.py`（**34 PASS**）+ `verify_kb_category_ui.py`（真 Chromium，
    **11 PASS**，零 JS 错误）；`tests/test_kb_category.py`（13 项）。

- **任务 2.6 知识库部门授权** ✅ 关联 M-K-03。超管为**部门联络员**按
  「用户 × 部门 × 知识类型」分配五个操作位（上传/查看/下载/删除/建子分类）。
  - **数据层**：`kb_permission`（`user_id × dept_id × ktype` 唯一，`dept_id=0`/`ktype='*'`
    通配全部）；`user_permission_for()` 取**最特化**的命中（精确部门 > 全部部门、
    精确类型 > 通配类型）。
  - **判定链 `can_operate()`**：`AUTH_ENABLED=false` → 放行（开发/演示）；管理员 → 恒过；
    未登录访客 → 仅管理性用途由路由守卫兜底；**已登录的非联络员 → 403**；
    部门联络员 → 需命中授权行且对应操作位为 1。
  - **接入点**：docs 路由守卫由「仅管理员」改为 `require_doc_operator`（管理员 + 联络员）；
    上传 → `canUpload`、改状态/删除 → `canDelete`、预览 → `canView`、原件下载 → `canDownload`。
  - **接口 4 个**：`GET`/`POST` `/api/admin/kb-permissions`、`PATCH`/`DELETE` `/{permId}`。
  - **验收**：`tools/verify_kb_permission.py`（**31 PASS**）+ `verify_kb_permission_ui.py`（真 Chromium，
    **9 PASS**）；`tests/test_kb_permission.py`（16 项，覆盖 AUTH 关闭/管理员/联络员/越权四态）。

- **任务 2.7 文档审核与文件管理** ✅ 关联 M-K-05 / M-K-09。
  - **审核流**：审核通过 = `status 2`（已发布，**立即可检索**）；驳回 = `status 0`（草稿，
    不再进检索 —— 检索层只读 `status=2`）。预览页对**待审**文档直接给出
    「✓ 审核通过 / ✕ 驳回」，点完即刷新状态与可检索性。
  - **筛选**：`GET /api/docs` 新增 `status` / `ktype` / `categoryId` / `dept` 四个查询参数，
    MySQL 模式**全部下推到 SQL WHERE**（不取回后内存过滤）；⚠ `data` **仍是数组**——
    既有前端与历史测试都按列表消费，改 envelope 属于破坏合同。
  - **批量操作**：`POST /api/docs/batch`，`action = publish | reject | delete | move`
    （`move` 需 `categoryId`，不传则移到「未分类」）。逐个文档先做 2.6 的授权前置校验，
    再执行批量 SQL（单条 `UPDATE ... WHERE doc_id IN (...)`，空 id 短路不产 SQL），
    成功后统一 `clear_index()` 并落操作日志。
  - **前端**：文档表格新增复选框列 + 表头全选；筛选行新增类型/分类两个下拉；
    表格下方批量条显示已选计数并支持通过/驳回/移动/删除/清空。
  - **验收**：`tests/test_kb_audit.py`（26 项：JSON 合同 + repo 桩 + API 桩）、
    `tools/verify_kb_audit.py`（**51 PASS** 无库；`--mysql` 时 **77 PASS**，含真实库
    快照→操作→恢复式幂等验收）、`tools/verify_kb_audit_ui.py`（真 Chromium，**24 PASS**：
    筛选回填、批量条联动、审核点击流、零 JS/HTTP 错误）。
  - ⚠ **两个踩过的坑已修**：①`.batch-bar`/`.btn-ghost` 的 `display` 会覆盖 UA 的
    `[hidden]{display:none}`，导致「hidden 了还显示」→ admin.css 顶部加全局兜底；
    ②`DocBatchRequest.docIds` 原为 `list[int]`，把对外编号 `"D001"` 在 schema 层拒成 422，
    与 `_parse_doc_id` 的契约矛盾 → 放宽为 `list[int|str]`。
  - ⚠ **迁移缺口**：2.5/2.6 改 `schema.sql` 时漏了迁移脚本，线上库缺 `kb_permission` 表
    （管理页一调 `/api/admin/kb-permissions` 就 500）→ 新增
    `tools/migrate_kb_2_5_2_6.py`（SHOW TABLES 取差集 + `CREATE TABLE IF NOT EXISTS`，幂等）
    并纳入 `tests/test_migrations.py` 的 DDL 守卫。

- **任务 2.8 知识点管理** ✅（M-K-10）
  - 术语：**知识点 = 检索切片**（`kb_chunk`），对外编号 `D001-C003`。
  - **后端**：`kb_repo` 新增知识点仓储（`list_points` 六个筛选 + 分页、`get_point`、
    `point_scope`、`max_chunk_seq`、`insert_point`、`update_point_fields`、`delete_point`、
    `save_point_vector`/`drop_point_vector`、`recount_doc_chunks`）；
    `app/services/kb_knowledge.py` 负责编排；`app/api/knowledge.py` 提供 9 个端点
    （前缀 `/api/knowledge`，见接口表）。
  - **再加工四件套**：添加文本内容（`start`/`end` 两个位置）、删除内容
    （按片段文本或 `[start,end)` 区间，**删空正文直接拒绝**并引导改用删除知识点）、
    插入图片（只接受该文档已有图片键，复用 `doc_preview` 的两层穿越防护）、
    删除无效切片。
  - **单点重算向量**：正文或标题路径一变，立刻用 `embed_text()` 重算该切片向量并
    置 `vec_synced=1`；**向量失败不回滚内容**——改为置 0 + 返回 `warning`，
    因为丢内容比丢向量严重，且 `vec_synced` 本身就是「待对账」标记位。
  - **导出**：CSV（`utf-8-sig`，Excel 双击不乱码）/ Markdown（按文档分节）/ JSON；
    `Content-Disposition` 带 RFC 5987 文件名 + `nosniff`。导出前逐个知识点做
    `canDownload` 校验，**有一条越权即整体 403**（不做「部分成功」以免数据外泄）。
  - **前端**：新增独立页 `/points`（`points.{html,js,css}`）——按「部门/类型/块类型/关键词」
    筛选、分页、勾选批量导出、编辑弹窗（正文 + 标题路径 + 块类型 + 图片键 + 三个再加工面板）、
    新建切片、删除确认。管理页顶栏加「知识点管理」入口。
  - **鉴权口径**（沿用 2.6）：浏览/详情 → `canView`；新建/编辑/再加工 → `canUpload`；
    删除 → `canDelete`；导出 → `canDownload`。
  - **验收**：`tests/test_kb_points.py`（57 项：纯逻辑 + 服务编排 + 接口层）、
    `tools/verify_kb_points.py`（**104 PASS** 无库；`--mysql` 时 **135 PASS**，含真实库
    快照→操作→恢复式幂等）、`tools/verify_kb_points_ui.py`（真 Chromium，**36 PASS**：
    检索、批量条、编辑弹窗、再加工三件套真实写库、新建、真实下载事件、删除确认流、
    零 JS/HTTP 错误）。
  - ⚠ **为什么单独成页而不是新增页签**：知识点是「切片级」浏览维度，与文档管理的
    「文档级」不同，独立页语义更清晰。（当时加页签会打挂 5 个断言页签数为 4 的浏览器
    验收工具；2.10 新增「系统管理」页签时已把这些断言统一改为 5。）

- **任务 2.9 角色权限** ✅（M-SYS-01 / M-SYS-03 菜单部分）
  - **两层模型正交**：2.6 管「能碰哪些数据」（用户 × 部门 × 知识类型），本任务管
    「能进哪些页面、能点哪些按钮」（角色 × 权限点）。`app/security/permissions.py`
    定义 **16 个模块 / 36 个权限点**（3.10 新增 `stats.view` / `stats.export`，
    3.11 新增 `unrecognized.view` / `unrecognized.manage`，
    4.9 新增 `feedback.view` / `feedback.manage`，
    4.5 新增 `gov.view` / `gov.manage`，
    4.4 新增 `access.view` / `access.manage` / `access.export`）；
    管理员恒有全部权限，部门联络员默认 8 个，
    其他管理角色（`role_type=2`）按 `sys_role_permission` 配置。
  - **数据层**：`sys_role_permission(role_id, perm_code)` 新表 + `sys_user.password_hash`
    可空列；`tools/migrate_rbac_2_9.py` 幂等迁移（`--dry-run` 可预览）。
    密码用 PBKDF2-HMAC-SHA256（260k 次）存储；`password_hash` 为 NULL 的历史演示账号
    仍可免密登录，**设置密码后即强制校验**。
  - **路由守卫**：`required_for(method, path)` 集中映射请求 → 权限点，管理路由统一挂
    `require_permission`，避免逐个端点漏挂；未识别的 `/api/admin/*` 按最严格的
    `rbac.manage` 处理。越权返回 **403** 并落 `ACCESS_DENIED` 操作日志（含路径与角色）。
    权限每次请求实时查库，**改矩阵无需重新登录**；JWT 仅携带 `rid`/`mgr` 供展示。
  - **前端**：`static/auth.js` 全局 `NXZAuth`——登录弹窗、顶栏身份徽标、`[data-perm]`
    控制菜单/按钮可见性（逗号分隔 = OR）、403 遮罩、带 Bearer 的 blob 下载与图片授权
    （`data-auth-src`/`data-auth-href`）。RBAC 管理嵌在「系统管理」页签底部（不新增第 6 个
    页签）：角色卡片 + 权限勾选矩阵 + 管理员账号 CRUD；密码回显只显示「已设置/演示账号」。
  - **兼容**：`AUTH_ENABLED=false` 时 `NXZAuth` 立即就绪并一律放行，既有演示流程不变；
    `true` 时未登录 401 / 越权 403，页面初始数据加载按权限跳过（不产生 403 噪声）。
  - **验收**：`tests/test_rbac_admin.py`（11 项）+ `test_auth.py`（23 项）；
    `tools/verify_rbac.py`（活库 **31 PASS**：登录/矩阵/越权/日志/自锁保护）；
    `tools/verify_rbac_ui.py`（真 Chromium **25 PASS**：受限角色菜单与按钮、越权 403、
    `/points` 门禁、角色矩阵增删、日志筛选、零 JS 错误）。
  - ⚠ **批量端点按动作二次校验**：`POST /api/docs/batch` 路径只能映射到 `docs.audit`，
    `action=delete` 在端点内再校验 `docs.delete`，否则只有审核权的人能借批量接口删文档。

- **任务 2.10 关键词库 + 操作日志** ✅（M-SYS-07）
  - **数据层**：新增 `sensitive_word` 表（`word` 唯一、`kind` = sensitive/keyword、
    `status` 启停、分类/备注/创建人），`schema.sql`、SQLAlchemy 模型与
    `tools/migrate_sensitive_2_10.py`（幂等，已对线上库执行）三处同步；
    `app/db/sensitive_repo.py` 提供 CRUD、按词判重、启用词条读取。
  - **加载源切库**：`app/security/sensitive.py` 改为 **DB 优先 → 文件 → 内置**，
    内置词表始终合并；MySQL 模式下读启用词条，**30 秒 TTL** 缓存，
    增删改后显式 `invalidate()` —— **下一问即生效**，多进程最长 30 秒收敛。
    JSON 模式行为不变（读文件 + 内置）。
  - **接口**：`GET/POST /api/admin/sensitive-words`、
    `PATCH/DELETE /api/admin/sensitive-words/{id}`；写操作落
    `SENSITIVE_CREATE/UPDATE/DELETE` 操作日志。JSON 模式词表为只读视图
    （`editable=false`，写操作 400 + 指引），不伪造可编辑能力。
  - **操作日志**：`GET /api/admin/operation-logs` 增加操作人 / 动作 / 起止日期筛选与
    页码分页；MySQL 把筛选**下推到 SQL** 并 outerjoin `sys_user` 带出操作人姓名；
    响应附动作中文标签与操作人候选列表。JSON 模式明确返回空集（无操作日志存储）。
  - **前端**：管理页新增第 5 个页签「系统管理」——词库管理（新增/启停/改名/删除，
    关键词标注「暂不参与风控」）与操作日志（筛选、分页、明细 JSON、导出入口）。
  - **验收**：`tests/test_sensitive_admin.py`（16 项：加载优先级/缓存失效、词库 CRUD、
    判重、JSON 只读、日志筛选桩）；`tools/verify_sensitive.py`（JSON **28 PASS** /
    `--mysql` **33 PASS**，含真实库新增→提问被拒→停用→不再拒→删除）；
    `tools/verify_sensitive_ui.py`（真 Chromium **13 PASS**，零 JS 错误）。
  - ⚠ **测试词条不要带 8 位以上连续数字**：日志脱敏会把数字段当学号掩码，
    审计明细里的词条名与原文对不上（这是 1.12 脱敏规则的预期行为，不是缺陷）。

- **任务 2.11 管理后台整合** ✅（M-SYS-04 / M-SYS-05 范围内）
  - **平台默认话术单一来源**：新增 `app/core/replies.py`，
    `DEFAULT_RISK_REPLY` / `DEFAULT_FALLBACK_REPLY` 成为唯一文本来源；
    `sensitive.RISK_REPLY`、`generator` 系统提示词、`llm_client` 模拟拒答、
    `agent_store` 默认值全部改为引用，消除三处各写一份的漂移。
  - **助手话术真正接线**：`AgentRuntime.risk_reply` / `fallback_reply` 从 `agent` 表读取，
    覆盖四条拒答路径——敏感提问拦截、空召回、模型主动拒答、敏感回答兜底。
    未配置时回落到平台默认；**`agent_runtime.PENDING_FIELDS` 已删除**，
    接口 `pendingFields` 恒为 `[]`（配置不再有「可存不可用」项）。
  - **统一导航**：5 个页签（文档 / 知识点 / 智能体 / 系统 / 日志）+ 9 个站内入口全部可达；
    `/points`、`/preview?doc=`、返回对话页无死链；受限角色按 2.9 权限点隐藏入口。
  - **验收**：`tests/test_pipeline.py` 新增 4 项助手话术覆盖用例；
    `tools/verify_admin_integration.py`（真实 Chromium **15 PASS**：配置落库/回显、
    测试面板话术覆盖、5 页签、9 站内链接 200、预览与返回、零 JS 错误）；
    全量 `pytest` **965 passed / 15 skipped**（共 980 条）；`tools/verify_agent_config_ui.py` **49 PASS**。
  - ⚠ **UI 验收不依赖真实向量**：本机未装 `ml` extra 时向量后端降级为哈希，余弦普遍低于
    0.1，非零 `minScore` 会把召回全部滤掉。该工具验证 UI 接线，阈值固定 0；
    阈值过滤由 `test_min_score_filters_results` 单独覆盖。

### 批3 · 渠道、协作与运营分析（已完成，11/11）

- **任务 3.1 渠道接入管理** ✅（M-C-01，2026-10-20）
  - **存储层** `app/db/channel_store.py`（新增）：MySQL 复用既有 `channel` / `channel_agent`
    两张表，JSON 模式落 `data/channels.json`；主助手恒在绑定集合首位，历史渠道无需回填
    也能正确展示，写入时保证主助手存在且排第一。
  - **服务层** `app/services/channel_admin.py`（新增）：渠道 CRUD + 绑定助手 + 生成
    `/?channel=<code>` 渠道 URL、`embed=1` 嵌入链接、iframe / 自包含浮窗代码；
    编码规范、唯一性、默认/绑定助手存在性、主题色/位置/文案一次校验报全
    （400/404/409）；公开端只返回已上架助手，渠道停用统一 404。
  - **接口** `app/api/channels.py`（新增）：管理端 5 个端点挂 `channels.view/manage`
    权限点并写 `CHANNEL_CREATE/UPDATE/DELETE` 操作日志；公开端
    `GET /api/channels/{code}` 免登录，供对话页/门户/iframe 读取渠道配置。
    `needAuth` 作为认证开关透传（真正的登录拦截依赖 4.1）。
  - **前端**：独立页 `/channels`（`static/channels.{html,js,css}`，在 `main.py` 于
    `StaticFiles` 挂载前注册）；对话页 `app.js` 读取 `?channel=` 后应用渠道名称/
    欢迎语/主题色/默认助手，`embed=1` 进入精简嵌入布局。
  - **迁移**：`tools/migrate_channel_3_1.py` 幂等补建表并回填 `channel_agent`。
  - **验收**：`tests/test_channel_admin.py`（15 项）、`tools/verify_channels.py`
    （**63 PASS**）、`tools/verify_channels_ui.py`（真 Chromium **38 PASS**，
    零 JS 错误，渠道分流/嵌入模式/默认助手透传全覆盖）。

- **任务 3.2 门户个性化配置** ✅（M-C-02/03/04，2026-10-20）
  - **门户配置** `themeConfig` 在原主题色/浮窗位置/按钮文案之上扩展：AI 形象
    （`assistantName` / `avatarText` / `avatarUrl`）、常见问题（`faq` ≤8 条）、
    快捷方式（`shortcuts` ≤8 条，动作 `ask`/`link`）、推荐应用（`recommendedApps` ≤8 个）、
    客服与反馈入口（`serviceEntry` / `feedback`，含开关、文案、链接）。
    所有文本限长、链接走协议白名单（拒绝 `javascript:` / `data:` 等），
    `themeConfig` 总大小 ≤16 KB，非法字段一次报全（400）。
  - **渠道复制** `POST /api/admin/channels/{id}/copy`（M-C-04）：自动生成不冲突编码
    （`<code>-copy`、`-2`…）与「原名（副本）」，深拷贝门户配置、继承可用助手
    （默认助手失效时回退），**副本默认停用**并记录 `copy_of` 源渠道；源渠道与副本
    后续可各自独立修改。操作日志新增 `CHANNEL_COPY`。
  - **管理端**：渠道页新增「门户个性化」配置区、行内认证一键开关与「复制」操作；
    列表行展示主题色/问答条数/应用数等摘要。
  - **用户端门户**：对话首屏按渠道配置渲染 AI 形象名称与头像（文字/图片）、
    常见问题、快捷方式、推荐应用卡片、客服/反馈入口；全部文案走 `textContent`、
    链接走 `safeUrl`，渠道配置不构成 XSS 注入面。
  - **验收**：`tests/test_channel_admin.py` 扩至 **21 项**（门户配置往返/14 类非法输入、
    认证开关、复制继承与独立编辑）；`tools/verify_channels.py` **81 PASS**、
    `tools/verify_channels_ui.py` 真 Chromium **57 PASS**（零 JS 错误）；
    全量 `pytest` **974 passed / 15 skipped**（共 989 条）。

- **任务 3.3 应用管理与角色授权** ✅（M-C-05/06，2026-10-20）
  - **存储层** `app/db/app_store.py`（新增）：MySQL `app` 表 + JSON `data/apps.json`
    双后端；应用来源分 `agent`（绑定智能体）与 `link`（外链），无需额外 `app_code`，
    `appId` 即稳定标识。
  - **服务层** `app/services/app_admin.py`（新增）：应用 CRUD、名称/简介/图标/排序/
    启停校验，`visible_roles` 支持「全部」与五类身份（访客/本科生/研究生/教职工/
    离退休人员）；智能体应用在助手**下架或删除**时自动隐藏，外链应用不受影响；
    链接/图标协议白名单。
  - **接口** `app/api/apps.py`（新增）：管理端 `GET/POST/PATCH/DELETE
    /api/admin/apps` 挂 `apps.view/manage` 并写 `APP_CREATE/UPDATE/DELETE` 操作日志；
    公开端 `GET /api/apps?role=` 按身份过滤（登录态以令牌角色优先，未知身份按访客）。
  - **前端**：独立管理页 `/apps`（`static/apps.{html,js,css}`，后台顶栏入口，
    复用 2.9 页面权限门）；对话首屏新增「应用广场」，按当前身份渲染应用卡片——
    智能体卡片点击即切换会话助手，外链卡片新窗口打开，身份切换后自动刷新。
  - **迁移**：`tools/migrate_app_3_3.py` 幂等建 `app` 表。
  - **验收**：`tests/test_app_admin.py`（12 项）、`tools/verify_apps.py`
    （JSON **45 PASS** / MySQL **48 PASS**）、`tools/verify_apps_ui.py`
    （真 Chromium **31 PASS**，零 JS 错误，含角色过滤与上下架联动）。

- **任务 3.4 渠道×角色会话路由** ✅（M-C-07 / P-B-08，2026-10-20）
  - **存储** `channel.route_config`（JSON，MySQL 由 `tools/migrate_route_3_4.py`
    幂等补列）：按五类身份（访客/本科生/研究生/教职工/离退休人员）各存一条路由，
    含默认助手、专属欢迎语、引导问题（≤6 条、单条 ≤120 字）与六项模块开关
    （`guideQuestions` / `shortcuts` / `recommendedApps` / `appSquare` /
    `serviceEntry` / `feedback`），配置总大小 ≤16 KB；路由助手必须是该渠道已绑定助手。
  - **公开端** `GET /api/channels/{code}?role=`：命中身份时覆写助手/欢迎语/引导问题/
    模块开关；未配置身份沿用渠道默认（`route.matched=false` + 全模块开启）；未知身份
    归一「访客」；路由助手已下架/解绑时回退渠道默认，避免隐式错误路由。
  - **管理端**：渠道清单新增「路由」操作，弹窗内按身份启用/配置助手、欢迎语、
    引导问题与模块开关；编辑渠道解绑助手时失效路由自动回退，不阻断保存；
    更新渠道操作日志补 `routeRoles`；复制渠道完整继承路由配置。
  - **用户端**：对话页 `loadChannel()` 携带 `?role=`，首屏按路由渲染欢迎语/引导问题，
    并按模块开关隐藏快捷方式/推荐应用/应用广场/客服反馈；切换身份即时重拉渠道配置
    与广场并整块重渲染，后续提问使用该身份路由后的助手。
  - **验收**：`tests/test_channel_admin.py` **26 项** + `tests/test_route_chat.py` 2 项
    （同题不同身份的引用集合不同，且不越权）；`tools/verify_routes.py`
    JSON **29 PASS** / MySQL **30 PASS**；`tools/verify_routes_ui.py` 真 Chromium
    **27 PASS**（零 JS 错误）；全量 `pytest` **1010 passed / 15 skipped**（共 1025 条）。

- **任务 3.5 用户端门户** ✅（P-B-04/05/06/07，2026-10-20）
  - **三端布局**：对话页由单列升级为「`#history-panel` 会话历史 + `.portal-main`
    问答区」两栏；桌面历史栏常驻，≤960px 改为抽屉（遮罩 + 关闭按钮 + 顶栏开关），
    720/480px 收紧顶栏与气泡宽度并改为品牌/控件两行排列；`?kiosk=1` 进入智能硬件
    模式（隐藏历史与管理入口、示例问题/发送按钮 ≥44px、输入字号 17px）。
    嵌入模式（`?embed=1`）继续只保留问答。
  - **历史会话**：本机 `nxz.history`（≤30 条、按渠道隔离）在每次回答完成后记录
    会话 ID 与首问标题；点击条目经 `/api/sessions/{id}/messages` 恢复整段对话，
    支持单条删除与一键清空；清空后刷新不再自动恢复。会话 404 时自动移除失效条目。
  - **登录 / 退出**：复用 1.13 `nxz_token` 与 `/api/auth/login`、`/api/auth/me`；
    登录后顶栏显示「学工号 · 身份」、角色下拉锁定，问答/应用广场/渠道请求携带
    `Authorization: Bearer`（后端以 token 身份为准），退出后恢复访客自选身份。
    未登录仍保持免登录问答（P-B-04）。
  - **个性化**：欢迎语/引导问题沿用 3.2 主题配置与 3.4 角色路由，应用广场沿用
    3.3 按角色过滤，登录切换身份后整块重渲染。
  - **验收**：`tests/test_frontend.py` 新增 3 项契约守卫（该文件 84 项通过）；
    `tools/verify_portal_ui.py` JSON **38 PASS / 1 SKIP**、MySQL **51 PASS**
    （三端布局 + 历史 + 登录退出，零 JS 错误）；3.2/3.3/3.4/3.7 UI 回归全过；
    全量 `pytest` **1013 passed / 15 skipped**（共 1028 条）。

- **任务 3.6 敏感词切库回归** ✅（伴随 2.10 完成）
  - 检测逻辑未改，仅词表来源切换到 2.10 的 DB；已实机验证**双向生效**：
    新增启用词 → `/api/chat` 提问侧直接拒答且不检索；停用/删除 → 同一问题不再拒答。
  - 回归入口：`tools/verify_sensitive.py --mysql`（33 PASS）、
    `tools/verify_sensitive_ui.py`（13 PASS）。

- **任务 3.7 AI 生成内容标识** ✅（P-B-17）
  - **显式标识**：对话页每条 AI 回答下方展示「AI 生成」徽标 +
    「内容由 AI 生成，仅供参考」；实时回答、历史恢复、拒答三条路径一致，用户消息不标。
  - **隐式标识**：`app/core/ai_label.py` 统一生成元数据——
    `data.aiLabel`（`aigc` / `label` / `text` / `producer` / `produceId` /
    `standard` / `version` / `model`）与响应头 `X-AI-Generated` / `X-AI-Label` /
    `X-AI-Produce-Id`；流式在 `done` 事件下发同一份元数据；
    历史消息接口对 AI 消息补 `aiGenerated` + `aiLabel`。
  - `produceId` = `会话ID:消息ID`，可回溯 `chat_message` 原始记录；
    响应头只放 ASCII，中文提示语只进响应体。客户端会话 ID 可含中文，
    `X-AI-Produce-Id` 先做百分号编码再写出（3.10 验收发现并修复，附回归测试）。
  - **验收**：`tests/test_ai_label.py`（7 项）、`tools/verify_ai_label.py`
    （**16 PASS**，响应体/响应头/流式/历史/拒答）、
    `tools/verify_ai_label_ui.py`（真 Chromium **9 PASS**，零 JS 错误）。
  - ⚠ **只读模式必须跳过 `point_scope`**：它是 MySQL 专用查询，在 `KB_STORE=json`
    下调用会直接连不上库而 500——接口层的 `_guard_point` / `_guard_doc` 已按
    `settings.store_label` 提前返回，让服务层给出可读的「需要 MySQL」提示。

- **任务 3.8 多智能体切换** ✅（P-QA-06，2026-10-20）
  - **数据**：`agent.guide_questions` JSON 列（≤6 条、单条 ≤120 字；`NULL` 与 `[]`
    等价），六个场景预设内置各自的场景问题；`tools/migrate_agent_guide_3_8.py`
    幂等补列，管理端助手弹窗新增「场景引导问题」多行编辑（逐行一条）。
  - **公开接口**：`GET /api/agents?role=` 只返回上架且身份可见的助手，默认助手
    恒在首位并标记 `isDefault`，携带 `guideQuestions`；提示词、检索参数、模型等
    配置字段不下发。渠道公开卡片同步携带引导问题，并按身份过滤绑定助手。
  - **门户切换**：首屏渲染「切换助手」矩阵；顶栏 `#agent-select` 常驻，会话中途
    可切；切换即丢弃当前会话（`sessionId` 与本机记录同步清理），提问携带新
    `agentId`。引导问题优先级：显式切换的助手场景问题 → 3.4 角色路由问题 →
    默认助手场景问题 → 渠道 FAQ → 内置示例。应用广场智能体卡片复用同一逻辑；
    身份变化后不可见助手自动回退默认，矩阵/切换器不足 2 个助手时隐藏。
  - **验收**：`tests/test_agent_switch.py`（16 项）、`tests/test_frontend.py`
    （84 项）、`tools/verify_switcher.py` JSON/MySQL 各 **29 PASS**、
    `tools/verify_switcher_ui.py` 真 Chromium JSON/MySQL 各 **41 PASS**
    （矩阵/顶栏切换/会话重置/应用广场/角色收敛/根门户/移动端/kiosk，零 JS 错误）；
    全量 `pytest` **1031 passed / 15 skipped**（共 1046 条）。

- **任务 3.9 嵌入 SDK 与跨域接入** ✅（P-B-18 / I-07，2026-10-21）
  - **独立 SDK** `static/sdk/nxz.js`（v1.0.0，`/sdk/nxz.js` 直接分发）：一行
    `<script data-channel="…" data-color="…" data-label="…">` 自动生成浮窗；
    也可 `NengXiaoZhi.init({channel, token})` 编程式接入，实例支持
    `open/close/toggle/setToken/setUser/on/destroy`，多实例按
    `channel:instanceId` 隔离。基址在脚本加载期缓存，`load`/点击回调中初始化
    不会丢掉应用域名（编程式接入的关键回归点）。
  - **身份桥**：`postMessage` 握手 `nxz:sdk:ready` → `nxz:sdk:init` →
    `nxz:sdk:state` / `nxz:sdk:close`；父页只认自己 iframe 的消息，iframe 只认
    `window.parent`。令牌经 SDK 内存下发，**不写宿主页 localStorage、不进 URL**；
    `setToken('')` 即时回到访客，重新下发即恢复登录。门户 `?sdk=1` 模式在首屏
    等待 init（800ms 兜底）后再加载身份/渠道/助手/应用，避免访客态闪烁。
  - **CORS**：`app/core/cors.py` 统一生成参数，`CORS_ALLOW_ORIGINS`（逗号分隔，
    默认 `*`）、`CORS_ALLOW_CREDENTIALS`、`CORS_EXPOSE_HEADERS` 走环境变量；
    通配来源时强制关闭凭证（浏览器禁止 `*` + credentials），身份用
    `Authorization` 头传递，无需 Cookie。暴露 `Content-Disposition`、
    `X-Export-Truncated`、`X-AI-*` 等响应头供第三方页面读取。
  - **渠道接入代码**：`build_assets()` 新增 `sdkUrl` / `sdkSnippet`，管理端渠道页
    新增「嵌入 SDK（推荐）」页签，自动带上渠道编码、位置、主题色与按钮文案；
    iframe / 自包含浮窗两种旧方式保留。
  - **示例页** `/sdk-demo`：模拟第三方校园站点动态加载 SDK，提供渠道/令牌输入、
    自动浮窗与编程实例、事件日志，便于联调。
  - **验收**：`tests/test_embed_sdk.py`（15 项：MIME/缓存头、SDK 契约、
    `build_assets` 转义、CORS 预检/暴露头/通配降级、门户桥契约）；
    `tests/test_frontend.py` 锁定新增静态文件；`tools/verify_embed_sdk.py` 用
    两个端口模拟真实跨域，JSON 存储 **25 PASS**（自动 + 编程浮窗问答、AI 标识、
    setToken 双向、第三方 Origin、Authorization 跨域、预检、零 JS 错误）；
    `tools/verify_channels.py` **84 PASS**、`tools/verify_channels_ui.py` 真 Chromium
    **60 PASS**（含 SDK 页签代码断言）；全量 `pytest` **1080 passed / 15 skipped**
    （共 1095 条）。本任务不改存储层，MySQL 分支未单独复跑。

- **任务 3.10 统计与运营分析** ✅（P-QA-06，2026-10-21）
  - **口径与 2.1 同源**：`app/services/stats_admin.py`（新增）复用
    `chatlog_admin._collect()`，同一时间/渠道/助手/角色筛选下，统计「提问/回复」与
    会话日志命中数一致；两种后端共用 `SCAN_LIMIT=2000` 扫描窗口，响应 `window`
    暴露 `scanned/scanLimit/truncated/store`，**不伪造全量数据**。
    提问=配对问答数，回复=AI 消息数，未答=`refused=1`，成功率=(回复−未答)/回复，
    访问=会话数，人次=去重用户（JSON 无身份时按会话计并显式标注）。
  - **新增采集列**：`chat_session.device`（pc/mobile/tablet/other，空串=未采集）由
    `/api/chat` 按 UA 解析后首次写入（`set_session_device` 先到先得）；
    `chat_message.refused` 标记拒答。迁移 `tools/migrate_session_device_3_10.py`、
    `tools/migrate_message_refused_3_10.py` 均幂等，已对开发库执行。
  - **接口**：`GET /api/admin/stats/filters`（与日志同源下拉）、`/overview`
    （6 张指标卡 + ≤90 天趋势 + 渠道/助手统计）、`/rankings`（热门知识、知识分类、
    设备、情绪、应用；`limit` 1–50 默认 10）、`/export`（9 种 CSV，UTF-8 BOM）。
    无数据维度返回 `[]` + `notes` 说明，前端显示空态而非 0。
  - **管理页**：新增「数据统计」页签（`static/stats.js` 懒加载）——时间范围默认近 7 天，
    渠道/助手/角色筛选，6 张指标卡、按天趋势、渠道与助手排行、热门知识与分类
    表格、设备占比；TOP 维度可切柱状/饼图；「导出 CSV」按当前筛选与视图落盘，
    导出动作记 `STATS_EXPORT` 操作日志。
  - **大屏**：一键全屏（`requestFullscreen` + `fullscreenchange` 同步按钮态），
    深色大屏布局在 1920×1080 与移动端均可读。
  - **权限**：RBAC 扩为 **12 个模块 / 27 个权限点**，新增 `stats.view`
    （概览/排行）与 `stats.export`（导出）；`required_for` 已覆盖 `/api/admin/stats*`。
  - **交付边界**：咨询情绪依赖 4.6 打标、应用使用排行依赖 3.3 访问埋点，当前均返回
    接口 + 空态说明；设备维度自本任务起才有数据，历史会话显示「未采集」。
  - **验收**：`tests/test_stats_admin.py`（23 项）、`tests/test_frontend.py`（97 项）、
    `tools/verify_stats.py` JSON **88 PASS** / MySQL **83 PASS**、
    `tools/verify_stats_ui.py` 真 Chromium JSON/MySQL 各 **27 PASS**
    （含空数据区间、全屏、真实 CSV 下载、零 JS/HTTP 错误）；
    全量 `pytest` **1063 passed / 15 skipped**（共 1078 条）。

- **任务 3.11 未识别问题闭环** ✅（M-S-07～09，2026-10-21）
  - **三类入池触发**（`app/core/pipeline.py`）：检索为空 `weak_recall`、模型拒答
    `refused`、置信度低于 `UNRECOGNIZED_CONFIDENCE_THRESHOLD`（默认 0.35）
    的 `low_confidence`。敏感词命中是**主动拒答**，直接返回、不入池也不检索。
    每条记录带 `msg_id` / `confidence` / `role_snapshot` / `channel_code` /
    `agent_id` / `dept_name`，可回溯到原会话与原始消息。
  - **聚合与归类**：按归一化 `questionKey` 聚合（去标点/空白/全半角），累计频次、
    最近出现时间与多个来源；语义聚类用向量余弦（阈值
    `UNRECOGNIZED_CLUSTER_THRESHOLD=0.82`，最多 200 组，按签名缓存），
    业务域用关键词规则分类。聚类失败/无向量时退化为「单问题一簇」，不阻断列表。
  - **数据**：MySQL `unrecognized_question` 由
    `tools/migrate_unrecognized_3_11.py` 幂等补 **9 列 + 3 索引**
    （`msg_id` / `question_key` / `confidence` / `source` / `role_snapshot` /
    `channel_code` / `agent_id` / `dept_name` / `new_doc_id`），已对开发库执行；
    JSON 后端落 `data/unrecognized.json`。
  - **接口 6 个**：`GET /api/admin/unrecognized`（筛选/排序/分页/聚合）、
    `/filters`、`/{id}`（详情 + 会话追溯）、`POST /batch`（忽略/重新打开/归类，
    归类必须带 `dept`）、`POST /feedback`（反哺，仅 MySQL）、`GET /export`
    （CSV，UTF-8 BOM）。写操作统一记操作日志。
  - **管理页** `/unrecognized`（`static/unrecognized.{html,js,css}`）：
    状态/来源/业务域/角色/渠道/时间/关键词筛选，语义簇快捷筛选，
    多选批量处理，详情弹窗展示用户提问、AI 回复（拒答高亮）、引用与相似问法，
    支持单条忽略/重开/反哺与 CSV 导出。反哺生成 **草稿知识**（`status=0`），
    需走 2.7 审核后才会生效；JSON 模式明确提示「需 MySQL」并禁用提交。
  - **权限**：RBAC 扩为 **13 个模块 / 29 个权限点**，新增 `unrecognized.view`
    （列表/详情/导出）与 `unrecognized.manage`（批量处理/反哺）。
  - **验收**：`tests/test_unrecognized.py`（21 项：三类触发、聚合、聚类、
    筛选、批量、CSV、MySQL 反哺桩）；`tests/test_frontend.py`（105 通过 /
    6 跳过）；`tools/verify_unrecognized.py` **44 PASS**（真实 `/api/chat`
    自动入池、聚合、筛选、批量、CSV BOM、JSON 反哺 400）；
    `tools/verify_unrecognized_ui.py` 真 Chromium **18 PASS**（列表、簇标记、
    详情会话、JSON 反哺禁用、批量状态、CSV 下载、零 JS 错误）；
    全量 `pytest` **1116 passed / 15 skipped**（共 1131 条）。

### 批4 · 外部集成（进行中，6/9）

- **任务 4.1 统一身份认证（CAS）** ✅ 关联 I-01 / P-B-03。
  - **流程**：`GET /api/auth/cas/login` → CAS 登录页 → `/api/auth/cas/callback?ticket=` →
    `serviceValidate` 校验 → 302 回站内并携带**一次性交换码**（2 分钟、`purpose=cas`）→
    前端 `POST /api/auth/cas/exchange` 换正式 JWT。正式 token 不进 URL/Referer。
  - **适配与 mock**：`app/services/sso.py` 同时支持标准 CAS 3.0 `serviceValidate`
    （含默认/带命名空间两种 XML）与内置 mock CAS；`CAS_SERVER_URL` 为空时自动启用 mock，
    演示身份 `20230001` / `20240001` / `T10001` / `admin`，真实身份由上游决定。
  - **身份关联**：`auth_service.resolve_sso()` 按学工号匹配本地 `sys_user`，带出角色/部门；
    `CAS_TRUST_ROLE_ATTRIBUTE=false`（默认）时上游角色属性不提权，仅做提示映射。
  - **安全**：回调仅接受站内相对路径（拒绝 `//evil`、绝对 URL、反斜杠/换行）；
    ticket 一次性且绑定 service；交换码带 `jti` 防重放；mock 登录只回跳本站或
    `CAS_SERVICE_BASE` / `PUBLIC_BASE_URL` 显式配置的域名。
  - **前端**：管理后台与门户登录框均提供「统一身份认证登录」，回调后清理 URL 片段；
    访客模式保持可用，未登录仍可浏览门户，仅受 RBAC 页面门限制。
  - **验收**：`tests/test_sso.py`（25 项）；`tools/verify_sso.py` HTTP **23 PASS**
    （完整登录链路、交换码单次使用、非法 ticket、开放重定向、外部 service、角色映射、
    管理接口 401/403）；`tools/verify_sso_ui.py` 真 Chromium **17 PASS**
    （token 落库、URL 无交换码、身份徽标、403 提示、退出与访客模式、零 JS 错误）。

- **任务 4.3 问数服务（课表 / 成绩 / 一卡通）** ✅ 关联 P-DQ-01 / P-DQ-02 / P-DQ-03。
  - **识别与身份**：`app/services/data_query.py` 识别课表/成绩/一卡通意图，抽取
    周次/星期/学期/天数/条数；学工号只从登录 token 取（演示模式固定 `20230001`），
    请求体不能指定他人学号。教务仅本科生/研究生，一卡通另含教职工/离退休；
    未登录 401、越权 403、未映射学号 403。
  - **数据源**：`DQ_PROVIDER=mock|http`。HTTP 契约为
    `GET {DQ_JW_BASE_URL}/students/{userNo}/schedule?week=`、
    `.../grades?semester=`、`GET {DQ_CARD_BASE_URL}/accounts/{userNo}/card?days=&limit=`；
    Bearer 令牌只进请求头，错误不回显上游响应体。mock 提供确定性课表/成绩/流水，
    外部接口就绪后只改环境变量。
  - **结构化卡片**：统一输出 `intent/type/title/subtitle/identity/summary/columns/rows/
    empty/source/generatedAt/footnote`；课表按星期与时间排序，成绩计算加权平均与 GPA，
    一卡通给出余额及消费/充值汇总。`GET /api/data/capabilities` +
    `POST /api/data/query`，明细行数受 `DQ_MAX_ROWS` 限制。
  - **对话与前端**：非流式 `data.dataCards` 与 SSE `done.dataCards` 同步下发；门户渲染
    指标卡 + 表格 + 来源脚注，拒绝态展示原因与登录提示。卡片只用 `textContent` 写入；
    问数失败降级为空/拒绝卡片，不影响问答主链路。
  - **验收**：`tests/test_data_query.py`（22 项）；`tools/verify_data_query.py` HTTP
    **56 PASS**（401/403、身份取自 token、周次过滤、成绩/一卡通契约、参数校验、
    对话/SSE、拒绝卡片）；`tools/verify_data_query_ui.py` 真 Chromium **31 PASS**
    （课表/过滤/成绩/一卡通/教职工余额/访客拒绝卡片、零 JS 错误）。

- **任务 4.4 通行查询智能体** ✅ 关联 P-DQ-04、I-04。
  - **本地分析库**：新增 `access_event` 表与 JSON 落盘 `data/access_events.json`，
    `app/db/access_store.py` 双存储同接口。同步采用**整窗替换**（先完整拉取、成功后再
    替换），避免上游半途失败留下数据缺口；MySQL 分块写入 1000 条/批。
  - **同步**：`POST /api/admin/access/sync` 支持 `ACCESS_SYNC_PROVIDER=mock|http`。
    mock 生成确定性流水（事件号 `ACYYYYMMDD……`，含早晚通勤、午间、深夜与尾随告警）；
    HTTP 侧调用 `ACCESS_SYNC_URL?from=&to=`，可选 Bearer，兼容数组 / `items` /
    `data.items` 等返回形态，按事件号去重并做字段归一化。**空结果或上游错误一律拒绝
    覆盖本地库**，避免一次故障把历史数据清空。
  - **多维分析**：`GET /api/admin/access/overview` 输出总量/进入/离开/去重人数/异常率/
    深夜出入/日均/活跃天数/高峰时段，校区、学院、门禁点、逐日趋势（补齐零值）、
    24 小时分布、个人 TOP10、学院 TOP5 与异常明细；全部在本地聚合，前端只读结果。
  - **层级下钻**：`GET /api/admin/access/drilldown?dimension=campus|college|gate`
    分别要求 `campusId` / `college` / `gateId`，返回该范围的学院、门禁点与人员构成。
  - **智能报告与导出**：`GET /api/admin/access/report` 生成 Markdown 分析报告
    （总体/校区/学院/个人/异常/趋势/建议，明确声明只使用本地聚合指标、不向外部模型
    发送个人明细）；`download=true` 以附件下载。`GET /api/admin/access/export`
    导出 CSV（UTF-8 BOM、RFC 5987 文件名、超过 `ACCESS_EXPORT_LIMIT` 时带
    `X-Export-Truncated` 头，不静默截断）。
  - **隐私与权限**：接口、报告与 CSV 的学工号统一脱敏；路由挂
    `require_admin` + `require_permission`，RBAC 新增
    `access.view`（看板/下钻/明细）、`access.manage`（同步）、
    `access.export`（报告/导出），越权 403 并落操作日志。
  - **配置**：`ACCESS_SYNC_PROVIDER` / `ACCESS_SYNC_URL` / `ACCESS_SYNC_TOKEN` /
    `ACCESS_SYNC_TIMEOUT` / `ACCESS_SYNC_DAYS` / `ACCESS_MAX_RANGE_DAYS` /
    `ACCESS_MAX_EVENTS` / `ACCESS_EXPORT_LIMIT`，已同步 `.env.example`。
  - **数据**：`tools/migrate_access_4_4.py` 幂等建表，`--seed` 可直接灌入 mock 流水
    （JSON 模式无需迁移）。
  - **验收**：`tests/test_access.py`（12 项：mock 确定性、整窗替换、聚合守恒、
    筛选/下钻、分页脱敏、报告/CSV、HTTP 契约与失败保护、鉴权/RBAC、页面契约）；
    `tools/verify_access.py` HTTP **72 PASS**；`tools/verify_access_ui.py`
    真 Chromium **55 PASS**（空库→同步→看板→筛选→分页→下钻→报告/CSV 下载，
    零 JS 错误）。

- **任务 4.7 ASR 语音输入** ✅ 关联 C-04 / P-QA-01。
  - **适配器**：`app/services/asr.py` 支持 `ASR_PROVIDER=mock|openai`。mock 不访问网络，
    固定返回 `ASR_MOCK_TEXT`（默认「校园网怎么开通？」）；openai 按兼容
    `POST {ASR_BASE_URL}/audio/transcriptions` 的 multipart 契约调用（`file` /
    `model` / `language`），接口就绪后改环境变量即可，前端零改动。
  - **接口**：`GET /api/asr/status` 下发可用性、录音/大小/转写上限与支持格式；
    `POST /api/asr/transcribe` 接收 multipart `file`，返回 `text/provider/truncated/elapsedMs`。
    音频白名单（webm/ogg/wav/mp3/m4a/aac/flac）、单文件 10MB、录音 60 秒上限；
    转写文本按问答接口的限制截断到 500 字并标记 `truncated`。接口挂独立限流作用域。
  - **前端**：对话输入框新增麦克风按钮（能力不可用时隐藏）。`MediaRecorder` 录音，
    录音中显示计时与脉冲态，再次点击或到时自动结束；上传转写后文本填入输入框并聚焦，
    **不自动发送**，用户可编辑再走原有 `/api/chat/stream`。权限被拒、格式不支持、
    服务异常均有明确提示；录音结束即释放麦克风轨道。
  - **隐私**：不记录音频内容与转写文本；上游错误只回显通用话术，不转发响应体；
    `ASR_API_KEY` 只进请求头，状态接口与日志均不返回密钥。
  - **验收**：`tests/test_asr.py`（19 项：状态、mock、上传校验、multipart 契约、
    超时/上游错误、同一问答链路）；`tools/verify_asr.py` HTTP **33 PASS**
    （三套服务实例覆盖正常/边界/关闭态）；`tools/verify_asr_ui.py` 真 Chromium
    **20 PASS**（fake 麦克风录音→转写→发送问答、能力关闭隐藏入口、权限被拒提示、
    零 JS 错误）。

- **任务 4.5 导办事项与免登录票据** ✅ 关联 P-DB-01 / P-DB-02、M-G-01、I-03。
  - **事项数据**：`app/services/gov_admin.py` + `app/db/gov_store.py`（MySQL `gov_item` /
    JSON `data/gov_items.json`）。9 个主管部门字典、事项条件/材料/标签/可见角色、
    启停与生效时段；同一部门下重名拒绝，办理链接只允许 http(s) 与站内相对地址。
  - **同步**：`POST /api/admin/gov/sync` 支持 `GOV_SYNC_PROVIDER=mock|http`；
    HTTP 侧兼容数组 / `items` / `data.items` 包装，Bearer 令牌可选。同步按
    `itemId` → 「名称 + 部门」匹配 upsert，只更新上游字段，**不覆盖本地启停与生效时段**。
  - **对话推荐**：检索按「事项名 80 / 标签 30 / 部门 15」打分，并以办事意图词兜底常用事项；
    无把握时不插卡。非流式 `data.guideItems` 与 SSE `done.guideItems` 同步下发，
    推荐异常降级为空、不影响问答。
  - **免登录票据**：登录后 `POST /api/gov/items/{id}/ticket` 签发 `GT` + 24 字节随机串，
    默认 120 秒（钳制 30–300）；身份只存服务端，URL 仅携带不透明票据。
    `/hall` 兑换时原子消费（MySQL `WHERE used_at IS NULL` / JSON 加锁），
    重放 409、过期 410、越权 403；页面读取后立即 `history.replaceState` 移除票据并
    禁止 Referer 泄露。`needTicket=false` 的事项直接跳转，不签票。
  - **后台**：独立 `/gov` 页（同步、KPI、状态/生效筛选、搜索、新建/编辑、启停、
    删除），RBAC 新增 `gov.view` / `gov.manage`；管理写操作全部落操作日志。
  - **数据**：`tools/migrate_gov_4_5.py` 幂等建表并导入 9 条 mock 事项（已对开发库执行）；
    JSON 模式落 `data/gov_items.json` / `data/gov_tickets.json`。
  - **验收**：`tests/test_gov.py`（14 项）；`tools/verify_gov.py` HTTP **82 PASS**
    （鉴权、同步幂等与本地状态保留、CRUD/校验、角色与生效窗口、检索、票据单次消费、
    对话与 SSE 卡片）；`tools/verify_gov_ui.py` 真 Chromium **37 PASS**
    （卡片、弹窗取票、大厅身份、地址栏去票、重放/缺票、模拟表单、后台增改启停删、
    零 JS 错误）。

- **任务 4.6 客服体系** ✅ 关联 P-B-09/10/11/12/13、M-S-02/03/04/05/06。
  - **工单**：公开端 `GET /api/support/status` + `POST /api/support/tickets`
    （建单 / 查询 / 发消息 / 结束）。建单返回 `ticketNo` 与高熵 `accessKey`，
    服务端只存 SHA-256；缺凭证 422、错凭证/跨单访问 403，工单号不可枚举。
    同一会话复用未结束工单，结束后可重新建单（凭证轮换）。
  - **路由与坐席**：`support_group` 按渠道编码匹配分组，组内按在线坐席当前负载
    取最小值分配；无在线坐席进离线队列并落系统留言，坐席上线后返回待接入提醒。
    管理端 `/api/admin/support` 覆盖工单筛选/详情、分组与坐席 CRUD、接入/转交/
    回复/结束、留言列表与概览统计；结束原因与人工回复全部落库。
  - **实时与状态**：`/api/support/ws`（WebSocket）按工单房间广播新消息、接入与结束；
    门户挂件 4 秒轮询兜底。对话命中「找人工/客服/人工」时非流式与 SSE `done`
    下发 `handoffSuggested`/`handoffKeywords`，挂件显示红点引导。
  - **情绪与审计**：规则化负面词打标（`sentiment` 0/1）覆盖用户消息与工单；
    接入/回复/结束/转交写 `SUPPORT_CLAIM/REPLY/CLOSE/ASSIGN` 操作日志。
    RBAC 复用 2.9 预置的 `support.view` / `support.manage`。
  - **前端**：后台独立 `/support` 工作台（避免改动既有页签结构），门户 `support-widget.js`
    提供入口、面板、续接（localStorage）、结束与凭证失效清理；全部渲染走 `textContent`。
  - **部署注意**：WebSocket 需 Nginx 透传 `Upgrade`/`Connection`（5.1 模板已含）；
    事件总线为单进程内存实现，多 worker 部署需换 Redis 广播。
  - **数据**：`tools/migrate_support_4_6.py` 幂等建 `support_group`、扩列
    `agent_seat`/`ticket`/`ticket_message` 并写入演示分组与坐席（已对开发库执行）；
    纯 WebSocket 场景不受影响，JSON 模式落 `data/support_tickets.json`。
  - **验收**：`tests/test_support.py`（17 项）；`tools/verify_support.py` JSON
    **64 PASS** / MySQL **71 PASS**（路由、凭证、生命周期、离线转在线、情绪、
    管理端、WebSocket、审计、对话转人工）；`tools/verify_support_ui.py`
    真 Chromium **17 PASS**（零 JS 错误）。

- **任务 4.2 数据中台对接** ✅ 关联 I-02、M-SYS-02。
  - **上报标准**：`nxz.data-center.report.v1`，按自然日生成 `DC-CHATLOG-YYYYMMDD` /
    `DC-STATS-YYYYMMDD` 两个批次；同一批次内容未变再次执行只记 `skipped`，内容变化
    自动升 `revision`，`force=true` 强制重投；批次保存 checksum、payload 与投递状态，
    mock 投递写 `data/data_center_outbox/`，http 模式 POST 到数据中台。
  - **隐私**：默认只上报元数据与聚合统计，不含问答正文；开启
    `DATA_CENTER_INCLUDE_CONTENT=1` 后正文先脱敏（学号/手机号/证件号/邮箱）再截断
    500 字；统计含会话数、问答数、拒答数、成功率、平均时延、Token 与角色/渠道/助手分布。
  - **组织同步**：`mock` 为确定性示例组织/用户，`http` 从
    `{BASE_URL}{ORG_PATH}?mode=&since=` 拉取；按 `orgCode`/`userNo` upsert，重复执行
    不重复建档；全量同步只统计缺项，不删除或停用本地账号（保护管理员与历史账号）。
  - **定时**：每日上报、每日增量、每周全量按 `lastReportDate` / `lastIncrementalDate` /
    `lastFullDate` 水位去重；进程内调度默认关闭，生产由外部调度器调用
    `POST /api/admin/data-center/run-due`。
  - **接口与页面**：`/api/admin/data-center` 提供状态、任务、批次详情、预览、上报、
    同步、补跑与组织/用户镜像；RBAC 新增 `datacenter.view` / `datacenter.manage`，
    写操作落 `DATA_CENTER_REPORT` / `DATA_CENTER_SYNC` 审计；独立 `/data-center`
    管理页（KPI、预览抽屉、批次/任务/组织/用户页签）。
  - **数据**：`tools/migrate_data_center_4_2.py` 幂等建 `data_center_job` /
    `data_center_report` / `data_center_state`（已对开发库执行）；JSON 模式落
    `data/data_center.json`。
  - **验收**：`tests/test_data_center.py`（14 项）；`tools/verify_data_center.py`
    JSON **55 PASS** / MySQL **53 PASS**（幂等同步、批次跳过/升版、隐私、定时水位、
    HTTP 数据源、权限与审计）；`tools/verify_data_center_ui.py` 真 Chromium
    **28 PASS**（零 JS 错误）。

- **任务 4.8 本地模型 / MCP / 第三方智能体** ✅ 关联 C-05、M-A-07/10。
  - **边界**：模型唯一来源仍是网关（`LLM_PROVIDER=gateway` 默认）。集成中心的本地模型是
    **离线/校内私有推理的直连模式**，只有显式切到 `local` 才接管问答，不另建第二份模型台账；
    当前启用的本地模型可在页面一键切换，下一次请求立即生效（`llm_router` 每次取活跃路由，
    不缓存旧客户端）。未绑定模型编码或编码已删除时回退到当前启用模型，避免脏绑定打断问答。
  - **本地模型**：OpenAI 兼容端点（地址 + 模型名 + 可选 Key + 超时）；Key 经 AES-256-GCM
    落库，接口只回 `hasKey`，不回明文或前缀；支持增删改查、单活激活、`POST .../test`
    真实探活（先列 `/v1/models`，再发一次 `/v1/chat/completions`）；内网无鉴权端点允许空 Key。
  - **MCP**：HTTP / SSE / stdio 三种传输配置；HTTP 测试按 JSON-RPC `initialize` →
    `notifications/initialized` → `tools/list` 顺序握手，兼容 `application/json` 与 SSE 响应，
    并回传 `Mcp-Session-Id`；自定义 Header 加密存储，接口只回 `headerNames`；校验 stdio
    命令（拒绝 shell 元字符）、Header 名与 CRLF 注入。SSE/stdio 仅做配置管理，不代执行子进程。
  - **第三方智能体**：Dify（`POST {base}/chat-messages`，blocking）、Coze
    （`POST {base}/open_api/v2/chat`，带 `bot_id` 并取 `type=answer`）、通用 Webhook
    （POST `{base}`，读 `answer` / `reply` / `content`）三类适配器；Coze 强制应用 ID；
    Token 加密存储，接口只回 `hasToken`。
  - **mock / live 双来源**：`INTEGRATION_TEST_PROVIDER` / `INTEGRATION_MCP_PROVIDER` /
    `INTEGRATION_AGENT_PROVIDER` 默认 `mock`（确定性响应，离线可联调），置 `live` 走真实
    外部服务；测试结果（含失败）写审计，错误摘要不含密钥。
  - **数据与审计**：`tools/migrate_integrations_4_8.py` 幂等建 `integration_local_model` /
    `integration_mcp_server` / `integration_agent_app` 三表（已对开发库执行）；JSON 模式落
    `data/integrations.json`。RBAC 新增 `integrations.view` / `integrations.manage`
    （当前 **19 模块 / 42 权限点**）；保存/删除/激活/测试/写入示例分别落
    `INTEGRATION_SAVE/DELETE/ACTIVATE/TEST/SEED`。示例配置统一标记
    `示例数据（待替换为学校真实配置）`，`POST /api/admin/integrations/seed-examples`
    按编码幂等写入（生产模板不自动 seed）。
  - **前端**：独立 `/integrations` 页（KPI + 三个页签 + 抽屉表单 + 测试结果抽屉），
    后台顶栏入口按 `integrations.view` 显隐。
  - **配置**：`.env.example` 与 `deploy/env.production.example` 补记 `LLM_PROVIDER` 及
    `INTEGRATION_TEST_PROVIDER` / `INTEGRATION_TEST_TIMEOUT` / `INTEGRATION_MCP_PROVIDER` /
    `INTEGRATION_AGENT_PROVIDER`。
  - **验收**：`tests/test_integrations.py` 25 项；`tools/verify_integrations.py`
    JSON **99 PASS** / MySQL **99 PASS**（三表结构、密钥三态、加密落库、单活激活、
    mock/live 适配器、本地路由切换、审计与 RBAC）；`tools/verify_integrations_ui.py`
    真 Chromium **45 PASS**（空态 → 示例配置 → 新建/测试/切换/编码冲突 → 删除，零 JS 错误）。

- **任务 4.9 分享问答与用户反馈** ✅ 关联 P-QA-07 / P-QA-08。
  - **分享快照**：`POST /api/share` 冻结当时的 AI 回答（问题取最近一条用户消息），
    同一 `(sessionId, msgId)` 复用同一分享 ID；ID 形如 `Q…`（时间戳 base36 + 随机段），
    不可枚举。公开页 `/share/<id>` 服务端渲染已验证的回答 HTML（`app.core.richtext`），
    引用只暴露标题/部门/类型/相似度，不含 `chunkId`；携带 AI 生成标识；浏览数自增。
  - **点赞/点踩**：`POST /api/feedback` 按 `(sessionId, msgId)` 幂等 upsert；
    点赞 `rating=1` 会清空原因，点踩 `rating=-1` 必须带合法原因
    （不准确/不完整/不相关/其他），可附补充说明；重复评价以最后一次为准并重置处理状态。
    分享页与对话页均支持，前端用 `localStorage` 记住本人选择并即时更新计数。
  - **分享卡片**：复制纯文本、保存 Markdown（含引用与 AI 声明）、系统分享
    （`navigator.share`，不可用时复制链接）。
  - **后台**：`/feedback` 页支持评分/处理状态/关键词/日期筛选、KPI（总数/赞/踩/待处理/
    好评率）、详情与处理备注、重新打开、CSV 导出（BOM，上限 5000 行并提示截断）。
    RBAC 当前为 **19 模块 / 42 权限点**（4.8 新增 `integrations.view` /
    `integrations.manage`；4.9 新增 `feedback.view` / `feedback.manage`；
    4.5 再增 `gov.view` / `gov.manage`；4.4 再增 `access.view` / `access.manage` /
    `access.export`；4.6 接入 `support.view` / `support.manage`；4.2 新增
    `datacenter.view` / `datacenter.manage`），导出与处理写操作日志。
  - **数据**：MySQL 由 `tools/migrate_share_feedback_4_9.py` 幂等建 `qa_share` /
    `qa_feedback` 两表（已对开发库执行）；JSON 模式落 `data/shares.json` /
    `data/feedback.json`，分享、反馈与后台管理全链路同样可用（两套存储均已实跑验收）。
  - **验收**：`tests/test_share_feedback.py`（15 项）；`tools/verify_share_feedback.py`
    HTTP **61 PASS**（分享复用、公开数据脱敏、AI 头、浏览量、赞/踩 upsert、鉴权 401/403、
    筛选、CSV BOM 与文件名、处理/重开、404）；`tools/verify_share_feedback_ui.py`
    真 Chromium **42 PASS**（对话页操作条、点踩原因校验、分享弹窗、公开卡片与引用、
    计数翻转、Markdown 下载、后台 KPI/详情/处理/筛选/导出、零 JS 错误）。

### 批5 · 交付化（进行中）

- **任务 5.1 生产部署与 HTTPS** ✅
  - **常驻守护**：`deploy/service_supervisor.py` 启动 uvicorn，退出后 5 秒自动拉起；
    健康检查连续失败 3 次判定假死并重启；`run/stop.flag` 保证停止后不被守护重新拉起。
  - **Nginx TLS**：`deploy/nginx/nengxiaozhi.conf.template` + 渲染脚本，TLS 1.2/1.3、
    HTTP→HTTPS、HSTS/nosniff、64MB 上传、`proxy_buffering off` + `X-Accel-Buffering: no`
    保证 SSE 不攒批；`$http_host` 保留客户端端口。
  - **证书**：`deploy/scripts/gen_selfsigned_cert.py` 生成 SAN 正确的自签证书（测试/内网）；
    正式交付替换为学校 CA 证书同名文件即可。
  - **Windows**：`start/stop/status.ps1`（非管理员可用）+ `install-service.ps1`
    注册 `NengXiaoZhi-KBService` / `NengXiaoZhi-Nginx` 开机计划任务、失败重试、防火墙规则；
    **Linux**：systemd 单元（`Restart=always`、`After=docker.service`、基础沙箱）。
  - **MySQL**：compose 增加健康检查、`restart: unless-stopped`、端口只绑定 `127.0.0.1`；
    已实机重建容器验证 `healthy`，数据卷保留（7 文档 / 35 切片 / 7 助手 / 7 角色）。
  - **生产 env**：`deploy/env.production.example` 固定 `AUTH_ENABLED=true`、`LLM_MOCK=0`、
    MySQL 模式、模型下载关闭，密钥用占位符而不是空值。
  - **验收**：`tools/verify_deploy.py` **61 PASS / 0 FAIL / 0 SKIP**——静态配置、证书 SAN/配对、
    强杀 uvicorn 后自动拉起（PID 变化且健康恢复）、Docker Nginx + 自签证书真实 HTTPS
    （健康检查 200、HSTS、301 跳转、SSE 31 块、首块 92ms / 全程 288ms）。
  - ⚠ **实机边界**：本机未安装宿主 Nginx，HTTPS 段用 Docker Nginx 完成；正式证书签发、
    防火墙策略与服务器重启演练需在交付环境按 `deploy/README.md` 执行。

- **任务 5.2 离线交付包** ✅
  - **构建**：`tools/build_offline_bundle.py` 产出 `nengxiaozhi-offline-<版本>-<时间戳>/`，
    含应用源码、初始化 SQL、12 份示例知识、BGE 嵌入/精排模型（约 650 MB）、
    目标平台全部 wheel、Windows/Linux 脚本、`BUILD-INFO.json`、`MANIFEST.json`、`SHA256SUMS`。
    wheel 用 `pip download --only-binary=:all: --platform ...` 下载，**不含 PyTorch /
    sentence-transformers**；可选 `--with-docker-images` 导出 MySQL/Nginx 镜像 tar。
  - **安装布局**：`releases/<版本>-<时间戳>` + `current` 联接 + `shared/{.env,data,uploads,logs,
    run,models,knowledge-base}`；升级创建新 release、切换 `current`，业务数据不覆盖；
    回滚直接切回旧 release。Windows 用目录联接，Linux 用符号链接。
  - **离线依赖**：目标机只需同版本 64 位 Python；`pip install --no-index --find-links wheels`
    全程不联网，安装后执行 `import app.main` 与 `pip check` 自检。
  - **数据库**：`tools/offline_db_init.py` 空库导入 `schema.sql`（38 条语句）、
    顺序执行 **19 个幂等迁移**；仅全新安装执行 `seed_auth.py`（7 角色 / 4 演示账号，
    升级不重复写）。已用临时库实测：**39 张表、19 个迁移全 ok、admin 绑定正确**。
  - **验收**：`tools/verify_offline_bundle.py` 对正式包 **57 PASS / 0 FAIL / 0 WARN**——
    包完整性与 SHA-256、无敏感/运行产物、PS1 UTF-8 BOM、安装、`pip check`、
    BGE 嵌入 **512 维**实加载、健康检查、升级切换、回滚恢复、共享数据留存、安装历史。
  - **手册**：`deploy/offline/OFFLINE-INSTALL.md`（构建参数、Windows/Linux 安装、
    生产配置、初始账号、升级/回滚、故障排查）。
  - ⚠ **边界**：Linux 安装脚本已实现但本机无 Linux 环境，动态验收在 Windows 完成；
    正式交付前需在目标 Linux 机按手册复跑安装/升级/回滚。Docker 镜像导出为可选步骤。
  - ✅ **5.6 后重建（2026-10-21 16:45）**：`nengxiaozhi-offline-0.1.0-20261021-164520-5.6`
    （431 文件 / 775 MB）已纳入 5.6 限流、CORS 模板与同步元数据修复，
    `verify_offline_bundle.py` **57 PASS / 0 FAIL / 0 WARN**（`artifacts/verify-offline-5.6.log`）。

- **任务 5.3 性能与容量验证** ✅
  - **工具**：`tools/perf_load.py` 同时支持检索链微基准（嵌入/召回/精排/完整 retrieve + 进程
    CPU 秒）、切片规模模拟（克隆到 350/3500/10000）、闭环 HTTP 压测（流式/非流式、每档预热、
    TTFT 与总耗时分位、进程/主机 CPU、RSS），可用 `--affinity 8` 把服务绑定到 8 核，
    按 8C16G 目标机口径实测；压测数据按会话前缀清理，可重复执行。
  - **8C16G 实测（精排关闭，生产默认）**：16 并发 0% 错误，非流式 P95 **275ms**、
    流式首 token P95 **147ms** / 全程 P95 **417ms**；32 并发非流式 P95 415ms、
    处理能力 **≥96 RPS**，进程仅 2.4/8 核、RSS 259MB。
  - **精排代价**：topN=10/4 线程时单请求约 **2.68–2.94 CPU 秒**、单实例 **1.6–1.8 QPS**；
    8C 下 4 并发首 token P95 **1767–2066ms** 达标，8 并发 **3759–4520ms** 超 3s，
    RSS 峰值 1256MB。故生产模板 `RERANK_ENABLED=0`、`RERANK_TOPN=10`、
    `RERANK_THREADS=4`，精排仅作低并发质量模式。
  - **线程调优**：ORT 默认开满逻辑核并自旋，精排 10 候选墙钟不变但 CPU 7–10s；固定
    `EMBED_THREADS=2` + `RERANK_THREADS=4` 后降到 3.8 CPU 秒/请求，嵌入 P95 31ms → 5ms。
  - **规模**：10,000 切片时混合召回 P95 **240ms**（35 切片为 0.82ms），10k 内有余量；
    5 万以上需 Milvus（5.7）。
  - **报告**：`artifacts/perf-report.md`（汇总）+ 6 份微基准/HTTP 原始 JSON/Markdown。
  - ⚠ **边界**：LLM 为 mock，不含网关与模型生成；MySQL 与压测客户端不受 8 核绑定；
    交付前需在目标 8C16G 机复跑同一命令。

### 批1 · 引擎与服务层（已完成）

- **任务 1.1 工程骨架** ✅ 应用可启动、路由契约就位、配置加载正常。
- **任务 1.2 解析层** ✅ 已实现 8 种格式（md/txt/docx/pdf/xlsx/xls/csv/json），
  与 Node 原型在 9 个样例上**完全一致**（blocks/chunks/blockTypes/chunkChars/meta 零差异）。
  （任务 1.10 已把格式扩到 12 类，见下文；`image_sink=None` 时 docx/pptx 的块输出仍与原型逐块一致。）
- **任务 1.3 切片层** ✅ 标题栈、表格整块、FAQ 整对、400/60/40 滚动窗口；
  额外修复「400 字非硬切」，并去掉结尾 carry 重复碎片。
- **任务 1.4 向量化** ✅ `Xenova/bge-small-zh-v1.5`（ONNX，512 维，mean pooling + L2 归一化）；
  离线优先加载，与 Node 原型在 8 条文本上**逐值一致**（余弦 1.000000，最大绝对差 ~1e-7）。
- **任务 1.5 检索层** ✅ 中文 bigram BM25（K1=1.5/B=0.75）+ 向量余弦 + RRF（K=60）融合，
  支持 `hybrid`/`vector`/`bm25` 三种模式；与 Node 原型在真实 35 切片索引上**命中序列与分数一致**。
  并修复**角色缺省放行**漏洞：不传角色按「访客」过滤。
- **任务 1.6 精排层** ✅ `onnx-community/bge-reranker-v2-m3-ONNX`（q8 量化，交叉编码）；
  与原型在 11 个用例上 **top-1 一致 11/11**；候选数 ≤ topK 时跳过精排、加载失败自动降级。
- **任务 1.7 生成层** ✅ OpenAI 兼容客户端（`llm_client.py`）+ 提示词/引用/拒答编排（`generator.py`）；
  `LLM_MOCK=1` 走确定性模拟回答，与原型**逐字一致**（含越界/重复编号边界）；
  引用额外返回 `n`（正文原始编号），修复原型来源面板错位。
- **端到端接线** ✅ `pipeline.py` 编排层 + `index_store.py` 索引加载 + `db/chatlog.py` 会话存储；
  `/api/chat` 与 `/api/chat/stream` 已跑通真实链路（检索 → mock 生成 → 引用 → 落库 → 多轮）。
- **任务 1.15 前端接入** ✅ 对话页 + 管理后台 6 个文件已从原型 `kb-engine/src/web/public/`
  **字节级迁入** `static/`（有测试守卫，禁止无意改写）；`/admin` 路由已映射。
  接口契约已按原型对齐：SSE `delta.text` / `done.{answer,sessionId,msgId,latencyMs,citations,...}` /
  `error.error`，请求体接受 `sessionId`，会话历史 `data` 为消息数组。
  LLM 未配置时返回 **503**（原型行为），前端直接展示错误原因。
- **任务 1.14 数据层** ✅ 完成（`KB_STORE=mysql` 为默认）：
  - **连接与会话**（`db/session.py`）：惰性引擎 + `pool_pre_ping`/`pool_recycle`，
    `db_session()` 自动提交/回滚，`DatabaseUnavailable` 异常，`ping()` 探活。
  - **ORM 映射**（`db/models.py`）：13 个模型，列名与真实库 **100% 匹配**（已实证校验）。
  - **知识库仓储**（`db/kb_repo.py`）：`save_store` / `load_store` / `list_docs` /
    `update_doc_status` / `delete_doc` / `write_doc` / `count_knowledge` / `list_chunks` /
    `find_doc_by_hash` / `list_source_docs`，枚举与编号规则对齐原型。
  - **向量适配层**（`db/vector_repository.py`）：`save` / `load` / `delete_by_doc` / `stats`，
    首版落 `kb_chunk_vec_proto`（JSON 列），生产可整体替换为 Milvus 而调用方零改动。
  - **管理端**（`services/kb_admin.py` + `api/docs.py`）：上传入库、状态流转、级联删除，
    全链路走单文档事务；列表读 `kb_doc`（**含草稿/待审核/已下线**）。
  - **增量同步**（`services/kb_sync.py` + `api/sync.py`）：按 `file_hash` 增/改/删，
    与原型 `sync.js` 的目录遍历规则一致（跳过 `.`/`00-` 前缀与根目录散文件）。
  - **会话存储**（`db/chatlog.py`）：`chat_session` / `chat_message` / `answer_citation` /
    `token_usage` / `unrecognized_question` 全链路落库，按需 seed `agent` + `channel`。
  - **MySQL 索引接入**（`index_store._load_mysql()`）：数据库不可用/无数据时
    **降级为空快照并带 error**，不抛异常拖垮服务。
  - **工具**：`tools/ingest_mysql.py`（JSON → MySQL 全量重建）、
    `tools/check_db.py`（连通性验收）。
  - **一致性回归**：`tools/verify_store_roundtrip.py` 逐字段比对（往返零漂移，
    向量 max|Δ|≈1e-17）；`tools/verify_retrieval_consistency.py` 在 10 个问题上比对
    JSON/MySQL 两种存储的召回序列与得分（**44 项检查全通过，得分差 0.00e+00**）。
  - 冒烟实测（MySQL 模式）：`/api/health` `docs=7/chunks=35`、`/api/chat` 带引用、
    SSE 30 个 delta + done、`/api/sessions/{id}/messages` 多轮 4 条、`/api/sync` 增/改/删可用。
- **任务 1.8 多轮对话 ≥10 轮** ✅ 窗口由原型 `qa.js` 的 `getHistory(limit = 8)`（4 轮）
  提升到 **20 条消息（≈10 轮）**（`HISTORY_MAX_MESSAGES=20`）；
  上下文组装集中到 `app/session/context.py`：`ai → assistant` 归一、空内容丢弃、
  **截断后强制首条为 `user`**——修掉 OpenAI 兼容接口「`system` 后首条为 `assistant` 直接 400」的隐患；
  `rewrite_query()` 为后续指代消解预留（当前恒等）。
  验收：`tools/verify_multiturn.py` 在 MySQL 上连跑 12 轮 —— 窗口稳定 **20 条 / 10 轮**、
  首条恒为 `user`、跨会话 0 条；`tests/test_session_context.py`（14 项）+
  `test_load_context_mysql_beyond_ten_turns` 覆盖。
- **任务 1.13 认证与权限（RBAC）** ✅ 原型 `kb-engine` **完全没有鉴权**（`server.js` 无
  token/Authorization 逻辑），本项是**新增能力**，无 Node 侧可做一致性回归：
  - `app/security/auth.py`：**HS256 自实现**（`hmac` + `hashlib`，零第三方依赖）；
    标准声明 `sub`/`role`/`iat`/`exp`，签名用 `hmac.compare_digest` **常量时间**比较；
    `JWT_SECRET` 为空时退化为进程内临时密钥并告警（生产必须显式配置）。
  - `app/security/rbac.py`：角色集合与 `/api/meta` 的 `authRoles` **完全一致**（有测试守卫）；
    含 `sys_user.id_type → 角色` 映射（口径统一为「**离退休人员**」）。
  - `app/security/deps.py`：`get_current_user`（无 / 非法 token → **访客**，不抛错）+
    `require_admin`（`AUTH_ENABLED=true` 时：未登录 **401** / 非管理员 **403**）。
  - `app/api/auth.py`：`POST /api/auth/login`（凭学工号换 JWT；任务 2.9 起
    `sys_user.password_hash` 已设密码的账号必须带密码，NULL 的历史演示账号仍免密）、
    `GET /api/auth/me`（含实时权限点）。
  - `services/auth_service.py` + `tools/seed_auth.py`：7 个角色 + 4 个演示账号
    （`admin` / `20230001` / `20240001` / `T10001`），**幂等 upsert**。
  - **角色注入**：带有效 token 提问时以 **token 角色**为准，忽略请求体伪造的 `role`。
  - **默认 `AUTH_ENABLED=false`**（管理接口不拦截）——已迁移的前端管理页尚未带 token，
    便于开发与演示；**生产必须置 true**。
- **任务 1.12 安全基线** ✅ 同样为**新增能力**（原型 `kb-engine` 无任何安全实现）：
  - `app/security/crypto.py`：**AES-256-GCM** 字段加密（`sys_user.id_card_enc` / `phone_enc`）。
    存储格式 `b"v1" + nonce(12) + ciphertext+tag`，**版本前缀**便于将来轮换算法；
    密文被篡改/密钥不符统一抛 `CryptoError`（AEAD 认证标签检出）。
    密钥来自 `AES_KEY`（base64 32B 或任意口令 → SHA-256 派生）；留空退化为进程内临时密钥并告警。
  - `app/security/masking.py`：日志脱敏 `MaskingFilter`——证件号 / 手机号 / 学号 / 邮箱掩码，
    顺序为 **email → 学生号 → 手机 → 证件号**（先收窄更具体的模式，避免误伤）。
    `install_masking_filter()` 同时挂到 **root logger 与其每个 handler**
    （`logging` 的 filter 在子 logger 向父级传播时不会重复执行，只挂 root 会漏掉子 logger 直写的记录）。
  - `app/security/ratelimit.py`：**内存滑动窗口**限流（`collections.deque`，零依赖）。
    `rate_limit("chat")` / `rate_limit("admin")` 生成 FastAPI 依赖，超限 **429 + `Retry-After`**，
    话术「请求过于频繁，请稍后再试」；客户端标识取 `X-Forwarded-For` 首段（无则对端 IP）。
  - `app/security/sensitive.py`：**双向敏感词检测**（提问 + 回答）。
    内置意图词表覆盖「提示词注入/越狱」与「批量导出个人信息」两类；命中返回统一风控话术
    （`RISK_REPLY`）。`check_question` 命中时**直接拒答、不检索、不写未识别问题池**；
    `check_answer` 在生成后**就地改写** `answer/refused/citations/confidence`
    （前端 `finalize()` 用 `done.answer` 覆盖 DOM，故流式下同样生效）。
    可用 `SENSITIVE_WORDS_FILE` 追加外部词表（每行一词，`#` 注释）。
  - `app/security/oplog.py`：操作日志落 `operation_log`（上传 / 改状态 / 删除 / 同步），
    记录操作人、目标、IP 与 `detail`（**写库前经 `mask_detail` 脱敏**）；
    数据库不可用时**降级为日志输出**，不阻断业务。
  - **SQL 注入守卫**：`tests/test_security.py::test_no_sql_string_interpolation` 用 AST
    静态扫描 `app/` 下所有 `.py`，禁止 `text/execute/executemany/executescript` 首参为
    f-string / `%` / `+` 拼接 / `.format()`，除非该行显式标注 `# noqa: SQL-IDENTIFIER`。
    目前全库**仅 1 处**豁免——`kb_repo._truncate_kb_tables()` 的 `TRUNCATE TABLE {table}`，
    表名取自内部白名单常量（`TRUNCATE_ALLOWLIST`）并已断言校验，非用户输入。
  - **API 接入**：`/api/chat*` 挂 `rate_limit("chat")`；`/api/docs*`（含写操作）与 `/api/sync`
    挂 `require_admin` + `rate_limit("admin")` 并落操作日志；`main.py` 启动时 `install_masking_filter()`。
  - **开关**（见 `.env.example`）：`AUTH_ENABLED` / `RATE_LIMIT_ENABLED` /
    `SENSITIVE_CHECK_ENABLED` / `SENSITIVE_WORDS_FILE`；`tests/conftest.py` 默认关闭限流，
    避免用例之间互相触发 429。
- **任务 1.9 富文本回答渲染** ✅ 原型 `renderContent` 只支持 `**加粗**` + `[n]` + 换行，
  本项升级为 **Markdown 子集渲染器**，且前后端**同一套子集、逐字符一致**：
  - `app/core/richtext.py`（新增）：`parse_blocks()` 把回答解析为**结构化组件数据**
    （`heading` / `paragraph` / `list` / `code` / `quote` / `table` / `image` / `hr`，
    每个文本块同时给出 `inlines` 行内节点与 `text` 纯文本）；
    `render_html()` 渲染为**安全 HTML**；`to_text()` 供纯文本降级。
  - 行内：`**加粗**` / `*斜体*` / `~~删除线~~` / `` `行内代码` `` / `[文字](链接)` /
    `![alt](图片)` / `[n]` 引用上标。
  - **XSS 防护**：一切文本先转义（`& < > " '`），未识别语法按纯文本输出；
    链接与图片地址走**协议白名单**（仅 `http(s)` / `mailto` / `tel` 与相对路径，
    拒绝 `javascript:` / `data:` / `vbscript:` / `file:` / `blob:`）；
    校验前先剥离控制字符与空白，防 `java\tscript:` 之类的混淆绕过；
    外链统一加 `target="_blank" rel="noopener noreferrer"`。
  - **接口扩展**（向后兼容，`answer` 原样保留）：`/api/chat` 的 `data`、
    `/api/chat/stream` 的 `done`、`/api/sessions/{id}/messages` 的 `ai` 消息
    均新增 `blocks` 与 `answerHtml`，使**实时回答与历史恢复走同一套渲染结果**。
  - **前端** `static/app.js`：`renderContent` 升级为完整子集渲染器（流式阶段增量渲染），
    `finalize` / `loadHistory` **优先使用服务端 `answerHtml`**；`static/style.css`
    追加 `.rt-table` / `.rt-code` / `.rt-list` / `.rt-quote` / `.rt-figure` 样式。
  - **有意偏离原型**：`app.js` 与 `style.css` 不再与原型字节级一致，已登记在
    `tests/test_frontend.py::MODIFIED_FROM_PROTOTYPE`（守卫会校验「有原因且确实不同」，
    防止白名单腐烂）。
  - **一致性回归**：`tools/verify_richtext.py` 把同一批 34 条用例同时喂给后端
    `render_html()` 与前端 `renderContent()`（Node 加载 `static/app.js`，
    经 `globalThis.__NXZ_EXPORT__` 测试钩子取到渲染器），要求**逐字符一致**。
- **任务 1.11 文档预览与对比** ✅ 原型 `kb-engine` 的 `admin.js` **没有任何预览逻辑**
  （`grep raw|chunks|preview` 无命中），本项为**新增能力**，无 Node 侧可回归：
  - `app/services/doc_preview.py`（新增）：
    - `resolve_original(file_key)` —— 原件定位。`file_key` 两种形态：
      `uploads/<毫秒时间戳>-<安全名>` → `UPLOAD_DIR`；其余（如
      `01-信网中心/办事指南/xxx.md`）→ `KNOWLEDGE_DIR`。
      **两层穿越防护**：先拒绝含 `..` 的路径段，再在 `resolve()` 后校验仍在根目录内
      （兜住符号链接/短名等边缘情形）。
    - `describe_original()` —— 元信息（大小/mtime/MIME/可否内联）。**不返回绝对路径**，
      避免向管理端泄露服务器目录结构。
    - `extract_text()` —— 原文纯文本，带 **mtime+size 缓存**（原件被覆盖自动失效，
      上限 32 条 FIFO）。文本格式（`md/txt/csv/json`）**原样解码**（所见即原件，
      含 frontmatter）；二进制格式（`docx/pdf/xlsx/xls`）复用解析层还原。
    - `search_chunks()` —— 切片关键词检索：多词 **AND**、忽略大小写、返回命中区间
      `[start,end]`（每词上限 50 段，防前端渲染爆炸）；`only_matched=False` 时
      保留未命中切片，供对比视图的「只看命中」开关。
  - **接口**（两种存储模式都可用，见「管理端接口」表）：
    `GET /api/docs/{id}`、`GET /api/docs/{id}/preview?q=`、`GET /api/docs/{id}/text`、
    `GET /api/docs/{id}/raw?download=`，并给 `/chunks` 加 `?q=`。
    - `/chunks` **向后兼容**：不带 `q` 仍返回列表（1.14 契约）；带 `q` 时过滤并给每条
      补 `matches` / `titleMatches`。
    - `/preview` 一次返回原文 + 元信息 + **全部**切片（带命中区间）+ `matchCount`，
      前端无需二次请求即可实现「只看命中」。
    - `/raw` 默认 `inline`（PDF/图片/文本可直接看），不可内联的格式或 `?download=1`
      走 `attachment`；一律带 `X-Content-Type-Options: nosniff`，
      `Content-Disposition` 用 RFC 5987 编码（中文名不乱码）；取件落操作日志 `KB_DOWNLOAD`。
      **`.svg` / `.html` 不列入内联白名单**——它们可携带脚本，内联等于同源执行上传内容。
  - **前端**（新增文件，原型中不存在）：`static/preview.html` + `preview.js` + `preview.css`
    （左右分栏：左原文 / 右切片；关键词高亮、命中计数、只看命中、上下跳转、
    打开/下载原件）；`main.py` 加 `/preview` 显式路由（须在 `StaticFiles` 挂载前注册）；
    `admin.js` 文档列表每行加「预览」入口 → 两处均登记进
    `tests/test_frontend.py::MODIFIED_FROM_PROTOTYPE`（`admin.js` / `admin.css`），
    新增文件登记在 `ADDED_FRONTEND_FILES`（守卫会断言原型里**没有**同名文件）。
  - **前后端同源**：`preview.js` 的 `findRanges` / `parseTerms` 与后端
    `find_ranges` / `parse_terms` **逐字符等价**，由 `tools/verify_preview.py` 交叉校验
    （22 组区间用例 + 11 组分词用例）。用例限定在 **BMP 内**：JS 按 UTF-16 码元、
    Python 按码点计数，非 BMP 字符的区间数值天然不同（各自渲染器自洽），
    `preview_cases.assert_bmp_only()` 会拦住误加的非 BMP 用例。
  - **验收工具**：`tools/verify_preview.py`（自包含，默认 JSON 索引）——七组共
    **115 项检查**；含「索引里**每一篇**文档都能看到切片、原文可提取、原件可取、
    关键词命中正确切片、不存在的词零命中」的端到端验收；`--preview` 可另出静态快照。
- **任务 1.10 12 格式 + OCR + 原图** ✅ 原型 `SUPPORTED` 只有 8 种格式，本项为**新增能力**
  （`PLANNED_EXT` 已清空）：
  - **格式扩到 12 类**（18 个扩展名）：新增 `.docx→.doc`、`.pptx→.ppt`、以及**图片类**
    （`png/jpg/jpeg/bmp/webp/tif/tiff` 归一为一类）。
    - `.pptx` —— `app/core/parser/pptx.py`（python-pptx）：每页 `幻灯片 N` 一级标题 →
      页标题二级标题 → 文本框/表格/图片 → 演讲者备注（`备注：…`）；分组形状递归展开。
    - `.doc` / `.ppt` —— `app/core/parser/doc.py` + `office_convert.py`：OLE 二进制无纯 Python
      库可解，走 **LibreOffice** `soffice --headless --convert-to docx|pptx` 后复用正常解析链路
      （保留标题层级与表格）；`.doc` 另有 `antiword`/`catdoc` 抽**纯文本**的兜底。
      两者都不可用时抛 `ConverterUnavailable`，由服务层转成**可执行的业务提示**
      （而不是 500 或静默空文档）。
    - **图片类** —— `app/core/parser/image.py`：图片 → `heading(标题)` + `image` + `para(OCR 文本)`。
  - **可插拔 OCR** —— `app/core/parser/ocr.py`，`OCR_ENGINE=auto|rapidocr|tesseract|none`：
    - `rapidocr`（默认，`rapidocr-onnxruntime`）：**纯 pip、模型随 wheel 分发、无系统依赖**，
      中文开箱可用；
    - `tesseract`：需 `pytesseract` + 可执行文件（`TESSERACT_BIN` 或 PATH）+ `chi_sim` 语言包；
    - **绝不抛异常**：引擎缺失/图片损坏/引擎内部报错一律返回 `ok=False` 的降级结果
      （图片仍可入库，只是没有文字），并把原因写进 `warnings` 交给管理端。
  - **内容寻址原图** —— `save_original()` 按 **SHA-256 前 16 位**命名落
    `UPLOAD_DIR/original/<hash16>.<ext>`：天然去重（同一张图多文档只存一份）、
    先写临时文件再 `replace()` **原子替换**（并发上传不会读到半截内容）。
  - **`imageKeys` 贯通** —— `chunker.Chunk.image_keys` 累积块引用的原图；
    `to_dict()` / `load_store()` / `list_chunks()` **仅在非空时输出** `imageKeys`，
    保证既有 `store.json` 的**逐字段形状不变**（`tools/parity/compare.py` 9/9 零漂移）。
    - `docx_to_blocks` / `pptx_to_blocks` 的 `image_sink=None` 时**不抽取**内嵌图片，
      块输出与原型逐块一致 —— parity 回归走的就是这条路径。
  - **接口**：`GET /api/docs/{id}/images`（原件本身 + 内嵌图，带 `role` / `chunkSeqs` /
    宽高）与 `GET /api/docs/{id}/image?key=`（原图字节流）。**归属校验**：`key` 必须属于
    该文档，且必须是图片扩展名，否则 400/404 —— 不能用任意路径把服务器上其它文件当图片读出来。
    原图按内容寻址，内容不可变，故带 `Cache-Control: private, max-age=86400, immutable`。
  - **预览页适配**：`preview.js` 新增图片缩略图条（图片型文档标注「文字来自 OCR」）与
    切片卡片内的原图小图，便于「图 ↔ 切片」对照；图片型文档的原文标题改用**文档标题**
    （原图按哈希命名，不处理会显示成一串哈希）。
  - **验收工具**：`tools/make_samples.py` 现场生成 13 个样例覆盖 12 类格式
    （`.pdf` 复用仓库根含文本层的 PDF；`.doc/.ppt` 需 LibreOffice，`--with-soffice` 补齐），
    `tools/verify_formats.py` 七组共 **65 项检查**（含 `/images`、`/image` 的真实 HTTP 端到端
    与越权/穿越/非图片三类拒绝路径）。

### 与 Node 原型的有意差异（数据层）

| 位置 | 原型行为 | 本实现 | 原因 |
| --- | --- | --- | --- |
| `ingestUploaded` | 入库后删掉 staging 原件 | **保留**原件到 `uploads/` | 否则 `file_key` 指向不存在的文件，1.11 原文预览无从取件 |
| 上传查重 | 直接 insert，撞唯一键抛原始 SQL 错误 | 提前查 `file_hash` 并给出可读提示 | 前端能直接展示原因 |
| `syncKnowledgeBase` | 源目录为空时**静默删光**全部知识 | 源目录扫不到文件但库中有知识时**中止同步**并报错 | 避免 `KNOWLEDGE_DIR` 配错导致数据全灭 |
| 会话角色 | `INSERT IGNORE` 后不再更新 | 传入 `role` 时刷新 `role_snapshot` | 与 JSON 模式行为一致 |
| 历史引用 `ktype` | 直接返回枚举（前端显示 `guide`） | 转中文名（`办事指南`） | 与实时链路一致 |
| 会话历史 `n` | 无 | 按插入顺序回填 `n` | `answer_citation` 无该列，等价于前端的 `n \|\| startN+i` |
| 索引不可用 | — | `/api/chat` 返回 **503**「知识库不可用」 | 否则空召回会伪装成「知识库没有答案」的拒答 |


### 向量模型准备（离线交付）

模型随交付包分发，无需联网。首次准备（换机或清理后）：

```bash
python tools/fetch_model.py                                                  # 嵌入模型（约 91MB）
python tools/fetch_model.py --model onnx-community/bge-reranker-v2-m3-ONNX   # 精排模型（约 570MB）
python tools/fetch_model.py --force                                          # 强制重新准备
```

模型落地于 `models/<name>/`，已在 `.gitignore` 中忽略。
加载顺序：`KB_MODEL_DIR` / `KB_EMBED_DIR` / `KB_RERANK_DIR` → `models/<name>` → Node 原型缓存 → `HF_ENDPOINT` 下载。
嵌入模型全部不可用时会降级为**确定性哈希向量**并在 `/api/health` 中报
`embed_is_fallback=true`（仅保证链路可跑通，检索质量不可用于生产评估）；
精排模型不可用时降级为**不精排**（返回原序），不阻断问答主链路。

> **精排分数说明**：q8 量化权重在 onnxruntime-node（Node）与 onnxruntime（Python）
> 上存在算子实现差异，logits 绝对值有 0.5~1.3 的偏移；已验证 tokenizer 输出逐值一致，
> 差异不来自分词。排序结果 top-1 完全一致，末位偶有翻转（分数接近时）。

### 一致性回归工具

```bash
# 在 kb-service 目录下，比对 Python 与 Node 原型的解析/切片结果
python tools/parity/compare.py
# 比对向量（Python onnxruntime ↔ Node transformers.js）
python tools/parity/embed_compare.py
# 比对检索（真实索引上的命中序列与 cos/bm25/rrf）
python tools/parity/retrieve_compare.py
# 比对精排（排序一致性）
python tools/parity/rerank_compare.py
# 比对生成层（mock 回答、引用、拒答、置信度）
python tools/parity/generate_compare.py
# 指定 Node 可执行文件（未在 PATH 时）
NODE_BIN=/path/to/node python tools/parity/compare.py
```

### API 冒烟测试

```bash
# 自动启动服务 → 逐个探测接口 → 关闭服务
python tools/smoke_api.py
python tools/smoke_api.py --keep        # 探测后保持运行，便于手工调试
python tools/smoke_api.py --no-mock     # 用 .env 真实 LLM 配置（无 key 时验证 503 降级）
python tools/smoke_api.py --with-sync   # 额外真实调用 POST /api/sync（会写库）
```

> 默认**不**调用 `/api/sync`：它会按知识目录真实写库（把新文件补进库），
> 例行冒烟不应该改数据。需要验证同步时显式加 `--with-sync`。

### 前端契约联调校验

按 `static/app.js` 的真实消费逻辑跑一遍链路（而非只断言「接口 200」）：

```bash
python tools/verify_frontend_contract.py
# 校验点：请求体 sessionId → delta.text → done.{answer,sessionId,citations,latencyMs,...}
#        → done.blocks/answerHtml 与 answer 同源 → citation.n/chunkId
#        → 历史 data 为数组且 ai 消息带 answerHtml → 无 LLM 时 503
python tools/verify_frontend_contract.py --store mysql   # 用 MySQL 存储跑（需数据库已启动）
```

> **两个工具共用的子进程辅助**：`tools/_server.py`。它把 uvicorn 的 stdout/stderr
> 重定向到**日志文件**而不是 `subprocess.PIPE`——父进程不读管道时，子进程写满内核
> 缓冲区（Windows 约 64KB）会**阻塞在 write 上**，表现为「接口莫名读超时，但 curl 正常」。
> 数据库不可用时 uvicorn 会打印大段异常栈，正好会触发这个坑。
>
> 冒烟与契约工具默认 `--store json`：字段契约与存储无关，默认不依赖 MySQL；
> 数据库未启动时登录接口会返回 500（登录要读 `sys_user`），属**预期降级**，不算失败。

### 测试

```bash
pytest -q      # 764 项：骨架 + 解析/切片 + 向量 + 检索 + 精排 + 生成
               #        + 索引加载 + 会话存储 + 多轮上下文 + 编排端到端 + API 契约
               #        + 前端静态资源（含 Node 一致性 + JS↔HTML 的 DOM id 交叉校验）+ MySQL 数据层
               #        + 认证/权限 + 安全基线（加密/脱敏/限流/敏感词/操作日志/SQL 守卫）
               #        + 富文本渲染（含前后端逐字符一致性）
               #        + 文档预览与对比（含 MySQL 模式桩 + 前后端检索函数一致性）
               #        + 12 格式/OCR/原图（含 pptx 结构、docx 内嵌图、旧格式降级、原图接口归属校验）
               #        + 会话日志管理（2.1：双视图/筛选/分页不重不漏/CSV/鉴权）
               #        + 智能体助手管理（2.2：场景搭建/七项配置/校验/状态码契约）
               #        + 助手配置接线（2.3：检索配置归一与增量合并/运行时解析/
               #          提示词进 LLM 系统消息/检索范围与阈值/对话测试不落库/
               #          「未配置助手 ≡ 无助手」的一致性不变量）
               #        + 大模型与模型管理（2.4：存储层加密/掩码/路由三分支/
               #          参数透传到请求体/NULL 语义/引用删除保护/Key 只进不出）
               #        + MySQL 分支桩（2.1/2.2/2.4：假连接跑归一化同形 / NULL 语义 /
               #          筛选下推 / Key 只进不出，无需数据库）
               #        + LLM 网关接入（2.4 延伸：默认路径与库内模型两条成功链路、
               #          403/401/连接失败必须 502 而非 500、流式三态、errorDetail 契约、
               #          同名 X-Request-Id 归一化）
               #        + 迁移脚本守卫（DDL 经 SQLAlchemy text() 不得产生意外 bind 参数）
               #        + 模型网关可观测性（C-01 接入：探测三态 online/unknown/offline、
               #          真实 socket 验证、代理绕过、错误码表完整性）
```

> 当前实测：**`886 passed, 6 skipped`**（有 node + tokenizers 的环境）。
> 差异说明：本机 Docker 启动后，`nengxiaozhi-mysql` 容器自动就绪，
> 原「需 MySQL 才能跑」的 64 个用例**转为真实执行且全部通过**（此前长期被跳过）。
> 仅剩 6 项跳过（未找到 Node 时的前后端逐字符比对）。
> **2026-10-20 2.10 / 3.7 回归（另一台无 node / 无 tokenizers 的环境）**：
> `836 passed / 81 skipped`，基线为 `812 passed / 81 skipped`——**新增 24 项全过**；
> 跳过项（Node 比对、精排模型、符号链接）与本次改动无关。
> 富文本与预览的「前后端逐字符一致」用例需要本机有 `node`（可用 `NODE_BIN` 指定路径）。

> **无库环境下的双后端覆盖**：`tests/test_mysql_store.py` 需真实数据库（不可达即整体跳过），
> 因此 2.1/2.2/2.4 新增的 **MySQL 专属路径**另由 `tests/test_store_mysql_stub.py`（32 项）
> 用**场景驱动的假连接**覆盖：按调用顺序发牌返回结果集，断言①两种后端对外 **dict 键集合一致**、
> ②`temperature=0.0` 原样落库而**未配置落 NULL**、③会话级筛选与时间范围**出现在 SQL 里**
> （`literal_binds` 编译后比对）、④明文 Key 绝不出现在 SQL 参数里。
> 同一套断言另有可复现工具 `tools/verify_mysql_branch.py`（41 PASS）。
> **这是模式分支验收，不能替代实机验收** —— MySQL 语法、类型映射、字符集与事务语义
> 仍须在有库环境跑 `pytest tests/test_mysql_store.py` 与 `verify_*.py --store mysql`。
> ✅ **上述实机验收已完成**（本机 Docker 就绪后）：`test_mysql_store.py` 全过、
> `verify_retrieval_consistency.py`（44 项，JSON/MySQL 检索结果**完全一致**）、
> `verify_store_roundtrip.py`（往返与原型索引一致）、`verify_multiturn.py`（12 轮）、
> 以及 `verify_chatlogs/agents/prompt/model --store mysql` 全部 0 FAIL。
> 桩测试与实机结论**未出现分歧**。


> `tests/conftest.py` 在导入应用前固定 `LLM_MOCK=1` / `KB_STORE=json`，
> 并把**会话文件、助手文件与模型文件**（`data/sessions.json` / `data/agents.json` /
> `data/models.json`）一并重定向到各测试的临时目录，避免污染运行数据；
> 同时清空 `llm_router` 的路由缓存（模型增删改后缓存必须失效，否则会拿旧 `base_url` 请求）。
> 环境变量优先级高于 `.env`，所以即使 `.env` 是 `KB_STORE=mysql`，测试仍走 JSON。
> `tests/test_mysql_store.py` 在 MySQL 不可达时**整体跳过**；写操作要么包在
> 会回滚的事务里，要么在 `finally` 中自行清理，绝不留脏数据。


## 关键约定

- **检索参数**：RRF `K=60`、`topK=5`、切片 `400/60/40` 与原型一致；精排候选默认 30（原型值），
  生产模板按 5.3 压测改为 `RERANK_TOPN=10` 且默认关闭精排（见 `artifacts/perf-report.md`）。
- **角色缺省**：不传角色按「访客」处理，不再放行全部知识。
- **认证**（任务 1.13）：`Authorization: Bearer <jwt>`；**无 token 即访客**，公开接口与问答照常可用。
  管理接口（`/api/docs`、`/api/sync`）在 **`AUTH_ENABLED=true`** 时要求管理员（401/403）；
  带 token 提问时**以 token 角色为准**，忽略请求体里的 `role`。演示账号见 `tools/seed_auth.py`。
- **限流与风控**（任务 1.12）：`/api/chat*` 与 `/api/docs*`、`/api/sync` 均受限流约束，
  超限返回 **429 + `Retry-After`**；提问/回答命中敏感词返回风控话术（提问侧直接拒答，
  回答侧在生成后就地改写）。生产多实例部署时，内存限流应替换为 Redis 等共享存储。
- **引用编号**：解析回答中的 `[n]` 时保留原始编号，避免来源面板错位。
- **响应封装**：`{"ok": bool, "data": ..., "error": ...}`；字段一律 camelCase。
- **SSE 契约**（与原型 `server.js` 一致，`static/app.js` 直接消费）：
  - `delta` → `{"type":"delta","session_id":..,"text":<增量>}`
  - `done` → `{"type":"done","answer","refused","confidence","citations","retrieved","sessionId","msgId","model","latencyMs","blocks","answerHtml"}`
  - `error` → `{"type":"error","session_id":..,"error":<消息>}`
  - `blocks` / `answerHtml` 为任务 1.9 **追加**字段（老前端忽略即可）：
    `delta` 阶段前端按增量自行渲染，`done` 时以服务端结果为准。
  - `done` 的 `modelCode` / `modelSource` 为任务 2.4 **追加**字段（老前端忽略即可）：
    `modelCode` 是本次问答实际走的模型编码（默认路径为 `default`），
    `modelSource` 是 `env` / `db` / `fallback` 三者之一——用来回答「为什么没走我配的模型」。
    **默认路径也照样回填 `default`/`env`**：若回空，「压根没配模型」与「配了但没生效」
    在响应里长得一模一样，反而丢掉排查所需的区分度。
- **LLM 未配置**：`/api/chat` 与 `/api/chat/stream` 均返回 **503 + ok=false + error**（原型行为），
  前端据 `resp.ok` 直接展示原因。
- **前端文件默认不可随意改**：`tests/test_frontend.py` 会校验 `static/` 与原型 `public/`
  字节级一致；确需改写必须登记到该文件的 `MODIFIED_FROM_PROTOTYPE` 并写明原因
  （当前仅 `app.js` / `style.css` 因**任务 1.9 富文本渲染**而有意偏离）。
- **路径字段与 CWD 解耦**：`.env` 中 `STORE_PATH` / `MODEL_DIR` / `KNOWLEDGE_DIR` / `UPLOAD_DIR`
  写相对路径时，一律按服务根目录（`BASE_DIR`）解析，不受启动目录影响
  （否则模型会静默加载失败并降级为哈希向量）。

## 安全

- `.env` 含密钥，已在 `.gitignore` 中忽略，禁止提交。
- 数据库访问统一参数化，禁止拼接 SQL（AST 静态守卫 + `# noqa: SQL-IDENTIFIER` 白名单豁免）。
- 生产需配置 `AES_KEY`（字段加密）与 `JWT_SECRET`（令牌签名），并置 `AUTH_ENABLED=true`。
- 日志默认脱敏（证件号 / 手机号 / 学号 / 邮箱），`install_masking_filter()` 已随应用启动挂载。
- 敏感词双向检测默认开启；限流默认 60 次/分钟（内存窗口，多实例部署需换 Redis）。
- 详细实现见上文「任务 1.12 安全基线」。

```bash
# 安全基线验收（自包含，不依赖 MySQL）：加密/脱敏/限流/敏感词/操作日志/SQL 守卫
python tools/verify_security.py      # 55 项检查，全通过退出码 0
```

## 数据层（任务 1.14）

现有 MySQL 23 张表 + 1 视图（库 `nengxiaozhi`），DDL 见仓库根 `database/schema.sql`；
原型向量表 `kb_chunk_vec_proto` 见 `database/prototype-extra.sql`。

```bash
# 1) 确认 Docker Desktop 已启动（MySQL 跑在容器里）
docker info

# 2) 起库（首次自动执行建表脚本；凭据见 database/docker-compose.yml）
docker compose -f ../database/docker-compose.yml up -d

# 3) 连通性验收
python tools/check_db.py

# 4) 把原型 JSON 索引灌入 MySQL（全量重建，会 TRUNCATE 知识三表）
python tools/ingest_mysql.py
python tools/ingest_mysql.py --dry-run     # 只自检不写库

# 角色与演示账号（任务 1.13，幂等）
python tools/seed_auth.py
python tools/seed_auth.py --dry-run
```

> **国内网络提示**：若 `docker pull mysql:8.0` 因 Docker Hub 不可达而失败，
> 可改用可达镜像源拉取后再打标准标签（compose 便会直接使用本地镜像）：
>
> ```bash
> docker pull docker.m.daocloud.io/library/mysql:8.0
> docker tag  docker.m.daocloud.io/library/mysql:8.0 mysql:8.0
> ```
>
> 一劳永逸的做法是在 `~/.docker/daemon.json` 增加
> `"registry-mirrors": ["https://docker.m.daocloud.io"]` 并重启 Docker Desktop。

数据库就绪后，`.env` 的 `KB_STORE` 决定存储模式（**仓库内 `.env` 已设为 `mysql`**）：

| 模式 | 索引来源 | 管理写操作（上传/改状态/删除/同步） | 会话 |
| --- | --- | --- | --- |
| `mysql`（默认，生产） | `kb_doc`/`kb_chunk`（仅 `status=2`） | ✅ 全功能，列表含草稿/待审核/已下线 | 落 `chat_session` 等 5 张表 |
| `json`（降级，无数据库时） | `store_path` → `kb-engine/data/store.json` | ❌ 返回 400 并提示切换（只读模式） | 落 `data/sessions.json` |

### 管理端接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/docs` | 文档列表（`?status=` / `?dept=` / `?ktype=` / `?categoryId=` 过滤，MySQL 下全部下推 SQL；`data` **为数组**）（任务 2.7 扩展） |
| `POST` | `/api/docs` | 上传入库（`filename`/`data`(base64)/`dept`/`ktype`/`status`/`visibleRoles`/`title`） |
| `GET` | `/api/docs/{docId}` | 文档详情（任务 1.11） |
| `PATCH` | `/api/docs/{docId}` | 状态流转 `0/1/2/3`（`docId` 接受 `7` 或 `D007`） |
| `DELETE` | `/api/docs/{docId}` | 级联删除切片/向量/引用 |
| `POST` | `/api/docs/batch` | 批量操作：`action=publish\|reject\|delete\|move`（+`categoryId`），逐个文档过授权校验（任务 2.7 / M-K-09） |
| `GET` | `/api/docs/{docId}/chunks` | 切片明细（含未发布文档）；`?q=` 时按关键词过滤并补 `matches`（任务 1.11） |
| `GET` | `/api/docs/{docId}/preview` | 预览数据：原文 + 原件元信息 + 全部切片（带命中区间）+ `matchCount`（任务 1.11） |
| `GET` | `/api/docs/{docId}/text` | 提取后的原文纯文本（任务 1.11） |
| `GET` | `/api/docs/{docId}/raw` | 原件字节流；`?download=1` 强制下载（任务 1.11） |
| `GET` | `/api/docs/{docId}/images` | 文档图片清单：原件本身 + 内嵌图，带 `role` / `chunkSeqs` / 宽高（任务 1.10） |
| `GET` | `/api/docs/{docId}/image?key=` | 单张原图字节流；`key` 必须属于该文档且为图片（任务 1.10） |
| `POST` | `/api/sync` | 按 `file_hash` 增量同步知识目录 |
| `GET` | `/api/admin/chatlogs` | 会话日志·问答明细（`agentId`/`channelId`/`role`/`dateFrom`/`dateTo`/`q`/`page`/`pageSize`）（任务 2.1） |
| `GET` | `/api/admin/chatlogs/users` | 会话日志·咨询用户（按会话聚合，参数同上）（任务 2.1） |
| `GET` | `/api/admin/chatlogs/filters` | 筛选项：智能体 / 渠道 / 角色（任务 2.1） |
| `GET` | `/api/admin/chatlogs/export` | CSV 导出（`view=qa\|users`，与页面同筛选口径）（任务 2.1） |
| `GET` | `/api/admin/stats/filters` | 统计筛选项：渠道 / 助手 / 角色（与会话日志同源）（任务 3.10） |
| `GET` | `/api/admin/stats/overview` | 指标卡 + 按天趋势 + 渠道/助手统计（`agentId`/`channelId`/`role`/`dateFrom`/`dateTo`）（任务 3.10） |
| `GET` | `/api/admin/stats/rankings` | 热门知识/分类/设备/情绪/应用排行（同上筛选 + `limit` 1–50）（任务 3.10） |
| `GET` | `/api/admin/stats/export` | 统计 CSV 导出（`kind=summary\|trend\|channels\|agents\|knowledge\|categories\|devices\|sentiments\|apps`，UTF-8 BOM）（任务 3.10） |
| `GET` | `/api/admin/unrecognized` | 未识别问题聚合列表（状态/来源/业务域/角色/渠道/时间/关键词/排序/分页）（任务 3.11） |
| `GET` | `/api/admin/unrecognized/filters` | 未识别池筛选项 + 阈值/窗口/存储说明（任务 3.11） |
| `GET` | `/api/admin/unrecognized/{id}` | 单条详情 + 会话追溯 + 相似问法（任务 3.11） |
| `POST` | `/api/admin/unrecognized/batch` | 批量忽略/重新打开/归类（`questionKeys` + `action`，`classify` 需 `dept`）（任务 3.11） |
| `POST` | `/api/admin/unrecognized/feedback` | 反哺为草稿知识（仅 MySQL；JSON 返回 400）（任务 3.11） |
| `GET` | `/api/admin/unrecognized/export` | 未识别问题 CSV 导出（UTF-8 BOM，任务 3.11） |
| `GET` | `/api/admin/channels` | 渠道清单 + 可用助手 + 渠道 URL / 嵌入代码（任务 3.1） |
| `GET` | `/api/admin/channels/{id}` | 渠道详情（任务 3.1） |
| `POST` | `/api/admin/channels` | 新建渠道（编码/名称/默认助手/欢迎语/主题/认证开关/状态/可切换助手）（任务 3.1） |
| `PATCH` | `/api/admin/channels/{id}` | 更新渠道 / 绑定助手 / 启停 / 门户个性化 / 角色路由 `routeConfig`（任务 3.1/3.2/3.4） |
| `POST` | `/api/admin/channels/{id}/copy` | 一键复制渠道配置（自动编码/副本默认停用/记录 `copyOf`）（任务 3.2） |
| `DELETE` | `/api/admin/channels/{id}` | 删除渠道（任务 3.1） |
| `GET` | `/api/channels/{code}` | 公开渠道配置（免登录；`?role=` 解析角色路由，含门户主题；停用/不存在 404）（任务 3.1/3.2/3.4） |
| `GET` | `/api/admin/apps` | 应用广场清单 + 来源/身份字典 + 可选智能体（任务 3.3） |
| `GET` | `/api/admin/apps/{id}` | 应用详情（任务 3.3） |
| `POST` | `/api/admin/apps` | 新建应用（智能体/外链、可见身份、排序、启停）（任务 3.3） |
| `PATCH` | `/api/admin/apps/{id}` | 增量更新应用（任务 3.3） |
| `DELETE` | `/api/admin/apps/{id}` | 删除应用（任务 3.3） |
| `GET` | `/api/apps` | 公开应用广场（`?role=` 按身份过滤；登录态以令牌角色优先）（任务 3.3） |
| `GET` | `/api/admin/agents` | 助手列表（`status`/`q`）+ 类型字典 + 场景预设状态（任务 2.2） |
| `GET` | `/api/admin/agents/presets` | 场景智能体预设及搭建状态（任务 2.2） |
| `GET` | `/api/admin/agents/{id}` | 助手详情（任务 2.2） |
| `POST` | `/api/admin/agents` | 创建助手（M-A-02）（任务 2.2） |
| `POST` | `/api/admin/agents/presets/{sceneKey}` | 一键搭建场景智能体，已存在则复用（M-A-01）（任务 2.2） |
| `PATCH` | `/api/admin/agents/{id}` | 更新基础配置（M-A-03）（任务 2.2） |
| `POST` | `/api/admin/agents/{id}/status` | 上架 / 下架（M-A-03）（任务 2.2） |
| `DELETE` | `/api/admin/agents/{id}` | 删除助手（被渠道绑定时 409 拒绝）（任务 2.2） |
| `GET` | `/api/admin/kb-folders` | 知识文件夹清单（部门×类型 + 文档数/切片数）（任务 2.3） |
| `GET` | `/api/admin/kb-categories` | 知识分类列表（部门×类型 + 文档数；JSON 模式 `editable=false` 只读推导）（任务 2.5） |
| `POST` | `/api/admin/kb-categories` | 新建分类（`deptId` 或 `dept` 名称二选一 + `ktype`，同分类判重 409）（任务 2.5） |
| `PATCH` | `/api/admin/kb-categories/{id}` | 重命名 / 改排序（任务 2.5） |
| `DELETE` | `/api/admin/kb-categories/{id}` | 删除分类（有文档/有子分类 → 409）（任务 2.5） |
| `GET` | `/api/admin/kb-permissions` | 授权矩阵（用户×部门×类型 + 五操作位、联络员/部门/操作位字典）（任务 2.6） |
| `POST` | `/api/admin/kb-permissions` | 授权（`userId` + `deptId`/`ktype` + ≥1 操作位；同键判重 409）（任务 2.6） |
| `PATCH` | `/api/admin/kb-permissions/{permId}` | 更新操作位（任务 2.6） |
| `DELETE` | `/api/admin/kb-permissions/{permId}` | 撤销授权（任务 2.6） |
| `GET` | `/api/knowledge/points` | 知识点（=切片）分页列表；`dept`/`ktype`/`categoryId`/`docId`/`blockType`/`q`/`page`/`size`（任务 2.8） |
| `GET` | `/api/knowledge/points/{chunkId}` | 知识点详情（含全文；列表只给 `excerpt`）（任务 2.8） |
| `POST` | `/api/knowledge/points` | 新建内容切片（序号 = 当前最大值+1，服务端分配）（任务 2.8） |
| `PATCH` | `/api/knowledge/points/{chunkId}` | 编辑（未传键不覆盖；正文/标题变 → 单点重算向量）（任务 2.8） |
| `DELETE` | `/api/knowledge/points/{chunkId}` | 删除无效切片（连同向量，回写文档切片数）（任务 2.8） |
| `POST` | `/api/knowledge/points/{chunkId}/append-text` | 再加工 · 添加文本内容（`position`: `start`/`end`）（任务 2.8） |
| `POST` | `/api/knowledge/points/{chunkId}/remove-content` | 再加工 · 删除内容（`text` 片段 或 `start`/`end` 区间）（任务 2.8） |
| `POST` | `/api/knowledge/points/{chunkId}/insert-image` | 再加工 · 插入图片（只允许该文档已有图片键）（任务 2.8） |
| `POST` | `/api/knowledge/export` | 批量导出知识点（`csv` 带 BOM / `md` / `json`，附件下载）（任务 2.8） |
| `GET` | `/api/admin/agents/{id}/retrieval` | 助手检索配置 + 全局默认值 / 取值范围（任务 2.3） |
| `PUT` | `/api/admin/agents/{id}/retrieval` | 保存检索配置（**增量语义**，未传的键保持原值）（任务 2.3） |
| `POST` | `/api/admin/agents/{id}/test` | 助手对话测试（用该助手的提示词+检索配置，**不落库**）（任务 2.3） |
| `GET` | `/api/admin/models` | 模型列表 + 厂商模板 / 取值范围 / Key 加密状态（M-A-06）（任务 2.4） |
| `GET` | `/api/admin/models/choices` | 助手可绑定的模型下拉（只含**启用中** + `default`）（M-A-06）（任务 2.4） |
| `GET` | `/api/admin/models/route-preview` | 路由预览：只解析不发起调用（M-A-06）（任务 2.4） |
| `POST` | `/api/admin/models` | 新增模型（M-A-06）（任务 2.4） |
| `GET` | `/api/admin/models/{model_code}` | 模型详情 + 实际路由信息（M-A-06）（任务 2.4） |
| `PATCH` | `/api/admin/models/{model_code}` | 增量更新（`apiKey` 三态：不传/`""`/覆盖）（M-A-06）（任务 2.4） |
| `DELETE` | `/api/admin/models/{model_code}` | 删除模型（被助手引用时 409 并列出引用者）（M-A-06）（任务 2.4） |
| `POST` | `/api/admin/models/{model_code}/probe` | 连通性自测（失败也返回 200 + `ok:false`，**不留日志**）（M-A-06）（任务 2.4） |
| `GET` | `/api/admin/operation-logs` | 操作日志（`userId`/`action`/`dateFrom`/`dateTo`/`page`/`pageSize`；含操作人姓名与动作标签）（任务 2.10） |
| `GET` | `/api/admin/sensitive-words` | 词库列表（`kind`/`status`/`q`/`page`/`pageSize`；JSON 模式只读视图）（任务 2.10） |
| `POST` | `/api/admin/sensitive-words` | 新增词条（`word` 全局唯一，重复 409；下一问即生效）（任务 2.10） |
| `PATCH` | `/api/admin/sensitive-words/{id}` | 增量更新（改名/启停/分类/备注）（任务 2.10） |
| `DELETE` | `/api/admin/sensitive-words/{id}` | 删除词条（下一问即失效）（任务 2.10） |
| `GET` | `/api/admin/roles` | 管理角色清单 + 权限点目录（12 模块 / 27 权限点；JSON 模式 `editable=false`）（任务 2.9） |
| `POST` | `/api/admin/roles` | 新建管理角色（`roleCode` 唯一，重复 409）（任务 2.9） |
| `PATCH` | `/api/admin/roles/{id}` | 更新角色名称/备注/权限矩阵（内置管理员角色锁定）（任务 2.9） |
| `DELETE` | `/api/admin/roles/{id}` | 删除角色（内置角色 400；仍被账号引用 409）（任务 2.9） |
| `GET` | `/api/admin/admins` | 管理员账号清单（角色标签、密码状态、最近登录）（任务 2.9） |
| `POST` | `/api/admin/admins` | 新建管理员账号（学工号唯一；密码 PBKDF2 落库）（任务 2.9） |
| `PATCH` | `/api/admin/admins/{id}` | 更新姓名/角色/状态/密码（密码只写不回读；禁止自禁/自删）（任务 2.9） |
| `DELETE` | `/api/admin/admins/{id}` | 移除管理员角色（禁止移除自己）（任务 2.9） |

> 任务 2.3 起 `/api/chat` 与 `/api/chat/stream` 接受 `agentId`（camelCase，兼容 `agent_id`）：
> 传了但**不存在或已下架**返回 **400**，不静默回落；不传则与批1 行为完全一致。
> 检索参数的优先级是 **显式请求（`topK`）> 助手配置 > 全局默认（`.env`）**。

上传件（`file_key LIKE 'uploads/%'`）**不参与增量同步**，否则每次同步都会清掉用户上传的知识。

`/api/admin/*` 统一挂 `require_admin` + `require_permission` + `rate_limit("admin")`：
`AUTH_ENABLED=false`（默认）时放行；`true` 时未登录 **401**、非管理员或缺权限点 **403**
（越权落 `ACCESS_DENIED` 日志）。查询参数用 camelCase 别名（`agentId`/`pageSize`…）。

> 预览相关接口只读、两种存储模式都可用；`/raw` 取件会落 `KB_DOWNLOAD` 操作日志。
> `/chunks` 的**返回形状与存储无关**：不带 `q` 恒为列表（保持 1.14 契约）。

### 一致性回归

```bash
# MySQL 往返：读回后与原型 store.json 逐字段比对（文档/切片/向量）
python tools/verify_store_roundtrip.py
python tools/verify_store_roundtrip.py --reingest   # 先重灌再比对

# 检索一致性：JSON 与 MySQL 两种存储在同一批问题上的召回序列与得分
python tools/verify_retrieval_consistency.py
python tools/verify_retrieval_consistency.py --dump # 打印得分明细

# 多轮上下文：连续 >10 轮，校验窗口 ≥10 轮、首条为 user、跨会话隔离
python tools/verify_multiturn.py
python tools/verify_multiturn.py --turns 16 --keep   # 自定义轮数并保留测试会话

# 富文本渲染：同一批用例喂给后端 render_html() 与前端 renderContent()，要求逐字符一致
python tools/verify_richtext.py
python tools/verify_richtext.py --preview outputs/richtext_preview.html   # 另出可视化预览

# 文档预览与对比（任务 1.11）：原件定位/穿越防护 + MIME + 原文提取 + 检索语义
#   + 前后端检索函数一致 + 接口契约 + 每一篇文档的端到端验收
python tools/verify_preview.py
python tools/verify_preview.py --store mysql                              # 用 MySQL 索引跑
python tools/verify_preview.py --preview outputs/preview_snapshot.html --doc D001

# 12 格式 + OCR + 原图（任务 1.10）：先生成样例，再跑验收
python tools/make_samples.py                       # 生成到 outputs/samples/（13 个样例）
python tools/make_samples.py --with-soffice         # 有 LibreOffice 时顺带补齐 .doc/.ppt
python tools/verify_formats.py                      # 七组共 65 项检查
python tools/verify_formats.py --no-ocr             # 跳过真实 OCR 识别（快）
python tools/verify_formats.py --json               # 机器可读输出

# 助手提示词 / 检索参数 / 对话测试（任务 2.3）：检索配置归一与增量合并 + 运行时解析
#   + 提示词进 LLM 系统消息 + 检索范围/阈值 + 过滤时机 + 「未配置助手 ≡ 无助手」不变量
python tools/verify_prompt.py                       # 73 项检查
python tools/verify_prompt.py --store mysql         # 用 MySQL 跑同一套断言

# 大模型与模型管理（任务 2.4）：存储层加密/掩码 + 路由三分支 + 参数透传（假 LLM 端点）
#   + NULL 语义 + 引用删除保护 + 未绑定助手的一致性回归（八组共 123 项）
python tools/verify_gateway.py                       # 模型网关对接自检


# MySQL 分支桩验收（任务 2.1/2.2/2.4）：无需数据库
#   归一化同形 / 可辨识 NULL / 筛选下推 / API Key 只进不出（41 项）
python tools/verify_mysql_branch.py

# LLM 网关接入自检（任务 2.4 延伸）
python tools/verify_gateway.py                      # 静态：配置指向 / 模型库 / 助手绑定 / 风险（5 组）
python tools/verify_gateway.py --self-test          # 起假端点验证「在线分支」本身可用（13 项）
python tools/verify_gateway.py \                    # 在线：真实网关端到端（11 项断言）
    --base-url http://<gateway>:8080/v1 \
    --api-key  sk-campus-xxxxx \
    --model    <网关虚拟模型别名>
```

> `verify_gateway.py` 的在线断言包含**SSE 是否被整体缓冲**的判定（首块时间 vs 总时长），
> 以及 401 / 403 的错误码归因。

> **`verify_mysql_branch.py` 是模式分支验收，不是实机验收**。它用假连接替换真实 SQL，
> 只执行 MySQL 分支的 Python 代码，用来在没有数据库的机器上盯住归一化、NULL 语义与下推。
> MySQL 语法、类型映射、字符集与事务语义仍须在有库环境验证。

> 前两个工具都以 `kb-engine/data/store.json`（7 文档 / 35 切片）为基准。
> 若执行过 `/api/sync` 把知识目录里**多出的源文件**补进库，库中文档数会大于 7，
> 此时需要先 `python tools/ingest_mysql.py` 回到基准再跑回归。

# 生产部署（任务 5.1）：配置 / 证书 / 守护重启 / Docker Nginx HTTPS 端到端
python tools/verify_deploy.py                      # 61 项（无 Docker 时加 --skip-docker）
python tools/verify_deploy.py --skip-docker

# 离线交付（任务 5.2）：构建 → 验收（安装/升级/回滚/模型加载/数据留存）
python tools/build_offline_bundle.py --out dist/offline --reuse-wheels .build/wheels-cache
python tools/verify_offline_bundle.py --bundle dist/offline/<包名> --link-models
python tools/offline_db_init.py --schema ../database/schema.sql --seed-auth   # 全新 MySQL 库手工初始化

# 集成中心（任务 4.8）：三表结构 / 加密落库 / mock+live 适配器 / 本地路由 / 审计 / RBAC
python tools/verify_integrations.py                # JSON 99 项
python tools/verify_integrations.py --mysql        # MySQL 99 项

### 浏览器端到端验收（管理页）

HTTP 层看不见的东西（表单收集、弹窗与页签互斥、局部刷新、DOM 拼写漂移）
用真实 Chromium 兜底。**统一要求全程零 `pageerror` / `console.error`**，
截图落 `kb-service/artifacts/`，且工具自身幂等（跑完自清理或恢复原状）。

```bash
python tools/verify_chatlog_ui.py          # 2.1 会话日志页（31 项）
python tools/verify_agents_ui.py           # 2.2 智能体管理页（46 项）
python tools/verify_agent_config_ui.py     # 2.3 助手配置四页签 + 对话测试（49 项）
python tools/verify_gateway_ui.py          # C-01 模型网关页 + 全站打磨项（43 项）
python tools/verify_kb_category_ui.py      # 2.5 知识分类管理页（11 项）
python tools/verify_kb_permission_ui.py    # 2.6 知识库部门授权页（9 项）
python tools/verify_kb_audit_ui.py         # 2.7 文档审核与批量操作（24 项）
python tools/verify_kb_points.py           # 2.8 知识点管理（104 项；--mysql 135 项）
python tools/verify_kb_points_ui.py        # 2.8 知识点管理页（36 项）
python tools/verify_admin_integration.py   # 2.11 后台整合：5 页签 / 站内链接 / 话术覆盖（15 项）
python tools/verify_integrations_ui.py     # 4.8 集成中心：本地模型 / MCP / 智能体（45 项）
```

> 2.5 / 2.6 / 2.7 三个工具都跑 `KB_STORE=mysql` 起真实服务：分类与授权的写路径、
> 筛选下拉的回填、批量条联动、审核点击流都只有 MySQL 模式才有意义。
> 它们都遵守同一套幂等约定——**先快照、后操作、finally 恢复**（如 2.7 会先把最后一篇
> 临时置为「待审核」让审核按钮可见，跑完按快照恢复 status/published_at）。

> 浏览器内核在隔离目录（`PLAYWRIGHT_BROWSERS_PATH` 默认指向
> `C:\Users\wick\.workbuddy-ai\binaries\ms-playwright`），工具已内置该默认值。
> 端口默认 8023 / 8024 / 8025 / 8035 / 8045，可用 `--port` 改；`--headful` 可看到浏览器窗口。
> 有意触发的 4xx 会让浏览器记录 `console.error`，工具用正则将其归入 `expected_http_errors`，
> 并**额外断言确实产生了 4xx**（证明负路径真打到服务端）——零 JS 错误守卫并未放松。
> 2.7 的工具额外监听 `response` 事件，把 4xx/5xx 与 URL 一并报出来
> （线上库缺 `kb_permission` 表这个缺陷就是这样被抓到的）。

### 编号与枚举规则（与原型一致）

- `kb_doc.doc_id` 为自增整数，对外编号 `D{doc_id:03d}`；切片 `D001-C002`。
  因此**全量重建前必须 TRUNCATE**，让自增从 1 重新开始，保证编号对齐。
- `ktype`：中文 ↔ 枚举 `政策文件→policy` / `办事指南→guide` / `常见问题→faq` /
  `服务事项→item` / `组织机构→org`。
- `status`：`0 草稿 / 1 待审核 / 2 已发布 / 3 已下线`（源数据「已审核」也归一为 2）。
- 向量不入生产表：原型用 `kb_chunk_vec_proto`（JSON 列）模拟 Milvus，
  生产应替换为 Milvus 集合 `kb_chunk_vec`（HNSW）。
- 会话表为空时会按需 seed `agent(campus-default)` 与 `channel(portal-web)`
  ——`chat_session.channel_id` 是 NOT NULL，必须有默认渠道。

### 已知数据缺口（需业务确认）

知识目录 `knowledge-base/` 下有 **9 个**受支持的源文件，而 `store.json`（原型产物）
只收录了 **7 篇**——`03-后勤处/常见问题/宿舍空调使用FAQ.md` 与
`06-图书馆/办事指南/图书馆借阅流程与规则指南.md` 从未入过库。
本实现**没有**静默补录，需要时在管理页点「增量同步」（或 `POST /api/sync`）即可补齐。

### 模块状态

| 模块 | 状态 |
| --- | --- |
| `app/db/session.py` | ✅ 引擎/会话/探活 |
| `app/db/models.py` | ✅ 17 个 ORM 模型（列名已实证匹配，含 `sys_role`/`sys_user`/`sys_user_role`、任务 2.4 的 `llm_model`） |
| `app/db/kb_repo.py` | ✅ 知识库读写与管理（TRUNCATE 走白名单校验） |
| `app/db/vector_repository.py` | ✅ 向量适配层（MySQL JSON，可换 Milvus） |
| `app/services/kb_admin.py` | ✅ 解析/切片/向量化/上传入库（含内嵌图落盘与 OCR 告警） |
| `app/services/kb_sync.py` | ✅ 增量同步（汇总解析告警到 `warnings`） |
| `app/services/doc_preview.py` | ✅ 原文提取/切片检索/原件与原图定位（任务 1.11 + 1.10） |
| `app/core/parser/ocr.py` | ✅ 可插拔 OCR（rapidocr / tesseract / none，缺失即降级） |
| `app/core/parser/office_convert.py` | ✅ LibreOffice 转换 + antiword/catdoc 兜底 |
| `app/core/parser/image.py` | ✅ 图片解析 + 内容寻址原图存储 |
| `app/core/parser/pptx.py` | ✅ PPTX 逐页解析（标题/正文/表格/图片/备注） |
| `app/core/parser/doc.py` | ✅ .doc/.ppt 旧格式（经转换层） |
| `app/core/index_store.py` | ✅ JSON + MySQL 双模式 |
| `app/db/chatlog.py` | ✅ JSON + MySQL 双后端 |
| `app/session/context.py` | ✅ 多轮上下文（≥10 轮，首条强制 user） |
| `app/core/richtext.py` | ✅ Markdown 子集解析 + 安全 HTML（协议白名单 + 全转义） |
| `app/core/ai_label.py` | ✅ AI 生成内容标识元数据（显式文案 + 隐式 `aiLabel` / 响应头，任务 3.7） |
| `app/services/doc_preview.py` | ✅ 原件定位（穿越防护）+ MIME + 原文提取（mtime 缓存）+ 切片检索 |
| `static/preview.{html,js,css}` | ✅ 原文-切片对比视图（新增，非原型迁移） |
| `app/security/{auth,rbac,deps}.py` | ✅ HS256 JWT + RBAC + FastAPI 守卫 |
| `app/security/crypto.py` | ✅ AES-256-GCM 字段加密（版本前缀 + 篡改检出） |
| `app/security/masking.py` | ✅ 日志脱敏（证件号/手机/学号/邮箱） |
| `app/security/ratelimit.py` | ✅ 内存滑动窗口限流（429 + Retry-After） |
| `app/security/sensitive.py` | ✅ 敏感词双向检测（提问 + 回答）；词表 **DB 优先 / 文件兜底 / 内置合并**，30s TTL + 写后即时失效（2.10） |
| `app/security/oplog.py` | ✅ 操作日志落库（`detail` 写前脱敏，DB 不可用降级）+ 筛选/分页查询与动作标签（2.10） |
| `app/db/sensitive_repo.py` | ✅ 词库 CRUD / 判重 / 启用词条读取（2.10） |
| `app/services/sensitive_admin.py` | ✅ 词库管理编排（JSON 只读视图 + MySQL CRUD + 缓存失效）（2.10） |
| `app/db/agent_store.py` | ✅ 助手存储（JSON + MySQL 双后端，含 `retrieval_config` 归一/合并/校验） |
| `app/services/agent_admin.py` | ✅ 场景预设 + 助手 CRUD + 基础配置校验（2.2） |
| `app/services/agent_config.py` | ✅ 知识文件夹 + 检索配置读写 + 对话测试（2.3） |
| `app/services/chatlog_admin.py` | ✅ 会话日志双视图 + 筛选 + 分页 + CSV 导出（2.1） |
| `app/core/agent_runtime.py` | ✅ 助手运行时配置解析（提示词 + 检索参数 + 生成参数 + 风险/兜底话术，缺省即沿用全局；2.11 起无待接线字段，`pendingFields` 恒为 `[]`） |
| `app/core/generator.py` | ✅ 支持助手自定义 `system_prompt`（为空回退平台默认）+ 模型/参数路由上报 |
| `app/core/pipeline.py` | ✅ 支持 `agentId`：知识范围过滤（召回前）+ 阈值 + 提示词接线 + 生成参数归一（`gen_kwargs`） |
| `app/db/model_store.py` | ✅ 大模型存储（JSON + MySQL 双后端，Key 加密、掩码不泄露长度、`default` 保留编码不落库）（2.4） |
| `app/core/llm_router.py` | ✅ 模型路由三分支（env/db/fallback，回退不抛异常）+ 按编码缓存客户端（2.4）；`LLM_PROVIDER=local` 时改取集成中心当前启用模型（4.8） |
| `app/db/integration_store.py` | ✅ 集成注册表（JSON + MySQL 双后端，密钥 AES-256-GCM、单活本地模型、测试状态）（4.8） |
| `app/services/integration_admin.py` | ✅ 本地模型 / MCP / 智能体校验与 mock/live 适配器 + 本地路由解析（4.8） |
| `app/api/integrations.py` | ✅ `/api/admin/integrations` 概览、CRUD、激活、测试、示例配置（4.8） |
| `app/services/model_admin.py` | ✅ 模型 CRUD + 连通性自测（失败回网关错误码 / `requestId` / 处置建议）+ 路由预览 + 删除引用保护（2.4） |
| `app/core/llm_client.py` | ✅ OpenAI 兼容客户端；`top_p` 非 None 才下发；**本地地址绕过系统代理**；`LLM_PROVIDER=local` 支持无 Key 本地端点；`LLMError` 结构化（网关错误码 / `requestId` / 中文处置建议），`parse_upstream_error()` 兼容网关与 OpenAI 两种错误体 |
| `app/api/chat.py` | ✅ 非流式与 SSE 流式；**上游失败返回 502 + `errorDetail`**（接入网关前是 500） |
| `app/api/{docs,sync,meta}.py` | ✅ 管理端全功能 |

