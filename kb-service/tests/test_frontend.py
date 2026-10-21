"""前端静态资源接入测试（任务 1.15）。

覆盖：
- 对话页 / 管理页及其 CSS/JS 均可访问（含 /admin 显式路由）
- 迁入文件与原型 ``kb-engine/src/web/public/`` 字节级一致（真·迁入，非改写）
- **有意改写**的文件需登记在 :data:`MODIFIED_FROM_PROTOTYPE` 并写明原因（任务 1.9）
- 前端消费的接口契约已就位（docs 列表字段、meta 字典）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SERVICE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = SERVICE_DIR / "static"
PROTO_DIR = SERVICE_DIR.parent / "kb-engine" / "src" / "web" / "public"

FRONTEND_FILES = [
    "index.html",
    "app.js",
    "style.css",
    "admin.html",
    "admin.js",
    "admin.css",
]

#: 新增的前端文件（原型中不存在，不做字节级比对）
ADDED_FRONTEND_FILES = [
    "preview.html",
    "preview.js",
    "preview.css",
    # 任务 2.1 会话日志管理页
    "chatlog.js",
    # 任务 2.2 智能体/助手管理页
    "agent.js",
    # C-01 LLM 网关接入入口（模型网关页）
    "gateway.js",
    # 任务 2.8 知识点管理页（M-K-10，独立页面）
    "points.html",
    "points.js",
    "points.css",
    # 任务 3.1 渠道分身管理页（独立页面，不占后台页签）
    "channels.html",
    "channels.js",
    "channels.css",
    # 任务 3.3 应用广场管理页（独立页面）
    "apps.html",
    "apps.js",
    "apps.css",
    # 任务 3.10 统计分析页与大屏
    "stats.js",
    # 任务 3.9 嵌入 SDK 与第三方演示页
    "sdk/nxz.js",
    "sdk-demo.html",
    # 任务 3.11 未识别问题池（独立页面）
    "unrecognized.html",
    "unrecognized.js",
    "unrecognized.css",
    # 任务 4.6 客服工作台（独立页面，不占后台页签）
    "support.html",
    "support.js",
    "support.css",
    # 任务 4.6 对话页用户端客服面板
    "support-widget.js",
]

#: 有意偏离原型的文件 → 原因。**新增条目必须写清「为什么」**，
#: 否则字节级守卫就形同虚设（默认口径仍是「迁移而非改写」）。
MODIFIED_FROM_PROTOTYPE: dict[str, str] = {
    "index.html": (
        "任务 3.1 渠道分身：顶栏标题/副标题补充 #brand-title / #brand-subtitle 两个 id，"
        "供 app.js 按 ?channel= 读取渠道配置后改写欢迎语与主题色。"
        "任务 3.5 用户端门户：对话区由「顶栏 + 聊天 + 输入框」升级为"
        "「#history-panel 历史侧栏 + .portal-main（聊天 + 输入框）」两栏结构，"
        "顶栏新增历史抽屉按钮、#auth-box 登录态与登录弹窗（#login-modal）；"
        "既有 #chat/#input/#send/#role/#new-chat 全部保留、契约未变。"
        "任务 3.8 多智能体切换：顶栏新增 #agent-select 常驻助手切换器"
        "（助手不足 2 个时隐藏），会话中途也能切换。"
        "任务 4.6 人工客服：顶栏新增 #support-open「转人工」入口与 #support-panel "
        "对话框（消息流/输入框/结束会话），由 app.js 对接 /api/support。"
    ),
    "app.js": (
        "任务 1.9 富文本渲染：renderContent 由「**加粗** + [n] + 换行」升级为 "
        "Markdown 子集渲染器（标题/列表/代码/引用/表格/链接/图片），并加固 XSS 转义与"
        "链接协议白名单；finalize/loadHistory 优先消费后端 answerHtml。"
        "任务 3.1 渠道分身：启动时按 ?channel= 读取公开渠道配置（欢迎语/助手列表/主题色），"
        "对话请求附带 agentId；?embed=1 时切到嵌入精简布局。"
        "任务 3.2 门户个性化：首屏按 themeConfig 渲染 AI 形象（头像文字/图片）、常见问题、"
        "快捷方式、推荐应用与客服/反馈入口；所有文案走 textContent、链接走 safeUrl，"
        "避免渠道配置成为 XSS 注入面。"
        "任务 3.3 应用广场：启动与切换身份时按当前角色请求 /api/apps，欢迎页渲染"
        "智能体/外链应用卡片；智能体卡片点击切换 agentId，外链卡片经 safeUrl 后打开。"
        "任务 3.4 角色路由：loadChannel 携带 ?role=，首屏按 route 覆写欢迎语/引导问题，"
        "并按 modules 开关隐藏快捷方式/推荐应用/应用广场/客服反馈；切换身份重新拉取"
        "渠道配置与广场后整块重渲染，路由后的默认助手随身份变化。"
        "任务 3.5 用户端门户：新增本机会话历史（nxz.history：记录/恢复/删除/清空，"
        "恢复仍走 /api/sessions/{id}/messages）、登录/退出（nxz_token + /api/auth/login"
        "/api/auth/me，登录后角色下拉锁定、请求附带 Authorization）、?kiosk=1 智能硬件"
        "精简布局；未登录时保持原有访客自选身份行为。"
        "任务 3.8 多智能体切换：首屏按渠道绑定/公开上架清单渲染助手矩阵，"
        "顶栏 #agent-select 会话中途可切换；切换即丢弃当前会话并重建欢迎页，"
        "引导问题优先使用所选助手的场景问题（默认助手仍尊重 3.4 角色路由）。"
        "任务 4.6 人工客服：转人工按钮按客服状态显示/隐藏，创建工单后保存 accessKey "
        "并轮询会话；发送/结束走 /api/support/tickets/{id}/messages|close，"
        "离线留言与坐席接入提示就地渲染。"
    ),
    "style.css": (
        "任务 1.9 富文本渲染：追加 .rt-table/.rt-code/.rt-list/.rt-quote/.rt-figure 等样式，"
        "否则升级后的表格与代码块只有浏览器默认样式。"
        "任务 3.1 渠道分身：追加 body.embed-mode 规则，iframe/浮窗内隐藏顶栏控件与提示，"
        "压缩聊天区内边距。"
        "任务 3.2 门户个性化：追加 .avatar-img/.shortcut-row/.portal-apps/.portal-app/"
        ".portal-links 等规则，让头像图片、快捷方式与推荐应用卡片在深色主题下可读。"
        "任务 3.3 应用广场：追加 .app-square/.app-card(.active) 规则，让按角色展示的"
        "应用卡片与选中态在深色主题下可读。"
        "任务 3.5 用户端门户：追加 .portal-layout/.history-panel/.history-item/.auth-box"
        "/.login-modal 等两栏与登录样式；960px 以下历史栏改为抽屉（body.history-open），"
        "720/480px 收紧顶栏与消息宽度，body.kiosk-mode 放大触控目标并隐藏历史/管理入口。"
        "任务 3.8 多智能体切换：追加 .agent-matrix/.agent-chip 与 .agent-select 样式，"
        "让助手矩阵卡片、选中态与顶栏切换器在深色主题及 720px/kiosk 下可用。"
        "任务 4.6 人工客服：追加 .support-open/.support-panel/.support-card/"
        ".support-thread/.support-notice 等规则，让转人工对话框在桌面与移动端可用。"
    ),
    "admin.js": (
        "任务 1.11 文档预览与对比：文档列表每行新增「预览」入口（<a href=\"/preview?doc=…\">），"
        "指向新增的原文-切片对比页；其余逻辑与原型逐字一致。"
        "任务 2.1 会话日志：末尾追加页签切换（#tabs → #page-docs / #page-logs），"
        "切到「会话日志」时调用 chatlog.js 暴露的 window.initChatlogPage() 做懒加载；"
        "既有文档管理逻辑（load/upload/sync/render）未改动。"
        "任务 2.2 智能体管理：页签切换改为通用映射（PAGES + BOOTERS），"
        "新增 #page-agents 与 window.initAgentPage 钩子，同一套逻辑复用到两页懒加载。"
        "任务 2.4 模型管理：PAGES/BOOTERS 各加一项（#page-models + window.initModelPage），"
        "页签增至四个；切换逻辑本身未改。"
        "任务 2.5 知识分类管理：新增分类面板逻辑（renderCats/loadCats/createCat/"
        "renameCat/deleteCat + cat-* 节点），按「部门 × 知识类型」分组渲染，"
        "并随 editable 控制只读/可编辑；既有文档逻辑未动。"
        "任务 2.6 知识库部门授权：新增授权面板逻辑（renderPerm/loadPerms/"
        "grantPerm/revokePerm + perm-* 节点），对「用户 × 部门 × 类型」勾选五操作位"
        "并提交 /api/admin/kb-permissions，only editable 时显示授予表单。"
        "任务 2.7 文档审核与文件管理：文档表格新增首列复选框（data-chk）与"
        "表头全选（#chk-all），新增 #f-ktype/#f-category 筛选下拉与 #batch-bar "
        "批量条；fKtype/fCategory 状态下推到 /api/docs 查询串，批量通过/驳回/移动/"
        "删除走 POST /api/docs/batch（selected 集合 + syncBatchBar 同步计数），"
        "既有上传/同步/单条操作逻辑未改。"
        "任务 2.8 知识点管理：顶栏新增「知识点管理」入口（<a href=\"/points\">）。"
        "知识点单独成页而非新增页签——既有 5 个浏览器验收工具都断言 "
        "`#tabs .tab` 数量为 4，加页签会把它们全部打挂，而知识点页与文档管理"
        "是两套不同的浏览维度（切片级 vs 文档级），独立页更清晰。"
    ),
    "admin.css": (
        "任务 1.11 文档预览与对比：「预览」入口是 <a class=\"op-btn\">，需补 a.op-btn 规则"
        "（去下划线 + 保持行内块），否则链接会带浏览器默认下划线。"
        "任务 2.1 会话日志：追加 .tabs/.page/.cl-filters/.cl-note/.cl-table/.pager 等规则，"
        "并把 input[type=date] 纳入既有深色表单选择器（否则日期框在白底白字下不可用）。"
        "任务 2.2 智能体管理：追加 .scene-grid/.scene-card/.ag-table/.ag-icon/.modal 与"
        ".field textarea 深色样式（弹窗内 textarea 无样式会变成白底黑字）。"
        "任务 2.3 助手配置：追加 .modal-wide/.ag-tabs/.ag-tab/.ag-pane/.folder-checks/"
        ".ag-default-prompt/.ag-test-result 等规则（多页签弹窗、知识范围网格、"
        "测试结果面板在深色主题下的可读性）。"
        "任务 2.4 模型管理：追加 .md-table/.md-name/.md-code/.md-base/.md-ops/.md-global-grid/"
        ".md-probe 与 .btn-ghost.danger/.muted/.warn 等规则（模型表格需让长接口地址换行，"
        "否则撑破布局；「全局默认」卡片用网格排版）。"
        "任务 2.5 知识分类管理：追加 .cat-form/.cat-tree/.cat-group/.cat-dept/.cat-list/"
        ".cat-row/.cat-info/.cat-name/.cat-count/.cat-ops 等规则（分类面板需与上传表单同深色"
        "网格风格，分组区块与操作按钮在深色主题下可读）；并给 .cat-form[hidden] 补 "
        "display:none（默认 display:grid 会覆盖 UA 的 inline 隐藏规则，导致只读模式表单不隐藏）。"
        "任务 2.6 知识库部门授权：追加 .perm-form/.perm-flags/.perm-row/.perm-info/.perm-dept/"
        ".perm-flag(.on) 等规则（操作位勾选行、授权卡片在深色主题下可读）。"
        "任务 2.7 文档审核与文件管理：追加 .head-select（下拉箭头 + 深色不透明背景，"
        "否则 appearance:none 下 select 白底白字）、.batch-bar（批量条布局）/ .chk "
        "（复选框列对齐）/ .muted。"
    ),
    "admin.html": (
        "任务 1.9 前端视觉升级：为管理后台补充 —— 统计概览条、渐变主按钮、玻璃卡片、"
        "状态糖、更清晰的筛选 tab；保留 admin.js 依赖的全部 id/class "
        "（tbody/empty/count/filters/upload/sync 等），仅改包装结构与样式。"
        "任务 2.1 会话日志：新增 #tabs 页签条与 #page-logs 容器（筛选条/表格/分页/导出），"
        "原文档管理区块整体包进 #page-docs，id/class 一个未删；新增 chatlog.js 引入。"
        "任务 2.2 智能体管理：新增 #page-agents（场景卡片区 + 助手列表 + 新建/编辑弹窗）"
        "与 agent.js 引入；页签增至三个。"
        "任务 2.3 助手配置：编辑弹窗拆为四个页签（#ag-tabs + #ag-pane-basic/prompt/"
        "retrieval/test），基础字段原样包进 #ag-pane-basic（id 一个未删），"
        "新增提示词、检索配置（含知识范围勾选）与对话测试三块面板。"
        "任务 2.4 模型管理：新增 #page-models（模型列表 + 全局默认卡片 + 新增/编辑弹窗）"
        "与 model.js 引入，页签增至四个；助手弹窗「基础信息」页签新增模型下拉与"
        "温度/topP/最大输出三项（#ag-f-model/#ag-f-temp/#ag-f-topp/#ag-f-maxtok），"
        "既有字段 id 一个未删。"
        "任务 2.5 知识分类管理：文档列表后新增「知识分类管理」卡片（#cat-form 新建表单 + "
        "#cat-tree 分类树 + #cat-note 只读提示），JS 依赖的 cat-* id 全部存在于本页。"
        "任务 2.6 知识库部门授权：知识库页新增「知识库部门授权」卡片（#perm-form 授予表单 + "
        "#perm-list 授权矩阵 + #perm-note 只读提示），JS 依赖的 perm-* id 全部存在于本页。"
        "任务 2.7 文档审核与文件管理：文档表格首列新增复选框列头（#chk-all），"
        "筛选行新增 #f-ktype/#f-category 两个下拉，表格下方新增 #batch-bar 批量条"
        "（#batch-count/#batch-publish/#batch-reject/#batch-move/#batch-target-cat/"
        "#batch-delete/#batch-clear/#batch-msg），JS 依赖的 id 全部存在于本页。"
        "任务 3.3 应用广场：顶栏新增「应用广场」入口（<a href=\"/apps\">，"
        "权限点 apps.view），应用管理单独成页，不新增后台页签。"
        "任务 3.8 多智能体切换：助手弹窗基础信息新增 #ag-f-guides 场景引导问题"
        "（多行文本，每行一条），供门户切换助手后推荐问题。"
        "任务 4.6 人工客服：顶栏新增「客服工作台」入口（<a href=\"/support\">，"
        "权限点 support.view），工作台单独成页，不新增后台页签。"
    ),
}


@pytest.mark.parametrize("name", FRONTEND_FILES + ADDED_FRONTEND_FILES)
def test_static_file_served(name: str) -> None:
    resp = client.get(f"/{name}")
    assert resp.status_code == 200, f"{name} 应可访问"
    assert len(resp.content) > 0


def test_index_is_chat_page() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "能小智" in resp.text
    # 骨架占位页的文案不应再出现
    assert "问答引擎尚未接入" not in resp.text
    assert "app.js" in resp.text


def test_admin_route_maps_to_admin_html() -> None:
    for path in ("/admin", "/admin/"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} 应映射到 admin.html"
        assert "知识管理后台" in resp.text


def test_preview_route_maps_to_preview_html() -> None:
    """任务 1.11：/preview 必须显式映射到 preview.html（否则被 "/" 挂载吞掉）。"""
    for path in ("/preview", "/preview/"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} 应映射到 preview.html"
        assert "文档预览与对比" in resp.text
        assert "preview.js" in resp.text


@pytest.mark.parametrize("name", ADDED_FRONTEND_FILES)
def test_added_frontend_files_are_new(name: str) -> None:
    """新增文件在原型中**不应**存在——若原型也有了，就该走迁移而非新增。"""
    assert (STATIC_DIR / name).is_file(), f"{name} 不存在"
    assert not (PROTO_DIR / name).is_file(), (
        f"原型已存在 {name}，应改为「从原型迁入」并从 ADDED_FRONTEND_FILES 移除"
    )


def test_preview_js_has_highlight_helpers() -> None:
    """任务 1.11：对比视图的关键词高亮与导出钩子必须就位。"""
    src = (STATIC_DIR / "preview.js").read_text(encoding="utf-8")
    for token in ("findRanges", "parseTerms", "highlight", "MAX_RANGES_PER_TERM", "mark class"):
        assert token in src, f"preview.js 缺少 {token}"
    assert "__NXZ_EXPORT__" in src and "__NXZ_PREVIEW__" in src


def test_preview_js_dom_ids_exist_in_preview_html() -> None:
    """防拼写漂移：preview.js 引用的 DOM id 必须真在 preview.html 里。

    preview.js 不在 :func:`test_js_dom_ids_exist_in_html` 的覆盖范围内
    （那条只比对 admin.html），而任务 2.7 又给它加了一批审核控件 id，
    因此单独做一次交叉校验——JS 引错 id 只会在浏览器里炸，HTTP 层看不见。
    """
    js = (STATIC_DIR / "preview.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "preview.html").read_text(encoding="utf-8")

    # preview.js 用的是 `$` 别名（function $(id)）而非 el/getElementById
    ids = set(re.findall(r"\$\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    ids |= set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))

    # 任务 2.7 审核控件必须成对出现（按钮 + 反馈行）
    for token in ("audit-approve", "audit-reject", "audit-msg"):
        assert f'id="{token}"' in html, f"preview.html 缺少 {token}"

    assert len(ids) >= 10, f"未提取到足够的 id 引用（正则可能失效）：{sorted(ids)}"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, f"preview.js 引用了 preview.html 中不存在的 id：{missing}"


def test_points_js_dom_ids_exist_in_points_html() -> None:
    """任务 2.8：points.js 引用的 DOM id 必须真在 points.html 里。

    与 :func:`test_preview_js_dom_ids_exist_in_preview_html` 同一动机——
    ``test_js_dom_ids_exist_in_html`` 只比对 admin.html，新页若不复用
    ``el()`` 命名就更没人管。points.js 用的是 ``$`` 别名，因此单独校验。
    """
    js = (STATIC_DIR / "points.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "points.html").read_text(encoding="utf-8")

    ids = set(re.findall(r"\$\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    ids |= set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))

    # 再加工三件套与批量导出必须成对存在（缺一个就只是「看不见的按钮」）
    for token in (
        "pt-op-append-toggle", "pt-op-remove-toggle", "pt-op-image-toggle",
        "pt-export-csv", "pt-export-md", "pt-export-json",
        "pt-batch-count", "pt-chk-all", "pt-modal", "pt-create",
    ):
        assert f'id="{token}"' in html, f"points.html 缺少 {token}"

    assert len(ids) >= 30, f"未提取到足够的 id 引用（正则可能失效）：{sorted(ids)}"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, f"points.js 引用了 points.html 中不存在的 id：{missing}"


def test_points_page_registered_in_main() -> None:
    """任务 2.8：/points 必须在 StaticFiles 之前显式注册，否则被 "/" 吞掉。"""
    src = (SERVICE_DIR / "app" / "main.py").read_text(encoding="utf-8")
    assert '"points": _static_dir / "points.html"' in src
    # 注册语句必须出现在 mount("/") 之前
    assert src.index('"points": _static_dir') < src.index('app.mount("/"')


def test_channels_js_dom_ids_exist_in_channels_html() -> None:
    """任务 3.2：channels.js 新增的门户配置控件 id 必须真在 channels.html 里。"""
    js = (STATIC_DIR / "channels.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "channels.html").read_text(encoding="utf-8")

    ids = set(re.findall(r"\$\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    ids |= set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))

    for token in (
        "ch-f-assistant", "ch-f-avatar-text", "ch-f-avatar-url",
        "ch-f-faq", "ch-f-shortcuts", "ch-f-apps", "ch-f-position",
        "ch-f-service-enabled", "ch-f-service-label", "ch-f-service-url",
        "ch-f-feedback-enabled", "ch-f-feedback-label", "ch-f-feedback-url",
    ):
        assert f'id="{token}"' in html, f"channels.html 缺少 {token}"

    assert len(ids) >= 30, f"未提取到足够的 id 引用（正则可能失效）：{sorted(ids)}"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, f"channels.js 引用了 channels.html 中不存在的 id：{missing}"


def test_channels_js_has_portal_copy_and_auth_actions() -> None:
    """任务 3.2：管理页须支持门户配置、一键复制与认证开关（M-C-02/03/04）。"""
    src = (STATIC_DIR / "channels.js").read_text(encoding="utf-8")
    for token in (
        "/copy",
        "needAuth",
        "parseShortcuts",
        "parseApps",
        "recommendedApps",
        "serviceEntry",
        "feedback",
        "buildTheme",
    ):
        assert token in src, f"channels.js 缺少 {token}"


def test_app_js_has_portal_personalization() -> None:
    """任务 3.2：对话页须消费门户个性化配置，且链接统一过 safeUrl。"""
    src = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        "portalTheme",
        "addPortalShortcuts",
        "addPortalApps",
        "addPortalEntry",
        "assistantName",
        "avatarText",
        "avatarUrl",
        "recommendedApps",
        "serviceEntry",
        "feedback",
    ):
        assert token in src, f"app.js 缺少 {token}"
    assert src.count("safeUrl(") >= 3, "门户链接必须经过 safeUrl 协议白名单"


def test_unrecognized_js_dom_ids_exist_in_unrecognized_html() -> None:
    """任务 3.11：unrecognized.js 引用的 DOM id 必须真在 unrecognized.html 里。"""
    js = (STATIC_DIR / "unrecognized.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "unrecognized.html").read_text(encoding="utf-8")

    ids = set(re.findall(r"\$\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    ids |= set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))

    for token in (
        "un-state", "un-source", "un-dept", "un-role", "un-channel",
        "un-from", "un-to", "un-q", "un-sort",
        "un-batch", "un-batch-ignore", "un-batch-reopen",
        "un-batch-classify", "un-batch-feedback", "un-export",
        "un-detail", "un-detail-session", "un-variants",
        "un-feedback", "un-feedback-banner", "un-feedback-submit",
    ):
        assert f'id="{token}"' in html, f"unrecognized.html 缺少 {token}"

    assert len(ids) >= 30, f"未提取到足够的 id 引用（正则可能失效）：{sorted(ids)}"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, f"unrecognized.js 引用了 unrecognized.html 中不存在的 id：{missing}"


def test_support_page_registered_in_main() -> None:
    """任务 4.6：/support 必须在 StaticFiles 之前显式注册。"""
    src = (SERVICE_DIR / "app" / "main.py").read_text(encoding="utf-8")
    assert '"support": _static_dir / "support.html"' in src
    assert src.index('"support": _static_dir') < src.index('app.mount("/"')


def test_support_page_contract() -> None:
    """任务 4.6：工作台页面与脚本的关键节点/接口一一对应。"""
    html = (STATIC_DIR / "support.html").read_text(encoding="utf-8")
    for token in (
        "sp-total", "sp-queued", "sp-active", "sp-offline", "sp-negative",
        "sp-state", "sp-sentiment", "sp-group", "sp-seat-filter", "sp-channel",
        "sp-q", "sp-ticket-list", "sp-thread", "sp-thread-state", "sp-send",
        "sp-claim", "sp-close", "sp-assign-seat", "sp-ws", "sp-seat",
    ):
        assert f'id="{token}"' in html, f"support.html 缺少 {token}"

    src = (STATIC_DIR / "support.js").read_text(encoding="utf-8")
    for token in (
        "/api/admin/support/overview",
        "/api/admin/support/filters",
        "/api/admin/support/tickets",
        "/api/admin/support/reply-logs",
        "/api/admin/support/groups",
        "/api/admin/support/seats",
        "/claim",
        "/reply",
        "/close",
        "/assign",
        "/api/support/ws",
    ):
        assert token in src, f"support.js 未对接 {token}"


def test_chat_support_entry_contract() -> None:
    """任务 4.6：对话页提供转人工入口并消费公开客服接口。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="support-open"' in html and 'id="support-panel"' in html
    assert 'id="support-thread"' in html and 'id="support-input"' in html
    assert 'src="support-widget.js"' in html
    src = (STATIC_DIR / "support-widget.js").read_text(encoding="utf-8")
    for token in (
        "/api/support/status",
        "/api/support/tickets",
        "/messages",
        "/close",
        "accessKey",
    ):
        assert token in src, f"support-widget.js 未对接客服能力 {token}"
    app_src = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "nxz:handoff" in app_src and "handoffSuggested" in app_src


