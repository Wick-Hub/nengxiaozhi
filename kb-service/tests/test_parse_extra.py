"""12 格式 + OCR + 原图测试（任务 1.10）。

覆盖六层：

1. **格式登记** —— ``SUPPORTED_EXT`` / ``FORMAT_COUNT`` / ``is_supported``；
2. **OCR 层** —— 引擎探测与降级（绝不抛异常）、结果结构解析、单例复用；
3. **图片解析** —— 内容寻址落盘（去重/命名/原子写）、元信息、OCR 注入与降级；
4. **新增解析器** —— docx 内嵌图抽取、pptx 分页结构、旧格式转换与兜底；
5. **切片贯通** —— ``imageKeys`` 累积、空值不落字段、序列化往返；
6. **接口层** —— ``/api/docs/{id}/images``、``/api/docs/{id}/image?key=`` 的
   字段、字节流、缓存头与**归属校验**（key 必须属于该文档）。

设计原则：**不依赖 outputs/samples**（CI 里可能不存在），docx/pptx/png 全部现场生成；
缺失可选依赖（python-pptx / xlwt / Pillow）时按需跳过，而不是失败。
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import chunker
from app.core.parser import (
    FORMAT_COUNT,
    IMAGE_EXT,
    PLANNED_EXT,
    SUPPORTED_EXT,
    is_supported,
    parse_file,
)
from app.core.parser import image as image_parser
from app.core.parser import ocr, office_convert
from app.core.parser.block import Block
from app.main import app

client = TestClient(app)

KB_MD = "01-信网中心/办事指南/校园网开户与报修指南.md"


# ------------------------------------------------------------------ 造件工具
def _png_bytes(text: str = "校园网开户与报修指南") -> bytes:
    """生成一张含文字的 PNG（不依赖中文字体，缺字体时退回默认字体）。"""
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    font = None
    for candidate in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, 28)
            break
    if font is None:
        font = ImageFont.load_default()
    img = Image.new("RGB", (520, 90), "white")
    ImageDraw.Draw(img).text((16, 24), text, font=font, fill="black")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _docx_with_picture() -> bytes:
    pytest.importorskip("docx")
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    doc.add_heading("招生咨询常见问题", level=1)
    doc.add_paragraph("本指南适用于本科招生咨询。")
    doc.add_picture(BytesIO(_png_bytes()), width=Inches(3))
    doc.add_paragraph("录取分数以各省招生考试院公布为准。")
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pptx_bytes() -> bytes:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[0])
    s1.shapes.title.text = "能小智校园 AI 助手平台"
    s1.placeholders[1].text = "知识库建设与运维培训"
    s1.notes_slide.notes_text_frame.text = "开场先介绍平台定位。"

    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "知识入库流程"
    box = s2.shapes.add_textbox(Inches(0.8), Inches(1.6), Inches(8), Inches(2))
    tf = box.text_frame
    tf.text = "1. 上传原始文件"
    tf.add_paragraph().text = "2. 解析并切片"
    s2.shapes.add_picture(BytesIO(_png_bytes()), Inches(0.8), Inches(4), width=Inches(3))

    s3 = prs.slides.add_slide(prs.slide_layouts[5])
    s3.shapes.title.text = "切片参数"
    tbl = s3.shapes.add_table(
        2, 2, Inches(0.8), Inches(1.8), Inches(6), Inches(1.5)
    ).table
    tbl.cell(0, 0).text = "参数"
    tbl.cell(0, 1).text = "取值"
    tbl.cell(1, 0).text = "切片上限"
    tbl.cell(1, 1).text = "400 字"

    buf = BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ 1. 格式登记
def test_supported_ext_covers_12_format_classes() -> None:
    classes = [
        {".md"},
        {".txt"},
        {".docx"},
        {".doc"},
        {".pdf"},
        {".xlsx"},
        {".xls"},
        {".csv"},
        {".json"},
        {".pptx"},
        {".ppt"},
        set(IMAGE_EXT),
    ]
    assert len(classes) == FORMAT_COUNT == 12
    assert set().union(*classes) == set(SUPPORTED_EXT)
    assert PLANNED_EXT == frozenset(), "任务 1.10 已把「待扩展格式」全部落地"


@pytest.mark.parametrize("ext", sorted(SUPPORTED_EXT))
def test_is_supported_accepts_all_registered(ext: str) -> None:
    assert is_supported(f"x{ext}")
    assert is_supported(f"X{ext.upper()}")


def test_is_supported_rejects_unknown() -> None:
    assert not is_supported("x.xyz")
    assert not is_supported("noext")


# ------------------------------------------------------------------ 2. OCR 层
def test_ocr_stats_shape() -> None:
    info = ocr.stats()
    assert set(info) >= {"configured", "engine", "available", "lang", "maxSide"}
    assert isinstance(info["available"], list)
    assert info["configured"] in (ocr.ENGINE_AUTO, *ocr.available_engines(), ocr.ENGINE_NONE)


def test_resolve_engine_off_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ocr_engine", "none")
    assert ocr.resolve_engine() == ocr.ENGINE_NONE


@pytest.mark.parametrize("name", [ocr.ENGINE_RAPIDOCR, ocr.ENGINE_TESSERACT])
def test_resolve_engine_explicit_requires_availability(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """显式指定但不可用时 → ``none``（而不是抛异常）。"""
    monkeypatch.setattr(settings, "ocr_engine", name)
    expected = name if name in ocr.available_engines() else ocr.ENGINE_NONE
    assert ocr.resolve_engine() == expected


def test_resolve_engine_auto_falls_back_to_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ocr_engine", "auto")
    available = ocr.available_engines()
    assert ocr.resolve_engine() == (available[0] if available else ocr.ENGINE_NONE)


def test_ocr_disabled_result_is_degraded_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ocr_engine", "none")
    result = ocr.ocr_image(_png_bytes())
    assert result.ok is False
    assert result.engine == ocr.ENGINE_NONE
    assert result.error and "关闭" in result.error


def test_ocr_non_image_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """非图片输入必须返回业务错误，绝不抛异常。"""
    monkeypatch.setattr(settings, "ocr_engine", "auto")
    result = ocr.ocr_image(b"definitely not an image")
    assert result.ok is False
    assert result.error
    assert result.line_count == 0


def test_ocr_engine_exception_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """引擎内部抛错也要被兜住（否则一张坏图会拖垮整次入库）。"""
    monkeypatch.setattr(settings, "ocr_engine", "auto")
    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr.ENGINE_RAPIDOCR)

    def boom(_img):
        raise RuntimeError("模拟引擎崩溃")

    monkeypatch.setattr(ocr, "_run_rapidocr", boom)
    result = ocr.ocr_image(_png_bytes())
    assert result.ok is False
    assert "模拟引擎崩溃" in (result.error or "")


def test_ocr_rapidocr_result_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """rapidocr 返回 ``[[box, text, score], ...]``；顺序搞错会得到空文本或类型错误。"""
    monkeypatch.setattr(settings, "ocr_engine", "auto")
    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr.ENGINE_RAPIDOCR)

    class FakeEngine:
        def __call__(self, _arr):
            return [
                [[[0, 0], [10, 0], [10, 10], [0, 10]], "校园网", 0.98],
                [[[0, 12], [10, 12], [10, 22], [0, 22]], "报修", "0.91"],  # 字符串置信度
                [[[0, 24], [10, 24], [10, 34], [0, 34]], "   ", 0.5],  # 空白行丢弃
            ]

    monkeypatch.setattr(ocr, "_rapidocr_instance", lambda: FakeEngine())
    result = ocr.ocr_image(_png_bytes())
    assert result.ok is True
    assert result.engine == ocr.ENGINE_RAPIDOCR
    assert result.text == "校园网\n报修"
    assert result.line_count == 2
    assert result.lines[0].score == pytest.approx(0.98)
    assert result.lines[1].score == pytest.approx(0.91)
    assert result.lines[0].box == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]


def test_ocr_lines_to_text_skips_empty() -> None:
    lines = [ocr.OcrLine(text="a"), ocr.OcrLine(text=""), ocr.OcrLine(text="b")]
    assert ocr.lines_to_text(lines) == "a\nb"


def test_ocr_result_to_dict() -> None:
    result = ocr.OcrResult(ok=True, engine="rapidocr", text="hi", elapsed=1.23456)
    payload = result.to_dict()
    assert payload["ok"] is True and payload["lineCount"] == 0
    assert payload["elapsed"] == pytest.approx(1.235, abs=1e-3)


def test_ocr_image_respects_max_side(monkeypatch: pytest.MonkeyPatch) -> None:
    """送 OCR 前按长边等比缩放（大图显著拖慢推理）。"""
    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setattr(settings, "ocr_max_side", 100)
    img = ocr._load_image(_png_bytes())
    assert max(img.size) == 100


# ------------------------------------------------------------------ 3. 图片解析
def test_save_original_content_addressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    payload = b"\x89PNG\r\n\x1a\n" + b"payload-1" * 10
    key = image_parser.save_original(payload, ".png")
    digest = hashlib.sha256(payload).hexdigest()

    assert key == f"uploads/original/{digest[:16]}.png"
    target = tmp_path / "original" / f"{digest[:16]}.png"
    assert target.read_bytes() == payload
    assert not list((tmp_path / "original").glob("*.tmp")), "临时文件必须已被原子替换"

    # 同内容二次落盘 → 同一个键（内容寻址去重），且不重写文件
    before = target.stat().st_mtime_ns
    assert image_parser.save_original(payload, ".png") == key
    assert target.stat().st_mtime_ns == before

    # 不同内容 → 不同键
    assert image_parser.save_original(payload + b"x", ".png") != key


def test_save_original_normalizes_ext(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    assert image_parser.save_original(b"a", "png").endswith(".png")
    assert image_parser.save_original(b"b", ".JPG").endswith(".jpg")
    assert image_parser.save_original(b"c", "").endswith(".bin")


def test_image_meta_and_bad_bytes() -> None:
    info = image_parser.image_meta(_png_bytes())
    assert info["format"] == "png"
    assert (info["width"], info["height"]) == (520, 90)
    assert "error" in image_parser.image_meta(b"not an image")


def test_image_to_blocks_with_ocr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    data = _png_bytes()
    injected = ocr.OcrResult(
        ok=True,
        engine="fake",
        text="校园网开户与报修指南\n联系电话 029-85581234",
        lines=[ocr.OcrLine(text="校园网开户与报修指南"), ocr.OcrLine(text="联系电话 029-85581234")],
    )
    blocks, meta = image_parser.image_to_blocks(
        data, source_name="09-校园网开户公告.png", ocr_result=injected
    )

    assert [b.type for b in blocks] == ["heading", "image", "para"]
    assert blocks[0].text == "09-校园网开户公告"
    assert blocks[1].text == "09-校园网开户公告.png"
    assert blocks[1].meta["image_key"] == meta["imageKey"]
    assert blocks[2].text.startswith("校园网开户与报修指南")
    assert blocks[2].meta["ocr"] is True

    assert meta["ocrOk"] is True and meta["ocrEngine"] == "fake"
    assert meta["ocrLineCount"] == 2
    assert "warning" not in meta
    # 原图已落盘
    assert (tmp_path / "original").is_dir()


def test_image_to_blocks_degrades_without_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    failed = ocr.OcrResult(ok=False, engine="none", error="未安装 OCR 引擎")
    blocks, meta = image_parser.image_to_blocks(
        _png_bytes(), source_name="a.png", ocr_result=failed
    )

    assert [b.type for b in blocks] == ["heading", "image", "para"]
    assert "未安装 OCR 引擎" in blocks[2].text
    assert blocks[2].meta["ocr"] is False
    assert meta["ocrOk"] is False
    assert "未安装 OCR 引擎" in meta["warning"]


def test_image_to_blocks_uses_supplied_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """上传件应复用自己已落盘的对象键，而不是再按内容哈希存一份。"""
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    key = "uploads/1730000000000-a.png"
    blocks, meta = image_parser.image_to_blocks(
        _png_bytes(), source_name="a.png", image_key=key, ocr_result=ocr.OcrResult(ok=False)
    )
    assert meta["imageKey"] == key
    assert blocks[1].meta["image_key"] == key
    assert not (tmp_path / "original").exists()


# ------------------------------------------------------------------ 4. 解析器
def test_parse_file_dispatch_covers_all_formats(tmp_path: Path) -> None:
    """12 类格式的扩展名分发必须全部走通（旧格式允许「缺转换器」这一条降级路径）。"""
    pytest.importorskip("docx")
    from docx import Document

    docx_path = tmp_path / "a.docx"
    d = Document()
    d.add_heading("标题", level=1)
    d.add_paragraph("正文内容。")
    d.save(docx_path)

    xlsx_path = tmp_path / "a.xlsx"
    if _have("openpyxl"):
        from openpyxl import Workbook

        wb = Workbook()
        wb.active.append(["列", "值"])
        wb.active.append(["甲", "乙"])
        wb.save(xlsx_path)

    json_path = tmp_path / "a.json"
    json_path.write_text(json.dumps({"title": "FAQ", "items": [{"q": "问", "a": "答"}]}), "utf-8")

    md_path = tmp_path / "a.md"
    md_path.write_text("---\ntitle: 标题\n---\n\n# 标题\n\n正文。", encoding="utf-8")

    txt_path = tmp_path / "a.txt"
    txt_path.write_text("纯文本内容。", encoding="utf-8")

    csv_path = tmp_path / "a.csv"
    csv_path.write_text("列,值\n甲,乙\n", encoding="utf-8-sig")

    png_path = tmp_path / "a.png"
    png_path.write_bytes(_png_bytes())

    cases: list[tuple[str, Path, bool]] = [
        (".md", md_path, False),
        (".txt", txt_path, False),
        (".csv", csv_path, False),
        (".json", json_path, False),
        (".docx", docx_path, False),
        (".xlsx", xlsx_path, False),
        (".png", png_path, False),
    ]
    if _have("pptx"):
        pptx_path = tmp_path / "a.pptx"
        pptx_path.write_bytes(_pptx_bytes())
        cases.append((".pptx", pptx_path, False))
    # .doc / .ppt 依赖外部转换器；本环境无 LibreOffice 时必须走「业务异常」降级
    doc_path = tmp_path / "a.doc"
    doc_path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    cases.append((".doc", doc_path, True))
    ppt_path = tmp_path / "a.ppt"
    ppt_path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    cases.append((".ppt", ppt_path, True))

    for ext, path, maybe_converter in cases:
        if not path.is_file():
            continue
        assert is_supported(path), f"{ext} 应被登记为受支持"
        if maybe_converter and not office_convert.soffice_available():
            with pytest.raises(office_convert.ConverterUnavailable):
                parse_file(path)
            continue
        parsed = parse_file(path)
        assert parsed.blocks, f"{ext} 应产出解析块"


def test_parse_file_rejects_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "a.xyz"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持的格式"):
        parse_file(path)


def test_parse_file_image_display_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """内容寻址的原图默认用哈希名当标题，传 display_name 后应改用文档标题。"""
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    path = tmp_path / "c251a68395140d37.png"
    path.write_bytes(_png_bytes())

    parsed = parse_file(path, display_name="校园网开户公告")
    assert parsed.blocks[0].type == "heading"
    assert parsed.blocks[0].text == "校园网开户公告"
    assert parsed.meta["image"]["imageName"] == "校园网开户公告"

    plain = parse_file(path)
    assert plain.blocks[0].text == "c251a68395140d37"


def test_docx_image_sink_none_keeps_prototype_shape(tmp_path: Path) -> None:
    """不传 image_sink 时块输出必须与原型一致（parity 回归走这条路径）。"""
    path = tmp_path / "a.docx"
    path.write_bytes(_docx_with_picture())
    parsed = parse_file(path)
    image_blocks = [b for b in parsed.blocks if b.type == "image"]
    assert image_blocks, "docx 内嵌图片应产出 image 块"
    assert all(not b.meta for b in image_blocks), "不抽图时不应带 image_key"
    assert parsed.meta == {}


def test_docx_image_sink_extracts_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    path = tmp_path / "a.docx"
    path.write_bytes(_docx_with_picture())

    saved: list[str] = []

    def sink(data: bytes, ext: str) -> str:
        key = image_parser.save_original(data, ext)
        saved.append(key)
        return key

    parsed = parse_file(path, image_sink=sink)
    image_blocks = [b for b in parsed.blocks if b.type == "image"]
    assert len(image_blocks) == 1
    meta = image_blocks[0].meta
    assert meta["image_key"] == meta["image_keys"][0] == saved[0]
    assert meta["image_key"].startswith("uploads/original/")
    assert (tmp_path / "original").is_dir()


def test_pptx_structure(tmp_path: Path) -> None:
    pytest.importorskip("pptx")
    path = tmp_path / "a.pptx"
    path.write_bytes(_pptx_bytes())
    blocks = parse_file(path).blocks

    headings = [b.text for b in blocks if b.type == "heading" and b.level == 1]
    assert headings == ["幻灯片 1", "幻灯片 2", "幻灯片 3"]
    assert "能小智校园 AI 助手平台" in [b.text for b in blocks if b.level == 2]
    assert any(b.type == "table" and "切片上限" in b.text for b in blocks)
    assert any(b.type == "image" for b in blocks)
    assert any(b.text.startswith("备注：") for b in blocks)
    assert any("2. 解析并切片" in b.text for b in blocks)


def test_pptx_image_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pptx")
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    path = tmp_path / "a.pptx"
    path.write_bytes(_pptx_bytes())

    parsed = parse_file(path, image_sink=image_parser.save_original)
    keys = [b.meta.get("image_key") for b in parsed.blocks if b.type == "image"]
    assert keys and all(k and k.startswith("uploads/original/") for k in keys)


def test_doc_without_converter_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office_convert, "find_soffice", lambda: None)
    monkeypatch.setattr(office_convert, "find_doc_text_cli", lambda: None)
    from app.core.parser import doc as doc_parser

    with pytest.raises(office_convert.ConverterUnavailable) as ei:
        doc_parser.doc_to_blocks(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert "LibreOffice" in str(ei.value)


def test_doc_falls_back_to_cli_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 LibreOffice 但有 antiword/catdoc 时，抽纯文本入库（无结构但可用）。"""
    monkeypatch.setattr(office_convert, "soffice_available", lambda: False)
    monkeypatch.setattr(office_convert, "doc_text_via_cli", lambda _d: "# 标题\n\n正文内容。")
    from app.core.parser import doc as doc_parser

    blocks = doc_parser.doc_to_blocks(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert any(b.type == "heading" and b.text == "标题" for b in blocks)
    assert any("正文内容" in b.text for b in blocks)


def test_doc_uses_soffice_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """有转换器时走 docx 解析链路（保留标题层级与表格结构）。"""
    pytest.importorskip("docx")
    monkeypatch.setattr(office_convert, "soffice_available", lambda: True)
    monkeypatch.setattr(
        office_convert, "convert_bytes", lambda data, src, dst: _docx_with_picture()
    )
    from app.core.parser import doc as doc_parser

    blocks = doc_parser.doc_to_blocks(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert any(b.type == "heading" and b.text == "招生咨询常见问题" for b in blocks)


def test_ppt_without_converter_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office_convert, "soffice_available", lambda: False)
    from app.core.parser import doc as doc_parser

    with pytest.raises(office_convert.ConverterUnavailable):
        doc_parser.ppt_to_blocks(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


def test_office_convert_stats_shape() -> None:
    info = office_convert.stats()
    assert isinstance(info["sofficeAvailable"], bool)
    assert "timeout" in info and info["timeout"] > 0
    assert office_convert.SOFFICE_MISSING_HINT


def test_convert_bytes_without_soffice_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office_convert, "find_soffice", lambda: None)
    with pytest.raises(office_convert.ConverterUnavailable):
        office_convert.convert_bytes(b"x", ".doc", ".docx")


# ------------------------------------------------------------------ 5. 切片贯通
def test_chunks_accumulate_image_keys() -> None:
    key = "uploads/original/abc.png"
    chunks = chunker.blocks_to_chunks(
        [
            Block(type="heading", level=1, text="图片文档"),
            Block(type="para", text="下面是一张图。" * 4, meta={"image_key": key}),
            Block(type="image", text="abc.png", meta={"image_key": key, "image_keys": [key]}),
        ],
        {"dept": "信息网络中心", "ktype": "办事指南"},
        "01-信网中心/办事指南/图片文档.md",
    )
    payloads = [c.to_dict() for c in chunks]
    keys = {k for p in payloads for k in p.get("imageKeys", [])}
    assert keys == {key}, "同一张图在切片里必须去重"


def test_chunks_omit_image_keys_when_empty() -> None:
    """无图切片不得出现 imageKeys —— 否则会改变既有 store.json 的逐字段形状。"""
    chunks = chunker.blocks_to_chunks(
        [Block(type="para", text="纯文本段落。" * 20)],
        {"dept": "信息网络中心", "ktype": "办事指南"},
        "01-信网中心/办事指南/纯文本.md",
    )
    assert chunks and all("imageKeys" not in c.to_dict() for c in chunks)


def test_chunk_image_keys_roundtrip() -> None:
    chunk = chunker.Chunk(
        chunk_id="D001-C001", doc_id="D001", seq_no=1, text="x",
        image_keys=["uploads/original/a.png", "uploads/original/b.png"],
    )
    again = chunker.Chunk.from_dict(chunk.to_dict())
    assert again.image_keys == chunk.image_keys
    assert chunker.Chunk.from_dict({"text": "y"}).image_keys == []


# ------------------------------------------------------------------ 6. 接口层
_DOCS = [
    {
        "docId": "D001",
        "file": "uploads/original/{key}.png",
        "title": "校园网开户公告",
        "dept": "信息网络中心",
        "ktype": "办事指南",
        "status": "已审核",
        "chunkCount": 1,
    },
    {
        "docId": "D002",
        "file": KB_MD,
        "title": "校园网开户与报修指南",
        "dept": "信息网络中心",
        "ktype": "办事指南",
        "status": "已审核",
        "chunkCount": 2,
    },
]


@pytest.fixture()
def image_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """临时 upload_dir + 临时 JSON 索引：D001 图片型文档、D002 含内嵌图文档。"""
    from app.core.index_store import clear_index

    payload = _png_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    key = f"uploads/original/{digest[:16]}.png"

    uploads = tmp_path / "uploads" / "original"
    uploads.mkdir(parents=True)
    (uploads / f"{digest[:16]}.png").write_bytes(payload)

    docs = json.loads(json.dumps(_DOCS).replace("{key}", digest[:16]))
    store = {
        "model": "test",
        "createdAt": "2026-10-14T00:00:00Z",
        "docs": docs,
        "chunks": [
            {
                "chunkId": "D001-C001", "docId": "D001", "seqNo": 1, "charCount": 20,
                "dept": "信息网络中心", "ktype": "办事指南", "visibleRoles": ["全部"],
                "titlePath": "校园网开户公告", "blockType": "para",
                "text": "校园网开户与报修指南", "embedText": "校园网开户公告\n校园网开户与报修指南",
            },
            {
                "chunkId": "D002-C001", "docId": "D002", "seqNo": 1, "charCount": 20,
                "dept": "信息网络中心", "ktype": "办事指南", "visibleRoles": ["全部"],
                "titlePath": "校园网开户与报修指南", "blockType": "para",
                "text": "校园网开户与报修指南", "embedText": "x",
                "imageKeys": [key],
            },
            {
                "chunkId": "D002-C002", "docId": "D002", "seqNo": 2, "charCount": 12,
                "dept": "信息网络中心", "ktype": "办事指南", "visibleRoles": ["全部"],
                "titlePath": "校园网开户与报修指南", "blockType": "para",
                "text": "报修电话 029-85581234", "embedText": "y",
            },
        ],
    }
    store_path = tmp_path / "store.json"
    store_path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "store_path", store_path)
    clear_index()
    yield {"key": key, "payload": payload, "dir": tmp_path}
    clear_index()


