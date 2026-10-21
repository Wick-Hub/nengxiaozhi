"""原文预览与切片对比服务（任务 1.11 / 关联 M-K-08）。

把「文档 → 原件 → 原文纯文本 → 切片」串成一条可复现的预览链路：

* :func:`resolve_original` —— ``kb_doc.file_key`` → 原件物理路径（含路径穿越防护）
* :func:`extract_text` —— 把原件还原为纯文本（带 mtime + size 缓存）
* :func:`search_chunks` —— 切片关键词检索（多词 AND + 命中区间，供前端高亮）

**file_key 的两种形态**（与 :mod:`app.services.kb_admin` 一致）：

* ``uploads/<毫秒时间戳>-<安全文件名>`` —— 上传件，落在 ``settings.upload_dir``；
* 其它 —— 知识目录相对路径（如 ``01-信网中心/办事指南/校园网开户与报修指南.md``），
  落在 ``settings.knowledge_dir``。

原型 ``ingestUploaded`` 会在 ``finally`` 里删掉 staging 原件，导致 ``file_key``
指向不存在的文件、原文预览无从取件；本实现**保留原件**，因此上传件的原文预览同样可用
（见 :mod:`app.services.kb_admin` 模块注释）。

文本提取的两种口径（字段 ``source`` 区分）：

* ``raw`` —— ``.md/.txt/.csv/.json`` 等文本格式直接解码原文件，**所见即原件**；
* ``parsed`` —— 二进制格式（docx/pdf/xlsx/xls）复用解析层 ``parse_file`` 的块，
  按标题/段落/表格还原为可读文本。

两种口径都只用于**展示**；切片始终来自解析层，二者共同构成「原文 ↔ 切片」的对比基准。
"""

from __future__ import annotations

import logging
import mimetypes
import posixpath
import re
import threading
from pathlib import Path

from app.config import settings
from app.core.parser import PLANNED_EXT, SUPPORTED_EXT, parse_file

logger = logging.getLogger(__name__)

#: 上传件的 ``file_key`` 前缀（与 :mod:`app.services.kb_admin` 的写入口径一致）
UPLOAD_PREFIX = "uploads/"

#: 可直接按文本解码的格式（其余走解析层）
TEXT_EXT: frozenset[str] = frozenset({".md", ".txt", ".csv", ".json"})

#: 图片格式（任务 1.10）：既是「图片型文档」的判据，也是原图接口的白名单
IMAGE_EXT: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
)

#: 浏览器可内联展示的格式（其余一律走 ``attachment`` 下载）。
#: 注意：**不含 ``.svg`` / ``.html``** —— 它们可携带脚本，内联展示等于同源执行上传内容。
INLINE_EXT: frozenset[str] = frozenset(
    {
        ".pdf",
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
    }
)

#: 显式 MIME 表（``mimetypes`` 在各平台结果不一致，这里以服务端口径为准）
_MIME_OVERRIDE: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

_TERM_SPLIT_RE = re.compile(r"\s+")
#: 单条切片里每个关键词最多记录的高亮区间数（防御：短词在长文里可能命中上百次）
MAX_RANGES_PER_TERM = 50


class PreviewError(Exception):
    """预览相关的业务错误。

    ``status`` 为建议的 HTTP 状态码：路径非法 → 400，原件缺失 → 404。
    """

    def __init__(self, message: str, status: int = 404) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ 路径解析
def normalize_key(file_key: str) -> str:
    """统一 ``file_key``：反斜杠转正斜杠、去掉 ``./`` 前缀与首尾空白。"""
    key = str(file_key or "").replace("\\", "/").strip()
    while key.startswith("./"):
        key = key[2:]
    return key.lstrip("/")


def is_upload_key(file_key: str) -> bool:
    return normalize_key(file_key).startswith(UPLOAD_PREFIX)


def root_label(file_key: str) -> str:
    """逻辑根目录名（``uploads`` / ``knowledge``），用于展示而不泄露绝对路径。"""
    return "uploads" if is_upload_key(file_key) else "knowledge"


def _split_parts(key: str) -> list[str]:
    return [p for p in key.split("/") if p not in ("", ".")]


