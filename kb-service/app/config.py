"""应用配置：统一加载环境变量，供各模块引用。

对应任务 1.1（工程骨架与基础）。参数默认值与 kb-engine 原型保持一致，
避免迁移后检索行为漂移（RRF K=60、topK=5、切片 400/60/40）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# 需按 BASE_DIR 解析的相对路径字段（.env 里常写成 ./models 这类相对形式）
_PATH_FIELDS = ("store_path", "model_dir", "knowledge_dir", "upload_dir")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 运行 ----
    app_name: str = "kb-service"
    app_version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False
    #: 渠道 URL / 嵌入代码使用的对外基址；留空则取当前请求的 base_url。
    #: 生产经 Nginx 反代时建议显式配置，避免生成内网 127.0.0.1 地址。
    public_base_url: str = ""

    # ---- 跨域（任务 3.9 嵌入 SDK）----
    #: 逗号分隔的来源白名单；``*`` 表示任意来源（此时凭证必须关闭）。
    cors_allow_origins: str = "*"
    #: 仅在使用 Cookie 携带身份时需要开启；Auth 头 + 通配来源无需凭证。
    cors_allow_credentials: bool = False
    #: 跨域可读的响应头（导出截断标记、AI 标识等）
    cors_expose_headers: str = (
        "Content-Disposition,X-Export-Truncated,"
        "X-AI-Generated,X-AI-Label,X-AI-Produce-Id,X-AI-Label-Version"
    )

    # ---- 存储：mysql | json ----
    kb_store: str = "json"
    store_path: Path = BASE_DIR / "data" / "store.json"

    # ---- MySQL ----
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "nengxiaozhi"
    db_charset: str = "utf8mb4"

    # ---- LLM ----
    # gateway（默认，唯一模型来源=网关）| local（直连任务 4.8 启用的本地 OpenAI 兼容模型）
    llm_provider: str = "gateway"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = ""
    llm_temperature: float = 0.3
    llm_max_tokens: int = 1024
    llm_timeout: float = 60.0
    llm_mock: bool = False

    # ---- 集成中心（任务 4.8：本地模型 / MCP / 第三方智能体）----
    # mock = 内置确定性结果（离线联调）；live = 真实请求外部服务
    integration_test_provider: str = "mock"
    integration_test_timeout: float = 20.0
    integration_mcp_provider: str = "mock"
    integration_agent_provider: str = "mock"

    # ---- 向量与检索 ----
    model_dir: Path = BASE_DIR / "models"
    # 与 kb-engine 对齐：Node 版使用 Xenova 的 ONNX 版 bge-small-zh-v1.5（512 维，fp32）
    embed_model: str = "Xenova/bge-small-zh-v1.5"
    embed_dim: int = 512
    # HuggingFace 镜像（离线交付时可指向内网镜像或置空禁用下载）
    hf_endpoint: str = "https://hf-mirror.com"
    rerank_model: str = "onnx-community/bge-reranker-v2-m3-ONNX"
    rerank_enabled: bool = True
    rerank_topn: int = 30
    top_k: int = 5
    rrf_k: int = 60
    retrieve_mode: str = "hybrid"

    # ---- 切片 ----
    chunk_max: int = 400
    chunk_carry: int = 60
    chunk_min: int = 40

    # ---- 解析扩展（任务 1.10：12 格式 + OCR + 原图）----
    # OCR 引擎：auto（rapidocr → tesseract 依次探测）/ rapidocr / tesseract / none
    ocr_engine: str = "auto"
    # tesseract 语言包（中文需 chi_sim；rapidocr 内置中英，忽略此项）
    ocr_lang: str = "chi_sim+eng"
    # 送 OCR 前把图片长边压到该值以内（大图会显著拖慢推理）
    ocr_max_side: int = 2000
    # 显式指定 tesseract 可执行文件（未加入 PATH 时）
    tesseract_bin: str = ""
    # 显式指定 LibreOffice soffice（.doc/.ppt 转换用；未加入 PATH 时）
    soffice_bin: str = ""
    # soffice 单次转换超时（秒）
    soffice_timeout: float = 120.0

    # ---- 会话（>=10 轮）----
    history_max_messages: int = 20

    # ---- 未识别问题池（任务 3.11）----
    #: 低于该置信度（且未拒答）的回答自动入池
    unrecognized_confidence_threshold: float = 0.35
    #: 语义聚类余弦阈值：同一问题簇的最小相似度
    unrecognized_cluster_threshold: float = 0.82
    #: 单次列表最多参与聚类的问题组数（防止大池子拖慢管理页）
    unrecognized_cluster_limit: int = 200
    #: 入池开关（关闭后既不写库也不新增记录）
    unrecognized_enabled: bool = True

    # ---- 统一身份认证（任务 4.1）----
    #: CAS 服务端地址；留空 = 使用内置 mock CAS（联调/演示，外部接口就绪后填真实地址）
    cas_server_url: str = ""
    cas_login_path: str = "/cas/login"
    cas_validate_path: str = "/cas/serviceValidate"
    #: 回调 service 的对外基址；留空依次取 PUBLIC_BASE_URL / 当前请求地址
    cas_service_base: str = ""
    cas_timeout: float = 10.0
    #: 已登记账号是否信任 CAS 返回的角色属性（默认关闭：以本地 sys_user 绑定为准）。
    #: 未登记账号始终按其身份属性取普通角色；管理角色永不采纳上游属性。
    cas_trust_role_attribute: bool = False
    #: CAS 角色属性映射（上游编码:本地角色名，逗号分隔）
    cas_role_map: str = (
        "student:本科生,undergraduate:本科生,graduate:研究生,"
        "teacher:教职工,staff:教职工,retired:离退休人员,visitor:访客"
    )

    # ---- 语音识别（任务 4.7）----
    #: mock = 内置确定性转写（联调/演示）；openai = OpenAI 兼容 /audio/transcriptions
    asr_provider: str = "mock"
    asr_enabled: bool = True
    asr_base_url: str = "https://api.openai.com/v1"
    asr_api_key: str = ""
    asr_model: str = "whisper-1"
    #: 识别语言提示；留空交由模型自动判断
    asr_language: str = "zh"
    asr_timeout: float = 45.0
    asr_max_bytes: int = 10 * 1024 * 1024
    #: 前端录音上限（秒），仅下发用于提示与自动停止
    asr_max_seconds: int = 60
    #: mock 模式固定返回的文本（便于端到端验收复现）
    asr_mock_text: str = "校园网怎么开通？"

    # ---- 导办事项与免登录票据（任务 4.5）----
    #: 总开关：关闭后对话不再推荐事项，公开事项接口返回空；管理端仍可维护数据
    gov_enabled: bool = True
    #: mock = 内置演示事项（联调/演示）；http = 一网通办事项同步接口
    gov_sync_provider: str = "mock"
    gov_sync_url: str = ""
    gov_sync_token: str = ""
    gov_sync_timeout: float = 20.0
    #: 对话推荐卡片上限
    gov_recommend_limit: int = 3
    #: 免登录票据有效期（秒），短 TTL + 一次性使用
    gov_ticket_ttl_seconds: int = 120

    # ---- 问数服务（任务 4.3）----
    #: 总开关：关闭后对话不再附加个人数据卡片，接口返回 503
    dq_enabled: bool = True
    #: mock = 内置确定性数据（联调/演示）；http = 教务 / 一卡通真实接口
    dq_provider: str = "mock"
    dq_jw_base_url: str = ""
    dq_jw_token: str = ""
    dq_card_base_url: str = ""
    dq_card_token: str = ""
    dq_timeout: float = 15.0
    #: 单次返回的最大明细行数
    dq_max_rows: int = 50

    # ---- 通行查询智能体（任务 4.4）----
    #: mock = 内置确定性门禁流水（联调/演示）；http = 门禁数据平台同步接口
    access_sync_provider: str = "mock"
    access_sync_url: str = ""
    access_sync_token: str = ""
    access_sync_timeout: float = 30.0
    #: 同步最近天数；分析默认窗口同此值
    access_sync_days: int = 30
    #: 单次分析最大跨度（天）与单次同步最大流水数
    access_max_range_days: int = 90
    access_max_events: int = 200000
    #: 明细 CSV 单次导出上限
    access_export_limit: int = 10000

    # ---- 客服体系（任务 4.6）----
    #: 总开关：关闭后转人工与工作台接口返回 503
    support_enabled: bool = True
    #: 空库时自动写入演示分组与坐席（方便联调；已有数据不会覆盖）
    support_auto_seed: bool = True
    #: WebSocket 心跳间隔（秒）
    support_ws_heartbeat: int = 20
    #: 单条工单消息最大长度
    support_max_message: int = 2000

    # ---- 数据中台（任务 4.2 / I-02、M-SYS-02）----
    #: 总开关：关闭后上报/同步接口返回 503
    data_center_enabled: bool = True
    #: mock = 本地确定性组织数据 + 落盘收件箱；http = 数据中台接口
    data_center_provider: str = "mock"
    data_center_base_url: str = ""
    data_center_token: str = ""
    data_center_timeout: float = 20.0
    #: 组织导出与上报路径（相对 base_url）
    data_center_org_path: str = "/org/export"
    data_center_report_path: str = "/report"
    #: 聊天日志是否携带脱敏后的问答正文（默认不携带，只上报元数据与统计）
    data_center_include_content: bool = False
    #: 单批上报最大记录数
    data_center_max_records: int = 5000
    #: 定时任务：统计上报 / 组织增量 / 组织全量的触发时刻
    data_center_report_hour: int = 2
    data_center_incremental_hour: int = 3
    data_center_full_weekday: int = 6
    data_center_full_hour: int = 4
    #: 进程内调度线程（默认关；生产置 1，或改用外部调度器调用接口）
    data_center_scheduler_enabled: bool = False
    data_center_scheduler_interval: int = 60

    # ---- 安全 ----
    aes_key: str = ""
    jwt_secret: str = ""
    jwt_expire_minutes: int = 720
    rate_limit_per_minute: int = 60
    # 鉴权开关（任务 1.13）：默认关闭，便于开发/演示；生产必须置 true
    auth_enabled: bool = False
    # 安全基线（任务 1.12）
    rate_limit_enabled: bool = True
    sensitive_check_enabled: bool = True
    # 外部敏感词表（每行一词，# 为注释）；为空则只用内置意图词表
    sensitive_words_file: str = ""

    # ---- 目录 ----
    knowledge_dir: Path = BASE_DIR.parent / "knowledge-base"
    upload_dir: Path = BASE_DIR / "uploads"

    @field_validator(*_PATH_FIELDS, mode="after")
    @classmethod
    def _resolve_relative(cls, v: Path) -> Path:
        """把 .env 中的相对路径按服务根目录解析，避免依赖进程 CWD。

        否则 ``MODEL_DIR=./models`` 在非 kb-service 目录启动时会指向错误位置，
        导致模型静默加载失败并降级为哈希向量。
        """
        return v if v.is_absolute() else (BASE_DIR / v).resolve()

    @field_validator("asr_provider", mode="after")
    @classmethod
    def _check_asr_provider(cls, v: str) -> str:
        provider = (v or "").strip().lower()
        if provider not in ("mock", "openai"):
            raise ValueError("ASR_PROVIDER 只能是 mock 或 openai")
        return provider

    @field_validator("dq_provider", mode="after")
    @classmethod
    def _check_dq_provider(cls, v: str) -> str:
        provider = (v or "").strip().lower()
        if provider not in ("mock", "http"):
            raise ValueError("DQ_PROVIDER 只能是 mock 或 http")
        return provider

    @field_validator("dq_max_rows", mode="after")
    @classmethod
    def _check_dq_max_rows(cls, v: int) -> int:
        if not 1 <= int(v) <= 200:
            raise ValueError("DQ_MAX_ROWS 需在 1–200 之间")
        return int(v)

    @field_validator("access_sync_provider", mode="after")
    @classmethod
    def _check_access_sync_provider(cls, v: str) -> str:
        provider = (v or "").strip().lower()
        if provider not in ("mock", "http"):
            raise ValueError("ACCESS_SYNC_PROVIDER 只能是 mock 或 http")
        return provider

    @field_validator("access_sync_days", "access_max_range_days", mode="after")
    @classmethod
    def _check_access_days(cls, v: int, info) -> int:
        value = int(v)
        if info.field_name == "access_sync_days" and not 1 <= value <= 366:
            raise ValueError("ACCESS_SYNC_DAYS 需在 1–366 之间")
        if info.field_name == "access_max_range_days" and not 1 <= value <= 366:
            raise ValueError("ACCESS_MAX_RANGE_DAYS 需在 1–366 之间")
        return value

    @field_validator("access_max_events", mode="after")
    @classmethod
    def _check_access_max_events(cls, v: int) -> int:
        if not 100 <= int(v) <= 2_000_000:
            raise ValueError("ACCESS_MAX_EVENTS 需在 100–2000000 之间")
        return int(v)

    @field_validator("access_export_limit", mode="after")
    @classmethod
    def _check_access_export_limit(cls, v: int) -> int:
        if not 100 <= int(v) <= 100_000:
            raise ValueError("ACCESS_EXPORT_LIMIT 需在 100–100000 之间")
        return int(v)

    @field_validator("support_ws_heartbeat", "support_max_message", mode="after")
    @classmethod
    def _check_support_limits(cls, v: int, info) -> int:
        value = int(v)
        if info.field_name == "support_ws_heartbeat" and not 5 <= value <= 120:
            raise ValueError("SUPPORT_WS_HEARTBEAT 需在 5–120 秒之间")
        if info.field_name == "support_max_message" and not 50 <= value <= 5000:
            raise ValueError("SUPPORT_MAX_MESSAGE 需在 50–5000 字之间")
        return value

    @field_validator("data_center_provider", mode="after")
    @classmethod
    def _check_data_center_provider(cls, v: str) -> str:
        provider = (v or "").strip().lower()
        if provider not in ("mock", "http"):
            raise ValueError("DATA_CENTER_PROVIDER 只能是 mock 或 http")
        return provider

    @field_validator(
        "data_center_report_hour",
        "data_center_incremental_hour",
        "data_center_full_hour",
        mode="after",
    )
    @classmethod
    def _check_data_center_hours(cls, v: int) -> int:
        if not 0 <= int(v) <= 23:
            raise ValueError("数据中台任务时刻需在 0–23 之间")
        return int(v)

    @field_validator("data_center_full_weekday", mode="after")
    @classmethod
    def _check_data_center_weekday(cls, v: int) -> int:
        if not 0 <= int(v) <= 6:
            raise ValueError("DATA_CENTER_FULL_WEEKDAY 需在 0–6 之间（周一=0）")
        return int(v)

    @field_validator("data_center_max_records", mode="after")
    @classmethod
    def _check_data_center_max_records(cls, v: int) -> int:
        if not 100 <= int(v) <= 50_000:
            raise ValueError("DATA_CENTER_MAX_RECORDS 需在 100–50000 之间")
        return int(v)

    @field_validator("data_center_scheduler_interval", mode="after")
    @classmethod
    def _check_data_center_interval(cls, v: int) -> int:
        if not 10 <= int(v) <= 3600:
            raise ValueError("DATA_CENTER_SCHEDULER_INTERVAL 需在 10–3600 秒之间")
        return int(v)

    @field_validator("gov_sync_provider", mode="after")
    @classmethod
    def _check_gov_sync_provider(cls, v: str) -> str:
        provider = (v or "").strip().lower()
        if provider not in ("mock", "http"):
            raise ValueError("GOV_SYNC_PROVIDER 只能是 mock 或 http")
        return provider

    @field_validator("gov_recommend_limit", mode="after")
    @classmethod
    def _check_gov_recommend_limit(cls, v: int) -> int:
        if not 1 <= int(v) <= 5:
            raise ValueError("GOV_RECOMMEND_LIMIT 需在 1–5 之间")
        return int(v)

    @field_validator("gov_ticket_ttl_seconds", mode="after")
    @classmethod
    def _check_gov_ticket_ttl(cls, v: int) -> int:
        if not 30 <= int(v) <= 300:
            raise ValueError("GOV_TICKET_TTL_SECONDS 需在 30–300 秒之间")
        return int(v)

    # ---- 派生属性 ----
    @property
    def base_dir(self) -> Path:
        """服务根目录（kb-service/），供模型与缓存路径解析使用。"""
        return BASE_DIR

    @property
    def db_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset={self.db_charset}"
        )

    @property
    def store_label(self) -> str:
        return "mysql" if self.kb_store.lower() == "mysql" else "json"

    @property
    def llm_label(self) -> str:
        if self.llm_mock:
            return "mock"
        if not self.llm_api_key:
            return "unconfigured"
        return self.llm_model or "（跟随网关默认）"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
