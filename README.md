# 能小智 · 校园 AI 助手平台

面向全校师生的一站式校园 AI 助手。以 **RAG 知识问答**为核心，提供智能问答、事务导办、智能问数、通行查询等能力；配套智能体管理、知识库运营与运营分析，为信息化管理人员提供智能体搭建、渠道接入、权限与统计。

> 生产交付主体为 **`kb-service`（Python + FastAPI）**；Node 版 `kb-engine` 仅作参考原型，不再作为生产运行单元。

---

## 功能概览

| 批次 | 内容 | 状态 |
|------|------|------|
| **批1 · 引擎与服务层** | RAG 主路、12 种文档格式 + OCR、多轮上下文（≥10 轮）、富文本与引用、原文↔切片预览、安全/RBAC、MySQL 存取、前端接入 | 15/15 ✅ |
| **批2 · 管理端核心** | 会话日志、助手/智能体管理、提示词与检索参数、对话测试、模型接入、知识分类、部门授权、审核与文件管理、知识点管理、角色权限、关键词库/操作日志 | 11/11 ✅ |
| **批3 · 渠道、协作与运营** | 多渠道分身、门户个性化、应用广场与角色授权、渠道×角色路由、用户端门户、敏感词切库、AI 标识、多智能体切换、嵌入 SDK、统计与大屏、未识别问题闭环 | 11/11 ✅ |
| **批4 · 外部集成** | 统一身份认证（CAS）、组织中台同步、问数、通行查询、导办、客服工作台、ASR、集成中心（MCP/第三方智能体）、分享与反馈 | 9/9 ✅（mock 打通，可切真实接口） |
| **批5 · 交付化** | 生产部署（Nginx TLS/常驻/健康检查）、离线交付包、性能与容量验证、验收用例/演示/佐证、安全加固 | 5.1–5.4、5.6 ✅；5.5 待校方真实文档；5.7 Milvus 条件专项 |

---

## 技术栈

- **服务层**：Python ≥ 3.11 · FastAPI · Uvicorn · Pydantic v2（`pydantic-settings`）
- **数据层**：MySQL 8（`sqlalchemy` + `pymysql`）；无库时降级为 JSON 只读模式（`KB_STORE=json`）
- **RAG**：中文向量 `bge-small-zh-v1.5`（512 维）· 精排 `bge-reranker-v2-m3-ONNX` · 混合检索（向量 + BM25 + RRF）
- **解析**：12 种格式（PDF/Word/PPT/Excel/Markdown/TXT/HTML/CSV/JSON/图片…）+ 可插拔 OCR（默认 `rapidocr-onnxruntime`，可选 tesseract）
- **大模型**：经 OpenAI 兼容网关 `campus-ai-gateway` 接入（数据面协议级零代码，改 `.env` 即可切厂商）
- **前端**：原生 HTML/CSS/JS，SSE 流式问答；嵌入 SDK 一行接入第三方站点

---

## 目录结构

```text
.
├─ kb-service/            ★ 生产主体（Python/FastAPI + RAG 引擎 + 前端静态资源 + 验收工具）
│  ├─ app/                应用代码（api / core / db / services / security）
│  ├─ static/             对话页、管理后台、各业务页与嵌入 SDK
│  ├─ tools/              70 个 verify_* 验收工具 + 迁移脚本 + 压测工具
│  ├─ tests/              pytest 用例（48 个测试文件）
│  ├─ deploy/             生产部署与离线包脚本
│  └─ artifacts/          验收日志、性能报告、安全报告与验收材料
├─ database/              MySQL schema（schema.sql）与容器编排
├─ knowledge-base/        知识源（多格式文档）
├─ kb-engine/             Node.js 参考原型（一致性回归基准，不参与生产）
├─ 技术开发需求文档.md     完整需求/方案/进度文档（v1.16）
└─ README.md
```

> `campus-ai-gateway`（模型网关）为**独立上游仓库**，不随本仓库分发；接入方式见下文。

---

## 快速开始

```bash
cd kb-service

# 1) 核心依赖
python -m pip install -e .

# 2) 可选依赖：解析 / OCR / 向量精排 / 生成 / 测试
python -m pip install -e ".[parse,ocr,ml,llm,dev]"

# 3) 配置
cp .env.example .env
#   填写 DB_* / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / AES_KEY / JWT_SECRET
#   生产路径把 KB_STORE 设为 mysql；无库联调可用 KB_STORE=json + LLM_MOCK=1

# 4) 启动
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后常用入口：

| 页面 | 地址 |
|------|------|
| 对话页（SSE 流式 + 引用面板） | `http://127.0.0.1:8000/` |
| 知识管理后台 | `http://127.0.0.1:8000/admin` |
| 文档预览与对比 | `http://127.0.0.1:8000/preview` |
| 客服工作台 / 数据中台 / 集成中心 | `/support` · `/data-center` · `/integrations` |
| 公开分享卡片 / 嵌入 SDK 示例 | `/share/<shareId>` · `/sdk-demo` |
| 交互式 API 文档 / 健康检查 | `/docs` · `/api/health` |

生产部署（HTTPS、常驻守护、MySQL 健康检查、Windows/Linux 服务化）见 [`kb-service/deploy/README.md`](kb-service/deploy/README.md)。

---

## 接入大模型网关

数据面为 OpenAI 兼容协议，接入只需改 `.env`：

```ini
LLM_BASE_URL=http://127.0.0.1:8088/v1   # 网关地址
LLM_API_KEY=sk-campus-****              # 网关下发的业务 Key
LLM_MODEL=deepseek-v4-1-flash-economy-261017
```

网关 `campus-ai-gateway` 需在外部单独部署（Postgres + Redis）。业务 Key 由网关控制台创建，**明文只显示一次**；Key 的可用性取决于租户启用、Key 状态、授权模型与路由渠道。

---

## 测试与验收

```bash
cd kb-service
pytest -q                                   # 全量用例
python tools/verify_acceptance.py --rehearse   # 验收彩排 R-00~R-08
python tools/verify_security_5_6.py            # 安全加固验收（5.6）
python tools/verify_offline_bundle.py          # 离线交付包验收（5.2）
```

**最近一次基线（2026-10-21）**：`1251 passed / 75 skipped / 0 failed`；安全加固 `PASS 50 / WARN 0 / FAIL 0`；离线包 `57 PASS / 0 FAIL / 0 WARN`（含 install→upgrade→rollback）；验收彩排 `9/9 PASS`。日志见 `kb-service/artifacts/`。

---

## 文档

- [`技术开发需求文档.md`](技术开发需求文档.md) —— 需求、功能清单、技术方案与逐项进度（v1.16）
- [`kb-service/README.md`](kb-service/README.md) —— 服务层详细说明：目录、API 契约、数据层、一致性与验收
- [`kb-service/artifacts/acceptance/`](kb-service/artifacts/acceptance/) —— 验收用例、演示脚本、佐证清单、安全与交付前检查

---

## 说明

- 仓库内**不包含**任何真实密钥、`.env` 与业务数据；示例配置见 `.env.example`。
- `campus-ai-gateway` 与采购商务文件未纳入本仓库（见 `.gitignore`）。
- 当前版本：`0.1.0`。
