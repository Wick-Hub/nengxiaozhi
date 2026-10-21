"""文档预览与对比测试（任务 1.11 / 关联 M-K-08）。

覆盖四层：

1. **原件定位**：``file_key`` → 物理路径（两种形态 + 路径穿越防护）；
2. **文本提取**：文本格式原样解码、二进制格式走解析层、mtime 缓存、缺失降级；
3. **关键词检索**：AND 语义、大小写不敏感、命中区间、只看命中开关；
4. **接口层**：``/preview`` / ``/chunks?q=`` / ``/text`` / ``/raw`` / ``/{id}``
   以及**验收口径**——索引里每一篇文档都能看到切片，关键词能命中正确切片。

另有前后端一致性组：同一批用例同时喂给 ``app.services.doc_preview.find_ranges``
与前端 ``static/preview.js``（经 Node 加载），要求逐字符一致——未找到 Node 时跳过。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.index_store import get_index
from app.main import app
from app.services import doc_preview as dp

SERVICE_DIR = Path(__file__).resolve().parent.parent
PROBE = SERVICE_DIR / "tools" / "preview_probe.mjs"

sys.path.insert(0, str(SERVICE_DIR / "tools"))
from preview_cases import RANGE_CASES, TERM_CASES, assert_bmp_only  # noqa: E402

client = TestClient(app)

KB_MD = "01-信网中心/办事指南/校园网开户与报修指南.md"
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{3,}")


# ------------------------------------------------------------------ 1. 原件定位
def test_normalize_key() -> None:
    assert dp.normalize_key("uploads\\a.md") == "uploads/a.md"
    assert dp.normalize_key("./a/b.md") == "a/b.md"
    assert dp.normalize_key("/a/b.md") == "a/b.md"
    assert dp.normalize_key("  a/b.md  ") == "a/b.md"
    assert dp.normalize_key(None) == ""


def test_resolve_original_knowledge_file() -> None:
    path = dp.resolve_original(KB_MD)
    assert path.is_file()
    assert path == (Path(settings.knowledge_dir) / KB_MD).resolve()
    assert dp.root_label(KB_MD) == "knowledge"
    assert not dp.is_upload_key(KB_MD)


def test_resolve_original_upload_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    (tmp_path / "1730000000000-x.md").write_text("# hi", encoding="utf-8")
    key = "uploads/1730000000000-x.md"
    assert dp.is_upload_key(key)
    assert dp.root_label(key) == "uploads"
    assert dp.resolve_original(key) == (tmp_path / "1730000000000-x.md").resolve()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "../etc/passwd",
        "uploads/../secret.md",
        "a/../../b.md",
        "01-信网中心/../../etc/passwd",
        "uploads/",
    ],
)
def test_resolve_original_rejects_illegal_paths(bad: str) -> None:
    """路径穿越与空路径一律拒绝（400/404），绝不落到根目录之外。"""
    with pytest.raises(dp.PreviewError) as ei:
        dp.resolve_original(bad)
    assert ei.value.status in (400, 404)


def test_resolve_original_rejects_escape_via_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """符号链接指向根目录之外时也必须被 ``relative_to`` 兜住（第二层防护）。

    第一层是「拒绝 ``..`` 段」，第二层是 ``resolve()`` 后的包含性校验。
    注意：Windows 无 Developer Mode 时创建符号链接会失败，部分文件系统还会把
    符号链接静默降级为普通文件——两种情况下都无法验证该防护，直接跳过。
    """
    root = tmp_path / "kb"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = root / "leak.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    if not link.is_symlink():
        pytest.skip("当前环境把符号链接降级为普通文件，无法验证越界防护")

    monkeypatch.setattr(settings, "knowledge_dir", root)
    with pytest.raises(dp.PreviewError) as ei:
        dp.resolve_original("leak.md")
    assert ei.value.status == 400


# ------------------------------------------------------------------ 2. 元信息
def test_describe_original_existing() -> None:
    info = dp.describe_original(KB_MD)
    assert info["exists"] is True
    assert info["size"] > 0
    assert info["mtime"] > 0
    assert info["fileName"] == "校园网开户与报修指南.md"
    assert info["format"] == "md"
    assert info["error"] is None
    # 不向管理端泄露服务器绝对路径
    assert "path" not in info


def test_describe_original_missing() -> None:
    info = dp.describe_original("uploads/1730000000000-nope.md")
    assert info["exists"] is False
    assert info["size"] == 0
    assert info["error"]


@pytest.mark.parametrize(
    ("name", "mime", "inline"),
    [
        ("a.md", "text/markdown; charset=utf-8", True),
        ("a.txt", "text/plain; charset=utf-8", True),
        ("a.csv", "text/csv; charset=utf-8", True),
        ("a.pdf", "application/pdf", True),
        ("a.png", "image/png", True),
        ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", False),
        ("a.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", False),
        ("a.bin", "application/octet-stream", False),
        # SVG/HTML 可携带脚本，绝不内联（否则等于同源执行上传内容）
        ("a.svg", "image/svg+xml", False),
        ("a.html", "text/html; charset=utf-8", False),
    ],
)
def test_mime_and_inline(name: str, mime: str, inline: bool) -> None:
    assert dp.guess_mime(name) == mime
    assert dp.is_inline(name) is inline


# ------------------------------------------------------------------ 3. 文本提取
def test_extract_text_raw_markdown() -> None:
    result = dp.extract_text(KB_MD)
    assert result["ok"] is True
    assert result["source"] == "raw"
    assert result["chars"] > 100
    assert result["lines"] > 1
    # 文本格式原样解码：frontmatter 也在（所见即原件）
    assert result["text"].startswith("---")
    assert "校园网开户与报修指南" in result["text"]


def test_extract_text_parsed_binary() -> None:
    result = dp.extract_text("02-财务处/常见问题/财务报销常见问题.xlsx")
    assert result["ok"] is True
    assert result["source"] == "parsed"
    assert result["chars"] > 0
    assert "## 工作表：" in result["text"]


def test_extract_text_cache() -> None:
    dp.clear_text_cache()
    first = dp.extract_text(KB_MD)
    assert first["cached"] is False
    second = dp.extract_text(KB_MD)
    assert second["cached"] is True
    assert second["text"] == first["text"]
    # force 绕过缓存
    assert dp.extract_text(KB_MD, force=True)["cached"] is False


def test_extract_text_missing_file() -> None:
    result = dp.extract_text("uploads/1730000000000-nope.md")
    assert result["ok"] is False
    assert result["text"] == ""
    assert result["error"]


def test_extract_text_unsupported_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "knowledge_dir", tmp_path)
    (tmp_path / "a.zzz").write_bytes(b"\x00\x01")
    result = dp.extract_text("a.zzz")
    assert result["ok"] is False
    assert "暂不支持文本提取" in result["error"]


def test_blocks_to_text_headings_and_images() -> None:
    from app.core.parser import Block

    blocks = [
        Block(type="heading", text="标题", level=2),
        Block(type="para", text="正文"),
        Block(type="image", text="图1"),
        Block(type="para", text="   "),
    ]
    assert dp.blocks_to_text(blocks) == "## 标题\n\n正文\n\n[图片] 图1"


# ------------------------------------------------------------------ 4. 关键词检索
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (None, []),
        ("", []),
        ("   ", []),
        ("校园网", ["校园网"]),
        ("校园网 报修", ["校园网", "报修"]),
        ("校园网  报修", ["校园网", "报修"]),
        ("校园网 校园网", ["校园网"]),
    ],
)
def test_parse_terms(query: str | None, expected: list[str]) -> None:
    assert dp.parse_terms(query) == expected


@pytest.mark.parametrize(
    ("haystack", "term", "expected"),
    [
        ("校园网开户", "校园网", [[0, 3]]),
        ("ABCabc", "abc", [[0, 3], [3, 6]]),
        ("aaaa", "aa", [[0, 2], [2, 4]]),
        ("", "a", []),
        ("a", "", []),
    ],
)
def test_find_ranges(haystack: str, term: str, expected: list[list[int]]) -> None:
    assert dp.find_ranges(haystack, term) == expected


def test_find_ranges_is_capped() -> None:
    """短词在长文里命中极多时，区间数必须有上限（防前端渲染爆炸）。"""
    assert len(dp.find_ranges("a" * 5000, "a")) == dp.MAX_RANGES_PER_TERM


def test_search_chunks_and_semantics() -> None:
    chunks = [
        {"chunkId": "c1", "text": "校园网开户与报修", "titlePath": "A"},
        {"chunkId": "c2", "text": "校园网资费说明", "titlePath": "B"},
        {"chunkId": "c3", "text": "食堂菜谱", "titlePath": "C"},
    ]
    r = dp.search_chunks(chunks, "校园网")
    assert r["total"] == 3
    assert r["matchCount"] == 2
    assert [c["chunkId"] for c in r["items"]] == ["c1", "c2"]
    assert r["items"][0]["matches"] == [[0, 3]]

    # AND 语义：两个词都要命中
    r2 = dp.search_chunks(chunks, "校园网 报修")
    assert [c["chunkId"] for c in r2["items"]] == ["c1"]

    # 命中标题也算命中
    r3 = dp.search_chunks(chunks, "C")
    assert [c["chunkId"] for c in r3["items"]] == ["c3"]
    assert r3["items"][0]["titleMatches"] == [[0, 1]]


def test_search_chunks_empty_query_returns_all() -> None:
    chunks = [{"chunkId": "c1", "text": "甲"}, {"chunkId": "c2", "text": "乙"}]
    r = dp.search_chunks(chunks, "")
    assert r["total"] == 2
    assert r["matchCount"] == 2
    assert all(c["matched"] and not c["matches"] for c in r["items"])


def test_search_chunks_only_matched_false_keeps_all() -> None:
    chunks = [{"chunkId": "c1", "text": "甲"}, {"chunkId": "c2", "text": "乙"}]
    r = dp.search_chunks(chunks, "甲", only_matched=False)
    assert r["matchCount"] == 1
    assert [c["chunkId"] for c in r["items"]] == ["c1", "c2"]
    assert r["items"][1]["matched"] is False
    assert r["items"][1]["matches"] == []


def test_search_chunks_is_case_insensitive() -> None:
    chunks = [{"chunkId": "c1", "text": "Wi-Fi 连接失败"}]
    assert dp.search_chunks(chunks, "wi-fi")["matchCount"] == 1
    assert dp.search_chunks(chunks, "WI-FI")["matchCount"] == 1


# ------------------------------------------------------------------ 5. 前后端一致性
def test_range_cases_are_bmp_only() -> None:
    """非 BMP 字符（emoji 等）在 JS/Python 里长度口径不同，用例必须避开。"""
    assert_bmp_only()


def _node_bin() -> str | None:
    candidate = os.environ.get("NODE_BIN") or "node"
    if shutil.which(candidate) or Path(candidate).is_file():
        return candidate
    return None


@pytest.fixture(scope="module")
def frontend_ranges() -> dict:
    binary = _node_bin()
    if not binary:
        pytest.skip("未找到 Node，跳过前后端一致性比对（可用 NODE_BIN 指定）")
    payload = {
        "ranges": [list(c) for c in RANGE_CASES],
        "terms": TERM_CASES,
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
    assert proc.returncode == 0, f"Node 探针失败：{proc.stderr[:400]}"
    return json.loads(proc.stdout)


@pytest.mark.parametrize(("haystack", "term"), RANGE_CASES)
def test_find_ranges_matches_frontend(haystack: str, term: str, frontend_ranges: dict) -> None:
    index = RANGE_CASES.index((haystack, term))
    expected = [[list(r) for r in dp.find_ranges(haystack, term)]]
    assert frontend_ranges["ranges"][index] == expected[0], (
        f"preview.js 的 findRanges({haystack!r}, {term!r}) 与后端不一致"
    )


@pytest.mark.parametrize("query", TERM_CASES)
def test_parse_terms_matches_frontend(query: str, frontend_ranges: dict) -> None:
    index = TERM_CASES.index(query)
    assert frontend_ranges["terms"][index] == dp.parse_terms(query)


# ------------------------------------------------------------------ 6. 接口层
def _first_doc_id() -> str:
    idx = get_index()
    assert idx.ok, f"索引不可用：{idx.error}"
    assert idx.docs, "索引里没有文档"
    return str(idx.docs[0]["docId"])


def test_docs_detail() -> None:
    doc_id = _first_doc_id()
    body = client.get(f"/api/docs/{doc_id}").json()
    assert body["ok"] is True
    assert body["data"]["docId"] == doc_id
    assert body["data"]["title"]
    assert body["data"]["chunkCount"] >= 0


def test_docs_detail_404() -> None:
    assert client.get("/api/docs/D999").status_code == 404
    # JSON 模式按字符串比对文档编号，非法编号同样落到「不存在」；
    # MySQL 模式会先经 parse_public_doc_id 校验并返回 400。
    assert client.get("/api/docs/not-a-number").status_code in (400, 404)


def test_chunks_list_shape_unchanged() -> None:
    """向后兼容：不带 q 时仍返回**列表**（1.14 契约）。"""
    doc_id = _first_doc_id()
    data = client.get(f"/api/docs/{doc_id}/chunks").json()["data"]
    assert isinstance(data, list) and data
    for c in data:
        assert c["docId"] == doc_id
        assert c["chunkId"]
        assert "matches" not in c  # 不检索时不注入额外字段


def test_chunks_with_query_filters_and_annotates() -> None:
    doc_id = _first_doc_id()
    all_chunks = client.get(f"/api/docs/{doc_id}/chunks").json()["data"]
    term = _pick_term(all_chunks[0]["text"])
    hits = client.get(f"/api/docs/{doc_id}/chunks", params={"q": term}).json()["data"]
    assert hits, f"关键词 {term!r} 应命中至少一条切片"
    assert len(hits) <= len(all_chunks)
    assert all(c["matches"] or c["titleMatches"] for c in hits)
    assert all(c["matched"] is True for c in hits)
    # 命中区间必须落在 text 上，且切片内容就是原文
    for c in hits:
        for start, end in c["matches"]:
            assert c["text"][start:end].lower() == term.lower()


def test_text_endpoint() -> None:
    doc_id = _first_doc_id()
    body = client.get(f"/api/docs/{doc_id}/text").json()
    assert body["ok"] is True
    assert body["data"]["chars"] > 0
    assert body["data"]["text"]
    assert body["data"]["source"] in ("raw", "parsed")


def test_preview_endpoint_shape() -> None:
    doc_id = _first_doc_id()
    body = client.get(f"/api/docs/{doc_id}/preview").json()
    assert body["ok"] is True
    d = body["data"]
    for key in ("doc", "original", "text", "textMeta", "query", "terms", "total", "matchCount", "chunks"):
        assert key in d, f"preview 缺少 {key}"
    assert d["total"] > 0
    assert len(d["chunks"]) == d["total"]
    assert d["matchCount"] == d["total"]  # 无检索词时全部「命中」
    assert d["original"]["exists"] is True
    assert d["textMeta"]["ok"] is True
    # 不泄露服务器绝对路径
    assert "path" not in d["original"]


def test_preview_endpoint_with_query() -> None:
    doc_id = _first_doc_id()
    chunks = client.get(f"/api/docs/{doc_id}/chunks").json()["data"]
    term = _pick_term(chunks[0]["text"])
    d = client.get(f"/api/docs/{doc_id}/preview", params={"q": term}).json()["data"]
    assert d["terms"] == [term]
    assert d["matchCount"] >= 1
    # 对比视图需要拿到**全部**切片（未命中的置灰），而不是过滤后的结果
    assert len(d["chunks"]) == d["total"]
    assert any(c["matches"] for c in d["chunks"])


def test_preview_unknown_doc_404() -> None:
    assert client.get("/api/docs/D999/preview").status_code == 404


def test_raw_inline_and_attachment() -> None:
    doc_id = _first_doc_id()
    resp = client.get(f"/api/docs/{doc_id}/raw")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("inline;")
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    # 返回的就是原件字节
    src = dp.resolve_original(KB_MD).read_bytes()
    assert resp.content == src

    dl = client.get(f"/api/docs/{doc_id}/raw", params={"download": 1})
    assert dl.headers["content-disposition"].startswith("attachment;")


def test_raw_404_for_unknown_doc() -> None:
    assert client.get("/api/docs/D999/raw").status_code == 404


def test_raw_does_not_accept_client_supplied_paths() -> None:
    """原件路径只来自 ``kb_doc.file_key``，客户端无法通过 URL 指定任意文件。"""
    for evil in ["..%2F..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd", "....//etc/passwd"]:
        assert client.get(f"/api/docs/{evil}/raw").status_code in (400, 404), evil
        assert client.get(f"/api/docs/{evil}/text").status_code in (400, 404), evil
        assert client.get(f"/api/docs/{evil}/preview").status_code in (400, 404), evil


def test_raw_path_normalization_stays_inside_doc_namespace() -> None:
    """``/docs/D001/../D002/raw`` 会被 HTTP 层规范化为 ``/docs/D002/raw``。

    这只可能换成**另一篇已登记文档**，无法落到任意文件——路径穿越的真正入口
    是 ``file_key``，而它只来自数据库，客户端无从注入。
    """
    resp = client.get("/api/docs/D001/../D002/raw")
    assert resp.status_code == 200
    assert resp.content == client.get("/api/docs/D002/raw").content


# ------------------------------------------------------------------ 7. 验收口径
def test_every_indexed_doc_is_previewable() -> None:
    """任意已入库文档都可查看切片，且原文可提取。"""
    idx = get_index()
    assert idx.ok, f"索引不可用：{idx.error}"
    assert idx.docs, "索引里没有文档"

    for doc in idx.docs:
        doc_id = str(doc["docId"])
        body = client.get(f"/api/docs/{doc_id}/preview").json()
        assert body["ok"] is True, f"{doc_id} 预览失败：{body.get('error')}"
        d = body["data"]
        assert d["total"] > 0, f"{doc_id} 没有切片"
        assert [c["seqNo"] for c in d["chunks"]] == list(range(1, d["total"] + 1)), (
            f"{doc_id} 切片序号不连续"
        )
        for c in d["chunks"]:
            assert c["chunkId"] == f"{doc_id}-C{c['seqNo']:03d}"
            assert c["text"]
            assert c["charCount"] == len(c["text"])

        assert d["original"]["exists"] is True, f"{doc_id} 原件缺失：{d['original']['error']}"
        assert d["textMeta"]["ok"] is True, f"{doc_id} 原文提取失败：{d['textMeta']['error']}"
        assert d["text"], f"{doc_id} 原文为空"
        assert client.get(f"/api/docs/{doc_id}/raw").status_code == 200


def test_keyword_hits_correct_chunk() -> None:
    """关键词检索必须命中**包含该词的切片**，且不误报。"""
    idx = get_index()
    assert idx.docs

    for doc in idx.docs:
        doc_id = str(doc["docId"])
        chunks = [c for c in idx.chunks if c.doc_id == doc_id]
        assert chunks, f"{doc_id} 无切片"
        target = chunks[0]
        term = _pick_term(target.text)

        hits = client.get(f"/api/docs/{doc_id}/chunks", params={"q": term}).json()["data"]
        assert target.chunk_id in {c["chunkId"] for c in hits}, (
            f"{doc_id} 检索 {term!r} 未命中 {target.chunk_id}"
        )
        # 每条命中都真的含该词（正文或标题路径）
        for c in hits:
            blob = (c["text"] + "\n" + (c["titlePath"] or "")).lower()
            assert term.lower() in blob, f"{c['chunkId']} 被误判为命中"

        # 不存在的词必须零命中
        none_hits = client.get(
            f"/api/docs/{doc_id}/chunks", params={"q": "绝无此词条ZZZQ"}
        ).json()["data"]
        assert none_hits == [], f"{doc_id} 对不存在的词返回了命中"


def _pick_term(text: str) -> str:
    """从切片正文里挑一个连续中文片段作为检索词。"""
    m = _CJK_RUN_RE.search(text or "")
    assert m, f"切片无可检索的中文片段：{text[:60]!r}"
    return m.group(0)[:4]


# ------------------------------------------------------------------ 8. MySQL 模式
_MYSQL_DOC = {
    "docId": 1,
    "title": "校园网开户与报修指南",
    "dept": "信息网络中心",
    "ktype": "办事指南",
    "fileFormat": "md",
    # MySQL 仓储的 fileKey 是**完整相对路径**（上传件为 uploads/xxx）
    "fileKey": KB_MD,
    "fileSize": 1713,
    "chunkCount": 2,
    "visibleRoles": ["全部"],
    "status": 2,
    "statusName": "已发布",
}

_MYSQL_CHUNKS = [
    {
        "chunkId": "D001-C001",
        "docId": "D001",
        "seqNo": 1,
        "charCount": 6,
        "dept": "信息网络中心",
        "ktype": "办事指南",
        "visibleRoles": ["全部"],
        "titlePath": "校园网开户与报修指南",
        "blockType": "para",
        "text": "校园网开户",
        "embedText": "校园网开户与报修指南\n校园网开户",
    },
    {
        "chunkId": "D001-C002",
        "docId": "D001",
        "seqNo": 2,
        "charCount": 4,
        "dept": "信息网络中心",
        "ktype": "办事指南",
        "visibleRoles": ["全部"],
        "titlePath": "其他",
        "blockType": "para",
        "text": "食堂菜谱",
        "embedText": "其他\n食堂菜谱",
    },
]


@pytest.fixture()
def mysql_mode(monkeypatch: pytest.MonkeyPatch):
    """把接口层切到 MySQL 分支，但用桩数据替代真实库（无需 Docker/MySQL）。"""
    from app.api import docs as docs_api
    from app.db import kb_repo

    monkeypatch.setattr(docs_api, "_mysql_mode", lambda: True)
    monkeypatch.setattr(kb_repo, "get_doc", lambda doc_id: dict(_MYSQL_DOC) if doc_id == 1 else None)
    monkeypatch.setattr(kb_repo, "list_chunks", lambda doc_id: [dict(c) for c in _MYSQL_CHUNKS])
    yield


def test_mysql_mode_preview_uses_filekey(mysql_mode: None) -> None:
    """MySQL 模式没有 ``file`` 字段，必须回落到 ``fileKey``（完整相对路径）。"""
    d = client.get("/api/docs/D001/preview").json()["data"]
    assert d["doc"]["docId"] == 1
    assert d["original"]["fileKey"] == KB_MD
    assert d["original"]["exists"] is True
    assert d["textMeta"]["ok"] is True and d["text"]
    assert d["total"] == 2
    assert [c["seqNo"] for c in d["chunks"]] == [1, 2]


def test_mysql_mode_raw_and_text(mysql_mode: None) -> None:
    raw = client.get("/api/docs/D001/raw")
    assert raw.status_code == 200
    assert raw.content == dp.resolve_original(KB_MD).read_bytes()
    txt = client.get("/api/docs/D001/text").json()
    assert txt["ok"] is True and txt["data"]["fileKey"] == KB_MD


def test_mysql_mode_chunks_search(mysql_mode: None) -> None:
    all_chunks = client.get("/api/docs/D001/chunks").json()["data"]
    assert len(all_chunks) == 2
    hits = client.get("/api/docs/D001/chunks", params={"q": "校园网"}).json()["data"]
    assert [c["chunkId"] for c in hits] == ["D001-C001"]
    assert hits[0]["matches"] == [[0, 3]]


def test_mysql_mode_404_and_400(mysql_mode: None) -> None:
    assert client.get("/api/docs/D999/preview").status_code == 404
    assert client.get("/api/docs/D999").status_code == 404
    assert client.get("/api/docs/D999/raw").status_code == 404
    # 非法编号在 MySQL 模式下先经 parse_public_doc_id 校验 → 400
    assert client.get("/api/docs/not-a-number").status_code == 400
    assert client.get("/api/docs/not-a-number/raw").status_code == 400


def test_mysql_mode_degrades_when_db_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据库不可用时：只读接口返回 ok=false（raw 走 503），而不是 500。"""
    from app.api import docs as docs_api
    from app.db import kb_repo
    from app.db.session import DatabaseUnavailable

    def boom(*_a, **_kw):
        raise DatabaseUnavailable("connection refused")

    monkeypatch.setattr(docs_api, "_mysql_mode", lambda: True)
    monkeypatch.setattr(kb_repo, "get_doc", boom)
    monkeypatch.setattr(kb_repo, "list_chunks", boom)

    for path in ("/api/docs/D001", "/api/docs/D001/preview", "/api/docs/D001/text"):
        body = client.get(path).json()
        assert body["ok"] is False, path
        assert "数据库不可用" in body["error"], path
    assert client.get("/api/docs/D001/chunks").json()["ok"] is False
    assert client.get("/api/docs/D001/raw").status_code == 503