def test_unrecognized_page_registered_in_main() -> None:
    """任务 3.11：/unrecognized 必须在 StaticFiles 之前显式注册。"""
    src = (SERVICE_DIR / "app" / "main.py").read_text(encoding="utf-8")
    assert '"unrecognized": _static_dir / "unrecognized.html"' in src
    assert src.index('"unrecognized": _static_dir') < src.index('app.mount("/"')


def test_unrecognized_js_contract() -> None:
    """任务 3.11：管理页须对接查询 / 详情 / 批量 / 反哺 / 导出五类接口。"""
    src = (STATIC_DIR / "unrecognized.js").read_text(encoding="utf-8")
    for token in (
        "/api/admin/unrecognized/filters",
        "/api/admin/unrecognized?",
        "/api/admin/unrecognized/batch",
        "/api/admin/unrecognized/feedback",
        "/api/admin/unrecognized/export",
        "/api/admin/unrecognized/' + encodeURIComponent",
        "questionKeys",
        "feedbackAvailable",
        "weak_recall",
        "low_confidence",
        "refused",
        "unrecognized.view",
        "unrecognized.manage",
        "__NXZ_UNRECOGNIZED__",
    ):
        assert token in src, f"unrecognized.js 缺少 {token}"
    # JSON 模式必须显式提示「反哺需 MySQL」，不能假装成功
    assert "只读" in src and "MySQL" in src

    css = (STATIC_DIR / "unrecognized.css").read_text(encoding="utf-8")
    for token in (".un-filters", ".un-clusters", ".un-table", ".un-state.st-0",
                  ".un-detail-grid", ".un-banner"):
        assert token in css, f"unrecognized.css 缺少 {token}"

    auth = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")
    assert "path === '/unrecognized' ? 'unrecognized.view'" in auth