def resolve_original(file_key: str) -> Path:
    """``file_key`` → 原件物理路径。

    路径穿越防护分两层：

    1. **拒绝 ``..`` 段**（含 ``uploads/../xx`` 这类绕过前缀的写法）；
    2. ``resolve()`` 后校验仍在根目录内（防符号链接 / Windows 短名等边缘情形）。

    :raises PreviewError: 路径非法（400）或未记录原件路径（404）
    """
    key = normalize_key(file_key)
    if not key:
        raise PreviewError("文档未记录原件路径（file_key 为空）", 404)

    parts = _split_parts(key)
    if any(p == ".." for p in parts):
        raise PreviewError(f"非法原件路径（含上级目录）：{file_key}", 400)

    if is_upload_key(key):
        base = Path(settings.upload_dir)
        rel_parts = parts[1:]
    else:
        base = Path(settings.knowledge_dir)
        rel_parts = parts

    if not rel_parts:
        raise PreviewError(f"非法原件路径（缺少文件名）：{file_key}", 400)

    base = base.resolve()
    target = (base / Path(*rel_parts)).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise PreviewError(f"非法原件路径（越出根目录）：{file_key}", 400) from None
    return target


# ------------------------------------------------------------------ 元信息
def guess_mime(name: str | Path) -> str:
    """按扩展名推断 MIME；文本类统一补 ``charset=utf-8``。"""
    suffix = Path(str(name)).suffix.lower()
    mime = _MIME_OVERRIDE.get(suffix)
    if mime is None:
        mime = mimetypes.guess_type(str(name))[0] or "application/octet-stream"
    if mime.startswith("text/") and "charset" not in mime:
        mime = f"{mime}; charset=utf-8"
    return mime


def is_inline(name: str | Path) -> bool:
    """是否允许浏览器内联展示（其余走 ``attachment`` 下载）。"""
    return Path(str(name)).suffix.lower() in INLINE_EXT


def describe_original(file_key: str) -> dict:
    """原件元信息（**不含绝对路径**，避免向管理端泄露服务器目录结构）。

    :return: ``{fileKey, fileName, root, format, exists, size, mtime, mime,
              inline, textLike, error}``
    """
    key = normalize_key(file_key)
    suffix = Path(key).suffix.lower()
    info = {
        "fileKey": key,
        "fileName": posixpath.basename(key) or "",
        "root": root_label(key),
        "format": suffix.lstrip("."),
        "exists": False,
        "size": 0,
        "mtime": None,
        "mime": guess_mime(key),
        "inline": is_inline(key),
        "textLike": suffix in TEXT_EXT,
        "error": None,
    }
    try:
        path = resolve_original(key)
    except PreviewError as exc:
        info["error"] = str(exc)
        return info

    if not path.is_file():
        info["error"] = f"原件不存在或不是文件：{key}"
        return info

    try:
        st = path.stat()
    except OSError as exc:  # 权限 / 竞态
        info["error"] = f"原件读取失败：{exc}"
        return info

    info["exists"] = True
    info["size"] = st.st_size
    info["mtime"] = st.st_mtime
    return info


# ------------------------------------------------------------------ 原图（任务 1.10）
def is_image_key(file_key: str | Path) -> bool:
    """扩展名是否属于图片（原图接口白名单）。"""
    return Path(normalize_key(str(file_key))).suffix.lower() in IMAGE_EXT


def describe_image(file_key: str) -> dict:
    """原图元信息（在 :func:`describe_original` 之上补宽高）。

    :return: ``{key, name, format, exists, size, mtime, mime, width, height, error}``
    """
    base = describe_original(file_key)
    info = {
        "key": base["fileKey"],
        "name": base["fileName"],
        "format": base["format"],
        "exists": base["exists"],
        "size": base["size"],
        "mtime": base["mtime"],
        "mime": base["mime"],
        "width": None,
        "height": None,
        "error": base["error"],
    }
    if not info["exists"] or not is_image_key(info["key"]):
        return info
    try:
        from PIL import Image

        path = resolve_original(info["key"])
        with Image.open(path) as img:  # 惰性读头，不解码整张图
            info["width"], info["height"] = img.size
            if not info["mime"].startswith("image/"):
                info["mime"] = Image.MIME.get(img.format or "", info["mime"])
    except Exception as exc:  # noqa: BLE001 - 图片损坏/缺 Pillow 都要能降级展示
        info["error"] = f"图片元信息读取失败：{exc}"
    return info


