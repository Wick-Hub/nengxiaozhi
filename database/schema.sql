-- =====================================================================
-- "能小智"校园 AI 助手平台 — 数据库结构字典(建表 DDL)
-- 对应《02-技术方案设计.md》§5 数据库设计
-- 环境: MySQL 8.0+ / utf8mb4 / InnoDB
-- 说明:
--   * 本文件同时作为招标 I-02「数据结构字典」交付物
--   * 敏感字段(手机号/证件号)按 §4.6 以 AES 加密后存入,列名后缀 _enc
--   * 向量不入 MySQL,存 Milvus 集合 kb_chunk_vec(§5.3),表内仅存元数据
-- =====================================================================

CREATE DATABASE IF NOT EXISTS nengxiaozhi
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE nengxiaozhi;

-- =====================================================================
-- 域 1:用户与组织(对接统一身份认证 I-01、数据中台同步 M-SYS-02)
-- =====================================================================

-- 组织机构(部门树,与知识库一级目录对应)
CREATE TABLE sys_org (
  org_id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '部门ID',
  parent_id     BIGINT NOT NULL DEFAULT 0 COMMENT '上级部门ID,0=根',
  org_code      VARCHAR(64) NOT NULL COMMENT '部门编码(对接数据中台)',
  org_name      VARCHAR(128) NOT NULL COMMENT '部门名称',
  org_path      VARCHAR(512) NOT NULL DEFAULT '' COMMENT '部门全路径,如 /1/12/120/',
  sort_no       INT NOT NULL DEFAULT 0 COMMENT '排序号',
  status        TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  synced_at     DATETIME COMMENT '最近同步时间(数据中台)',
  UNIQUE KEY uk_org_code (org_code),
  KEY idx_parent (parent_id)
) COMMENT='组织机构';

-- 角色(本科生/研究生/教职工/离退休/访客 + 管理角色)
CREATE TABLE sys_role (
  role_id     BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '角色ID',
  role_code   VARCHAR(64) NOT NULL COMMENT '角色编码,如 teacher/student/visitor',
  role_name   VARCHAR(64) NOT NULL COMMENT '角色名称',
  role_type   TINYINT NOT NULL DEFAULT 1 COMMENT '1身份角色 2管理角色',
  is_public   TINYINT NOT NULL DEFAULT 0 COMMENT '1=访客可见(公开)',
  remark      VARCHAR(255) DEFAULT '' COMMENT '备注',
  UNIQUE KEY uk_role_code (role_code)
) COMMENT='系统角色';

-- 用户(权威源=学校统一身份认证,本地表作缓存与扩展)
CREATE TABLE sys_user (
  user_id       BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '用户ID',
  user_no       VARCHAR(64) NOT NULL COMMENT '学工号(唯一)',
  real_name     VARCHAR(64) NOT NULL DEFAULT '' COMMENT '姓名',
  id_card_enc   VARBINARY(128) COMMENT '证件号(AES加密,§4.6)',
  phone_enc     VARBINARY(64) COMMENT '手机号(AES加密)',
  email         VARCHAR(128) DEFAULT '' COMMENT '邮箱',
  password_hash VARCHAR(255) COMMENT '管理账号密码哈希(PBKDF2,任务2.9;NULL=沿用内部演示免密登录)',
  id_type       TINYINT NOT NULL DEFAULT 1 COMMENT '1本科生 2研究生 3教职工 4离退休 5访客',
  org_id        BIGINT COMMENT '主部门',
  user_status   TINYINT NOT NULL DEFAULT 1 COMMENT '1在册 0注销',
  last_login_at DATETIME COMMENT '最近登录',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_user_no (user_no),
  KEY idx_org (org_id),
  KEY idx_id_type (id_type)
) COMMENT='用户表';

-- 用户-角色关联(一人多角色,如既是研究生又是教务员)
CREATE TABLE sys_user_role (
  id        BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id   BIGINT NOT NULL,
  role_id   BIGINT NOT NULL,
  UNIQUE KEY uk_user_role (user_id, role_id),
  KEY idx_role (role_id)
) COMMENT='用户角色关联';

-- 角色-权限点关联(菜单/按钮级,任务2.9;管理员角色恒有全部权限,无需落行)
CREATE TABLE sys_role_permission (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  role_id    BIGINT NOT NULL,
  perm_code  VARCHAR(64) NOT NULL COMMENT '权限点,如 docs.view/agents.manage',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_role_perm (role_id, perm_code),
  KEY idx_perm (perm_code)
) COMMENT='角色权限矩阵';

-- =====================================================================
-- 域 2:渠道与智能体(§4.1 多渠道分身、§4.2 会话路由、M-A-04/06)
-- =====================================================================