def test_list_images_for_image_doc(image_env: dict) -> None:
    resp = client.get("/api/docs/D001/images")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["isImageDoc"] is True
    assert data["total"] == 1
    img = data["images"][0]
    assert img["role"] == "original"
    assert img["key"] == image_env["key"]
    assert img["exists"] is True
    assert img["size"] == len(image_env["payload"])
    assert (img["width"], img["height"]) == (520, 90)
    assert img["chunkSeqs"] == []
    assert not img["error"]
    assert "path" not in img, "接口不得回传服务器绝对路径"


def test_list_images_for_embedded_doc(image_env: dict) -> None:
    data = client.get("/api/docs/D002/images").json()["data"]
    assert data["isImageDoc"] is False
    assert data["total"] == 1
    img = data["images"][0]
    assert img["role"] == "embedded"
    assert img["key"] == image_env["key"]
    assert img["chunkSeqs"] == [1]


def test_list_images_unknown_doc_404() -> None:
    assert client.get("/api/docs/D999/images").status_code == 404


def test_get_image_streams_original_bytes(image_env: dict) -> None:
    resp = client.get(f"/api/docs/D001/image?key={image_env['key']}")
    assert resp.status_code == 200
    assert resp.content == image_env["payload"]
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in resp.headers["cache-control"]