def collect_images(doc: dict, chunks: list[dict]) -> list[dict]:
    """汇总与某文档相关的全部图片（去重、保持首次出现顺序）。

    两个来源：

    * **原件本身** —— 文档是图片型（``.png/.jpg/…``）时，``file_key`` 指向原图；
    * **内嵌图片** —— 切片里的 ``imageKeys``（docx/pptx 抽取的原图），
      同时记录引用它的切片序号，供预览页把图与切片对照起来。

    :return: ``[{key, name, role, exists, size, mtime, mime, format, width, height,
              chunkSeqs, error}]``，``role`` 为 ``original`` / ``embedded``
    """
    out: list[dict] = []
    index: dict[str, dict] = {}

    def add(key: str, role: str, chunk_seq: int | None = None) -> None:
        norm = normalize_key(key)
        if not norm or not is_image_key(norm):
            return
        item = index.get(norm)
        if item is None:
            item = {"role": role, "chunkSeqs": [], **describe_image(norm)}
            index[norm] = item
            out.append(item)
        if chunk_seq is not None and chunk_seq not in item["chunkSeqs"]:
            item["chunkSeqs"].append(chunk_seq)

    doc_key = original_key(doc)
    if is_image_key(doc_key):
        add(doc_key, "original")

    for c in chunks:
        seq = c.get("seqNo") or c.get("seq_no")
        for key in c.get("imageKeys") or c.get("image_keys") or []:
            add(str(key), "embedded", int(seq) if seq else None)

    for item in out:
        item["chunkSeqs"].sort()
    return out


def resolve_doc_image(doc: dict, chunks: list[dict], key: str) -> Path:
    """校验 ``key`` **属于该文档**后返回原图物理路径。

    安全口径：客户端传来的 ``key`` 必须能在该文档的图片集合里找到（原件本身或内嵌图），
    否则 404；随后仍走 :func:`resolve_original` 的路径穿越防护。

    :raises PreviewError: key 非图片（400）、不属于该文档或文件缺失（404）
    """
    norm = normalize_key(key)
    if not norm:
        raise PreviewError("缺少图片 key", 400)
    if not is_image_key(norm):
        raise PreviewError(f"非图片类型：{norm}", 400)

    allowed = {item["key"] for item in collect_images(doc, chunks)}
    if norm not in allowed:
        raise PreviewError(f"图片不属于该文档：{norm}", 404)

    path = resolve_original(norm)
    if not path.is_file():
        raise PreviewError(f"原图不存在或不是文件：{norm}", 404)
    return path


# ------------------------------------------------------------------ 文本提取
#: 缓存键 ``(物理路径, mtime_ns, size)`` —— 原件被覆盖后自然失效
_TEXT_CACHE: dict[tuple[str, int, int], str] = {}
_TEXT_LOCK = threading.Lock()
_TEXT_CACHE_MAX = 32


def _decode_text(buf: bytes) -> str:
    """按 UTF-8(-BOM) → GB18030 依次尝试解码，最终以替换字符兜底。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return buf.decode(enc)
        except UnicodeDecodeError:
            continue
    return buf.decode("utf-8", errors="replace")


def blocks_to_text(blocks) -> str:
    """解析块 → 可读纯文本（标题补 ``#``、图片补 ``[图片]`` 占位，块间空行分隔）。"""
    out: list[str] = []
    for b in blocks:
        text = (b.text or "").strip()
        if b.type == "heading":
            out.append(f"{'#' * max(1, int(b.level or 1))} {text}".rstrip())
        elif b.type == "image":
            out.append(f"[图片] {text}".rstrip())
        elif text:
            out.append(text)
    return "\n\n".join(x for x in out if x)


def clear_text_cache() -> None:
    """清空原文文本缓存（测试与知识库变更后调用）。"""
    with _TEXT_LOCK:
        _TEXT_CACHE.clear()


