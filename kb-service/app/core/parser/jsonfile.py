"""JSON 解析（对齐 kb-engine/src/parse/json.js）。

数组元素若含 q/question 视为问答对（qa），否则转为 para。
"""

from __future__ import annotations

import json

from app.core.parser.block import Block


def json_to_blocks(obj) -> list[Block]:
    if isinstance(obj, list):
        blocks: list[Block] = []
        for item in obj:
            if isinstance(item, dict) and (item.get("q") or item.get("question")):
                q = item.get("q") or item.get("question")
                a = item.get("a") or item.get("answer") or ""
                variants = item.get("variants")
                if isinstance(variants, list):
                    variants = "；".join(str(v) for v in variants)
                variants = variants or ""
                text = f"问：{q}\n答：{a}"
                if variants:
                    text += f"\n同义问法：{variants}"
                blocks.append(Block(type="qa", text=text))
            elif isinstance(item, dict):
                blocks.append(Block(type="para", text=json.dumps(item, ensure_ascii=False)))
        return blocks
    return [Block(type="para", text=json.dumps(obj, ensure_ascii=False, indent=1))]