-- AI 助手(智能体)主配置
CREATE TABLE agent (
  agent_id      BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '助手ID',
  agent_code    VARCHAR(64) NOT NULL COMMENT '助手编码',
  agent_name    VARCHAR(128) NOT NULL COMMENT '助手名称',
  agent_type    VARCHAR(32) NOT NULL DEFAULT 'chat' COMMENT 'chat问答/db问数/guide导办/access通行/custom',
  system_prompt TEXT COMMENT '系统提示词(M-A-04)',
  model_code    VARCHAR(64) NOT NULL DEFAULT 'default' COMMENT '路由模型编码,经LLM统一网关解析(M-A-06)',
  -- 生成参数(M-A-06):NULL = 沿用平台默认(.env),与 retrieval_config 的「可辨识空值」一致。
  -- 刻意不用 NOT NULL DEFAULT 物化全局值:否则改 .env 后已建助手不跟随,
  -- 且无法区分「没配过」与「显式配成了和默认一样」。0.0/1.0 是合法温度,不能当哨兵值。
  temperature   DECIMAL(3,2) COMMENT '温度,NULL=沿用平台默认',
  top_p         DECIMAL(3,2) COMMENT 'topP,NULL=沿用平台默认',
  max_tokens    INT COMMENT '最大输出token,NULL=沿用平台默认',
  visible_roles VARCHAR(255) NOT NULL DEFAULT '全部' COMMENT '可见角色,逗号分隔',
  icon_url      VARCHAR(255) DEFAULT '' COMMENT '形象图',
  description   VARCHAR(255) NOT NULL DEFAULT '' COMMENT '助手描述(M-A-03)',
  stream_enabled TINYINT NOT NULL DEFAULT 1 COMMENT '1流式输出 0一次性返回(M-A-03)',
  risk_reply    VARCHAR(512) NOT NULL DEFAULT '' COMMENT '风控话术:命中敏感词时的固定回复(M-A-03)',
  fallback_reply VARCHAR(512) NOT NULL DEFAULT '' COMMENT '兜底话术:知识库未命中时的固定回复(M-A-03)',
  retrieval_config JSON COMMENT '检索配置(M-A-05)：topK/rerankTopn/minScore/depts/ktypes;缺省即沿用全局',
  guide_questions JSON COMMENT '场景引导问题(P-QA-06多智能体切换)：最多6条、单条≤120字;空=不单独推荐',
  status        TINYINT NOT NULL DEFAULT 1 COMMENT '1上架 0下架',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_agent_code (agent_code)
) COMMENT='AI助手(智能体)';

-- 大模型配置(LLM 统一网关,M-A-06 大模型配置 / M-SYS-06 模型管理 / C-01 网关)
-- 说明:
--   * agent.model_code 指向本表 model_code;保留编码 default 表示「走 .env 全局配置」,
--     该编码**不落库**——这样「未配置模型」与批1 行为由机制保证一致,一致性回归不受影响
--   * api_key 以 AES-256-GCM 加密存入(复用 §4.6 字段加密),对外接口一律回显掩码
--   * 刻意**不做**模型级温度/max_tokens 默认值:参数链保持三级
--     「显式请求 > 助手配置(NULL 即沿用全局) > 平台默认」,避免同一数值出现两处定义
CREATE TABLE llm_model (
  model_id     BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '模型ID',
  model_code   VARCHAR(64) NOT NULL COMMENT '模型编码(agent.model_code 引用;default 保留给 .env)',
  model_name   VARCHAR(128) NOT NULL COMMENT '展示名称,如 豆包·Doubao-pro-32k',
  vendor       VARCHAR(32) NOT NULL COMMENT '厂商:deepseek/doubao/qwen/glm/kimi/openai/local/custom',
  api_base     VARCHAR(255) NOT NULL DEFAULT '' COMMENT 'OpenAI 兼容 base_url,如 https://ark.cn-beijing.volces.com/api/v3',
  remote_model VARCHAR(128) NOT NULL DEFAULT '' COMMENT '厂商侧模型名,如 doubao-pro-32k',
  api_key_enc  VARBINARY(512) COMMENT 'API Key 密文(AES-256-GCM),本地模型可空',
  timeout      FLOAT COMMENT '单次调用读超时(秒),NULL=沿用全局;本地模型可调大',
  is_local     TINYINT NOT NULL DEFAULT 0 COMMENT '1=校内本地模型(C-05,通常无需 API Key)',
  sort_no      INT NOT NULL DEFAULT 0 COMMENT '列表排序',
  remark       VARCHAR(255) NOT NULL DEFAULT '' COMMENT '备注(用途/负责人等)',
  status       TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用(停用后引用它的助手回退全局默认)',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_model_code (model_code),
  KEY idx_vendor (vendor),
  KEY idx_status (status)
) COMMENT='大模型配置';