@pytest.mark.parametrize(
    "key_tpl",
    [
        "uploads/original/deadbeefdeadbeef.png",   # 不存在
        "uploads/original/{other}.png",            # 换成另一张（不存在）
        "uploads/../etc/passwd",                   # 路径穿越
        "01-信网中心/办事指南/校园网开户与报修指南.md",  # 非图片
    ],
)
def test_get_image_rejects_bad_keys(image_env: dict, key_tpl: str) -> None:
    key = key_tpl.replace("{other}", "0" * 16)
    resp = client.get(f"/api/docs/D001/image?key={key}")
    assert resp.status_code in (400, 404), f"{key} 应被拒绝，实际 {resp.status_code}"


def test_get_image_rejects_key_of_other_doc(image_env: dict) -> None:
    """A 文档的图片不能在 B 文档下取出来（归属校验）。"""
    other = image_parser.save_original(b"\x89PNG\r\n\x1a\nother", ".png")
    assert client.get(f"/api/docs/D002/image?key={other}").status_code == 404
    # 同 key 在 D001/D002 下都应可取出（该图确实被两篇文档引用）
    assert client.get(f"/api/docs/D002/image?key={image_env['key']}").status_code == 200


def test_get_image_requires_key(image_env: dict) -> None:
    assert client.get("/api/docs/D001/image").status_code == 422


