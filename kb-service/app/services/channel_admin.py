"""渠道接入管理服务（任务 3.1 / 3.2 / M-C-01·02·04）。

职责：渠道 CRUD、绑定助手、生成渠道 URL 与网页集成代码（iframe / 浮窗），
以及门户个性化配置（主题、AI 形象、常见问题、快捷方式、推荐应用、客服/反馈
入口）的校验与渠道一键复制。

**渠道路由口径**：对外链接固定为 ``/?channel=<code>``，iframe/浮窗加
``embed=1``。前端对话页读取 ``channel`` 参数后取本模块的公开信息，并把
``defaultAgentId`` 作为 ``agentId`` 传给问答接口——这就是 3.1 验收里的
「集成代码可嵌入外部页面并正确路由」。

**配置存放**：个性化字段全部收在 ``channel.theme_config``（JSON）里，不新增
数据库列；3.1 已建好的 ``theme_config`` / ``copy_of`` 两列足以承载 3.2。
"""

from __future__ import annotations

import html
import json
import logging
import re
from urllib.parse import quote

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import agent_store, channel_store
from app.db.session import DatabaseUnavailable

logger = logging.getLogger(__name__)

#: 渠道编码：小写字母/数字开头，允许 ``-`` / ``_``，2–64 位（与 agentCode 同规则）
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
MAX_NAME = 128
MAX_WELCOME = 1024
MAX_THEME_BYTES = 16384
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_PRIMARY = "#5b8cff"

#: 门户个性化配置上限（任务 3.2 / M-C-02）
MAX_FAQ = 8
MAX_FAQ_ITEM = 120
MAX_SHORTCUTS = 8
MAX_SHORTCUT_LABEL = 16
MAX_SHORTCUT_PROMPT = 200
MAX_APPS = 8
MAX_APP_NAME = 32
MAX_APP_DESC = 80
MAX_ENTRY_LABEL = 16
MAX_LINK = 512
MAX_AVATAR_TEXT = 6
MAX_ASSISTANT_NAME = 32

#: 会话路由（任务 3.4 / M-C-07）：渠道 × 角色 → 助手 / 欢迎语 / 引导问题 / 模块开关
ROLES = ("访客", "本科生", "研究生", "教职工", "离退休人员")
ROUTE_MODULES = (
    "guideQuestions",
    "shortcuts",
    "recommendedApps",
    "appSquare",
    "serviceEntry",
    "feedback",
)
MAX_GUIDES = 6
MAX_GUIDE_ITEM = 120
MAX_ROUTE_BYTES = 16384

_UNSAFE_SCHEME = ("javascript:", "vbscript:", "data:", "file:", "blob:")
_SAFE_PREFIX = ("http://", "https://", "/", "#", "./", "../")


def default_modules() -> dict:
    """角色路由的模块开关缺省值：全部展示（与 3.2 未配置路由时一致）。"""
    return {key: 1 for key in ROUTE_MODULES}


def normalize_role(raw) -> str:
    """未知/空身份一律按访客处理，避免前端自造角色绕过路由。"""
    role = (raw or "").strip()
    return role if role in ROLES else "访客"


def _clean_modules(raw) -> tuple[dict | None, str | None]:
    if raw is None:
        return default_modules(), None
    if not isinstance(raw, dict):
        return None, "功能模块开关必须是对象"
    unknown = set(raw) - set(ROUTE_MODULES)
    if unknown:
        return None, "未知功能模块：" + "、".join(sorted(unknown))
    modules = default_modules()
    for key, value in raw.items():
        if isinstance(value, bool):
            modules[key] = 1 if value else 0
            continue
        try:
            modules[key] = 1 if int(value) else 0
        except (TypeError, ValueError):
            return None, f"功能模块「{key}」开关取值非法"
    return modules, None


