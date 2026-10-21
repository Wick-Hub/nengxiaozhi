"""日志脱敏（任务 1.12）。

对日志与审计明细中的**学号/工号、手机号、证件号、邮箱**做掩码，
避免敏感数据落盘。同时提供 :class:`MaskingFilter`，挂到 root logger 上全局生效。

掩码规则：

| 类型 | 示例输入 | 输出 |
| --- | --- | --- |
| 证件号（18 位） | `110101199001011234` | `110101********1234` |
| 手机号（11 位） | `13812345678` | `138****5678` |
| 学号/工号（8–12 位） | `20230001` | `2023****` |
| 邮箱 | `zhangsan@x.edu.cn` | `z***@x.edu.cn` |

**替换顺序敏感**：先证件号（18 位）→ 手机号（11 位）→ 学号（8–12 位）→ 邮箱。
否则 11 位手机号会先被「学号」规则吃掉。
"""

from __future__ import annotations

import logging
import re

_ID_CARD_RE = re.compile(r"(?<![0-9Xx])(\d{6})\d{8}(\d{3}[0-9Xx])(?![0-9Xx])")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_STUDENT_RE = re.compile(r"(?<!\d)(\d{4})\d{4,8}(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def mask_id_card(text: str) -> str:
    """证件号：保留前 6 后 4。"""
    return _ID_CARD_RE.sub(r"\1********\2", text)


def mask_phone(text: str) -> str:
    """手机号：保留前 3 后 4。"""
    return _PHONE_RE.sub(r"\1****\2", text)


def mask_student_no(text: str) -> str:
    """学号/工号：保留前 4。"""
    return _STUDENT_RE.sub(r"\1****", text)


def mask_email(text: str) -> str:
    """邮箱：只留首字符。"""
    return _EMAIL_RE.sub(r"\1***\2", text)


def mask_text(text: str) -> str:
    """按敏感度从强到弱依次脱敏（顺序不可调换）。"""
    if not text:
        return text
    return mask_email(mask_student_no(mask_phone(mask_id_card(text))))


class MaskingFilter(logging.Filter):
    """日志脱敏过滤器。

    在格式化**之前**把 ``msg % args`` 拼好再掩码，这样连参数里的敏感值也覆盖到。
    脱敏本身失败不能影响业务日志，因此整体包在 ``try`` 里。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging 约定
        try:
            if isinstance(record.msg, str):
                if record.args:
                    record.msg = mask_text(record.msg % record.args)
                    record.args = ()
                else:
                    record.msg = mask_text(record.msg)
        except Exception:  # noqa: BLE001 - 脱敏失败不阻断日志
            pass
        return True


def install_masking_filter(target: logging.Logger | None = None) -> MaskingFilter:
    """安装脱敏过滤器；重复调用不会重复挂载。

    **注意**：``logging`` 的 filter 只在记录**直接**经由该 logger 时才执行，
    子 logger 记录传播到父 logger 时**不会再过父 logger 的 filter**。
    因此除了 root logger 本身，还必须挂到 root 的每个 **handler** 上，
    这样所有经过该 handler 的记录（含子 logger 的）都会被脱敏。
    """
    root = target if target is not None else logging.getLogger()

    flt: MaskingFilter | None = None
    for existing in root.filters:
        if isinstance(existing, MaskingFilter):
            flt = existing
            break
    if flt is None:
        flt = MaskingFilter()
        root.addFilter(flt)

    for handler in root.handlers:
        if not any(isinstance(f, MaskingFilter) for f in handler.filters):
            handler.addFilter(flt)

    return flt


def mask_detail(detail: dict | None) -> dict | None:
    """对审计明细里的字符串值做递归脱敏（``operation_log.detail`` 用）。"""
    if detail is None:
        return None
    if isinstance(detail, dict):
        return {k: mask_detail(v) for k, v in detail.items()}
    if isinstance(detail, (list, tuple)):
        return [mask_detail(v) for v in detail]
    if isinstance(detail, str):
        return mask_text(detail)
    return detail