def test_preview_exposes_images_and_is_image(image_env: dict) -> None:
    d1 = client.get("/api/docs/D001/preview").json()["data"]
    assert d1["isImage"] is True
    assert len(d1["images"]) == 1
    assert d1["images"][0]["role"] == "original"

    d2 = client.get("/api/docs/D002/preview?q=报修").json()["data"]
    assert d2["isImage"] is False
    assert [i["key"] for i in d2["images"]] == [image_env["key"]]
    # 「报修」同时命中 C001（标题路径「校园网开户与报修指南」）与 C002（正文），
    # 与检索口径一致：命中判定覆盖 text 与 titlePath。
    assert d2["matchCount"] == 2
    by_id = {c["chunkId"]: c for c in d2["chunks"]}
    assert by_id["D002-C001"]["imageKeys"] == [image_env["key"]]
    assert "imageKeys" not in by_id["D002-C002"]


def test_collect_images_dedupes_and_orders(image_env: dict) -> None:
    """同一张图被多个切片引用时只出现一次，并汇总全部切片序号。"""
    from app.services import doc_preview as dp

    key = image_env["key"]
    doc = {"docId": "D002", "file": KB_MD, "title": "x"}
    chunks = [
        {"seqNo": 3, "imageKeys": [key]},
        {"seqNo": 1, "imageKeys": [key, "uploads/original/" + "a" * 16 + ".png"]},
        {"seqNo": 2, "imageKeys": []},
    ]
    images = dp.collect_images(doc, chunks)
    assert [i["key"] for i in images] == [key, "uploads/original/" + "a" * 16 + ".png"]
    assert images[0]["chunkSeqs"] == [1, 3]
    assert images[1]["chunkSeqs"] == [1]
    assert images[1]["exists"] is False and images[1]["error"]


