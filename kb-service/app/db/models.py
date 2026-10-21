"""ORM 模型映射（任务 1.14）。

只映射服务实际读写的表（见 database/schema.sql），字段名与列名逐一对齐，
不做重命名，便于与 DDL/交付物《数据结构字典》直接对照。

状态机：kb_doc.status / kb_chunk.status：0 草稿 / 1 待审核 / 2 已发布 / 3 已下线。
枚举：kb_doc.ktype：policy / guide / faq / item / org（中文名见 KTYPE_NAME）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    DATE,
    DATETIME,
    FLOAT,
    INT,
    JSON,
    TEXT,
    VARCHAR,
    VARBINARY,
    BigInteger,
    DECIMAL,
    Index,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, TINYINT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DOC_STATUS = {0: "草稿", 1: "待审核", 2: "已发布", 3: "已下线"}

# 中文知识类型 ↔ 生产枚举（与 kb-engine/src/store/mysql.js 完全一致）
KTYPE_MAP = {
    "政策文件": "policy",
    "办事指南": "guide",
    "常见问题": "faq",
    "服务事项": "item",
    "组织机构": "org",
}
KTYPE_NAME = {v: k for k, v in KTYPE_MAP.items()}

# 索引/前端中的中文状态 → 数值状态码
STATUS_MAP = {"草稿": 0, "待审核": 1, "已审核": 2, "已发布": 2, "已下线": 3}
STATUS_NAME = ["草稿", "待审核", "已发布", "已下线"]

# chat_message.msg_role
MSG_ROLE_USER = 1
MSG_ROLE_AI = 2
MSG_ROLE_AGENT = 3
MSG_ROLE_SYSTEM = 4


class Base(DeclarativeBase):
    pass


class SysOrg(Base):
    """组织机构（知识库一级目录对应部门）。"""

    __tablename__ = "sys_org"

    org_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    parent_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    org_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    org_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    org_path: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    sort_no: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    synced_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_org_code", "org_code", unique=True),)


class KbCategory(Base):
    """知识分类（部门 × 知识类型）。"""

    __tablename__ = "kb_category"

    category_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dept_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ktype: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)
    category_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    parent_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sort_no: Mapped[int] = mapped_column(INT, nullable=False, default=0)


class KbPermission(Base):
    """知识库部门授权（任务 2.6 / M-K-03）。

    超管为部门联络员按「用户 × 部门 × 知识类型」分配操作位；
    ``dept_id=0`` 与 ``ktype='*'`` 表示全部部门 / 全部类型。
    """

    __tablename__ = "kb_permission"

    perm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dept_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ktype: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="*")
    can_upload: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    can_view: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    can_download: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    can_delete: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    can_create_sub: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_perm", "user_id", "dept_id", "ktype", unique=True),)


class KbDoc(Base):
    """知识文档。``doc_id`` 自增，切片编号 ``D{doc_id:03d}-C{seq:03d}`` 由此派生。"""

    __tablename__ = "kb_doc"

    doc_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_title: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    category_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dept_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ktype: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)
    file_format: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    file_key: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    file_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, default="")
    chunk_count: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    visible_roles: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="全部")
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    version_no: Mapped[int] = mapped_column(INT, nullable=False, default=1)
    source_type: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="manual")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    audited_by: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime | None] = mapped_column(DATETIME)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)


class KbChunk(Base):
    """知识切片元数据（向量不入本表）。"""

    __tablename__ = "kb_chunk"

    chunk_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    doc_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    seq_no: Mapped[int] = mapped_column(INT, nullable=False)
    block_type: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="para")
    title_path: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    char_count: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    page_no: Mapped[int | None] = mapped_column(INT)
    image_keys: Mapped[dict | None] = mapped_column(JSON)
    visible_roles: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="全部")
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=2)
    vec_synced: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)


class KbChunkVecProto(Base):
    """切片向量（原型用 JSON 列模拟 Milvus 集合 kb_chunk_vec）。

    生产环境应替换为 Milvus；本表仅用于单机演示与离线交付。
    """

    __tablename__ = "kb_chunk_vec_proto"

    chunk_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    embedding: Mapped[list] = mapped_column(JSON, nullable=False)
    dim: Mapped[int] = mapped_column(INT, nullable=False, default=512)
    model: Mapped[str] = mapped_column(VARCHAR(128), nullable=False, default="")
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)


class ChatSession(Base):
    __tablename__ = "chat_session"

    session_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    visitor_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    role_snapshot: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="访客")
    device: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="")
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)


class ChatMessage(Base):
    __tablename__ = "chat_message"

    msg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    msg_role: Mapped[int] = mapped_column(TINYINT, nullable=False)
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    content_type: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="text")
    asr_text: Mapped[str | None] = mapped_column(TEXT)
    trace_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    sentiment: Mapped[int | None] = mapped_column(TINYINT)
    confidence: Mapped[float | None] = mapped_column(DECIMAL(5, 4))
    refused: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)


class AnswerCitation(Base):
    __tablename__ = "answer_citation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    msg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    doc_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    similarity: Mapped[float | None] = mapped_column(DECIMAL(5, 4))


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    model_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(INT)
    first_token_ms: Mapped[int | None] = mapped_column(INT)
    call_state: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    fallback_from: Mapped[str | None] = mapped_column(VARCHAR(64), default="")
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)


class UnrecognizedQuestion(Base):
    __tablename__ = "unrecognized_question"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    question: Mapped[str] = mapped_column(VARCHAR(1024), nullable=False)
    cluster_id: Mapped[int | None] = mapped_column(BigInteger)
    predict_dept: Mapped[int | None] = mapped_column(BigInteger)
    handle_state: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    handler_id: Mapped[int | None] = mapped_column(BigInteger)
    new_chunk_id: Mapped[str | None] = mapped_column(VARCHAR(64))
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    handled_at: Mapped[datetime | None] = mapped_column(DATETIME)
    # ---- 任务 3.11：聚合、归因与反哺追溯 ----
    msg_id: Mapped[int | None] = mapped_column(BigInteger)
    question_key: Mapped[str] = mapped_column(VARCHAR(255), default="")
    confidence: Mapped[float | None] = mapped_column(DECIMAL(5, 4))
    source: Mapped[str] = mapped_column(VARCHAR(20), default="")
    role_snapshot: Mapped[str] = mapped_column(VARCHAR(32), default="")
    channel_code: Mapped[str] = mapped_column(VARCHAR(64), default="")
    agent_id: Mapped[str] = mapped_column(VARCHAR(64), default="")
    dept_name: Mapped[str] = mapped_column(VARCHAR(64), default="")
    new_doc_id: Mapped[int | None] = mapped_column(BigInteger)


class QaShare(Base):
    """问答分享卡片快照（任务 4.9 / P-QA-07）。

    分享内容在创建时冻结：原会话后续变化不影响已发出的链接。
    """

    __tablename__ = "qa_share"

    share_id: Mapped[str] = mapped_column(VARCHAR(24), primary_key=True)
    session_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    msg_id: Mapped[int | None] = mapped_column(BigInteger)
    question: Mapped[str] = mapped_column(VARCHAR(1024), nullable=False, default="")
    answer: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    citations: Mapped[list | None] = mapped_column(JSON)
    agent_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False, default="")
    channel_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    role_snapshot: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    views: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)


class QaFeedback(Base):
    """用户点赞/点踩反馈（任务 4.9 / P-QA-08、M-S-06）。"""

    __tablename__ = "qa_feedback"

    feedback_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    share_id: Mapped[str] = mapped_column(VARCHAR(24), nullable=False, default="")
    session_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    msg_id: Mapped[int | None] = mapped_column(BigInteger)
    rating: Mapped[int] = mapped_column(TINYINT, nullable=False)
    reason_code: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="")
    reason: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    question: Mapped[str] = mapped_column(VARCHAR(1024), nullable=False, default="")
    answer_excerpt: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    role_snapshot: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    channel_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    handled: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    handle_note: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    handled_at: Mapped[datetime | None] = mapped_column(DATETIME)


class OperationLog(Base):
    __tablename__ = "operation_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    target_type: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)
    target_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)
    ip_addr: Mapped[str | None] = mapped_column(VARCHAR(64), default="")
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)


class SensitiveWord(Base):
    __tablename__ = "sensitive_word"

    word_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    word: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    kind: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="sensitive")
    category: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="自定义")
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    remark: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)


class Agent(Base):
    __tablename__ = "agent"

    agent_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    agent_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    agent_type: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="chat")
    system_prompt: Mapped[str | None] = mapped_column(TEXT)
    model_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="default")
    temperature: Mapped[float | None] = mapped_column(DECIMAL(3, 2))
    top_p: Mapped[float | None] = mapped_column(DECIMAL(3, 2))
    max_tokens: Mapped[int | None] = mapped_column(INT)
    visible_roles: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="全部")
    icon_url: Mapped[str | None] = mapped_column(VARCHAR(255), default="")
    # 任务 2.2（M-A-03 助手基础配置）：描述 / 流式启停 / 风控话术 / 兜底话术
    description: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    stream_enabled: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    risk_reply: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    fallback_reply: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    # 任务 2.3（M-A-05 检索配置）：topK/rerankTopn/minScore/depts/ktypes。
    # 可空且无默认值——MySQL 的 JSON 列不支持「非表达式默认」，且**缺省即沿用全局**
    # 正是我们要的语义（NULL 与 {} 等价）。
    retrieval_config: Mapped[dict | None] = mapped_column(JSON)
    # 任务 3.8（P-QA-06 多智能体切换）：场景引导问题，最多 6 条、单条 ≤120 字。
    # 可空且无默认值——NULL 与 [] 等价，表示「不单独推荐」，门户回退到渠道/FAQ 话术。
    guide_questions: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_agent_code", "agent_code", unique=True),)


class LlmModel(Base):
    """大模型配置（任务 2.4 / M-A-06·M-SYS-06·C-01）。

    ``model_code = "default"`` 是**保留编码**，表示「走 ``.env`` 全局配置」，
    本表不存该行——这样「未配置模型」与批1 行为由机制保证一致。

    ``api_key_enc`` 为 AES-256-GCM 密文（:mod:`app.security.crypto`），
    对外接口只回显掩码，明文仅 :mod:`app.core.llm_router` 解密时可见。
    """

    __tablename__ = "llm_model"

    model_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    model_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    vendor: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)
    api_base: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    remote_model: Mapped[str] = mapped_column(VARCHAR(128), nullable=False, default="")
    api_key_enc: Mapped[bytes | None] = mapped_column(VARBINARY(512))
    timeout: Mapped[float | None] = mapped_column(FLOAT)
    is_local: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    sort_no: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    remark: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_model_code", "model_code", unique=True),)


class Channel(Base):
    __tablename__ = "channel"

    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    channel_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    default_agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    welcome_text: Mapped[str | None] = mapped_column(VARCHAR(1024), default="")
    theme_config: Mapped[dict | None] = mapped_column(JSON)
    route_config: Mapped[dict | None] = mapped_column(JSON)
    embed_url: Mapped[str | None] = mapped_column(VARCHAR(255), default="")
    embed_snippet: Mapped[str | None] = mapped_column(TEXT)
    need_auth: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    copy_of: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_channel_code", "channel_code", unique=True),)


class ChannelAgent(Base):
    """渠道内可见的智能体集合（任务 3.1）。

    ``default_agent_id`` 是渠道主助手，恒在集合内；本表存其余可切换助手与排序。
    与 ``channel`` 无外键（DDL 口径一致），删除渠道时由仓储层清理关联行。
    """

    __tablename__ = "channel_agent"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sort_no: Mapped[int] = mapped_column(INT, nullable=False, default=0)

    __table_args__ = (Index("uk_channel_agent", "channel_id", "agent_id", unique=True),)


class App(Base):
    """智能体广场应用（任务 3.3 / M-C-05·06）。

    ``source_type`` 为 ``agent`` 时 ``agent_id`` 指向 :class:`Agent`，为 ``link``
    时 ``link_url`` 指向外部应用；``visible_roles`` 逗号分隔，``全部`` 表示不限角色。
    """

    __tablename__ = "app"

    app_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    app_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    source_type: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="agent")
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    link_url: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    category: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="")
    description: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    icon_url: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    sort_no: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    visible_roles: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="全部")
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("idx_app_sort", "sort_no", "app_id"),)


class GovItem(Base):
    """一网通办导办事项（任务 4.5 / P-DB-01、M-G-01）。

    ``effective_from`` / ``effective_to`` 为空表示不限；启停与生效时段由本地
    管理端维护，事项同步只更新业务字段，避免上游覆盖本地运营决策。
    """

    __tablename__ = "gov_item"

    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_name: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    dept_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    hall_url: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    need_ticket: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    conditions: Mapped[str | None] = mapped_column(TEXT)
    materials: Mapped[str | None] = mapped_column(TEXT)
    keyword_tags: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    visible_roles: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="全部")
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    effective_from: Mapped[datetime | None] = mapped_column(DATETIME)
    effective_to: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (
        Index("idx_gov_item_dept", "dept_id"),
        Index("idx_gov_item_status", "status"),
    )


class GovTicket(Base):
    """免登录办理票据（任务 4.5 / P-DB-02）。

    票据是服务端高熵随机串，身份只存在服务端；短 TTL + 一次性消费，
    URL 中不携带学号等明文身份。
    """

    __tablename__ = "gov_ticket"

    ticket_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    item_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_no: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    user_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    role: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="")
    issued_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (
        Index("idx_gov_ticket_expires", "expires_at"),
        Index("idx_gov_ticket_item", "item_id"),
    )


class SupportGroup(Base):
    """客服分组（任务 4.6 / M-SYS-04）。

    ``channel_ids`` 存渠道编码（逗号分隔），空表示「全部渠道」；转人工时按
    工单来源渠道匹配分组，组内再挑在线且未满负载的坐席。
    """

    __tablename__ = "support_group"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    channel_ids: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_support_group_name", "group_name", unique=True),)


class AgentSeat(Base):
    """客服坐席（任务 4.6 / M-SYS-04·05）。

    坐席归属一个分组；``online_flag`` 是接待开关（工作台上下线或 WebSocket
    连接状态），``max_sessions`` 限制同时接待量。
    """

    __tablename__ = "agent_seat"

    seat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    user_no: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    seat_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_ids: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    max_sessions: Mapped[int] = mapped_column(INT, nullable=False, default=5)
    online_flag: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)


class Ticket(Base):
    """转人工工单（任务 4.6 / P-B-09～13、M-S-02～06）。

    ``access_key_hash`` 是用户侧查询凭证的 SHA-256；原始凭证只在创建工单时
    返回一次，避免顺序工单号被枚举后读到他人会话。
    """

    __tablename__ = "ticket"

    ticket_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="")
    session_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    channel_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    user_no: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    user_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    role: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="")
    seat_id: Mapped[int | None] = mapped_column(BigInteger)
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    ticket_state: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    sentiment: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    sentiment_label: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="正常")
    subject: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    access_key_hash: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    accepted_at: Mapped[datetime | None] = mapped_column(DATETIME)
    last_message_at: Mapped[datetime | None] = mapped_column(DATETIME)
    closed_at: Mapped[datetime | None] = mapped_column(DATETIME)
    close_reason: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (
        Index("idx_ticket_state", "ticket_state"),
        Index("idx_ticket_seat", "seat_id"),
        Index("idx_ticket_session", "session_id"),
    )


class TicketMessage(Base):
    """工单消息（任务 4.6 / P-B-10·13、M-S-04·05）。

    ``sender_type``：1 用户 / 2 坐席 / 3 系统。``pushed_flag`` 记录坐席消息是否
    已被用户端拉取（离线回复推送），``read_flag`` 记录用户消息是否已被坐席读取。
    """

    __tablename__ = "ticket_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_type: Mapped[int] = mapped_column(TINYINT, nullable=False)
    sender_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    sentiment: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    pushed_flag: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    read_flag: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("idx_ticket_message_ticket", "ticket_id"),)


class AccessEvent(Base):
    """门禁通行流水本地同步库（任务 4.4 / P-DQ-04、I-04）。

    上游门禁平台按时间窗同步到本地后，所有多维分析、下钻与排行都在本地完成，
    避免每次查询都压到门禁平台。``event_no`` 为上游流水号（唯一），重复同步
    幂等；``abnormal`` 由上游标记或本地夜间时段规则补充。
    """

    __tablename__ = "access_event"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_no: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    event_time: Mapped[datetime] = mapped_column(DATETIME, nullable=False)
    direction: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    campus_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    campus_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    college_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False, default="")
    gate_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gate_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    user_no: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    user_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    role: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="")
    abnormal: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    abnormal_reason: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (
        Index("uk_access_event_no", "event_no", unique=True),
        Index("idx_access_time", "event_time"),
        Index("idx_access_campus", "campus_id", "event_time"),
        Index("idx_access_college", "college_name"),
        Index("idx_access_gate", "gate_id"),
        Index("idx_access_user", "user_no"),
        Index("idx_access_abnormal", "abnormal"),
    )


class SysRole(Base):
    """系统角色（任务 1.13）：``role_type`` 1=身份角色 / 2=管理角色。"""

    __tablename__ = "sys_role"

    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    role_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    role_type: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    is_public: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    remark: Mapped[str | None] = mapped_column(VARCHAR(255), default="")

    __table_args__ = (Index("uk_role_code", "role_code", unique=True),)


class SysUser(Base):
    """用户（任务 1.13）。权威源=学校统一身份认证，本地表作缓存与扩展。

    ``id_card_enc`` / ``phone_enc`` 为 AES 密文（任务 1.12），本层只做透传，不解密。
    """

    __tablename__ = "sys_user"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_no: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    real_name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    id_card_enc: Mapped[bytes | None] = mapped_column(VARBINARY(128))
    phone_enc: Mapped[bytes | None] = mapped_column(VARBINARY(64))
    email: Mapped[str | None] = mapped_column(VARCHAR(128), default="")
    #: 管理账号密码哈希（任务 2.9，PBKDF2）；NULL = 沿用内部演示免密登录
    password_hash: Mapped[str | None] = mapped_column(VARCHAR(255))
    # 1 本科生 / 2 研究生 / 3 教职工 / 4 离退休 / 5 访客
    id_type: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    org_id: Mapped[int | None] = mapped_column(BigInteger)
    user_status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DATETIME)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_user_no", "user_no", unique=True),)


class SysUserRole(Base):
    """用户-角色关联（任务 1.13，一人可多角色）。"""

    __tablename__ = "sys_user_role"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (Index("uk_user_role", "user_id", "role_id", unique=True),)


class SysRolePermission(Base):
    """角色-权限点关联（任务 2.9，菜单/按钮级）。

    管理员角色（``role_code='admin'``）恒有全部权限，不落行；
    其余管理角色按本表配置。
    """

    __tablename__ = "sys_role_permission"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    perm_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_role_perm", "role_id", "perm_code", unique=True),)


class DataCenterJob(Base):
    """数据中台上报 / 组织同步任务（任务 4.2 / I-02、M-SYS-02）。"""

    __tablename__ = "data_center_job"

    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="report")
    mode: Mapped[str] = mapped_column(VARCHAR(32), nullable=False, default="")
    status: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="running")
    started_at: Mapped[datetime | None] = mapped_column(DATETIME)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME)
    total: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    checksum: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    detail: Mapped[str | None] = mapped_column(JSON)
    error: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")

    __table_args__ = (
        Index("idx_dc_job_started", "started_at"),
        Index("idx_dc_job_type", "job_type", "status"),
    )


class DataCenterReport(Base):
    """数据中台上报批次（按 batchId 幂等，内容变化才升 revision）。"""

    __tablename__ = "data_center_report"

    report_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    kind: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="chatlog")
    report_date: Mapped[datetime] = mapped_column(DATE, nullable=False)
    revision: Mapped[int] = mapped_column(INT, nullable=False, default=1)
    checksum: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="pending")
    rows_count: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    payload: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    error: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (
        Index("uk_dc_batch", "batch_id", unique=True),
        Index("idx_dc_report_date", "report_date"),
    )


class DataCenterState(Base):
    """数据中台任务水位（上次上报/增量/全量日期与增量游标）。"""

    __tablename__ = "data_center_state"

    state_key: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    state_value: Mapped[str] = mapped_column(VARCHAR(1024), nullable=False, default="")
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)


class IntegrationLocalModel(Base):
    """本地 OpenAI 兼容大模型（任务 4.8 / C-05）。

    ``api_key_enc`` 为 AES-256-GCM 密文，接口只回 ``hasKey``。
    ``is_active=1`` 表示 ``LLM_PROVIDER=local`` 时对话默认使用该端点，全表至多一条。
    """

    __tablename__ = "integration_local_model"

    local_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    model_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    base_url: Mapped[str] = mapped_column(VARCHAR(512), nullable=False)
    remote_model: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    api_key_enc: Mapped[bytes | None] = mapped_column(VARBINARY(1024))
    timeout: Mapped[float] = mapped_column(FLOAT, nullable=False, default=30)
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    is_active: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    remark: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    last_test_at: Mapped[datetime | None] = mapped_column(DATETIME)
    last_test_state: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="")
    last_test_error: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    last_latency_ms: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_ilm_code", "model_code", unique=True),)


class IntegrationMcpServer(Base):
    """MCP 服务配置（任务 4.8 / M-A-07）。``headers_enc`` 存加密后的 JSON。"""

    __tablename__ = "integration_mcp_server"

    server_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    server_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    server_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    transport: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="http")
    endpoint: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    command: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    args_json: Mapped[list | None] = mapped_column(JSON)
    headers_enc: Mapped[bytes | None] = mapped_column(VARBINARY(4096))
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    remark: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    last_test_at: Mapped[datetime | None] = mapped_column(DATETIME)
    last_test_state: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="")
    last_test_error: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    last_latency_ms: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    tools_json: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_imcp_code", "server_code", unique=True),)


class IntegrationAgentApp(Base):
    """第三方智能体应用（任务 4.8 / M-A-10）：Dify / Coze / 自定义 Webhook。"""

    __tablename__ = "integration_agent_app"

    app_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    app_code: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    app_name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    kind: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="dify")
    base_url: Mapped[str] = mapped_column(VARCHAR(512), nullable=False)
    remote_app_id: Mapped[str] = mapped_column(VARCHAR(128), nullable=False, default="")
    token_enc: Mapped[bytes | None] = mapped_column(VARBINARY(1024))
    status: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    remark: Mapped[str] = mapped_column(VARCHAR(255), nullable=False, default="")
    last_test_at: Mapped[datetime | None] = mapped_column(DATETIME)
    last_test_state: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="")
    last_test_error: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    last_latency_ms: Mapped[int] = mapped_column(INT, nullable=False, default=0)
    last_reply: Mapped[str] = mapped_column(VARCHAR(512), nullable=False, default="")
    created_at: Mapped[datetime | None] = mapped_column(DATETIME)
    updated_at: Mapped[datetime | None] = mapped_column(DATETIME)

    __table_args__ = (Index("uk_iagent_code", "app_code", unique=True),)