-- 渠道(分身):官网/二级网站/企微/公众号/大厅/硬件
CREATE TABLE channel (
  channel_id      BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '渠道ID',
  channel_code    VARCHAR(64) NOT NULL COMMENT '渠道编码',
  channel_name    VARCHAR(128) NOT NULL COMMENT '渠道名称',
  default_agent_id BIGINT NOT NULL COMMENT '绑定主助手',
  welcome_text    VARCHAR(1024) DEFAULT '' COMMENT '欢迎语',
  theme_config    JSON COMMENT '主题色/浮窗/形象等UI配置(M-C-01)',
  route_config    JSON COMMENT '渠道×角色路由:助手/欢迎语/引导问题/模块开关(M-C-07)',
  embed_url       VARCHAR(255) DEFAULT '' COMMENT '独立访问URL',
  embed_snippet   TEXT COMMENT 'iframe/浮窗嵌入代码',
  need_auth       TINYINT NOT NULL DEFAULT 1 COMMENT '1需统一认证 0访客可用',
  copy_of         BIGINT COMMENT '复制来源渠道ID(M-C-02 一键复制)',
  status          TINYINT NOT NULL DEFAULT 1,
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_channel_code (channel_code),
  KEY idx_default_agent (default_agent_id)
) COMMENT='渠道分身';

-- 渠道-助手关联(渠道内可见的智能体集合)
CREATE TABLE channel_agent (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  channel_id  BIGINT NOT NULL,
  agent_id    BIGINT NOT NULL,
  sort_no     INT NOT NULL DEFAULT 0,
  UNIQUE KEY uk_channel_agent (channel_id, agent_id)
) COMMENT='渠道助手关联';

-- 智能体广场应用(M-C-05/06:名称/来源/分类/简介/图标/排序/启停/角色可见)
CREATE TABLE app (
  app_id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '应用ID',
  app_name      VARCHAR(128) NOT NULL COMMENT '应用名称',
  source_type   VARCHAR(16) NOT NULL DEFAULT 'agent' COMMENT '来源:agent智能体/link外部链接',
  agent_id      BIGINT COMMENT '来源为智能体时的助手ID',
  link_url      VARCHAR(512) NOT NULL DEFAULT '' COMMENT '来源为外部链接时的地址',
  category      VARCHAR(32) NOT NULL DEFAULT '' COMMENT '分类',
  description   VARCHAR(255) NOT NULL DEFAULT '' COMMENT '简介',
  icon_url      VARCHAR(255) NOT NULL DEFAULT '' COMMENT '图标(URL或emoji)',
  sort_no       INT NOT NULL DEFAULT 0 COMMENT '排序,小的在前',
  visible_roles VARCHAR(255) NOT NULL DEFAULT '全部' COMMENT '可见角色,逗号分隔',
  status        TINYINT NOT NULL DEFAULT 1 COMMENT '1上架 0下架',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_app_sort (sort_no, app_id),
  KEY idx_app_agent (agent_id)
) COMMENT='智能体广场应用';

-- 引导问题(渠道×角色维度配置)
CREATE TABLE guide_question (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  channel_id  BIGINT NOT NULL COMMENT '所属渠道',
  role_id     BIGINT NOT NULL DEFAULT 0 COMMENT '目标角色,0=全部',
  question    VARCHAR(255) NOT NULL COMMENT '引导问题文本',
  agent_id    BIGINT COMMENT '点击路由到的助手',
  sort_no     INT NOT NULL DEFAULT 0,
  KEY idx_channel_role (channel_id, role_id)
) COMMENT='引导问题';

-- =====================================================================
-- 域 3:知识库(M-K-*;元数据契约与 knowledge-base frontmatter 一致)
-- =====================================================================

-- 知识分类(部门×知识类型两维,M-K-02)
CREATE TABLE kb_category (
  category_id  BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '分类ID',
  dept_id      BIGINT NOT NULL COMMENT '所属部门(sys_org)',
  ktype        VARCHAR(32) NOT NULL COMMENT 'policy政策/guide指南/faq问答/item事项/org机构',
  category_name VARCHAR(128) NOT NULL COMMENT '分类名称',
  parent_id    BIGINT NOT NULL DEFAULT 0,
  sort_no      INT NOT NULL DEFAULT 0,
  KEY idx_dept_ktype (dept_id, ktype)
) COMMENT='知识分类';

-- 知识库部门授权(M-K-03):超管为部门联络员按「用户×部门×知识类型」授权操作位
CREATE TABLE kb_permission (
  perm_id         BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '授权ID',
  user_id         BIGINT NOT NULL COMMENT '被授权用户(sys_user,部门联络员)',
  dept_id         BIGINT NOT NULL DEFAULT 0 COMMENT '授权部门(0=全部部门)',
  ktype           VARCHAR(32) NOT NULL DEFAULT '*' COMMENT '授权知识类型(*=全部)',
  can_upload      TINYINT NOT NULL DEFAULT 0 COMMENT '可上传',
  can_view        TINYINT NOT NULL DEFAULT 0 COMMENT '可查看',
  can_download    TINYINT NOT NULL DEFAULT 0 COMMENT '可下载',
  can_delete      TINYINT NOT NULL DEFAULT 0 COMMENT '可删除',
  can_create_sub  TINYINT NOT NULL DEFAULT 0 COMMENT '可建子分类',
  created_by      BIGINT COMMENT '授权人(super_admin)',
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_perm (user_id, dept_id, ktype),
  KEY idx_perm_user (user_id),
  KEY idx_perm_dept (dept_id)
) COMMENT='知识库部门授权';

