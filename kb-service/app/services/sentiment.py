"""轻量情绪打标（任务 4.6 / M-S-02）。

词表规则而非模型调用：客服工单与问答消息共用同一口径，标签写库后
3.10 统计的「咨询情绪」即可出数。口径与 ``chat_message.sentiment`` 一致：

* ``1`` 负面
* ``0`` 正常

只对用户输入打标，客服/AI 回复不参与判定。规则可解释、离线可用，
后续接入模型时替换 :func:`tag` 的实现即可。
"""

from __future__ import annotations

NEGATIVE = 1
NORMAL = 0

LABELS = {NEGATIVE: "负面", NORMAL: "正常"}

#: 明确表达不满 / 故障 / 投诉的词。命中任一即标负面。
NEGATIVE_WORDS: tuple[str, ...] = (
    "投诉",
    "举报",
    "差评",
    "垃圾",
    "太差",
    "很差",
    "极差",
    "生气",
    "愤怒",
    "不满",
    "失望",
    "无语",
    "离谱",
    "坑人",
    "骗人",
    "没人管",
    "没人处理",
    "一直没人",
    "处理不了",
    "解决不了",
    "太慢",
    "拖了",
    "故障",
    "坏了",
    "打不开",
    "登不上",
    "用不了",
    "失败",
    "扣费",
    "多扣",
    "退款",
    "退钱",
    "断网",
    "崩溃",
    "急",
    "催",
)

#: 否定语境里出现这些词时，不按负面处理（如「不着急」「没有故障」）。
_SOFTENERS: tuple[str, ...] = ("不急", "不着急", "没有故障", "没坏", "不投诉")


def tag(text: str) -> int:
    """返回 1（负面）或 0（正常）。空文本按正常处理。"""
    content = (text or "").strip()
    if not content:
        return NORMAL
    for phrase in _SOFTENERS:
        if phrase in content:
            content = content.replace(phrase, "")
    return NEGATIVE if any(word in content for word in NEGATIVE_WORDS) else NORMAL


def label(code: int | None) -> str:
    return LABELS.get(int(code or 0), "正常")
