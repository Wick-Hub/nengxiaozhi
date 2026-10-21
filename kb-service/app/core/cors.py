"""跨域配置（任务 3.9）。

嵌入 SDK 的第三方页面与知识服务通常不同源，需要显式放开：

* ``CORS_ALLOW_ORIGINS=*``（默认）适合先跑通；生产建议填学校站点白名单。
* 浏览器规范禁止「通配来源 + 凭证」，两者同时出现时这里强制关闭凭证，
  避免看似配置成功、实际预检失败。身份透传使用 ``Authorization`` 头，
  **不需要** ``allow_credentials``。
"""

from __future__ import annotations

from app.config import Settings


def parse_csv(raw: str | None) -> list[str]:
    values = [item.strip() for item in str(raw or "").split(",")]
    return [item for item in values if item]


def cors_options(settings: Settings) -> dict:
    origins = parse_csv(settings.cors_allow_origins) or ["*"]
    wildcard = "*" in origins
    expose = parse_csv(settings.cors_expose_headers)
    return {
        "allow_origins": ["*"] if wildcard else origins,
        "allow_credentials": bool(settings.cors_allow_credentials) and not wildcard,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "expose_headers": expose,
        "max_age": 600,
    }