-- 知识文档
CREATE TABLE kb_doc (
  doc_id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '文档ID',
  doc_title     VARCHAR(255) NOT NULL COMMENT '文档标题',
  category_id   BIGINT NOT NULL COMMENT '所属分类',
  dept_id       BIGINT NOT NULL COMMENT '主管部门',
  ktype         VARCHAR(32) NOT NULL COMMENT '知识类型',
  file_format   VARCHAR(16) NOT NULL COMMENT 'doc/docx/pdf/png/jpg/json/md/pptx/ppt/txt/xlsx/csv(M-K-06)',
  file_key      VARCHAR(255) NOT NULL COMMENT 'MinIO 对象键(原件,SEC-03 本地存储)',
  file_size     BIGINT NOT NULL DEFAULT 0,
  file_hash     CHAR(64) NOT NULL DEFAULT '' COMMENT 'SHA-256,去重',
  chunk_count   INT NOT NULL DEFAULT 0 COMMENT '切片数',
  visible_roles VARCHAR(255) NOT NULL DEFAULT '全部' COMMENT '可见角色,逗号分隔(M-K-03)',
  status        TINYINT NOT NULL DEFAULT 0 COMMENT '0草稿 1待审核 2已发布 3已下线(M-K-05 审核流)',
  version_no    INT NOT NULL DEFAULT 1 COMMENT '版本号,再加工产生新版本(M-K-09)',
  source_type   VARCHAR(32) NOT NULL DEFAULT 'manual' COMMENT 'manual上传/sync同步/import导入/feedback反哺(M-S-07)',
  created_by    BIGINT COMMENT '上传人',
  audited_by    BIGINT COMMENT '审核人',
  published_at  DATETIME COMMENT '发布时间',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_file_hash (file_hash),
  KEY idx_dept_ktype (dept_id, ktype),
  KEY idx_status (status),
  KEY idx_category (category_id)
) COMMENT='知识文档';

-- 知识切片(权威元数据源;向量在 Milvus kb_chunk_vec,可全量重建)
CREATE TABLE kb_chunk (
  chunk_id      VARCHAR(64) PRIMARY KEY COMMENT '切片ID,如 D002-C003(与原型编号一致)',
  doc_id        BIGINT NOT NULL COMMENT '所属文档',
  seq_no        INT NOT NULL COMMENT '文档内序号',
  block_type    VARCHAR(16) NOT NULL DEFAULT 'para' COMMENT 'para段落/table整表/faq问答对/image图片',
  title_path    VARCHAR(512) NOT NULL DEFAULT '' COMMENT '标题层级路径(检索上下文)',
  content       MEDIUMTEXT NOT NULL COMMENT '切片文本(markdown富文本)',
  char_count    INT NOT NULL DEFAULT 0,
  page_no       INT COMMENT '来源页码',
  image_keys    JSON COMMENT '引用的MinIO图片对象键列表',
  visible_roles VARCHAR(255) NOT NULL DEFAULT '全部' COMMENT '默认继承文档,可切片级覆盖',
  status        TINYINT NOT NULL DEFAULT 2 COMMENT '随文档状态',
  vec_synced    TINYINT NOT NULL DEFAULT 0 COMMENT '1=已同步Milvus(对账用)',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_doc (doc_id, seq_no),
  KEY idx_block_type (block_type),
  KEY idx_vec_synced (vec_synced)
) COMMENT='知识切片元数据';

-- =====================================================================
-- 域 4:会话与引用(P-QA-*、M-S-*、M-T-*)
-- =====================================================================

CREATE TABLE chat_session (
  session_id    VARCHAR(64) PRIMARY KEY COMMENT '会话ID',
  user_id       BIGINT COMMENT '用户ID,访客为NULL',
  visitor_id    VARCHAR(64) COMMENT '访客设备指纹(免登录渠道)',
  channel_id    BIGINT NOT NULL COMMENT '来源渠道',
  agent_id      BIGINT COMMENT '当前助手',
  role_snapshot VARCHAR(64) NOT NULL DEFAULT '访客' COMMENT '会话角色快照(P-B-07)',
  device        VARCHAR(16) NOT NULL DEFAULT '' COMMENT '访问设备(M-T-03)：pc/mobile/tablet/other,空=未采集',
  status        TINYINT NOT NULL DEFAULT 1 COMMENT '1AI会话 2转人工中 3已结束',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_user (user_id),
  KEY idx_channel_agent (channel_id, agent_id),
  KEY idx_created (created_at)
) COMMENT='聊天会话';