def test_resolve_doc_image_rejects_non_image(image_env: dict) -> None:
    from app.services import doc_preview as dp

    doc = {"docId": "D002", "file": KB_MD}
    with pytest.raises(dp.PreviewError) as ei:
        dp.resolve_doc_image(doc, [], KB_MD)
    assert ei.value.status == 400
    with pytest.raises(dp.PreviewError):
        dp.resolve_doc_image(doc, [], "")


# ------------------------------------------------------------------ 7. 服务层
def test_parse_doc_returns_warnings_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    from app.services import kb_admin

    path = tmp_path / "a.md"
    path.write_text("---\ntitle: 标题\ndept: 信息网络中心\nktype: 办事指南\n---\n\n# 标题\n\n正文。" * 2, encoding="utf-8")

    result = kb_admin.parse_doc(path)
    assert result["ok"] is True
    assert result["warnings"] == []
    assert result["chunks"]


def test_parse_doc_collects_image_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OCR 不可用时入库不能失败，但必须把原因带出来给管理端。"""
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    from app.services import kb_admin

    path = tmp_path / "a.png"
    path.write_bytes(_png_bytes())
    monkeypatch.setattr(
        image_parser, "ocr_image", lambda _d: ocr.OcrResult(ok=False, error="未安装 OCR 引擎")
    )

    result = kb_admin.parse_doc(path, default_title="公告")
    assert result["ok"] is True
    assert any("未安装 OCR 引擎" in w for w in result["warnings"])
    assert result["doc_meta"]["title"] == "公告"


def test_supported_formats_sorted() -> None:
    from app.services import kb_admin

    formats = kb_admin.supported_formats()
    assert formats == sorted(formats)
    assert ".pptx" in formats and ".png" in formats and ".doc" in formats


def _have(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None