def test_admin_html_has_unrecognized_entry() -> None:
    """任务 3.11：后台顶栏须有未识别问题入口，权限点与页面门控一致。"""
    admin = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    assert 'href="/unrecognized" data-perm="unrecognized.view"' in admin


def test_apps_js_dom_ids_exist_in_apps_html() -> None:
    """任务 3.3：apps.js 引用的 DOM id 必须真在 apps.html 里。"""
    js = (STATIC_DIR / "apps.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "apps.html").read_text(encoding="utf-8")

    ids = set(re.findall(r"\$\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    ids |= set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))

    for token in (
        "ap-f-name", "ap-f-source", "ap-f-agent", "ap-f-link",
        "ap-f-category", "ap-f-icon", "ap-f-desc", "ap-f-sort",
        "ap-f-status", "ap-f-roles", "ap-save", "ap-tbody",
    ):
        assert f'id="{token}"' in html, f"apps.html 缺少 {token}"

    assert len(ids) >= 15, f"未提取到足够的 id 引用（正则可能失效）：{sorted(ids)}"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, f"apps.js 引用了 apps.html 中不存在的 id：{missing}"


def test_apps_page_registered_in_main() -> None:
    """任务 3.3：/apps 必须在 StaticFiles 之前显式注册。"""
    src = (SERVICE_DIR / "app" / "main.py").read_text(encoding="utf-8")
    assert '"apps": _static_dir / "apps.html"' in src
    assert src.index('"apps": _static_dir') < src.index('app.mount("/"')


def test_apps_js_contract() -> None:
    """任务 3.3：管理页须对接 CRUD 与角色字典，并暴露权限门控。"""
    src = (STATIC_DIR / "apps.js").read_text(encoding="utf-8")
    for token in (
        "'/api/admin/apps'",
        "/api/admin/apps/${state.editingId}",
        "/api/admin/apps/${item.appId}",
        "visibleRoles",
        "sourceType",
        "agentMissing",
        "roleOptions",
        "apps.manage",
        "apps.view",
        "全部",
    ):
        assert token in src, f"apps.js 缺少 {token}"


def test_chat_page_has_app_square() -> None:
    """任务 3.3 / 3.4：对话页须按角色拉取广场并渲染智能体/外链卡片。"""
    src = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        "squareApps",
        "loadApps",
        "addAppSquare",
        "routeModules",
        "/api/apps?role=",
        "app-square",
        "app-card",
        "dataset.appId",
        "dataset.source",
        "sourceType",
    ):
        assert token in src, f"app.js 缺少 {token}"
    assert "safeUrl(item.linkUrl" in src, "外链应用必须经过 safeUrl"


def test_channels_route_ui_contract() -> None:
    """任务 3.4：管理页须有角色路由弹窗，并提交 routeConfig。"""
    js = (STATIC_DIR / "channels.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "channels.html").read_text(encoding="utf-8")
    for token in ("ch-route-modal", "ch-route-list", "ch-route-save", "ch-route-msg"):
        assert f'id="{token}"' in html, f"channels.html 缺少 {token}"
    for token in (
        "ROUTE_ROLES",
        "ROUTE_MODULES",
        "routeConfig",
        "openRoute",
        "saveRoute",
        "data-route-enabled",
        "data-route-agent",
        "data-route-guides",
        "data-route-module",
    ):
        assert token in js, f"channels.js 缺少 {token}"


def test_app_js_consumes_channel_route() -> None:
    """任务 3.4：对话页须按身份请求渠道配置并消费路由模块开关。"""
    src = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        "routeInfo",
        "routeModules",
        "guideQuestions",
        "appSquare",
        "serviceEntry",
        "feedback",
        "?role=${role}",
        "route.guideQuestions",
    ):
        assert token in src, f"app.js 缺少 {token}"


def test_portal_layout_and_history_contract() -> None:
    """任务 3.5：门户三端骨架与历史会话契约（P-B-05）。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for token in (
        'class="portal-layout"',
        'class="portal-main"',
        'id="history-panel"',
        'id="history-list"',
        'id="history-toggle"',
        'id="history-overlay"',
        'id="history-close"',
        'id="history-clear"',
        'id="new-chat"',
        'id="role"',
        'id="chat"',
        'id="input"',
        'id="send"',
    ):
        assert token in html, f"index.html 缺少 {token}"

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        "LS_HISTORY",
        "rememberSession",
        "renderHistory",
        "openSession",
        "forgetSession",
        "openHistoryDrawer",
        "closeHistoryDrawer",
        "history-open",
        "readHistory",
        "saveHistory",
    ):
        assert token in js, f"app.js 缺少 {token}"
    # 历史恢复必须复用既有会话接口，不得自造第二条读取路径
    assert "/api/sessions/${encodeURIComponent(sid)}/messages" in js
    # 会话完成后写入本机历史
    assert "rememberSession(sessionId, question)" in js


def test_portal_auth_contract() -> None:
    """任务 3.5：登录/退出模块与 token 身份联动（P-B-04）。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for token in (
        'id="auth-box"',
        'id="login-modal"',
        'id="login-form"',
        'id="lm-user"',
        'id="lm-pass"',
        'id="lm-role"',
        'id="lm-msg"',
        'id="lm-submit"',
    ):
        assert token in html, f"index.html 缺少 {token}"

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        "/api/auth/login",
        "/api/auth/me",
        "LS_TOKEN = 'nxz_token'",
        "Authorization",
        "Bearer ",
        "effectiveRole",
        "applyRoleFromAuth",
        "renderAuth",
        "submitLogin",
        "logout",
    ):
        assert token in js, f"app.js 缺少 {token}"
    # 登录后身份以 token 为准：角色下拉锁定，问答与广场请求带 token
    assert "roleEl.disabled = true" in js
    assert "apiFetch('/api/chat/stream'" in js
    assert "apiFetch(`/api/apps?role=${role}`)" in js
    assert "apiFetch(`/api/channels/" in js
    # 访客模式仍可自选身份（未登录时下拉可用）
    assert "roleEl.disabled = false" in js


def test_agent_switch_portal_contract() -> None:
    """任务 3.8：门户助手矩阵、顶栏切换器与引导问题优先级契约。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="agent-select"' in html, "index.html 缺少助手切换器 #agent-select"

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in (
        "loadPublicAgents",
        "/api/agents?role=",
        "agentCards",
        "switchAssistant",
        "agentSwitched",
        "renderAgentSelect",
        "addAgentMatrix",
        "agent-matrix-box",
        "guideQuestionsFor",
        "guideQuestions",
        "findAgentCard",
        "defaultAgentId",
        "selectedAgentId",
        "getElementById('agent-select')",
    ):
        assert token in js, f"app.js 缺少 {token}"
    # 切换助手必须开启新上下文，不能沿用上一个助手的会话
    switch_body = js.split("function switchAssistant", 1)[1].split("function ensureAgentVisible", 1)[0]
    assert "sessionId = null" in switch_body
    assert "removeItem(LS_SESSION)" in switch_body
    assert "addWelcome()" in switch_body

    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for token in (".agent-matrix", ".agent-chip", ".agent-select", ".agent-matrix-box"):
        assert token in css, f"style.css 缺少 {token}"


def test_agent_admin_guide_questions_contract() -> None:
    """任务 3.8：管理端可编辑场景引导问题，提交字段与后端对齐。"""
    html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    assert 'id="ag-f-guides"' in html, "admin.html 缺少 #ag-f-guides"
    js = (STATIC_DIR / "agent.js").read_text(encoding="utf-8")
    assert "guideQuestions" in js
    assert "ag-f-guides" in js


def test_portal_three_device_styles() -> None:
    """任务 3.5：PC / 移动 / 智能硬件三端自适应样式必须就位。"""
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for token in (
        "@media (max-width: 960px)",
        "@media (max-width: 720px)",
        "@media (max-width: 480px)",
        "body.history-open .history-panel",
        "translateX(-104%)",
        "body.kiosk-mode .history-panel",
        "body.kiosk-mode .sample",
        "body.embed-mode .history-panel",
    ):
        assert token in css, f"style.css 缺少 {token}"

    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "param('kiosk') === '1'" in js
    assert "classList.add('kiosk-mode')" in js


def test_admin_html_has_apps_entry() -> None:
    """任务 3.3：后台顶栏须有应用广场入口，导航权限点与页面门控一致。"""
    admin = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    assert 'href="/apps" data-perm="apps.view"' in admin
    auth = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")
    assert "path === '/apps' ? 'apps.view'" in auth


def test_admin_js_has_preview_entry() -> None:
    """任务 1.11：管理页每行应能跳转到预览页。"""
    src = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    assert "/preview?doc=" in src


def test_admin_html_has_chatlog_page() -> None:
    """任务 2.1：管理页须有页签与日志容器，且引入 chatlog.js。"""
    src = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    for token in (
        'id="tabs"',
        'id="page-docs"',
        'id="page-logs"',
        'class="cl-filters"',
        'id="cl-tbody"',
        'id="cl-pager"',
        'id="cl-export"',
        "chatlog.js",
    ):
        assert token in src, f"admin.html 缺少 {token}"
    # 原文档管理区块必须保留（不得为了加页签而删掉既有 id）
    for token in ('id="tbody"', 'id="empty"', 'id="upload"', 'id="sync"'):
        assert token in src, f"admin.html 丢失既有节点 {token}"


def test_admin_html_has_stats_page() -> None:
    """任务 3.10：统计页签、筛选条、指标卡、大屏与导出入口齐全。"""
    src = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    for token in (
        'data-tab="stats"',
        'data-perm="stats.view"',
        'id="page-stats"',
        'id="st-board"',
        'id="st-from"',
        'id="st-to"',
        'id="st-channel"',
        'id="st-agent"',
        'id="st-role"',
        'id="st-cards"',
        'id="st-trend"',
        'id="st-channels"',
        'id="st-agents"',
        'id="st-knowledge"',
        'id="st-categories"',
        'id="st-devices"',
        'id="st-sentiments"',
        'id="st-apps"',
        'id="st-export-summary"',
        'id="st-full"',
        'data-kind="knowledge"',
        "stats.js",
    ):
        assert token in src, f"admin.html 缺少 {token}"


def test_stats_js_contract() -> None:
    """任务 3.10：统计页须对接 4 个接口、支持导出与大屏、暴露懒加载入口。"""
    src = (STATIC_DIR / "stats.js").read_text(encoding="utf-8")
    for token in (
        "/api/admin/stats/filters",
        "/api/admin/stats/overview",
        "/api/admin/stats/rankings",
        "/api/admin/stats/export",
        "initStatsPage",
        "requestFullscreen",
        "fullscreenchange",
        "NXZAuth.download",
        "stats.export",
        "successRate",
        "scanLimit",
        "renderRankings(overview, rankings)",
    ):
        assert token in src, f"stats.js 缺少 {token}"


def test_chatlog_js_contract() -> None:
    """任务 2.1：日志页须对接 4 个管理接口并暴露懒加载入口。"""
    src = (STATIC_DIR / "chatlog.js").read_text(encoding="utf-8")
    for token in (
        "/api/admin/chatlogs/filters",
        "/api/admin/chatlogs/users",
        "/api/admin/chatlogs/export",
        "initChatlogPage",
        "totalPages",
        "scanLimit",
    ):
        assert token in src, f"chatlog.js 缺少 {token}"
    # 两个视图（问答明细 / 咨询用户）都必须有列定义
    assert "COLUMNS" in src and "'qa'" in src and "'users'" in src


#: 由 JS 在运行时插入的 id（不在 admin.html 静态结构中）
DYNAMIC_DOM_IDS = {"cl-prev", "cl-next"}


def test_admin_html_has_agent_page() -> None:
    """任务 2.2：管理页须有智能体页签、场景区、列表与编辑弹窗。"""
    src = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    for token in (
        'data-tab="agents"',
        'id="page-agents"',
        'id="ag-scenes"',
        'id="ag-tbody"',
        'id="ag-status-tabs"',
        'id="ag-modal"',
        'id="ag-f-roles"',
        'id="ag-f-stream"',
        'id="ag-f-status"',
        'id="ag-f-risk"',
        'id="ag-f-fallback"',
        "agent.js",
    ):
        assert token in src, f"admin.html 缺少 {token}"


def test_agent_js_contract() -> None:
    """任务 2.2：智能体页须对接 5 类接口并暴露懒加载入口。"""
    src = (STATIC_DIR / "agent.js").read_text(encoding="utf-8")
    for token in (
        "/api/admin/agents/presets/",
        "/api/admin/agents/${agent.agentId}/status",
        "/api/admin/agents/${editing.agentId}",
        "'/api/admin/agents'",
        "initAgentPage",
        "visibleRoles",
        "streamEnabled",
        "fallbackReply",
        "riskReply",
    ):
        assert token in src, f"agent.js 缺少 {token}"


def test_admin_html_has_agent_config_tabs() -> None:
    """任务 2.3：助手弹窗须有四个页签与提示词/检索配置/对话测试三块面板。"""
    src = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    for token in (
        'id="ag-tabs"',
        'data-agt="basic"',
        'data-agt="prompt"',
        'data-agt="retrieval"',
        'data-agt="test"',
        'id="ag-pane-prompt"',
        'id="ag-pane-retrieval"',
        'id="ag-pane-test"',
        'id="ag-f-prompt"',
        'id="ag-f-topk"',
        'id="ag-f-rerank"',
        'id="ag-f-minscore"',
        'id="ag-f-depts"',
        'id="ag-f-ktypes"',
        'id="ag-f-ret-note"',
        'id="ag-test-q"',
        'id="ag-test-run"',
        'id="ag-test-result"',
    ):
        assert token in src, f"admin.html 缺少 {token}"


def test_agent_js_config_contract() -> None:
    """任务 2.3：agent.js 须对接检索配置/对话测试接口，并保持「未传即沿用」的语义。"""
    src = (STATIC_DIR / "agent.js").read_text(encoding="utf-8")
    for token in (
        "/api/admin/kb-folders",
        "/retrieval`",
        "/test`",
        "switchTab",
        "systemPrompt",
        "retrievalConfig",
        "minScore",
        "depts",
        "ktypes",
        # 勾选状态不能只读 DOM（选项异步渲染，先渲染为空会把已存配置丢掉）
        "retrievalSel",
        "syncRetrievalSel",
    ):
        assert token in src, f"agent.js 缺少 {token}"


def test_agent_js_does_not_hardcode_prompt() -> None:
    """提示词与「沿用全局」的数值都必须来自后端，不得在前端写死第二份。"""
    src = (STATIC_DIR / "agent.js").read_text(encoding="utf-8")
    assert "你是「能小智」校园 AI 助手" not in src, "平台默认提示词应由后端下发，不得写死在前端"
    assert "defaultSystemPrompt" in src
    assert "retrievalDefaults" in src


# ------------------------------------------------------------------ 模型来源=网关（只读）
def test_admin_html_has_no_model_manage_page() -> None:
    """架构变更：**能小智不再有「模型管理」页** —— 模型的唯一来源是网关。

    这条是**反向守卫**：如果将来有人把模型管理页加回来（或在能小智侧重建模型台账），
    就会与「网关是唯一模型来源」的架构冲突，必须显式讨论而不是悄悄加。
    """
    src = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    for gone in (
        'data-tab="models"',
        'id="page-models"',
        'id="md-tbody"',
        'id="md-modal"',
        '<script src="model.js"></script>',
    ):
        assert gone not in src, (
            f"admin.html 不应再包含 {gone} —— 模型管理页已下线，模型来源改为网关"
        )
    # model.js 也应已删除
    assert not (STATIC_DIR / "model.js").exists(), (
        "static/model.js 应已删除（模型 CRUD 前端随管理页一并下线）"
    )


def test_admin_html_has_gateway_tab() -> None:
    """网关页签与页面必须存在（它是模型清单与网关状态的唯一入口）。"""
    src = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    for token in (
        'data-tab="gateway"',
        'id="page-gateway"',
        'id="gw-list"',
        'id="gw-console"',
        'id="gw-refresh"',
        'id="gw-guide"',
        'id="gw-hints"',
        '<script src="gateway.js"></script>',
    ):
        assert token in src, f"admin.html 缺少 {token}"


def test_gateway_js_renders_gateway_models() -> None:
    """gateway.js 须渲染**网关下发的模型清单**，并提供逐个自测。

    模型清单来自 ``GET /api/admin/models``（后端再向网关 ``/models`` 取），
    前端不得写死任何模型名或厂商地址。
    """
    src = (STATIC_DIR / "gateway.js").read_text(encoding="utf-8")
    for token in (
        "/api/admin/gateway/status",
        # 模型清单区块
        "gw-models",
        "renderModels",
        # 自测（走新的网关模型自测端点）
        "/api/admin/gateway/models/",
        "/probe",
        # 刷新要绕过 30 秒缓存
        "refresh=1",
        # 控制台地址由**后端**下发（consoleUrl），前端不得拼路径
        "consoleUrl",
        "dataset.url",
    ):
        assert token in src, f"gateway.js 缺少 {token}"

    # 不得写死模型名、厂商地址或控制台路径 —— 全部由后端下发
    for hard in ("deepseek-flash", "deepseek-v4-pro", "api.deepseek.com",
                 "dashscope.aliyuncs.com", "/admin-ui/"):
        assert hard not in src, f"gateway.js 不应写死 {hard}，须由后端下发"


def test_admin_js_tabs_match_new_structure() -> None:
    """页签从 5 个减为 4 个（知识库 / 智能体管理 / 模型网关 / 会话日志）。"""
    src = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    assert "models: document.getElementById('page-models')" not in src
    assert "models: 'initModelPage'" not in src
    assert "gateway: 'initGatewayPage'" in src
    assert "gateway: document.getElementById('page-gateway')" in src




def test_agent_js_model_binding_contract() -> None:
    """任务 2.4：agent.js 须支持绑定模型 + 生成参数，且空输入要显式提交 null。"""
    src = (STATIC_DIR / "agent.js").read_text(encoding="utf-8")
    for token in (
        "modelChoices",
        "modelDefaults",
        "modelRanges",
        "renderModelOptions",
        "updateModelHints",
        "modelCode",
        "temperature",
        "topP",
        "maxTokens",
        # 空输入 → 显式 null（= 沿用平台默认）。不传的话「清空参数」表达不出来。
        "ag-f-temp",
        "ag-f-topp",
        "ag-f-maxtok",
    ):
        assert token in src, f"agent.js 缺少 {token}"


@pytest.mark.parametrize(
    "name", ["chatlog.js", "admin.js", "agent.js", "gateway.js", "stats.js"]
)
def test_js_dom_ids_exist_in_html(name: str) -> None:
    """防拼写漂移：JS 引用的 DOM id 必须真在 admin.html 里。

    JS 引错 id 时浏览器只报 ``null.textContent`` 这类运行时错误，HTTP 层完全看不到，
    所以在这里做一次静态交叉校验。``model.js``（任务 2.4）也要覆盖——
    本任务的 2.4 浏览器验收正是靠这条 + 真实浏览器双重把关。
    """
    js = (STATIC_DIR / name).read_text(encoding="utf-8")
    html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")

    ids = set(re.findall(r"\bel\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    ids |= set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    ids -= DYNAMIC_DOM_IDS

    assert len(ids) >= 5, f"未从 {name} 提取到足够的 id 引用（正则可能失效）：{sorted(ids)}"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, f"{name} 引用了 admin.html 中不存在的 id：{missing}"


@pytest.mark.parametrize("name", FRONTEND_FILES)
def test_migrated_files_identical_to_prototype(name: str) -> None:
    """迁入的前端文件应与原型完全一致（迁移而非改写）。

    例外：登记在 :data:`MODIFIED_FROM_PROTOTYPE` 的文件为**有意改写**，
    由 :func:`test_modified_files_are_documented_and_differ` 单独把关。
    """
    proto = PROTO_DIR / name
    if not proto.is_file():
        pytest.skip(f"原型目录缺少 {name}")
    if name in MODIFIED_FROM_PROTOTYPE:
        pytest.skip(f"{name} 为有意改写（任务 1.9）：{MODIFIED_FROM_PROTOTYPE[name]}")
    assert (STATIC_DIR / name).read_bytes() == proto.read_bytes(), (
        f"{name} 与原型不一致——若确需修改请登记到 MODIFIED_FROM_PROTOTYPE 并写明原因"
    )


@pytest.mark.parametrize("name", sorted(MODIFIED_FROM_PROTOTYPE))
def test_modified_files_are_documented_and_differ(name: str) -> None:
    """白名单不能腐烂：条目必须有原因，且文件**确实**与原型不同。"""
    reason = MODIFIED_FROM_PROTOTYPE[name]
    assert len(reason) >= 20, f"{name} 的改写原因过于简略：{reason!r}"

    local = STATIC_DIR / name
    proto = PROTO_DIR / name
    assert local.is_file(), f"{name} 不存在"
    if proto.is_file():
        assert local.read_bytes() != proto.read_bytes(), (
            f"{name} 已登记为「有意改写」但内容与原型一致，应移出白名单"
        )


def test_no_skeleton_leftovers() -> None:
    """骨架期的说明页不应残留。"""
    assert (STATIC_DIR / "index.html").stat().st_size > 1500


def test_app_js_has_richtext_renderer() -> None:
    """任务 1.9：前端渲染器必须覆盖 Markdown 子集且带 XSS 防护。"""
    src = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in ("renderContent", "renderInlines", "safeUrl", "rt-table", "rt-code"):
        assert token in src, f"app.js 缺少 {token}"
    # 链接协议白名单必须存在，且不能在属性位置直接拼接未校验的地址
    assert "javascript" in src and "safeUrl(" in src


def test_style_css_has_richtext_styles() -> None:
    """任务 1.9：富文本块的样式必须就位。"""
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for token in (".rt-table", ".rt-code", ".rt-list", ".rt-quote"):
        assert token in css, f"style.css 缺少 {token}"


def test_app_js_has_data_card_renderer() -> None:
    """任务 4.3：问数卡片必须走 textContent 渲染并提供拒绝态。"""
    src = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in ("addDataCards", "dq-table", "dq-denied", "dataCards"):
        assert token in src, f"app.js 缺少 {token}"
    # 卡片只允许 el(...)/textContent 写入，禁止把上游字段拼进 innerHTML
    start = src.index("function addDataCards")
    end = src.index("function addAi")
    assert "innerHTML" not in src[start:end]


def test_style_css_has_data_card_styles() -> None:
    """任务 4.3：问数卡片样式必须就位。"""
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for token in (".dq-cards", ".dq-metrics", ".dq-table", ".dq-denied"):
        assert token in css, f"style.css 缺少 {token}"


def test_docs_list_has_admin_fields() -> None:
    """管理页 admin.js 依赖的字段必须齐全。"""
    docs = client.get("/api/docs").json()["data"]
    assert docs
    required = {
        "docId",
        "title",
        "fileKey",
        "fileFormat",
        "dept",
        "ktype",
        "status",
        "statusName",
        "chunkCount",
    }
    for d in docs:
        missing = required - set(d.keys())
        assert not missing, f"文档 {d.get('docId')} 缺少字段：{missing}"
        assert isinstance(d["status"], int), "status 应为数值（管理页筛选/配色用）"
        assert d["statusName"]


def test_meta_roles_first_is_all() -> None:
    """管理页把 roles[0] 默认勾选，必须是「全部」。"""
    meta = client.get("/api/meta").json()["data"]
    assert meta["roles"][0] == "全部"
    assert meta["ktypes"] and meta["depts"]