-- 聊天消息(回答含富文本与AI标识,P-B-14;日志检索M-S-01)
CREATE TABLE chat_message (
  msg_id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '消息ID',
  session_id    VARCHAR(64) NOT NULL,
  msg_role      TINYINT NOT NULL COMMENT '1用户 2AI 3客服坐席 4系统',
  content       MEDIUMTEXT NOT NULL COMMENT '消息内容',
  content_type  VARCHAR(16) NOT NULL DEFAULT 'text' COMMENT 'text/voice/image/card',
  asr_text      TEXT COMMENT '语音转写文本(ASR)',
  trace_id      VARCHAR(64) NOT NULL DEFAULT '' COMMENT '全链路Trace(§4.4)',
  sentiment     TINYINT COMMENT '情绪标签(M-S-02):0正常 1负面',
  confidence    DECIMAL(5,4) COMMENT '回答置信度(M-S-07 未识别判定)',
  refused       TINYINT NOT NULL DEFAULT 0 COMMENT 'AI消息是否拒答/未答(M-T-01),用户消息恒0',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_session (session_id),
  KEY idx_trace (trace_id),
  KEY idx_created (created_at)
) COMMENT='聊天消息';

-- 回答引用(知识点标注 P-QA-04、热门知识统计 M-T-05)
CREATE TABLE answer_citation (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  msg_id      BIGINT NOT NULL COMMENT 'AI消息ID',
  chunk_id    VARCHAR(64) NOT NULL COMMENT '引用切片',
  doc_id      BIGINT NOT NULL,
  similarity  DECIMAL(5,4) COMMENT '引用相似度',
  KEY idx_msg (msg_id),
  KEY idx_chunk (chunk_id),
  KEY idx_doc (doc_id)
) COMMENT='回答引用记录';

-- =====================================================================
-- 域 5:在线客服与留言(P-B-09/10、M-S-03/04)
-- =====================================================================

-- 客服分组(任务4.6/M-SYS-04:按渠道匹配,空channel_ids=全部渠道)
CREATE TABLE support_group (
  group_id     BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '分组ID',
  group_name   VARCHAR(64) NOT NULL COMMENT '分组名称',
  channel_ids  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '渠道编码(逗号分隔),空=全部',
  description  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '分组说明',
  status       TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_support_group_name (group_name)
) COMMENT='客服分组';

-- 客服坐席(任务4.6/M-SYS-04·05)
CREATE TABLE agent_seat (
  seat_id      BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '坐席ID',
  user_id      BIGINT NOT NULL DEFAULT 0 COMMENT '坐席关联用户,0=演示/未绑定',
  user_no      VARCHAR(64) NOT NULL DEFAULT '' COMMENT '坐席学工号(绑定登录账号)',
  seat_name    VARCHAR(64) NOT NULL COMMENT '坐席昵称',
  group_id     BIGINT COMMENT '所属客服分组',
  channel_ids  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '服务的渠道组(逗号分隔,M-SYS-04)',
  max_sessions INT NOT NULL DEFAULT 5 COMMENT '最大并发接待数',
  online_flag  TINYINT NOT NULL DEFAULT 0 COMMENT '1在线 0离线',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_seat_group (group_id),
  KEY idx_seat_user_no (user_no)
) COMMENT='客服坐席';

-- 转人工工单(任务4.6/P-B-09～13;access_key_hash只存SHA-256)
CREATE TABLE ticket (
  ticket_id       BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '工单ID',
  ticket_no       VARCHAR(32) NOT NULL DEFAULT '' COMMENT '工单号',
  session_id      VARCHAR(64) NOT NULL COMMENT '来源会话',
  channel_id      BIGINT NOT NULL DEFAULT 0 COMMENT '来源渠道ID',
  channel_code    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源渠道编码',
  user_id         BIGINT COMMENT '发起用户,访客NULL',
  user_no         VARCHAR(64) NOT NULL DEFAULT '' COMMENT '发起用户学工号',
  user_name       VARCHAR(64) NOT NULL DEFAULT '' COMMENT '发起用户显示名',
  role            VARCHAR(32) NOT NULL DEFAULT '' COMMENT '用户角色',
  seat_id         BIGINT COMMENT '分配坐席',
  group_id        BIGINT COMMENT '客服分组',
  ticket_state    TINYINT NOT NULL DEFAULT 1 COMMENT '1排队 2接待中 3离线留言 4已回复 5已关闭',
  sentiment       TINYINT NOT NULL DEFAULT 0 COMMENT '情绪:0正常 1负面',
  sentiment_label VARCHAR(16) NOT NULL DEFAULT '正常',
  subject         VARCHAR(255) NOT NULL DEFAULT '' COMMENT '问题摘要',
  access_key_hash VARCHAR(64) NOT NULL DEFAULT '' COMMENT '用户侧访问凭证SHA-256',
  accepted_at     DATETIME COMMENT '坐席接入时间',
  last_message_at DATETIME COMMENT '最后消息时间',
  closed_at       DATETIME,
  close_reason    VARCHAR(255) NOT NULL DEFAULT '',
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_state (ticket_state),
  KEY idx_seat (seat_id),
  KEY idx_session (session_id),
  KEY idx_ticket_no (ticket_no),
  KEY idx_ticket_group (group_id)
) COMMENT='转人工工单';