def _clean_route(raw, agent_ids: list[int]) -> tuple[dict | None, str | None]:
    """校验角色路由配置（任务 3.4 / M-C-07）。

    ``routes`` 按身份各一条：未配置的角色沿用渠道默认助手、欢迎语与全部模块，
    因此老渠道无需迁移即可继续工作。路由助手必须是**该渠道已绑定**的助手，
    否则公开端会出现「配了 A 却路由到 B」的隐式回退。
    """
    if raw is None:
        return {"routes": []}, None
    if not isinstance(raw, dict):
        return None, "角色路由配置必须是对象"
    routes_raw = raw.get("routes")
    if routes_raw is None:
        routes_raw = []
    if not isinstance(routes_raw, list):
        return None, "角色路由必须是数组"
    if len(routes_raw) > len(ROLES):
        return None, f"角色路由最多 {len(ROLES)} 条"
    bound = {int(aid) for aid in agent_ids}
    routes: list[dict] = []
    seen: set[str] = set()
    for idx, value in enumerate(routes_raw, 1):
        if not isinstance(value, dict):
            return None, f"第 {idx} 条角色路由格式不正确"
        role = str(value.get("role") or "").strip()
        if role not in ROLES:
            return None, f"第 {idx} 条角色路由身份非法，可选：" + "、".join(ROLES)
        if role in seen:
            return None, f"角色「{role}」重复配置路由"
        seen.add(role)

        raw_agent = value.get("agentId")
        agent_id: int | None
        if raw_agent in (None, "", 0, "0"):
            agent_id = None
        else:
            try:
                agent_id = int(raw_agent)
            except (TypeError, ValueError):
                return None, f"角色「{role}」的路由助手 ID 非法"
            if agent_id not in bound:
                return None, f"角色「{role}」的路由助手不在渠道绑定集合：agentId={agent_id}"

        welcome = _clean_text(value.get("welcomeText"), MAX_WELCOME)
        guides_raw = value.get("guideQuestions")
        if guides_raw is None:
            guides_raw = []
        if not isinstance(guides_raw, list):
            return None, f"角色「{role}」的引导问题必须是数组"
        guides: list[str] = []
        for item in guides_raw:
            if not isinstance(item, str):
                return None, f"角色「{role}」的引导问题条目必须是文本"
            text = item.strip()
            if not text:
                continue
            if len(text) > MAX_GUIDE_ITEM:
                return None, f"角色「{role}」的单条引导问题不能超过 {MAX_GUIDE_ITEM} 个字符"
            if text not in guides:
                guides.append(text)
        if len(guides) > MAX_GUIDES:
            return None, f"角色「{role}」的引导问题最多 {MAX_GUIDES} 条"

        modules, err = _clean_modules(value.get("modules"))
        if err:
            return None, f"角色「{role}」：{err}"
        routes.append(
            {
                "role": role,
                "agentId": agent_id,
                "welcomeText": welcome,
                "guideQuestions": guides,
                "modules": modules,
            }
        )
    result = {"routes": routes}
    try:
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None, "角色路由配置包含不可序列化的值"
    if size > MAX_ROUTE_BYTES:
        return None, f"角色路由配置不能超过 {MAX_ROUTE_BYTES} 字节"
    return result, None


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


def _clean_text(raw, limit: int) -> str:
    return (raw or "").strip()[:limit]


def _clean_code(raw) -> tuple[str | None, str | None]:
    code = (raw or "").strip().lower()
    if not code:
        return None, "渠道编码不能为空"
    if not CODE_RE.match(code):
        return None, "渠道编码需为 2–64 位小写字母/数字/-/_，且以字母或数字开头"
    return code, None


def _clean_link(raw, *, limit: int = MAX_LINK) -> tuple[str | None, str | None]:
    """门户跳转链接：仅允许 http(s)、站内路径与页内锚点，拒绝脚本协议。"""
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, "链接必须是字符串"
    url = raw.strip()
    if not url:
        return "", None
    if len(url) > limit:
        return None, f"链接不能超过 {limit} 个字符"
    if re.search(r"[\s\u0000-\u001f\u007f]", url):
        return None, "链接不能包含空白或控制字符"
    compact = url.lower()
    if compact.startswith(_UNSAFE_SCHEME):
        return None, "链接协议不安全，仅支持 http(s) 或站内路径"
    if not compact.startswith(_SAFE_PREFIX):
        return None, "链接需以 http(s)://、/ 或 # 开头"
    return url, None


def _clean_faq(raw) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "常见问题必须是数组"
    items: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            return None, "常见问题条目必须是文本"
        text = value.strip()
        if not text:
            continue
        if len(text) > MAX_FAQ_ITEM:
            return None, f"单条常见问题不能超过 {MAX_FAQ_ITEM} 个字符"
        if text not in items:
            items.append(text)
    if len(items) > MAX_FAQ:
        return None, f"常见问题最多 {MAX_FAQ} 条"
    return items, None


