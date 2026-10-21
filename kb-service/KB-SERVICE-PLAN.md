# kb-service 架构与迁移计划（Python 服务层）

> 本位据 grounded 于已核查的 kb-engine 实测与数据库结构；描述「已存在」的能力均可在
> E:/work_space/“能小智”校园 AI 助手平台/kb-engine 与 database/schema.sql 中定位复核。
> 本文件是计划文档，不构成代码变更。标 ✔ 表示有源码依据；未标注的为规划。

---

## 0. 结论摘要

以 Python 3.11 + FastAPI 新建 `kb-service/` 作为生产交付主体，承担知识引擎、问答生成与管理 API；
复用现有 MySQL 22 张表、现有多语种对话/管理前端与已验证的检索与生成参数（RRF K=60、topK=5、BGE 中文、DeepSeek 流式、最近 8 条历史）。
Node kb-engine 从「交付主体」调整为「参考原型」，不再作为生产运行单元。

- **启动与迁移价值**：Python 满足招标«采用 Java、GO 或 Python 语言中的一种或多种»（采购文件 408–410 行）；复用已有数据模型与前端，避免重做；AI 生态（BGE/向量/OCR）在 Python 更完整。
- **语言合规口径**：交付为「Python 3.11 服务层（AI 引擎 + 核心 API）」，Node 仅作参考，不作为交付主体。
- **约束**：签订后 15 日历日完成实施与验收；需生产环境 Java/Python 之外还满足 HTTPS、AES、防注入、安全检测（采购文件 406–410、492–495 行）。本计划将安全项一并纳入入住条件。

---

## 1. 范围与边界

### 1.1 在范围内（本服务层）
- 文档解析、切片、向量化、混合检索、精排、问答生成与流式输出
- 管理后端 API（文档 CRUD、状态机、上传、同步、会话历史、反馈、审计）
- 会话与引用落库、Token 计量、未识别问题池
- 与学校集成相关但**接口式**预留：统一身份认证、数据中台、一网通办、门禁/教务/一卡通（不实现在本轮）

### 1.2 不在范围内（需另行决策）
- 校园业务系统真实对接（课表/成绩/一卡通/门禁/菜谱）
- 多渠道（企业微信、公众号、钉钉、今日校园、西科E站）实体接入及页面 SDK
- 大屏可视化、在线客服、情绪识别、批量文件管理等
- 本地大模型 / 自建 LLM 网关（预留配置位）

> 招标对这些多为 ▲/■ 或承诺函；一份交付计划如不标注可演示/可截图来源会被扣分或影响演示。
> 本计划将上述项单列于第 8 节「未纳入本轮的响应说明」，供商务层决策。

---

## 2. 目标架构

```text
浏览器（现有 index.html / admin.html / app.js，改用新接口）
        │ HTTPS
        ▼
kb-service（Python 3.11 + FastAPI + uvicorn 多 worker）
├─ app/api        REST + SSE（/api/chat/stream 等）
├─ app/core       解析/切片/嵌入/检索/精排/生成
├─ app/db         SQLAlchemy / mysqlclient（复用现有22张表）
├─ app/models     向量模型缓存（bge-small-zh-v1.5、bge-reranker-v2-m3）
└─ app/security   鉴权、AES 字段加密、脱敏、审计、限流
        │
        ├── MySQL 8（现有 schema.sql + kb_chunk_vec_proto）
        └── LLM：DeepSeek(OpenAI兼容) 走校园出口代理，可 LLM_MOCK 联调
```

- 单体微服务化分销：FastAPI 不拆分微服务；若学校明确要 Java 域，再在其上叠 Spring Boot 业务后端，AI 引擎不动（见第 10 节）。

---

## 3. 模块拆分与职责（对应 kb-engine 迁移）