-- 工单消息(含客服离线回复→推送 P-B-10)
CREATE TABLE ticket_message (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  ticket_id   BIGINT NOT NULL,
  sender_type TINYINT NOT NULL COMMENT '1用户 2坐席 3系统',
  sender_name VARCHAR(64) NOT NULL DEFAULT '' COMMENT '发送人显示名',
  content     MEDIUMTEXT NOT NULL,
  sentiment   TINYINT NOT NULL DEFAULT 0 COMMENT '情绪:0正常 1负面',
  pushed_flag TINYINT NOT NULL DEFAULT 0 COMMENT '离线回复是否已推送',
  read_flag   TINYINT NOT NULL DEFAULT 0,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_ticket (ticket_id)
) COMMENT='工单消息';

-- =====================================================================
-- 域 6:导办事项(M-G-*)
-- =====================================================================

-- 办事事项库
CREATE TABLE gov_item (
  item_id       BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '事项ID',
  item_name     VARCHAR(255) NOT NULL COMMENT '事项名称',
  dept_id       BIGINT NOT NULL COMMENT '主管部门',
  hall_url      VARCHAR(512) NOT NULL DEFAULT '' COMMENT '一网通办直达URL',
  need_ticket   TINYINT NOT NULL DEFAULT 1 COMMENT '1需免登录票据(§3.3)',
  conditions    TEXT COMMENT '办理条件',
  materials     TEXT COMMENT '所需材料',
  keyword_tags  VARCHAR(512) DEFAULT '' COMMENT '检索关键词',
  visible_roles VARCHAR(255) NOT NULL DEFAULT '全部',
  status        TINYINT NOT NULL DEFAULT 1,
  effective_from DATETIME NULL COMMENT '生效开始,空=立即',
  effective_to   DATETIME NULL COMMENT '生效结束,空=长期',
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_dept (dept_id),
  FULLTEXT KEY ft_item (item_name, keyword_tags) WITH PARSER ngram
) COMMENT='导办事项';

-- 事项卡片配置(拖拽设计器 M-G-02)
CREATE TABLE gov_card_config (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  item_id     BIGINT NOT NULL,
  channel_id  BIGINT NOT NULL DEFAULT 0 COMMENT '0=全局默认',
  card_layout JSON COMMENT '卡片布局配置(设计器输出)',
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_item_channel (item_id, channel_id)
) COMMENT='事项卡片配置';

-- 免登录办理票据(P-DB-02):短TTL+一次性消费,身份只存服务端
CREATE TABLE gov_ticket (
  ticket_id   VARCHAR(64) PRIMARY KEY COMMENT '高熵随机票据',
  item_id     BIGINT NOT NULL,
  user_no     VARCHAR(64) NOT NULL DEFAULT '' COMMENT '学工号',
  user_name   VARCHAR(64) NOT NULL DEFAULT '' COMMENT '姓名/显示名',
  role        VARCHAR(32) NOT NULL DEFAULT '' COMMENT '兑换时身份',
  issued_at   DATETIME NOT NULL,
  expires_at  DATETIME NOT NULL,
  used_at     DATETIME NULL,
  KEY idx_expires (expires_at),
  KEY idx_item (item_id)
) COMMENT='导办免登录票据';

-- 门禁通行流水本地同步分析库(P-DQ-04/I-04):外部平台按时间窗同步后本地聚合
CREATE TABLE access_event (
  event_id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '流水ID',
  event_no        VARCHAR(64) NOT NULL DEFAULT '' COMMENT '上游流水号(幂等)',
  event_time      DATETIME NOT NULL COMMENT '通行时间',
  direction       TINYINT NOT NULL DEFAULT 1 COMMENT '1进入 0离开',
  campus_id       BIGINT NOT NULL DEFAULT 0 COMMENT '校区ID',
  campus_name     VARCHAR(64) NOT NULL DEFAULT '' COMMENT '校区名称',
  college_name    VARCHAR(128) NOT NULL DEFAULT '' COMMENT '学院/单位',
  gate_id         BIGINT NOT NULL DEFAULT 0 COMMENT '门禁点ID',
  gate_name       VARCHAR(64) NOT NULL DEFAULT '' COMMENT '门禁点名称',
  user_no         VARCHAR(64) NOT NULL DEFAULT '' COMMENT '学工号',
  user_name       VARCHAR(64) NOT NULL DEFAULT '' COMMENT '姓名',
  role            VARCHAR(32) NOT NULL DEFAULT '' COMMENT '身份角色',
  abnormal        TINYINT NOT NULL DEFAULT 0 COMMENT '1异常通行',
  abnormal_reason VARCHAR(64) NOT NULL DEFAULT '' COMMENT '异常原因',
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '同步入库时间',
  UNIQUE KEY uk_access_event_no (event_no),
  KEY idx_access_time (event_time),
  KEY idx_access_campus (campus_id, event_time),
  KEY idx_access_college (college_name),
  KEY idx_access_gate (gate_id),
  KEY idx_access_user (user_no),
  KEY idx_access_abnormal (abnormal)
) COMMENT='门禁通行流水本地分析库';

-- =====================================================================
-- 域 7:治理与审计(§4.4 网关、M-S-07、M-SYS-07、F-02)
-- =====================================================================