def _clean_shortcuts(raw) -> tuple[list[dict] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "快捷方式必须是数组"
    if len(raw) > MAX_SHORTCUTS:
        return None, f"快捷方式最多 {MAX_SHORTCUTS} 个"
    items: list[dict] = []
    for idx, value in enumerate(raw, 1):
        if not isinstance(value, dict):
            return None, f"第 {idx} 个快捷方式格式不正确"
        label = _clean_text(value.get("label"), MAX_SHORTCUT_LABEL)
        if not label:
            return None, f"第 {idx} 个快捷方式缺少名称"
        action = str(value.get("action") or "ask").strip().lower()
        if action not in {"ask", "link"}:
            return None, f"第 {idx} 个快捷方式动作只能是 ask / link"
        content = (value.get("content") or "").strip()
        if not content:
            return None, f"第 {idx} 个快捷方式缺少内容"
        if action == "link":
            content, err = _clean_link(content)
            if err:
                return None, f"第 {idx} 个快捷方式：{err}"
            if not content:
                return None, f"第 {idx} 个快捷方式缺少链接"
        elif len(content) > MAX_SHORTCUT_PROMPT:
            return None, f"第 {idx} 个快捷方式的问题不能超过 {MAX_SHORTCUT_PROMPT} 个字符"
        items.append(
            {
                "icon": _clean_text(value.get("icon"), 8),
                "label": label,
                "action": action,
                "content": content,
            }
        )
    return items, None


def _clean_apps(raw) -> tuple[list[dict] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "推荐应用必须是数组"
    if len(raw) > MAX_APPS:
        return None, f"推荐应用最多 {MAX_APPS} 个"
    items: list[dict] = []
    for idx, value in enumerate(raw, 1):
        if not isinstance(value, dict):
            return None, f"第 {idx} 个推荐应用格式不正确"
        name = _clean_text(value.get("name"), MAX_APP_NAME)
        if not name:
            return None, f"第 {idx} 个推荐应用缺少名称"
        url, err = _clean_link(value.get("url"))
        if err:
            return None, f"第 {idx} 个推荐应用：{err}"
        if not url:
            return None, f"第 {idx} 个推荐应用缺少链接"
        items.append(
            {
                "icon": _clean_text(value.get("icon"), 8),
                "name": name,
                "desc": _clean_text(value.get("desc"), MAX_APP_DESC),
                "url": url,
            }
        )
    return items, None


def _clean_entry(raw, default_label: str) -> tuple[dict | None, str | None]:
    if raw is None:
        return {"enabled": 0, "label": default_label, "url": ""}, None
    if not isinstance(raw, dict):
        return None, f"{default_label}入口配置必须是对象"
    try:
        enabled = 1 if int(raw.get("enabled", 0)) else 0
    except (TypeError, ValueError):
        return None, f"{default_label}入口开关取值非法"
    url, err = _clean_link(raw.get("url"))
    if err:
        return None, f"{default_label}入口：{err}"
    label = _clean_text(raw.get("label"), MAX_ENTRY_LABEL) or default_label
    if enabled and not url:
        return None, f"启用{default_label}入口时请填写链接"
    return {"enabled": enabled, "label": label, "url": url}, None


def _clean_theme(raw) -> tuple[dict | None, str | None]:
    """校验并归一化门户配置；未知键原样保留，便于后续扩展。

    已知键（任务 3.2 / M-C-02）：

    * 外观：``primaryColor`` / ``position`` / ``launcherText``
    * AI 形象：``assistantName`` / ``avatarText`` / ``avatarUrl``
    * 内容：``faq``（≤8 条）、``shortcuts``（≤8 个，ask/link）、
      ``recommendedApps``（≤8 个）
    * 入口：``serviceEntry`` / ``feedback``（``{enabled,label,url}``）
    """
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "主题配置必须是对象"
    theme = dict(raw)
    color = theme.get("primaryColor")
    if color is not None and (not isinstance(color, str) or not COLOR_RE.match(color)):
        return None, "主题色需为 #RRGGBB 格式"
    position = theme.get("position")
    if position is not None and position not in {"left", "right"}:
        return None, "浮窗位置只能是 left / right"
    launcher = theme.get("launcherText")
    if launcher is not None and (not isinstance(launcher, str) or len(launcher) > 16):
        return None, "浮窗按钮文案不能超过 16 个字符"

    if "assistantName" in theme:
        name = theme.get("assistantName")
        if not isinstance(name, str) or len(name.strip()) > MAX_ASSISTANT_NAME:
            return None, f"助手形象名称不能超过 {MAX_ASSISTANT_NAME} 个字符"
        theme["assistantName"] = name.strip()
    if "avatarText" in theme:
        text = theme.get("avatarText")
        if not isinstance(text, str) or len(text.strip()) > MAX_AVATAR_TEXT:
            return None, f"头像文字不能超过 {MAX_AVATAR_TEXT} 个字符"
        theme["avatarText"] = text.strip()
    if "avatarUrl" in theme:
        url, err = _clean_link(theme.get("avatarUrl"))
        if err:
            return None, f"头像图片：{err}"
        theme["avatarUrl"] = url
    if "faq" in theme:
        items, err = _clean_faq(theme.get("faq"))
        if err:
            return None, err
        theme["faq"] = items
    if "shortcuts" in theme:
        items, err = _clean_shortcuts(theme.get("shortcuts"))
        if err:
            return None, err
        theme["shortcuts"] = items
    if "recommendedApps" in theme:
        items, err = _clean_apps(theme.get("recommendedApps"))
        if err:
            return None, err
        theme["recommendedApps"] = items
    if "serviceEntry" in theme:
        entry, err = _clean_entry(theme.get("serviceEntry"), "客服")
        if err:
            return None, err
        theme["serviceEntry"] = entry
    if "feedback" in theme:
        entry, err = _clean_entry(theme.get("feedback"), "反馈")
        if err:
            return None, err
        theme["feedback"] = entry

    try:
        size = len(json.dumps(theme, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None, "主题配置包含不可序列化的值"
    if size > MAX_THEME_BYTES:
        return None, f"主题配置不能超过 {MAX_THEME_BYTES} 字节"
    return theme, None


def _agent_map() -> tuple[dict[int, dict], str | None]:
    try:
        agents = agent_store.list_agents()
    except DatabaseUnavailable as exc:
        return {}, f"数据库不可用：{exc}"
    except Exception as exc:  # noqa: BLE001 - 前端要可读错误
        logger.exception("读取助手列表失败")
        return {}, f"读取助手列表失败：{exc}"
    return {int(a["agentId"]): a for a in agents}, None


def _clean_agent_ids(raw, default_agent_id: int, agents: dict[int, dict]) -> tuple[list[int], str | None]:
    if raw is None:
        ids: list[int] = [int(default_agent_id)]
    elif not isinstance(raw, (list, tuple)):
        return [], "agentIds 必须是数组"
    else:
        ids = []
        for value in raw:
            try:
                aid = int(value)
            except (TypeError, ValueError):
                return [], f"助手 ID 非法：{value}"
            if aid > 0 and aid not in ids:
                ids.append(aid)
        if int(default_agent_id) not in ids:
            ids.insert(0, int(default_agent_id))
    if int(default_agent_id) not in ids:
        ids.insert(0, int(default_agent_id))
    if int(default_agent_id) not in agents:
        return [], f"默认助手不存在：agentId={default_agent_id}"
    missing = [aid for aid in ids if aid not in agents]
    if missing:
        return [], "以下助手不存在：" + "、".join(str(aid) for aid in missing)
    return ids, None


def _base_url(override: str | None = None) -> str:
    raw = (override or settings.public_base_url or "").strip()
    return raw.rstrip("/")


def build_assets(record: dict, base_url: str) -> dict:
    """生成渠道 URL 与 iframe / 浮窗代码；``base_url`` 为空时保留相对地址。"""
    code = record.get("channelCode") or ""
    name = record.get("channelName") or "能小智"
    base = _base_url(base_url)
    channel_url = f"{base}/?channel={quote(code, safe='')}"
    embed_url = f"{channel_url}&embed=1"
    theme = record.get("themeConfig") or {}
    color = theme.get("primaryColor") if isinstance(theme, dict) else None
    if not isinstance(color, str) or not COLOR_RE.match(color):
        color = DEFAULT_PRIMARY
    position = theme.get("position") if isinstance(theme, dict) else None
    if position not in {"left", "right"}:
        position = "right"
    launcher_text = theme.get("launcherText") if isinstance(theme, dict) else None
    if not isinstance(launcher_text, str) or not launcher_text.strip():
        launcher_text = "有问题问我"

    title = html.escape(name, quote=True)
    src = html.escape(embed_url, quote=True)
    iframe_snippet = (
        f'<iframe src="{src}" title="{title}" width="420" height="620" '
        'style="border:0;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.18)" '
        'loading="lazy" referrerpolicy="strict-origin-when-cross-origin"></iframe>'
    )

    # 浮窗代码是自包含的：外部页面只需在 </body> 前粘贴一次，无需再引入 SDK。
    # 正式 SDK（任务 3.9）见下方 sdkSnippet：支持多实例、编程式开关与身份透传。
    js_src = json.dumps(embed_url, ensure_ascii=False).replace("</", "<\\/")
    js_label = json.dumps(str(launcher_text), ensure_ascii=False).replace("</", "<\\/")
    side = "right: 24px;" if position == "right" else "left: 24px;"
    float_snippet = f"""<script>
(function () {{
  var src = {js_src};
  var label = {js_label};
  var open = false;
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = label;
  btn.setAttribute('aria-label', label);
  btn.style.cssText = 'position:fixed;{side}bottom:24px;z-index:2147483001;border:0;border-radius:999px;padding:12px 18px;background:{color};color:#fff;font-size:14px;cursor:pointer;box-shadow:0 8px 24px rgba(0,0,0,.25)';
  var panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;{side}bottom:82px;z-index:2147483002;width:400px;max-width:calc(100vw - 32px);height:600px;max-height:calc(100vh - 110px);display:none;border-radius:14px;overflow:hidden;box-shadow:0 18px 60px rgba(0,0,0,.35);background:#fff';
  var frame = document.createElement('iframe');
  frame.src = src;
  frame.title = {json.dumps(name, ensure_ascii=False)};
  frame.style.cssText = 'width:100%;height:100%;border:0';
  var close = document.createElement('button');
  close.type = 'button';
  close.textContent = '×';
  close.setAttribute('aria-label', '关闭');
  close.style.cssText = 'position:absolute;top:8px;right:8px;width:28px;height:28px;border:0;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:18px;line-height:1;cursor:pointer';
  panel.appendChild(frame);
  panel.appendChild(close);
  document.body.appendChild(btn);
  document.body.appendChild(panel);
  btn.addEventListener('click', function () {{
    open = !open;
    panel.style.display = open ? 'block' : 'none';
  }});
  close.addEventListener('click', function () {{
    open = false;
    panel.style.display = 'none';
  }});
}})();
</script>"""
    sdk_url = f"{base}/sdk/nxz.js"
    sdk_attrs = " ".join(
        [
            f'src="{html.escape(sdk_url, quote=True)}"',
            f'data-channel="{html.escape(code, quote=True)}"',
            f'data-position="{position}"',
            f'data-color="{color}"',
            f'data-label="{html.escape(launcher_text, quote=True)}"',
            f'data-title="{title}"',
            "defer",
        ]
    )
    sdk_snippet = f"<script {sdk_attrs}></script>"
    return {
        "channelUrl": channel_url,
        "embedUrl": embed_url,
        "iframeSnippet": iframe_snippet,
        "floatSnippet": float_snippet,
        "sdkUrl": sdk_url,
        "sdkSnippet": sdk_snippet,
        # 兼容 DDL 的 embed_snippet 列：默认存 iframe 版本
        "embedSnippet": iframe_snippet,
    }


def _agent_cards(
    record: dict,
    agents: dict[int, dict],
    *,
    public: bool = False,
    role: str | None = None,
) -> list[dict]:
    """渠道绑定的助手卡片。

    ``public=True`` 时只下发上架且对当前身份可见的助手（任务 3.8：用户端
    助手矩阵不能出现「已下架」或「本身份不可见」的助手）。
    """
    cards: list[dict] = []
    for aid in record.get("agentIds") or []:
        agent = agents.get(int(aid))
        if agent is None:
            continue
        if public and int(agent.get("status", 0)) != 1:
            continue
        visible_roles = agent.get("visibleRoles") or ["全部"]
        if public and role is not None and "全部" not in visible_roles and role not in visible_roles:
            continue
        card = {
            "agentId": int(agent["agentId"]),
            "agentName": agent.get("agentName") or "",
            "agentCode": agent.get("agentCode") or "",
            "agentType": agent.get("agentType") or "chat",
            "iconUrl": agent.get("iconUrl") or "",
            "description": agent.get("description") or "",
            "guideQuestions": list(agent.get("guideQuestions") or []),
            "status": int(agent.get("status", 0)),
            "statusName": agent.get("statusName") or "",
            "isDefault": int(agent["agentId"]) == int(record["defaultAgentId"]),
        }
        cards.append(card)
    return cards


def _decorate(record: dict, agents: dict[int, dict], base_url: str) -> dict:
    cards = _agent_cards(record, agents)
    default = next((c for c in cards if c["isDefault"]), None)
    assets = build_assets(record, base_url)
    return {
        **record,
        **assets,
        "agents": cards,
        "defaultAgentName": default["agentName"] if default else "",
        "statusName": channel_store.status_name(record["status"]),
        "editable": True,
    }


def list_channels(base_url: str = "") -> dict:
    agents, error = _agent_map()
    if error:
        return _fail(error, status=503)
    try:
        items = channel_store.list_channels()
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    except Exception as exc:  # noqa: BLE001
        logger.exception("渠道列表查询失败")
        return _fail(f"渠道列表查询失败：{exc}", status=503)

    decorated = [_decorate(item, agents, base_url) for item in items]
    options = [
        {
            "agentId": a["agentId"],
            "agentCode": a.get("agentCode") or "",
            "agentName": a.get("agentName") or "",
            "agentType": a.get("agentType") or "chat",
            "iconUrl": a.get("iconUrl") or "",
            "status": a.get("status", 0),
            "statusName": a.get("statusName") or "",
        }
        for a in sorted(agents.values(), key=lambda x: x["agentId"])
    ]
    return _ok(
        {
            "items": decorated,
            "total": len(decorated),
            "enabled": sum(1 for item in decorated if item["status"] == 1),
            "agentOptions": options,
            "baseUrl": _base_url(base_url),
            "editable": True,
            "source": settings.store_label,
        }
    )


def get_channel(channel_id: int, base_url: str = "") -> dict:
    agents, error = _agent_map()
    if error:
        return _fail(error, status=503)
    try:
        item = channel_store.get_channel(channel_id)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    if item is None:
        return _fail(f"渠道不存在：channelId={channel_id}", status=404)
    return _ok(_decorate(item, agents, base_url))


def _validate_new(payload: dict, agents: dict[int, dict]) -> tuple[dict | None, str | None]:
    code, err = _clean_code(payload.get("channelCode"))
    if err:
        return None, err
    name = _clean_text(payload.get("channelName"), MAX_NAME)
    if not name:
        return None, "渠道名称不能为空"
    try:
        default_agent_id = int(payload.get("defaultAgentId"))
    except (TypeError, ValueError):
        return None, "请选择默认助手"
    if default_agent_id not in agents:
        return None, f"默认助手不存在：agentId={default_agent_id}"
    welcome = _clean_text(payload.get("welcomeText"), MAX_WELCOME)
    theme, err = _clean_theme(payload.get("themeConfig"))
    if err:
        return None, err
    try:
        need_auth = 1 if int(payload.get("needAuth", 1)) else 0
        status = 1 if int(payload.get("status", 1)) else 0
    except (TypeError, ValueError):
        return None, "认证开关/状态取值非法"
    agent_ids, err = _clean_agent_ids(payload.get("agentIds"), default_agent_id, agents)
    if err:
        return None, err
    route_config, err = _clean_route(payload.get("routeConfig"), agent_ids)
    if err:
        return None, err
    return {
        "channel_code": code,
        "channel_name": name,
        "default_agent_id": default_agent_id,
        "welcome_text": welcome,
        "theme_config": theme,
        "route_config": route_config,
        "need_auth": need_auth,
        "status": status,
        "agent_ids": agent_ids,
    }, None


def create_channel(payload: dict, *, base_url: str = "") -> dict:
    agents, error = _agent_map()
    if error:
        return _fail(error, status=503)
    fields, err = _validate_new(payload, agents)
    if err:
        return _fail(err, status=400)
    try:
        if channel_store.get_by_code(fields["channel_code"]) is not None:
            return _fail(f"渠道编码已存在：{fields['channel_code']}", status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)

    assets = build_assets(
        {
            "channelCode": fields["channel_code"],
            "channelName": fields["channel_name"],
            "themeConfig": fields["theme_config"],
        },
        base_url,
    )
    try:
        channel_id = channel_store.create_channel(
            channel_code=fields["channel_code"],
            channel_name=fields["channel_name"],
            default_agent_id=fields["default_agent_id"],
            welcome_text=fields["welcome_text"],
            theme_config=fields["theme_config"],
            route_config=fields["route_config"],
            embed_url=assets["channelUrl"][:255],
            embed_snippet=assets["embedSnippet"],
            need_auth=fields["need_auth"],
            status=fields["status"],
            agent_ids=fields["agent_ids"],
        )
        item = channel_store.get_channel(channel_id)
    except IntegrityError:
        return _fail(f"渠道编码已存在：{fields['channel_code']}", status=409)
    except ValueError as exc:
        return _fail(str(exc), status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    if item is None:
        return _fail("渠道创建后读取失败", status=500)
    return _ok(_decorate(item, agents, base_url))


def _next_copy_code(source_code: str) -> tuple[str | None, str | None]:
    base = f"{source_code}-copy"
    for suffix in ("", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9"):
        candidate = base[: 64 - len(suffix)] + suffix if suffix else base[:64]
        if channel_store.get_by_code(candidate) is None:
            return candidate, None
    return None, "该渠道副本过多，请手动指定渠道编码"


def _copy_route_config(raw: dict, agent_ids: list[int]) -> dict:
    """复制渠道时深拷贝角色路由；已解绑/删除的助手回退渠道默认，不阻断复制。"""
    try:
        config = json.loads(json.dumps(raw or {}, ensure_ascii=False))
    except (TypeError, ValueError):
        return {"routes": []}
    bound = {int(aid) for aid in agent_ids}
    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue
        agent_id = route.get("agentId")
        if agent_id is not None and int(agent_id) not in bound:
            route["agentId"] = None
    return config


def copy_channel(channel_id: int, payload: dict | None = None, *, base_url: str = "") -> dict:
    """复制渠道全部配置（任务 3.2 / M-C-04）。

    新渠道默认**停用**：复制出的链接不会在运营确认前对外生效；``copy_of``
    记录来源渠道 ID。助手若已删除会自动从绑定集合中剔除，默认助手失效时
    回退到首个可用助手。
    """
    payload = payload or {}
    agents, error = _agent_map()
    if error:
        return _fail(error, status=503)
    try:
        source = channel_store.get_channel(channel_id)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    if source is None:
        return _fail(f"渠道不存在：channelId={channel_id}", status=404)

    requested = (payload.get("channelCode") or "").strip().lower()
    try:
        if requested:
            code, err = _clean_code(requested)
            if err:
                return _fail(err, status=400)
        else:
            code, err = _next_copy_code(source["channelCode"])
            if err:
                return _fail(err, status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)

    requested_name = (payload.get("channelName") or "").strip()
    name = _clean_text(
        requested_name or f"{source['channelName']}（副本）", MAX_NAME
    )

    agent_ids = [
        int(aid)
        for aid in source.get("agentIds") or []
        if int(aid) in agents
    ]
    default_id = int(source["defaultAgentId"])
    if default_id not in agents:
        default_id = agent_ids[0] if agent_ids else next(iter(agents), 0)
    if not default_id:
        return _fail("没有可用助手，无法复制渠道", status=400)

    fields, err = _validate_new(
        {
            "channelCode": code,
            "channelName": name,
            "defaultAgentId": default_id,
            "welcomeText": source.get("welcomeText") or "",
            "themeConfig": json.loads(
                json.dumps(source.get("themeConfig") or {}, ensure_ascii=False)
            ),
            "routeConfig": _copy_route_config(
                source.get("routeConfig") or {}, agent_ids
            ),
            "needAuth": source.get("needAuth", 1),
            "status": 0,
            "agentIds": agent_ids or [default_id],
        },
        agents,
    )
    if err:
        return _fail(err, status=400)

    assets = build_assets(
        {
            "channelCode": fields["channel_code"],
            "channelName": fields["channel_name"],
            "themeConfig": fields["theme_config"],
        },
        base_url,
    )
    try:
        new_id = channel_store.create_channel(
            channel_code=fields["channel_code"],
            channel_name=fields["channel_name"],
            default_agent_id=fields["default_agent_id"],
            welcome_text=fields["welcome_text"],
            theme_config=fields["theme_config"],
            route_config=fields["route_config"],
            embed_url=assets["channelUrl"][:255],
            embed_snippet=assets["embedSnippet"],
            need_auth=fields["need_auth"],
            status=fields["status"],
            copy_of=int(channel_id),
            agent_ids=fields["agent_ids"],
        )
        item = channel_store.get_channel(new_id)
    except IntegrityError:
        return _fail(f"渠道编码已存在：{fields['channel_code']}", status=409)
    except ValueError as exc:
        return _fail(str(exc), status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    if item is None:
        return _fail("渠道复制后读取失败", status=500)
    return _ok(_decorate(item, agents, base_url))


def update_channel(channel_id: int, payload: dict, *, base_url: str = "") -> dict:
    agents, error = _agent_map()
    if error:
        return _fail(error, status=503)
    try:
        current = channel_store.get_channel(channel_id)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    if current is None:
        return _fail(f"渠道不存在：channelId={channel_id}", status=404)

    merged = {
        "channelCode": current["channelCode"],
        "channelName": current["channelName"],
        "defaultAgentId": current["defaultAgentId"],
        "welcomeText": current["welcomeText"],
        "themeConfig": current["themeConfig"],
        "routeConfig": current.get("routeConfig") or {},
        "needAuth": current["needAuth"],
        "status": current["status"],
        "agentIds": current["agentIds"],
    }
    merged.update({k: v for k, v in payload.items() if v is not None})
    if payload.get("agentIds") is not None and payload.get("routeConfig") is None:
        # 编辑渠道时解绑助手：失效的角色路由回退渠道默认，避免旧路由阻断保存
        merged["routeConfig"] = _copy_route_config(
            merged["routeConfig"], merged["agentIds"]
        )
    fields, err = _validate_new(merged, agents)
    if err:
        return _fail(err, status=400)
    try:
        duplicate = channel_store.get_by_code(fields["channel_code"])
        if duplicate is not None and int(duplicate["channelId"]) != int(channel_id):
            return _fail(f"渠道编码已存在：{fields['channel_code']}", status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)

    assets = build_assets(
        {
            "channelCode": fields["channel_code"],
            "channelName": fields["channel_name"],
            "themeConfig": fields["theme_config"],
        },
        base_url,
    )
    try:
        changed = channel_store.update_channel(
            channel_id,
            channel_code=fields["channel_code"],
            channel_name=fields["channel_name"],
            default_agent_id=fields["default_agent_id"],
            welcome_text=fields["welcome_text"],
            theme_config=fields["theme_config"],
            route_config=fields["route_config"],
            embed_url=assets["channelUrl"][:255],
            embed_snippet=assets["embedSnippet"],
            need_auth=fields["need_auth"],
            status=fields["status"],
            agent_ids=fields["agent_ids"],
        )
        item = channel_store.get_channel(channel_id)
    except IntegrityError:
        return _fail(f"渠道编码已存在：{fields['channel_code']}", status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    if not changed or item is None:
        return _fail(f"渠道不存在：channelId={channel_id}", status=404)
    return _ok(_decorate(item, agents, base_url))


def delete_channel(channel_id: int) -> dict:
    try:
        deleted = channel_store.delete_channel(channel_id)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    if not deleted:
        return _fail(f"渠道不存在：channelId={channel_id}", status=404)
    return _ok({"channelId": channel_id, "deleted": True})


def public_info(channel_code: str, role: str | None = None) -> dict:
    """门户/嵌入页读取的公开配置；停用或不存在统一 404。

    任务 3.4（M-C-07）：``role`` 命中渠道路由时，助手、欢迎语、引导问题与
    功能模块按该身份覆写；未配置该身份则整体沿用渠道默认（``route.matched=false``）。
    路由助手若已下架/解绑，回退到渠道默认助手，再回退到首个上架助手。
    """
    try:
        item = channel_store.get_by_code(channel_code)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    if item is None or item["status"] != 1:
        return _fail("渠道不存在或已停用", status=404)
    agents, error = _agent_map()
    if error:
        return _fail(error, status=503)
    normalized = normalize_role(role)
    cards = _agent_cards(item, agents, public=True, role=normalized)
    visible_ids = {card["agentId"] for card in cards}
    channel_default = item["defaultAgentId"] if item["defaultAgentId"] in visible_ids else None
    routes = (item.get("routeConfig") or {}).get("routes") or []
    matched = next(
        (r for r in routes if isinstance(r, dict) and r.get("role") == normalized), None
    )
    route_agent = None
    if matched and matched.get("agentId") is not None:
        aid = int(matched["agentId"])
        if aid in visible_ids:
            route_agent = aid
    default_agent_id = route_agent or channel_default
    if default_agent_id is None and cards:
        default_agent_id = cards[0]["agentId"]

    welcome_text = (matched or {}).get("welcomeText") or item["welcomeText"]
    guide_questions = list((matched or {}).get("guideQuestions") or [])
    modules = default_modules()
    if matched:
        modules.update(matched.get("modules") or {})
    return _ok(
        {
            "channelId": item["channelId"],
            "channelCode": item["channelCode"],
            "channelName": item["channelName"],
            "welcomeText": welcome_text,
            "defaultAgentId": default_agent_id,
            "themeConfig": item["themeConfig"],
            "needAuth": item["needAuth"],
            "agents": cards,
            "route": {
                "role": normalized,
                "matched": matched is not None,
                "agentId": default_agent_id,
                "welcomeText": welcome_text,
                "guideQuestions": guide_questions,
                "modules": modules,
            },
        }
    )
