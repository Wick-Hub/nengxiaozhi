"""平台级默认话术的唯一来源（任务 2.11）。

助手可在管理页覆盖 ``riskReply`` / ``fallbackReply``；未配置时统一回落到这里，
避免同一句话散落在生成层、安全层与存储层三处各写一份。
"""

from __future__ import annotations

#: 命中敏感词 / 越狱时的风控话术
DEFAULT_RISK_REPLY = "抱歉，该问题涉及敏感内容，我无法回答，建议联系相关部门咨询。"

#: 未召回 / 模型拒答时的兜底话术
DEFAULT_FALLBACK_REPLY = "抱歉，知识库中未找到相关信息，建议联系相关部门或转人工咨询。"