-- LLM 调用计量与配额(LLM统一网关写入,成本管控/统计M-T-*)
CREATE TABLE token_usage (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  trace_id     VARCHAR(64) NOT NULL DEFAULT '',
  channel_id   BIGINT COMMENT '渠道',
  agent_id     BIGINT COMMENT '助手',
  user_id      BIGINT COMMENT '用户',
  model_code   VARCHAR(64) NOT NULL COMMENT '实际路由到的模型',
  prompt_tokens     INT NOT NULL DEFAULT 0,
  completion_tokens INT NOT NULL DEFAULT 0,
  total_tokens      INT NOT NULL DEFAULT 0,
  latency_ms   INT COMMENT '总耗时',
  first_token_ms INT COMMENT '首token耗时(流式,P95<3s指标)',
  call_state   TINYINT NOT NULL DEFAULT 1 COMMENT '1成功 0失败 2降级 3限流拒绝',
  fallback_from VARCHAR(64) DEFAULT '' COMMENT '降级前模型',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_created (created_at),
  KEY idx_channel (channel_id, created_at),
  KEY idx_agent (agent_id, created_at),
  KEY idx_trace (trace_id)
) COMMENT='LLM调用计量';

-- 操作审计日志(管理后台全留痕,等保要求 M-SYS-07)
CREATE TABLE operation_log (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id      BIGINT COMMENT '操作人',
  action       VARCHAR(64) NOT NULL COMMENT '动作,如 KB_PUBLISH/AGENT_UPDATE',
  target_type  VARCHAR(32) NOT NULL COMMENT '对象类型',
  target_id    VARCHAR(64) NOT NULL COMMENT '对象ID',
  detail       JSON COMMENT '变更明细(前后值)',
  ip_addr      VARCHAR(64) DEFAULT '',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_user_time (user_id, created_at),
  KEY idx_action (action)
) COMMENT='操作审计日志';

-- 敏感词 / 关键词库(M-SYS-07)
CREATE TABLE sensitive_word (
  word_id      BIGINT PRIMARY KEY AUTO_INCREMENT,
  word         VARCHAR(128) NOT NULL COMMENT '词条',
  kind         VARCHAR(16) NOT NULL DEFAULT 'sensitive' COMMENT 'sensitive敏感词 / keyword关键词',
  category     VARCHAR(32) NOT NULL DEFAULT '自定义' COMMENT '分类',
  status       TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  remark       VARCHAR(255) NOT NULL DEFAULT '',
  created_by   BIGINT COMMENT '创建人',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_word (word),
  KEY idx_kind_status (kind, status)
) COMMENT='敏感词/关键词库';

-- 未识别问题池(置信度低→聚类→反哺,M-S-07 闭环)
CREATE TABLE unrecognized_question (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  session_id    VARCHAR(64),
  question      VARCHAR(1024) NOT NULL COMMENT '用户原话',
  cluster_id    BIGINT COMMENT '语义聚类簇',
  predict_dept  BIGINT COMMENT '预测归属部门',
  handle_state  TINYINT NOT NULL DEFAULT 0 COMMENT '0待处理 1已反哺 2已忽略',
  handler_id    BIGINT COMMENT '处理人',
  new_chunk_id  VARCHAR(64) COMMENT '反哺生成的切片',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  handled_at    DATETIME,
  -- 任务 3.11：聚合、归因与反哺追溯
  msg_id        BIGINT COMMENT '关联的 AI 消息 chat_message.msg_id',
  question_key  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '归一化问题（聚合键）',
  confidence    DECIMAL(5,4) COMMENT '回答置信度',
  source        VARCHAR(20) NOT NULL DEFAULT '' COMMENT 'weak_recall/refused/low_confidence',
  role_snapshot VARCHAR(32) NOT NULL DEFAULT '' COMMENT '提问者角色快照',
  channel_code  VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源渠道编码',
  agent_id      VARCHAR(64) NOT NULL DEFAULT '' COMMENT '回答问题时的助手 ID',
  dept_name     VARCHAR(64) NOT NULL DEFAULT '' COMMENT '归属部门/业务域（人工归类）',
  new_doc_id    BIGINT COMMENT '反哺生成的知识文档 doc_id',
  KEY idx_state (handle_state),
  KEY idx_cluster (cluster_id),
  KEY idx_question_key (question_key),
  KEY idx_source (source),
  KEY idx_created (created_at)
) COMMENT='未识别问题池';

-- 问答分享卡片(任务 4.9 / P-QA-07):创建时冻结问答快照
CREATE TABLE qa_share (
  share_id      VARCHAR(24) PRIMARY KEY COMMENT '公开分享ID(URL安全)',
  session_id    VARCHAR(64) NOT NULL COMMENT '来源会话',
  msg_id        BIGINT COMMENT '来源AI消息',
  question      VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '用户问题快照',
  answer        MEDIUMTEXT NOT NULL COMMENT '回答快照(纯文本/Markdown)',
  citations     JSON COMMENT '引用来源快照',
  agent_name    VARCHAR(128) NOT NULL DEFAULT '' COMMENT '回答助手名称',
  channel_code  VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源渠道',
  role_snapshot VARCHAR(64) NOT NULL DEFAULT '' COMMENT '提问者角色快照',
  views         INT NOT NULL DEFAULT 0 COMMENT '分享页打开次数',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_created (created_at),
  KEY idx_session (session_id)
) COMMENT='问答分享卡片';