def _cache_put(key: tuple[str, int, int], value: str) -> None:
    with _TEXT_LOCK:
        if len(_TEXT_CACHE) >= _TEXT_CACHE_MAX:
            # 简单 FIFO 淘汰：预览是低频操作，无需精确 LRU
            for k in list(_TEXT_CACHE)[: _TEXT_CACHE_MAX // 2]:
                _TEXT_CACHE.pop(k, None)
        _TEXT_CACHE[key] = value


def extract_text(file_key: str, force: bool = False, title: str | None = None) -> dict:
    """原件 → 原文纯文本（带缓存）。

    :param title: 文档标题。**仅对图片型原件生效** —— 原图按内容哈希命名
        （``<hash16>.png``），不传标题时原文首行会是一串哈希。

    :return: ``{ok, text, chars, lines, format, source, cached, mtime, error}``
    """
    info = describe_original(file_key)
    suffix = Path(info["fileKey"]).suffix.lower()
    base = {
        "text": "",
        "chars": 0,
        "lines": 0,
        "format": suffix.lstrip("."),
        "source": "",
        "cached": False,
        "mtime": info["mtime"],
        "error": None,
    }
    if not info["exists"]:
        return {"ok": False, **base, "error": info["error"]}

    if suffix not in SUPPORTED_EXT and suffix not in PLANNED_EXT:
        supported = " ".join(sorted(SUPPORTED_EXT))
        return {"ok": False, **base, "error": f"该格式暂不支持文本提取：{suffix}（支持 {supported}）"}

    path = resolve_original(info["fileKey"])
    st = path.stat()
    # 缓存键带上 title：图片型原件的标题会影响解析结果（标题行）
    ck = (str(path), st.st_mtime_ns, st.st_size, str(title or ""))
    if not force:
        with _TEXT_LOCK:
            hit = _TEXT_CACHE.get(ck)
        if hit is not None:
            return {
                "ok": True,
                **base,
                "text": hit,
                "chars": len(hit),
                "lines": hit.count("\n") + 1 if hit else 0,
                "source": "raw" if suffix in TEXT_EXT else "parsed",
                "cached": True,
            }

    try:
        if suffix in TEXT_EXT:
            text = _decode_text(path.read_bytes())
            source = "raw"
        else:
            text = blocks_to_text(parse_file(path, display_name=title or None).blocks)
            source = "parsed"
    except Exception as exc:  # noqa: BLE001 - 解析失败要以业务错误返回，不是 500
        logger.warning("原文提取失败：%s（%s）", path, exc)
        return {"ok": False, **base, "error": f"原文提取失败：{exc}"}

    _cache_put(ck, text)
    return {
        "ok": True,
        **base,
        "text": text,
        "chars": len(text),
        "lines": text.count("\n") + 1 if text else 0,
        "source": source,
    }


# ------------------------------------------------------------------ 关键词检索
def parse_terms(query: str | None) -> list[str]:
    """检索词：空格/制表符分隔，去空、去重（保持出现顺序）。"""
    terms = [t for t in _TERM_SPLIT_RE.split(str(query or "").strip()) if t]
    return list(dict.fromkeys(terms))


def find_ranges(haystack: str, term: str, limit: int = MAX_RANGES_PER_TERM) -> list[list[int]]:
    """``term`` 在 ``haystack`` 中的全部出现区间 ``[[start, end], ...]``（忽略大小写）。"""
    if not haystack or not term:
        return []
    low_h = haystack.lower()
    low_t = term.lower()
    out: list[list[int]] = []
    start = 0
    step = max(1, len(term))
    while len(out) < limit:
        i = low_h.find(low_t, start)
        if i < 0:
            break
        out.append([i, i + len(term)])
        start = i + step
    return out


def search_chunks(
    chunks: list[dict],
    query: str | None = None,
    *,
    only_matched: bool = True,
) -> dict:
    """切片关键词检索。

    * 多个关键词按 **AND** 语义（全部命中才算命中），与 BM25 检索的直觉一致；
    * 命中判定覆盖 ``text`` 与 ``titlePath``；高亮区间分别给出
      （``matches`` 对应 ``text``，``titleMatches`` 对应 ``titlePath``）；
    * ``query`` 为空 → 全部切片命中（``matchCount == total``），前端无需分支；
    * ``only_matched=False`` 时**保留未命中的切片**（``matches`` 为空），
      供对比视图实现「只看命中」开关——列表接口 ``/chunks?q=`` 用默认的过滤口径。

    :return: ``{query, terms, total, matchCount, items}``，``items`` 为切片
             （原字段 + ``matches`` / ``titleMatches`` / ``matched``）
    """
    terms = parse_terms(query)
    items: list[dict] = []
    matched = 0
    for c in chunks:
        text = c.get("text") or ""
        title_path = c.get("titlePath") or ""
        matches: list[list[int]] = []
        title_matches: list[list[int]] = []
        hit = True
        if terms:
            low_text = text.lower()
            low_title = title_path.lower()
            hit = all(t.lower() in low_text or t.lower() in low_title for t in terms)
        if hit:
            for t in terms:
                matches.extend(find_ranges(text, t))
                title_matches.extend(find_ranges(title_path, t))
            matches.sort()
            title_matches.sort()
            matched += 1
        elif only_matched:
            continue

        items.append(
            {
                **c,
                "matches": matches,
                "titleMatches": title_matches,
                "matched": hit,
            }
        )

    return {
        "query": str(query or ""),
        "terms": terms,
        "total": len(chunks),
        "matchCount": matched,
        "items": items,
    }


# ------------------------------------------------------------------ 组装
def build_preview(doc: dict, chunks: list[dict], query: str | None = None) -> dict:
    """预览页一次性所需数据（原文 + 元信息 + 切片 + 检索结果）。

    ``doc`` 需含 ``fileKey``（MySQL）或 ``file``（JSON 索引，完整相对路径）——
    统一用 :func:`original_key` 取值。
    """
    file_key = original_key(doc)
    original = describe_original(file_key)
    doc_title = str(doc.get("title") or "").strip() or None
    text_info = (
        extract_text(file_key, title=doc_title)
        if original["exists"]
        else {
            "ok": False,
            "text": "",
            "chars": 0,
            "lines": 0,
            "format": original["format"],
            "source": "",
            "cached": False,
            "mtime": None,
            "error": original["error"],
        }
    )
    search = search_chunks(chunks, query, only_matched=False)
    images = collect_images(doc, chunks)
    file_key = original_key(doc)

    return {
        "doc": doc,
        "original": original,
        "isImage": is_image_key(file_key),
        "images": images,
        "text": text_info["text"],
        "textMeta": {
            "ok": text_info["ok"],
            "chars": text_info["chars"],
            "lines": text_info["lines"],
            "source": text_info["source"],
            "cached": text_info["cached"],
            "error": text_info["error"],
        },
        "query": search["query"],
        "terms": search["terms"],
        "total": search["total"],
        "matchCount": search["matchCount"],
        "chunks": search["items"],
    }


def original_key(doc: dict) -> str:
    """取原件相对路径。

    JSON 索引的文档条目用 ``file``（完整相对路径，如
    ``01-信网中心/办事指南/xxx.md``），MySQL 仓储用 ``fileKey``（同样为完整相对路径，
    如 ``uploads/1730000000000-a.md``）。``file`` 优先，因为它不会退化成 basename。
    """
    return str(doc.get("file") or doc.get("fileKey") or "")


__all__ = [
    "IMAGE_EXT",
    "INLINE_EXT",
    "MAX_RANGES_PER_TERM",
    "TEXT_EXT",
    "UPLOAD_PREFIX",
    "PreviewError",
    "blocks_to_text",
    "build_preview",
    "clear_text_cache",
    "collect_images",
    "describe_image",
    "describe_original",
    "extract_text",
    "find_ranges",
    "guess_mime",
    "is_image_key",
    "is_inline",
    "is_upload_key",
    "normalize_key",
    "original_key",
    "parse_terms",
    "resolve_doc_image",
    "resolve_original",
    "root_label",
    "search_chunks",
]