| kb-service 模块 | 职责 | 对应现有实现（已核查） |
| --- | --- | --- |
| `core/parser` | md/txt/docx/pdf/xlsx/xls/csv/json 8 类解析；规划 doc/pptx/png/jpg 与 OCR 扩展 | kb-engine/src/parse/*（8 种） |
| `core/chunker` | 标题层级切片、400/60/40 滚动、表格整块、FAQ 整对、角色元数据 | kb-engine/src/chunk/chunker.js（chars 400/60/40 已定义） |
| `core/embedder` | bge-small-zh-v1.5 本地向量化（512 维，mean+normalize） | kb-engine/src/embed/embedder.js |
| `core/retriever` | 向量+BM25（中文 bigram）+RRF K=60+角色过滤+精排 | kb-engine/src/retrieve/bm25.js、retriever.js、rerank.js |
| `core/generator` | 提示词组装、引用解析、拒答识别、置信度、SSE 流式 | kb-engine/src/llm/generator.js、client.js |
| `db/mysql` | 现有 22 表接入；查询一律参数化；AES 加密字段读写；会话/引用/Token 落库 | database/schema.sql、kb-engine/src/store/mysql.js、chatlog.js |
| `api/*` | `/api/chat`、`/api/chat/stream`、`/api/sessions/{id}/messages`、`/api/docs`、`/api/sync`、`/api/meta`、`/api/health` 等 | kb-engine/src/web/server.js（路由 297–318 行） |
| `security/*` | 统一身份接入（CAS/OAuth2/LDAP 预留）、JWT、RBAC、脱敏、操作日志、限流（F-02） | 现有无；此项为新增 |

---

## 4. 数据策略（复用而非新建）

### 4.1 直接复用
- `database/schema.sql` 22 张表（库名 `nengxiaozhi`）与 `prototype-extra.sql` 的 `kb_chunk_vec_proto` 原型向量表。
- 字段结构、状态机（0草稿/1待审核/2已发布/3已下线）、`visible_roles`、`file_key`、`file_hash`、`version_no` 保持不变。

### 4.2 兼容性处理
- 现有 `chunk_id` 规则 `D00N-C00N` 与 `doc_id` 自动编号保持，便于前端引用面板与审计继续兼容。
- 原型向量 JSON 存 MySQL；生产环境切换 Milvus 时仅替换 `app/db/vector_repository` 适配层（见 4.3）。

### 4.3 向量存储适配层
- `db/vector_repository.py` 提供统一接口：`search(chunk_ids, vector, top_k)`、`upsert(records)`、`delete(doc_id)`。
- 第一版实现为 MySQL JSON（与现有 kb_chunk_vec_proto 一致）；后续可替换为 Milvus SDK，不改变应用层。

---

## 5. 接口契约（与现有前端对齐）

以下路由与 kb-engine/src/web/server.js 一致，由 Python 重新实现并保持响应结构：

| 方法 | 路径 | 用途 | 对应前端 |
| --- | --- | --- | --- |
| POST | /api/chat | 非流式问答 | app.js（备用） |
| POST | /api/chat/stream | 流式问答，SSE `delta`→`done` | app.js（主用） |
| GET | /api/health | 健康检查（llm/store/chunks/docs/roles） | - |
| GET | /api/docs | 文档列表 | admin.js |
| POST | /api/docs | 上传入库（base64 body） | admin.js |
| PATCH | /api/docs/{id} | 改状态（0/1/2/3） | admin.js |
| DELETE | /api/docs/{id} | 删除文档与切片/向量/引用 | admin.js |
| POST | /api/sync | 增量同步 | admin.js |
| GET | /api/meta | depts/ktypes/statuses/roles | admin.js |
| GET | /api/sessions/{id}/messages | 历史会话 | app.js |

- SSE 事件格式与现有 app.js 解析保持一致：`data: {"type":"delta"|"done"|"error", ...}`。
- 响应字段沿用 `ok/data/error` 封装。

---

## 6. 迁移步骤（两周，适用于 15 日历日约束）

> 以下为推荐顺序，具体以学校项目节奏为准；不含真实业务系统对接。

| 步骤 | 内容 | 估算 |
| --- | --- | --- |
| P0 | 建 kb-service 骨架、FastAPI、uvicorn、依赖锁定（pyproject） | 0.5d |
| P1 | 移植解析/切片/嵌入/检索/精排；用现有 8 个样例文档回归检索结果与 kb-engine 一致 | 2d |
| P2 | 移植 MySQL 存取（复用 22 表）与向量适配层 | 1d |
| P3 | 移植聊天接口与 SSE（含会话/引用/Token 落库） | 1.5d |
| P4 | 管理 API 与上传/同步/状态机 | 1d |
| P5 | 安全加固：HTTPS、JWT/RBAC、AES 字段加密、脱敏、操作日志、限流（配合安全检测） | 2d |
| P6 | 前端切到新接口；回归与文档更新 | 1d |
| P7 | 生产部署（Windows 服务 / systemd，离线依赖）、压测、验收文档 | 1.5d |

> 上述合计约 11.5 天，为成熟实施路径；真实排期须纳入学校评审与联调时间。

---

## 7. 关键迁移要点与风险

### 7.1 从 kb-engine 迁移时必须保留（已核查）
- RRF K=60、topK=5、召回候选 30（RERANK.topN）
- 检索角色过滤：`visible_roles` 含「全部」或匹配角色才可见
- 生成提示词结构（系统提示词 + 历史 + 资料 + 问题 + 身份）
- 引用解析与置信度规则（`cos` 首条、拒答正则、置信度 0.15）
- 分块参数 400/60/40、表格整块、FAQ 整对

### 7.2 计划内需要修复的已有问题（建议一并解决）
1. **角色缺省放行**：未传角色时当前过滤直接放行全部知识（retriever.js:3–6 已核实）。Python 版应默认访客，避免泄露内部知识。
2. **全文重载性能**：每次全量 MySQL JSON 向量 + 内存余弦在本轮不变，但需在 P5/P7 给出并发与缓存说明。
3. **引用编号完整性**：当前解析只保留被引用编号（generator.js:28–41 已核实），引用面板与正文编号可能错位。Python 版应保留原始号码并传递。
4. **上传原件丢失**：上传 staging 文件入库后即删，`file_key` 悬空（admin.js:191–198 已核实）。本轮至少保留原件目录或在 P5 纳入 MinIO/本地对象存储。
5. **更新/删除缺事务与审计**：上传/同步多表写入无事务；更新复用切片 ID；删除直接删引用（admin.js:73–80、118–168 已核实）。Python 版统一走事务与幂等。
6. **文档格式差距**：招标要求 12 种（doc/docx/pdf/png/jpg/json/md/pptx/ppt/txt/xlsx/csv，采购文件 210–222 行）；当前仅 8 种且扫描件无 OCR。Python 版应规划 doc/pptx/图片 OCR（MinerU/OSS 或本地 OCR）为可插拔扩展。
7. **多轮不足**：招标«不少于 10 轮»（采购文件 95 行）；当前最近 8 条历史约 4 轮。Python 版直接提升历史条数并做查询改写/指代消解（P5/P6 可评估）。

### 7.3 合规对齐（配合招标与安全检测）
- 开发语言：Python 3.11（满足 SEC-02）
- 防注入：统一参数化查询（mysqlclient/SQLAlchemy），杜绝字符串拼接
- 保密数据：AES 加密存储 `id_card_enc`、`phone_enc`（schema.sql 已预留列），密钥走环境变量
- HTTPS：部署层启用 TLS 证书，全站 HTTPS
- 操作日志：管理操作写 `operation_log`
- 限流：对 `/api/chat*` 与管理接口实现配额（F-02，30000 RPM 为 LLM 网关承诺）

---

## 8. 未纳入本轮的响应说明（供商务/运营决策）

| 招标要求 | 现状 | 本轮处理 |
| --- | --- | --- |
| 多渠道≥5（★） | 未接入 | 承诺函；接口预留，页面 SDK 后续 |
| 已落地案例/截图（▲） | 无实际业务 | 需商务侧准备案例材料 |
| ■演示项 | 部分有 demo | 需明确演示清单与截至时间 |
| 大屏/客服/情绪/批量 | 未实现 | 需商务评估是否补齐 |
| 本地大模型/LLM 网关 | 未实现 | 需明确采用 DeepSeek API 还是本地推理 |
| 语音/ASR（★3年） | 未实现 | 需商务确认 API 服务承诺 |

---

## 9. 部署与运行

- 环境：Python 3.11+，`pip install -e .` 或 requirements/pyproject；uvicorn `--workers 4`（可调）。
- 配置：`.env` 注入 `DB_*`、`LLM_API_KEY`、`AES_KEY`、`JWT_SECRET`；禁止入库。
- 前端：现有 html/css/js 指向新接口，作为静态资源同服；可保持 `/` 与 `/admin`。
- 模型缓存：bge-small-zh-v1.5 与 bge-reranker-v2-m3 离线拷贝至本地模型目录。
- 提示：真实生成默认调用 DeepSeek API（外呼）；离线联调用 `LLM_MOCK=1`。

---

## 10. 后续演进（如学校明确要 Java 域）

- 在 AI 引擎（Python）之上叠 Spring Boot 3 业务后端：门户、渠道、权限、统计。
- 中间通过 HTTP/内部接口调用 Python 引擎；MySQL 为共同数据层。
- 此演进不改变本计划中的数据模型与接口契约。

---

## 11. 开放问题（需集成方/学校确认）

1. 是否接受 Python 3.11 作为生产语言（满足 SEC-02）？
2. DeepSeek API 是否经校园出口代理可达？是否接受 LLM 外呼（招标要求 API 服务≥3 年）？
3. 样例知识替换为学校真实文档的时间与流程？
4. 是否需要本轮就实现 12 种格式与图片 OCR？
5. ■演示项与大屏/客服等是否纳入本轮交付范围？

---

*版本：draft v0.1｜日期：2026-10-10｜状态：待评审*