"""Markdown / 纯文本解析（对齐 kb-engine/src/parse/markdown.js）。"""

from __future__ import annotations

import re

from app.core.parser.block import Block

_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_IMAGE_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)")
_SEP_RE = re.compile(r"^:?-{2,}:?$")


def split_row(line: str) -> list[str]:
    s = line
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def is_separator(cells: list[str]) -> bool:
    return len(cells) > 0 and all(_SEP_RE.match(c) for c in cells)


def to_md_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    sep = ["---"] * len(rows[0])
    lines = [rows[0], sep, *rows[1:]]
    return "\n".join("| " + " | ".join(r) + " |" for r in lines)


def md_to_blocks(md: str) -> list[Block]:
    lines = re.split(r"\r?\n", md)
    blocks: list[Block] = []
    para: list[str] = []
    table_lines: list[str] = []

    def flush_para() -> None:
        t = "\n".join(para).strip()
        if t:
            blocks.append(Block(type="para", text=t))
        para.clear()

    def flush_table() -> None:
        if not table_lines:
            return
        rows = [split_row(line) for line in table_lines]
        rows = [r for r in rows if not is_separator(r)]
        if rows:
            blocks.append(Block(type="table", text=to_md_table(rows), rows=rows))
        table_lines.clear()

    for line in lines:
        t = line.strip()
        if _TABLE_ROW_RE.match(t):
            if not table_lines:
                flush_para()
            table_lines.append(t)
            continue
        flush_table()
        if not t:
            flush_para()
            continue
        h = _HEADING_RE.match(t)
        if h:
            flush_para()
            blocks.append(Block(type="heading", level=len(h.group(1)), text=h.group(2).strip()))
            continue
        if _IMAGE_RE.match(t):
            flush_para()
            blocks.append(Block(type="image", text=t))
            continue
        para.append(t)
    flush_table()
    flush_para()
    return blocks
