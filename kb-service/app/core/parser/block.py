"""解析层的公共数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Block:
    """解析后的统一内容块。

    type: heading | para | table | qa | image
    """

    type: str
    text: str = ""
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class ParsedDocument:
    meta: dict = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
