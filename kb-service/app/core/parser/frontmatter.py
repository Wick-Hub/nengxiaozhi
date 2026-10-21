"""YAML frontmatter 解析（对齐 kb-engine/src/parse/frontmatter.js）。

只解析 `key: value` 单层结构，不引入 YAML 依赖，行为与原型一致。
"""

from __future__ import annotations

import re

_FM_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$")
_KV_RE = re.compile(r"^([A-Za-z_\u4e00-\u9fff-]+)\s*:\s*(.*)$")


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """返回 (meta, body)。无 frontmatter 时 meta 为空、body 为原文。"""
    meta: dict[str, str] = {}
    body = raw
    m = _FM_RE.match(raw)
    if m:
        for line in re.split(r"\r?\n", m.group(1)):
            kv = _KV_RE.match(line)
            if kv:
                meta[kv.group(1).strip()] = kv.group(2).strip()
        body = m.group(2)
    return meta, body
