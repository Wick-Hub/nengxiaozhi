"""切片层（任务 1.3）。

1:1 对齐 kb-engine/src/chunk/chunker.js：
    - 标题层级切片，stack 记录 title_path（"A > B"）
    - 表格整块、FAQ 整对、图片占位
    - 滚动窗口 max=400 / carry=60 / min=40（末尾碎片并入上一块）
    - 元数据 dept / ktype / visible_roles，缺省「全部」
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.core.parser import Block

# 与 kb-engine/src/config.js 的 KTYPE_DIRS 一致
KTYPE_DIRS = ["政策文件", "办事指南", "常见问题", "服务事项", "组织机构"]

_ROLE_SPLIT_RE = re.compile(r"[,，、]")
_DEPT_PREFIX_RE = re.compile(r"^\d+-")


@dataclass
class Chunk:
    chunk_id: str = ""
    doc_id: str = ""
    seq_no: int = 0
    char_count: int = 0
    dept: str = "未分类"
    ktype: str = "未分类"
    visible_roles: list[str] = field(default_factory=lambda: ["全部"])
    title_path: str = ""
    block_type: str = "para"
    text: str = ""
    embed_text: str = ""
    # 向量由任务 1.4 的 embedder 填充；切片阶段为空
    embedding: list[float] = field(default_factory=list)
    # 该切片引用的原图对象键（任务 1.10，落 kb_chunk.image_keys）
    image_keys: list[str] = field(default_factory=list)

    def to_dict(self, include_embedding: bool = False) -> dict:
        """转成与 store.json / 前端一致的下划线外字段名。

        ``imageKeys`` **仅在非空时输出**——否则会改变既有 store.json 的逐字段形状，
        破坏与原型索引的一致性回归。
        """
        d = {
            "chunkId": self.chunk_id,
            "docId": self.doc_id,
            "seqNo": self.seq_no,
            "charCount": self.char_count,
            "dept": self.dept,
            "ktype": self.ktype,
            "visibleRoles": self.visible_roles,
            "titlePath": self.title_path,
            "blockType": self.block_type,
            "text": self.text,
            "embedText": self.embed_text,
        }
        if self.image_keys:
            d["imageKeys"] = self.image_keys
        if include_embedding:
            d["embedding"] = self.embedding
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        """从 store.json / MySQL 行反序列化（与 to_dict 对称）。"""
        return cls(
            chunk_id=d.get("chunkId", ""),
            doc_id=d.get("docId", ""),
            seq_no=d.get("seqNo", 0),
            char_count=d.get("charCount", 0),
            dept=d.get("dept", "未分类"),
            ktype=d.get("ktype", "未分类"),
            visible_roles=list(d.get("visibleRoles") or ["全部"]),
            title_path=d.get("titlePath", ""),
            block_type=d.get("blockType", "para"),
            text=d.get("text", ""),
            embed_text=d.get("embedText", ""),
            embedding=list(d.get("embedding") or []),
            image_keys=list(d.get("imageKeys") or []),
        )


def parse_roles(v) -> list[str]:
    """解析 visible_roles：支持 `[a,b]` / `a、b` / 空值。空值或解析为空 -> ['全部']。"""
    if not v:
        return ["全部"]
    s = str(v).replace("[", "").replace("]", "")
    arr = [x.strip() for x in _ROLE_SPLIT_RE.split(s)]
    arr = [x for x in arr if x]
    return arr or ["全部"]


def _relative_parts(file_path: str | Path) -> list[str]:
    fp = Path(file_path).resolve()
    root = Path(settings.knowledge_dir).resolve()
    try:
        return list(fp.relative_to(root).parts)
    except ValueError:
        return [fp.parent.name, fp.name]


def infer_from_path(file_path: str | Path) -> tuple[str, str]:
    """从路径推断 dept（首段 `NN-` 前缀）与 ktype（第二段命中 KTYPE_DIRS）。"""
    rel = _relative_parts(file_path)
    dept = ""
    ktype = ""
    if rel and _DEPT_PREFIX_RE.match(rel[0]):
        dept = _DEPT_PREFIX_RE.sub("", rel[0])
    if len(rel) > 1 and rel[1] in KTYPE_DIRS:
        ktype = rel[1]
    return dept, ktype


def split_windows(text: str, size: int, carry: int) -> list[str]:
    """把超长文本切成不超过 size 的窗口，相邻窗口重叠 carry 个字符。

    修复原型「400 字不是硬上限」的问题：原型对超过 400 的段落会整块输出，
    导致单条切片可能远大于 400。此处改为真正的滑动窗口。
    文本不超过 size 时原样返回，保证与原型在常规样例上完全一致。
    """
    if size <= 0 or len(text) <= size:
        return [text]
    step = size - carry if size - carry > 0 else size
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        out.append(text[i : i + size])
        if i + size >= n:
            break
        i += step
    return out


def _image_keys_of(block: Block) -> list[str]:
    """从解析块 meta 里取原图对象键（任务 1.10）。

    ``docx``/``pptx`` 的图片块可能带多张（``image_keys``），图片型文档带一张
    （``image_key``）。两者都接受。
    """
    meta = block.meta or {}
    keys = meta.get("image_keys")
    if isinstance(keys, (list, tuple)) and keys:
        return [str(k) for k in keys if k]
    single = meta.get("image_key")
    return [str(single)] if single else []


def blocks_to_chunks(blocks: list[Block], doc_meta: dict, file_path: str | Path) -> list[Chunk]:
    """把解析块切成带元数据的切片。"""
    d_dept, d_ktype = infer_from_path(file_path)
    dept = doc_meta.get("dept") or d_dept or "未分类"
    ktype = doc_meta.get("ktype") or d_ktype or "未分类"
    visible_roles = parse_roles(doc_meta.get("visible_roles"))

    raw: list[dict] = []
    stack: list[str | None] = []
    cur: list[str] = []
    cur_images: list[str] = []

    def emit(text: str, block_type: str, images: list[str] | None = None) -> None:
        t = text.strip()
        if not t:
            return
        title_path = " > ".join(s for s in stack if s)
        raw.append(
            {
                "title_path": title_path,
                "block_type": block_type,
                "text": t,
                "image_keys": list(dict.fromkeys(images or [])),
            }
        )

    def flush() -> None:
        if cur:
            emit("\n".join(cur), "para", cur_images)
            cur.clear()
            cur_images.clear()

    for b in blocks:
        if b.type == "heading":
            flush()
            while len(stack) < b.level:
                stack.append(None)
            stack[b.level - 1] = b.text
            del stack[b.level:]
        elif b.type == "table":
            flush()
            emit(b.text, "table")
        elif b.type == "qa":
            flush()
            emit(b.text, "faq")
        elif b.type == "image":
            cur.append(f"[图片] {b.text}")
            cur_images.extend(_image_keys_of(b))
        elif b.type == "para":
            cur.append(b.text)
            cur_images.extend(_image_keys_of(b))
            buf = "\n".join(cur)
            if len(buf) > settings.chunk_max:
                for piece in split_windows(buf, settings.chunk_max, settings.chunk_carry):
                    emit(piece, "para", cur_images)
                tail = buf[-settings.chunk_carry :] if settings.chunk_carry else ""
                cur.clear()
                cur_images.clear()
                if tail:
                    cur.append(tail)
    flush()

    # 去掉结尾仅由 carry 重叠产生的重复碎片（是上一块的严格后缀且长度 <= carry）
    if (
        len(raw) > 1
        and raw[-1]["text"]
        and len(raw[-1]["text"]) <= settings.chunk_carry
        and raw[-2]["text"].endswith(raw[-1]["text"])
    ):
        raw.pop()

    # 末尾碎片（< min）并入上一块
    if len(raw) > 1 and len(raw[-1]["text"]) < settings.chunk_min:
        raw[-2]["text"] += "\n" + raw[-1]["text"]
        raw[-2]["image_keys"] = list(
            dict.fromkeys(raw[-2]["image_keys"] + raw[-1]["image_keys"])
        )
        raw.pop()

    chunks: list[Chunk] = []
    for i, c in enumerate(raw):
        title_path = c["title_path"]
        text = c["text"]
        chunks.append(
            Chunk(
                seq_no=i + 1,
                char_count=len(text),
                dept=dept,
                ktype=ktype,
                visible_roles=visible_roles,
                title_path=title_path,
                block_type=c["block_type"],
                text=text,
                embed_text=f"{title_path}\n{text}" if title_path else text,
                image_keys=c["image_keys"],
            )
        )
    return chunks