-- 用户点赞/点踩反馈(任务 4.9 / P-QA-08、M-S-06)
CREATE TABLE qa_feedback (
  feedback_id   BIGINT PRIMARY KEY AUTO_INCREMENT,
  share_id      VARCHAR(24) NOT NULL DEFAULT '' COMMENT '来自分享页时记录',
  session_id    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源会话',
  msg_id        BIGINT COMMENT '来源AI消息',
  rating        TINYINT NOT NULL COMMENT '1点赞 -1点踩',
  reason_code   VARCHAR(32) NOT NULL DEFAULT '' COMMENT 'inaccurate/incomplete/irrelevant/other',
  reason        VARCHAR(512) NOT NULL DEFAULT '' COMMENT '补充说明',
  question      VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '问题快照',
  answer_excerpt VARCHAR(512) NOT NULL DEFAULT '' COMMENT '回答摘要',
  role_snapshot VARCHAR(64) NOT NULL DEFAULT '' COMMENT '提问者角色快照',
  channel_code  VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源渠道',
  handled       TINYINT NOT NULL DEFAULT 0 COMMENT '0待处理 1已处理',
  handle_note   VARCHAR(512) NOT NULL DEFAULT '' COMMENT '处理备注',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  handled_at    DATETIME,
  KEY idx_rating (rating),
  KEY idx_handled (handled),
  KEY idx_created (created_at)
) COMMENT='问答反馈';

-- 数据中台上报/同步任务(任务 4.2 / I-02、M-SYS-02)
CREATE TABLE data_center_job (
  job_id        BIGINT PRIMARY KEY AUTO_INCREMENT,
  job_type      VARCHAR(32) NOT NULL COMMENT 'report上报 / org_sync组织同步',
  mode          VARCHAR(32) NOT NULL DEFAULT '' COMMENT 'chatlog/stats/all 或 incremental/full',
  status        VARCHAR(16) NOT NULL DEFAULT 'running' COMMENT 'running/success/failed',
  started_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at   DATETIME,
  total         INT NOT NULL DEFAULT 0 COMMENT '本任务处理记录数',
  created_count INT NOT NULL DEFAULT 0,
  updated_count INT NOT NULL DEFAULT 0,
  skipped_count INT NOT NULL DEFAULT 0,
  failed_count  INT NOT NULL DEFAULT 0,
  checksum      VARCHAR(64) NOT NULL DEFAULT '' COMMENT '结果校验和',
  detail        JSON COMMENT '批次明细/同步计数',
  error         VARCHAR(512) NOT NULL DEFAULT '',
  KEY idx_dc_job_started (started_at),
  KEY idx_dc_job_type (job_type, status)
) COMMENT='数据中台上报/同步任务';

-- 上报批次(按 batchId 幂等;内容变化才升 revision)
CREATE TABLE data_center_report (
  report_id   BIGINT PRIMARY KEY AUTO_INCREMENT,
  batch_id    VARCHAR(64) NOT NULL COMMENT 'DC-CHATLOG-YYYYMMDD 等',
  kind        VARCHAR(16) NOT NULL DEFAULT 'chatlog' COMMENT 'chatlog/stats',
  report_date DATE NOT NULL,
  revision    INT NOT NULL DEFAULT 1,
  checksum    VARCHAR(64) NOT NULL DEFAULT '',
  status      VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/delivered/failed',
  rows_count  INT NOT NULL DEFAULT 0,
  payload     MEDIUMTEXT COMMENT 'nxz.data-center.report.v1 报文',
  error       VARCHAR(512) NOT NULL DEFAULT '',
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_dc_batch (batch_id),
  KEY idx_dc_report_date (report_date)
) COMMENT='数据中台上报批次';

-- 任务水位(上次上报/增量/全量日期与增量游标)
CREATE TABLE data_center_state (
  state_key   VARCHAR(64) PRIMARY KEY,
  state_value VARCHAR(1024) NOT NULL DEFAULT '',
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) COMMENT='数据中台任务水位';

-- =====================================================================
-- 数据中台对接(I-02):上行数据共享视图示例
-- 上报聊天日志时按期聚合导出,不暴露明细表
-- =====================================================================
CREATE VIEW v_chat_log_daily AS
SELECT DATE(m.created_at) AS stat_date,
       s.channel_id,
       s.agent_id,
       s.role_snapshot,
       COUNT(*) AS msg_count
FROM chat_message m
JOIN chat_session s ON s.session_id = m.session_id
WHERE m.msg_role IN (1, 2)
GROUP BY DATE(m.created_at), s.channel_id, s.agent_id, s.role_snapshot;
