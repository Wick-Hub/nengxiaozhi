"""文档预览与对比验收（任务 1.11 / 关联 M-K-08）。

七组自检，**不依赖 MySQL**（默认 JSON 索引），可直接复现：

1. **原件定位**：``file_key`` 两种形态 → 物理路径，路径穿越一律拒绝；
2. **元信息**：MIME 映射与「可内联」白名单（SVG/HTML 绝不内联）；
3. **原文提取**：文本格式原样解码、二进制格式走解析层、mtime 缓存、缺失降级；
4. **关键词检索**：AND 语义、大小写不敏感、命中区间、只看命中开关；
5. **前后端一致性**：同一批用例喂给后端 ``find_ranges`` / ``parse_terms``
   与前端 ``static/preview.js``（经 Node 加载），要求逐字符一致；
6. **接口层契约**：``/preview`` / ``/chunks?q=`` / ``/text`` / ``/raw`` / ``/{id}``；
7. **端到端验收**：索引里**每一篇**文档都能看到切片、原文可提取、原件可取、
   关键词命中正确切片（任务单验收口径）。

用法::

    python tools/verify_preview.py
    python tools/verify_preview.py --store mysql          # 验证 MySQL 模式
    python tools/verify_preview.py --preview outputs/preview.html --doc D001

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PROBE = SERVICE_DIR / "tools" / "preview_probe.mjs"
PASSED = 0
FAILED = 0

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{3,}")


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def node_bin() -> str | None:
    candidate = os.environ.get("NODE_BIN") or "node"
    if shutil.which(candidate) or Path(candidate).is_file():
        return candidate
    return None


def pick_term(text: str) -> str | None:
    m = _CJK_RUN_RE.search(text or "")
    return m.group(0)[:4] if m else None


# ------------------------------------------------------------------ 1
def verify_paths(dp) -> None:
    section("1. 原件定位与路径穿越防护")
    from app.config import settings

    key = "01-信网中心/办事指南/校园网开户与报修指南.md"
    path = dp.resolve_original(key)
    check("知识目录相对路径可解析", path.is_file(), str(path))
    check("落在 KNOWLEDGE_DIR 内", path.is_relative_to(Path(settings.knowledge_dir).resolve()))
    check("root_label=knowledge", dp.root_label(key) == "knowledge")

    check("反斜杠被归一", dp.normalize_key("uploads\\a.md") == "uploads/a.md")
    check("前缀 ./ 被剥离", dp.normalize_key("./a/b.md") == "a/b.md")
    check("首部 / 被剥离", dp.normalize_key("/a/b.md") == "a/b.md")
    check("空值归一为空串", dp.normalize_key(None) == "")

    upload_key = "uploads/1730000000000-x.md"
    check("uploads 前缀识别", dp.is_upload_key(upload_key) and dp.root_label(upload_key) == "uploads")
    up = dp.resolve_original(upload_key)
    check("上传件落在 UPLOAD_DIR 内", up.is_relative_to(Path(settings.upload_dir).resolve()))

    for bad in (
        "",
        "   ",
        "../etc/passwd",
        "uploads/../secret.md",
        "a/../../b.md",
        "01-信网中心/../../etc/passwd",
        "uploads/",
    ):
        try:
            dp.resolve_original(bad)
            check(f"拒绝非法路径 {bad!r}", False, "竟然解析成功")
        except dp.PreviewError as exc:
            check(f"拒绝非法路径 {bad!r}", exc.status in (400, 404), f"{exc.status}")


# ------------------------------------------------------------------ 2
def verify_meta(dp) -> None:
    section("2. 元信息 / MIME / 内联白名单")
    info = dp.describe_original("01-信网中心/办事指南/校园网开户与报修指南.md")
    check("原件存在", info["exists"] is True)
    check("大小 > 0", info["size"] > 0, f"{info['size']} B")
    check("文件名正确", info["fileName"] == "校园网开户与报修指南.md")
    check("不泄露服务器绝对路径", "path" not in info)

    miss = dp.describe_original("uploads/1730000000000-nope.md")
    check("缺失原件标记 exists=False", miss["exists"] is False and bool(miss["error"]))

    cases = [
        ("a.md", "text/markdown; charset=utf-8", True),
        ("a.txt", "text/plain; charset=utf-8", True),
        ("a.csv", "text/csv; charset=utf-8", True),
        ("a.pdf", "application/pdf", True),
        ("a.png", "image/png", True),
        ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", False),
        ("a.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", False),
        ("a.bin", "application/octet-stream", False),
    ]
    for name, mime, inline in cases:
        check(f"{name} → {mime}", dp.guess_mime(name) == mime and dp.is_inline(name) is inline)

    # 可携带脚本的类型绝不内联
    for name in ("a.svg", "a.html", "a.htm"):
        check(f"{name} 不内联（防同源执行）", dp.is_inline(name) is False)


# ------------------------------------------------------------------ 3
def verify_text(dp) -> None:
    section("3. 原文文本提取")
    md = "01-信网中心/办事指南/校园网开户与报修指南.md"
    dp.clear_text_cache()
    first = dp.extract_text(md)
    check("Markdown 提取成功", first["ok"] is True, first.get("error") or "")
    check("口径为原件文本 raw", first["source"] == "raw")
    check("含 frontmatter（所见即原件）", first["text"].startswith("---"))
    check("字符数与行数一致", first["chars"] == len(first["text"]) and first["lines"] > 1)
    second = dp.extract_text(md)
    check("二次读取命中缓存", second["cached"] is True and second["text"] == first["text"])
    check("force 绕过缓存", dp.extract_text(md, force=True)["cached"] is False)

    xlsx = dp.extract_text("02-财务处/常见问题/财务报销常见问题.xlsx")
    check("xlsx 走解析层", xlsx["ok"] is True and xlsx["source"] == "parsed", xlsx.get("error") or "")
    check("xlsx 文本非空", xlsx["chars"] > 0, f"{xlsx['chars']} 字")

    missing = dp.extract_text("uploads/1730000000000-nope.md")
    check("缺失原件降级不抛错", missing["ok"] is False and bool(missing["error"]))

    from app.core.parser import Block

    blocks = [
        Block(type="heading", text="标题", level=2),
        Block(type="para", text="正文"),
        Block(type="image", text="图1"),
    ]
    check("blocks→text 还原结构", dp.blocks_to_text(blocks) == "## 标题\n\n正文\n\n[图片] 图1")


# ------------------------------------------------------------------ 4
def verify_search(dp) -> None:
    section("4. 关键词检索语义")
    chunks = [
        {"chunkId": "c1", "text": "校园网开户与报修", "titlePath": "A"},
        {"chunkId": "c2", "text": "校园网资费说明", "titlePath": "B"},
        {"chunkId": "c3", "text": "食堂菜谱", "titlePath": "C"},
    ]
    r = dp.search_chunks(chunks, "校园网")
    check("单词检索命中 2 条", r["matchCount"] == 2 and r["total"] == 3)
    check("命中区间正确", r["items"][0]["matches"] == [[0, 3]], str(r["items"][0]["matches"]))
    check("区间可回切出原词", r["items"][0]["text"][0:3] == "校园网")

    r2 = dp.search_chunks(chunks, "校园网 报修")
    check("多词 AND 语义", [c["chunkId"] for c in r2["items"]] == ["c1"])
    r3 = dp.search_chunks(chunks, "C")
    check("标题路径也参与命中", [c["chunkId"] for c in r3["items"]] == ["c3"])

    empty = dp.search_chunks(chunks, "")
    check("空检索返回全部", empty["matchCount"] == 3 and all(not c["matches"] for c in empty["items"]))

    loose = dp.search_chunks(chunks, "校园网", only_matched=False)
    check("only_matched=False 保留未命中", len(loose["items"]) == 3 and loose["matchCount"] == 2)
    check("未命中条目 matched=False", loose["items"][2]["matched"] is False)

    check("大小写不敏感", dp.search_chunks([{"text": "Wi-Fi"}], "wi-fi")["matchCount"] == 1)
    check("无命中返回空", dp.search_chunks(chunks, "绝无此词")["matchCount"] == 0)
    check("区间数有上限", len(dp.find_ranges("a" * 5000, "a")) == dp.MAX_RANGES_PER_TERM)
    check("parse_terms 去重", dp.parse_terms("甲 甲 乙") == ["甲", "乙"])
    check("parse_terms 空值", dp.parse_terms(None) == [] and dp.parse_terms("  ") == [])


# ------------------------------------------------------------------ 5
def verify_parity() -> None:
    section("5. 前后端检索函数一致性（逐字符）")
    sys.path.insert(0, str(SERVICE_DIR / "tools"))
    from preview_cases import (  # noqa: PLC0415
        IMAGE_CASES,
        RANGE_CASES,
        TERM_CASES,
        assert_bmp_only,
        assert_image_cases_consistent,
    )

    from app.services import doc_preview as dp  # noqa: PLC0415

    assert_bmp_only()
    assert_image_cases_consistent()
    check("用例限定在 BMP 内", True, f"{len(RANGE_CASES)} 组区间 / {len(TERM_CASES)} 组分词")

    binary = node_bin()
    if not binary:
        print("  [SKIP] 未找到 Node（可设 NODE_BIN 指定），跳过一致性比对")
        return
    print(f"  使用 Node：{binary}")

    payload = {
        "ranges": [list(c) for c in RANGE_CASES],
        "terms": TERM_CASES,
        "images": [[doc_id, img] for doc_id, img, _ in IMAGE_CASES],
    }
    with tempfile.TemporaryDirectory() as tmp:
        cases_file = Path(tmp) / "cases.json"
        cases_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [binary, str(PROBE), str(cases_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(SERVICE_DIR),
        )
    if proc.returncode != 0:
        check("Node 探针可运行", False, (proc.stderr or "").strip()[:200])
        return
    check("Node 探针可运行", True)
    fe = json.loads(proc.stdout)

    bad_ranges = 0
    for i, (hay, term) in enumerate(RANGE_CASES):
        py = [[list(x) for x in dp.find_ranges(hay, term)]]
        if fe["ranges"][i] != py[0]:
            bad_ranges += 1
            if bad_ranges <= 3:
                print(f"        差异：({hay!r}, {term!r}) 后端 {py[0]} / 前端 {fe['ranges'][i]}")
    check(f"findRanges 一致（{len(RANGE_CASES)} 组）", bad_ranges == 0, f"{bad_ranges} 组不一致")

    bad_terms = sum(
        1 for i, q in enumerate(TERM_CASES) if fe["terms"][i] != dp.parse_terms(q)
    )
    check(f"parseTerms 一致（{len(TERM_CASES)} 组）", bad_terms == 0, f"{bad_terms} 组不一致")

    # ---- 图片缩略图（任务 1.10）：前端 imageCard 的 URL 与转义必须符合预期 ----
    if "images" not in fe:
        check("探针返回图片用例结果", False, "preview_probe.mjs 未输出 images（版本不匹配？）")
        return
    bad_imgs = 0
    for i, (doc_id, img, expect) in enumerate(IMAGE_CASES):
        got = fe["images"][i] or {}
        url, html = got.get("url", ""), got.get("html", "")
        problems: list[str] = []
        want_url = dp_url(doc_id, img["key"])
        if url != want_url:
            problems.append(f"url={url!r} 期望 {want_url!r}")
        for token in expect.get("must", []):
            if token not in html:
                problems.append(f"缺少 {token!r}")
        for token in expect.get("must_not", []):
            if token in html:
                problems.append(f"不应出现 {token!r}")
        if problems:
            bad_imgs += 1
            if bad_imgs <= 3:
                print(f"        图片用例 {doc_id}/{img.get('name')!r}：{'；'.join(problems)}")
    check(f"imageCard 渲染一致（{len(IMAGE_CASES)} 组）", bad_imgs == 0, f"{bad_imgs} 组不一致")


def dp_url(doc_id: str, key: str) -> str:
    """前端 :func:`imageUrl` 的期望值。

    JS ``encodeURIComponent`` 不转义 ``!~*'()``，而 Python ``quote`` 默认会 ——
    这里显式把它们放进 ``safe``，两者才对同一输入给出同一结果。
    """
    from urllib.parse import quote

    safe = "!~*'()"
    return f"/api/docs/{quote(str(doc_id), safe=safe)}/image?key={quote(str(key), safe=safe)}"


# ------------------------------------------------------------------ 6 / 7
def verify_api() -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from app.core.index_store import get_index  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415
    from app.services import doc_preview as dp  # noqa: PLC0415

    client = TestClient(app)
    idx = get_index()
    if not idx.ok or not idx.docs:
        section("6. 接口层契约")
        check("索引可用", False, idx.error or "索引里没有文档")
        return

    doc_id = str(idx.docs[0]["docId"])

    section("6. 接口层契约")
    body = client.get(f"/api/docs/{doc_id}").json()
    check("GET /{id} 文档详情", body["ok"] is True and str(body["data"]["docId"]) == doc_id)
    check("GET /{id} 不存在 → 404", client.get("/api/docs/D999").status_code == 404)

    chunks = client.get(f"/api/docs/{doc_id}/chunks").json()["data"]
    check("/chunks 无 q 时仍返回列表（向后兼容）", isinstance(chunks, list) and bool(chunks))
    check("/chunks 不检索时不注入 matches", all("matches" not in c for c in chunks))

    term = pick_term(chunks[0]["text"])
    assert term
    hits = client.get(f"/api/docs/{doc_id}/chunks", params={"q": term}).json()["data"]
    check("/chunks?q 过滤生效", 0 < len(hits) <= len(chunks), f"{len(hits)}/{len(chunks)}")
    check("/chunks?q 带回命中区间", all(c["matches"] or c["titleMatches"] for c in hits))
    check(
        "/chunks?q 区间可回切出原词",
        all(c["text"][s:e].lower() == term.lower() for c in hits for s, e in c["matches"]),
    )

    txt = client.get(f"/api/docs/{doc_id}/text").json()
    check("/text 返回原文", txt["ok"] is True and txt["data"]["chars"] > 0)

    prev = client.get(f"/api/docs/{doc_id}/preview").json()["data"]
    check("/preview 字段齐全", all(
        k in prev for k in ("doc", "original", "text", "textMeta", "terms", "total", "matchCount", "chunks")
    ))
    check("/preview 无检索词时全命中", prev["matchCount"] == prev["total"] == len(prev["chunks"]))
    check("/preview 不泄露绝对路径", "path" not in prev["original"])

    qprev = client.get(f"/api/docs/{doc_id}/preview", params={"q": term}).json()["data"]
    check(
        "/preview?q 保留全部切片（供「只看命中」开关）",
        len(qprev["chunks"]) == qprev["total"] and qprev["matchCount"] >= 1,
    )

    raw = client.get(f"/api/docs/{doc_id}/raw")
    check("/raw 内联预览", raw.status_code == 200 and raw.headers["content-disposition"].startswith("inline;"))
    check("/raw 带 nosniff", raw.headers.get("x-content-type-options") == "nosniff")
    check("/raw 中文文件名 RFC5987 编码", "filename*=UTF-8''" in raw.headers["content-disposition"])
    check(
        "/raw 返回原件字节",
        raw.content == dp.resolve_original(dp.original_key(prev["doc"])).read_bytes(),
    )
    dl = client.get(f"/api/docs/{doc_id}/raw", params={"download": 1})
    check("/raw?download=1 强制下载", dl.headers["content-disposition"].startswith("attachment;"))
    check("/raw 不存在 → 404", client.get("/api/docs/D999/raw").status_code == 404)

    for evil in ("..%2F..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd"):
        check(
            f"/raw 拒绝客户端路径 {evil!r}",
            client.get(f"/api/docs/{evil}/raw").status_code in (400, 404),
        )

    section("7. 端到端验收（索引里每一篇文档）")
    print(f"  索引：{len(idx.docs)} 篇文档 / {len(idx.chunks)} 条切片")
    for doc in idx.docs:
        did = str(doc["docId"])
        p = client.get(f"/api/docs/{did}/preview").json()["data"]
        ok_chunks = (
            p["total"] > 0
            and [c["seqNo"] for c in p["chunks"]] == list(range(1, p["total"] + 1))
            and all(c["chunkId"] == f"{did}-C{c['seqNo']:03d}" and c["text"] for c in p["chunks"])
        )
        check(f"{did} 切片可见且编号连续", ok_chunks, f"{p['total']} 条")
        check(f"{did} 原文可提取", p["textMeta"]["ok"] is True and bool(p["text"]),
              p["textMeta"].get("error") or f"{p['textMeta']['chars']} 字")
        check(f"{did} 原件可取", client.get(f"/api/docs/{did}/raw").status_code == 200)

        target = next(c for c in idx.chunks if c.doc_id == did)
        kw = pick_term(target.text)
        assert kw
        got = client.get(f"/api/docs/{did}/chunks", params={"q": kw}).json()["data"]
        check(f"{did} 关键词 {kw!r} 命中正确切片", target.chunk_id in {c["chunkId"] for c in got})
        none = client.get(f"/api/docs/{did}/chunks", params={"q": "绝无此词条ZZZQ"}).json()["data"]
        check(f"{did} 不存在的词零命中", none == [])


# ------------------------------------------------------------------ preview
def _hl(text: str, ranges: list[list[int]], esc) -> str:
    """与 static/preview.js::highlight 同构的快照渲染（仅用于静态预览）。"""
    s = text or ""
    if not ranges:
        return esc(s)
    out, pos = [], 0
    for a, b in ranges:
        if a < pos or a >= b or b > len(s):
            continue
        out.append(esc(s[pos:a]))
        out.append(f'<mark class="hit">{esc(s[a:b])}</mark>')
        pos = b
    out.append(esc(s[pos:]))
    return "".join(out)


def write_preview(path: Path, doc_id: str | None) -> Path:
    """把真实预览数据渲染成静态 HTML 快照（复用 static/admin.css + preview.css）。"""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from app.core.index_store import get_index  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415
    from app.services import doc_preview as dp  # noqa: PLC0415

    client = TestClient(app)
    idx = get_index()
    if doc_id is None:
        doc_id = str(idx.docs[0]["docId"]) if idx.docs else ""

    data = client.get(f"/api/docs/{doc_id}/preview", params={"q": ""}).json()["data"]
    doc = data["doc"]
    terms = dp.parse_terms("校园网")
    if terms and terms[0] not in (data["text"] or ""):
        terms = []
    qtext = " ".join(terms)
    data = client.get(f"/api/docs/{doc_id}/preview", params={"q": qtext}).json()["data"]

    def esc(s: str) -> str:
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    admin_css = (SERVICE_DIR / "static" / "admin.css").read_text(encoding="utf-8")
    preview_css = (SERVICE_DIR / "static" / "preview.css").read_text(encoding="utf-8")

    all_terms = dp.parse_terms(data["query"])
    text_ranges: list[list[int]] = []
    for t in all_terms:
        text_ranges.extend(dp.find_ranges(data["text"], t))
    text_ranges.sort()

    chunk_html = []
    for c in data["chunks"]:
        title_html = (
            _hl(c["titlePath"], c["titleMatches"], esc) if c["titleMatches"] else esc(c["titlePath"])
        )
        chunk_html.append(
            f'<div class="chunk{" dim" if not c["matched"] else ""}">'
            f'<div class="chunk-head"><span class="chunk-seq">#{c["seqNo"]}</span>'
            f'<span class="chunk-type">{esc(c["blockType"])}</span>'
            f'<span>{c["charCount"]} 字</span>'
            f'<span class="mono">{esc(c["chunkId"])}</span>'
            f'{("<span class=\'chunk-hint\'>标题命中</span>") if c["titleMatches"] else ""}'
            f'{f"<div class=\'chunk-title-path\'>{title_html}</div>" if c["titlePath"] else ""}'
            f'</div><div class="chunk-text">{_hl(c["text"], c["matches"], esc)}</div></div>'
        )

    tags = [
        f'#{doc.get("docId")}', doc.get("dept"), doc.get("ktype"), doc.get("statusName"),
        str(doc.get("fileFormat") or "").upper(), data["original"]["fileName"],
        f'{doc.get("chunkCount")} 切片',
    ]
    tag_html = "".join(f'<span class="tag">{esc(t)}</span>' for t in tags if t)
    marks = len(text_ranges) + sum(len(c["matches"]) + len(c["titleMatches"]) for c in data["chunks"])

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>文档预览与对比快照 · {esc(doc.get("title"))}</title>
<style>
{admin_css}
{preview_css}
.snapshot-note {{ background:#fffbeb; border:1px solid #fde68a; border-radius:10px;
  padding:10px 14px; font-size:13px; color:#92400e; }}
</style></head>
<body>
<header class="topbar">
  <div class="brand"><span class="logo">览</span><div class="brand-text">
    <div class="title">文档预览与对比 · 静态快照</div>
    <div class="subtitle">原文 ↔ 切片 · 关键词检索（任务 1.11）</div>
  </div></div>
</header>
<main class="wrap-wide">
  <div class="snapshot-note">
    这是 <code>tools/verify_preview.py --preview</code> 生成的**静态快照**（取自真实接口数据，
    检索词 <b>{esc(data["query"]) or "（无）"}</b>，共 <b>{marks}</b> 处高亮）。
    交互版请运行服务后访问 <code>/preview?doc={esc(str(doc.get("docId")))}</code>。
  </div>
  <section class="card doc-head"><div class="doc-head-main">
    <h3>{esc(doc.get("title"))}</h3><div class="doc-tags">{tag_html}</div>
  </div></section>
  <section class="card toolbar">
    <div class="field grow"><label>关键词检索</label>
      <input type="search" value="{esc(data["query"])}" readonly></div>
    <div class="hit-stat">命中 <b>{data["matchCount"]}</b>/{data["total"]} 条切片</div>
  </section>
  <section class="split">
    <div class="card pane">
      <div class="pane-head"><h3>原文</h3>
        <span class="pane-meta">{data["textMeta"]["chars"]} 字 · {data["textMeta"]["lines"]} 行</span></div>
      <div class="pane-body" id="text-body">{_hl(data["text"], text_ranges, esc)}</div>
    </div>
    <div class="card pane">
      <div class="pane-head"><h3>切片</h3>
        <span class="pane-meta">共 {data["total"]} 条</span></div>
      <div class="pane-body">{"".join(chunk_html)}</div>
    </div>
  </section>
</main>
</body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


# ------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="文档预览与对比验收（任务 1.11）")
    ap.add_argument("--store", choices=["json", "mysql"], default="json",
                    help="索引来源（默认 json，无需 MySQL）")
    ap.add_argument("--preview", metavar="PATH", help="额外输出一份静态预览 HTML 快照")
    ap.add_argument("--doc", help="--preview 使用的文档编号（默认取第一篇）")
    args = ap.parse_args(argv)

    # 必须在导入 app.* 之前固定环境（app.config 导入即实例化并缓存）
    os.environ["KB_STORE"] = args.store
    # 本工具是功能验收而非压测：关闭限流，避免遍历文档时触发 429。
    # 限流本身由 tests/test_security.py 与端到端实测覆盖。
    os.environ["RATE_LIMIT_ENABLED"] = "false"
    os.environ.setdefault("LLM_MOCK", "1")

    sys.path.insert(0, str(SERVICE_DIR))
    sys.path.insert(0, str(SERVICE_DIR / "tools"))

    from app.services import doc_preview as dp  # noqa: PLC0415

    print("=" * 68)
    print("kb-service 文档预览与对比验收（任务 1.11 / M-K-08）")
    print(f"存储模式 {args.store} | 后端 app/services/doc_preview.py | 前端 static/preview.js")
    print("=" * 68)

    verify_paths(dp)
    verify_meta(dp)
    verify_text(dp)
    verify_search(dp)
    verify_parity()
    verify_api()

    if args.preview:
        section("8. 静态预览快照")
        out = write_preview(Path(args.preview), args.doc)
        check("快照已生成", out.is_file(), str(out))

    print("\n" + "=" * 68)
    print(f"汇总：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
