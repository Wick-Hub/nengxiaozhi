"""生成 12 格式样例文件（任务 1.10 验收用）。

为什么要生成而不是直接用仓库里的文件：知识目录只有 md/xlsx/json 三种，
`.doc/.ppt/.pptx/.png` 等无法覆盖，验收需要**可复现的输入**。

生成策略：

* 纯 Python 可写的格式（md/txt/csv/json/xlsx/xls/docx/pptx/png/jpg）**现场生成**；
* `.pdf` **复制**仓库根目录的 ``采购内容及要求.pdf``（有文本层，无需额外依赖）；
* `.doc` / `.ppt` 是 OLE 二进制旧格式，**没有纯 Python 写入库**，只有装了
  LibreOffice 才能生成——标记为 ``needs_soffice``，验收工具按需跳过。

用法::

    python tools/make_samples.py                 # 生成到 outputs/samples/
    python tools/make_samples.py --out /tmp/s    # 指定输出目录
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

#: 仓库根目录的 PDF（含文本层，直接复用避免引入 PDF 生成依赖）
PDF_SOURCE = SERVICE_DIR.parent / "采购内容及要求.pdf"

_CN_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

SAMPLE_TEXT = "校园网开户与报修指南"
SAMPLE_LINES = (
    "校园网开户与报修指南",
    "联系电话 029-85581234",
    "办公地点 信息化服务大厅",
)


@dataclass
class Sample:
    """一个样例文件。"""

    fmt: str
    name: str
    kind: str  # generated | copied | needs_soffice
    expect: list[str] = field(default_factory=list)
    note: str = ""
    path: Path | None = None

    def to_dict(self) -> dict:
        return {
            "format": self.fmt,
            "name": self.name,
            "kind": self.kind,
            "expect": self.expect,
            "note": self.note,
            "path": str(self.path) if self.path else "",
        }


def find_cn_font() -> str | None:
    for candidate in _CN_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def make_png_bytes(lines: tuple[str, ...] = SAMPLE_LINES) -> bytes:
    """生成一张含中文的 PNG（OCR 样例）。"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = find_cn_font()
    font = ImageFont.truetype(font_path, 34) if font_path else ImageFont.load_default()
    img = Image.new("RGB", (900, 60 + 70 * len(lines)), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((24, 24 + 70 * i), line, font=font, fill="black")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_all(out_dir: Path, *, pdf_source: Path | None = None) -> list[Sample]:
    """生成全部样例，返回 :class:`Sample` 列表（``path`` 已填好）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    samples: list[Sample] = []

    def add(sample: Sample) -> Sample:
        sample.path = out_dir / sample.name
        samples.append(sample)
        return sample

    # 1. md（含 frontmatter）
    md = (
        "---\ntitle: 校园网开户与报修指南\ndept: 信息网络中心\n"
        "ktype: 办事指南\nvisible_roles: 全部\nstatus: 已审核\n---\n\n"
        "# 校园网开户与报修指南\n\n## 一、开户与账号\n\n"
        "- 新生学号、教职工工号即校园网账号，入学/入职工后由信息网络中心统一开通。\n"
        "- 初始密码为身份证后 6 位，首次登录后须立即修改。\n\n"
        "## 二、上网资费\n\n"
        "| 套餐 | 月流量 | 费用（元/月） |\n| --- | --- | --- |\n"
        "| 基础套餐 | 20GB | 10 |\n| 标准套餐 | 50GB | 20 |\n"
    )
    (out_dir / "01-校园网开户与报修指南.md").write_text(md, encoding="utf-8")
    add(Sample(".md", "01-校园网开户与报修指南.md", "generated", ["校园网", "基础套餐"]))

    # 2. txt
    txt = (
        "一卡通挂失与补办指南\n\n"
        "一卡通丢失后请立即挂失，挂失渠道包括：\n"
        "1. 自助服务终端\n2. 网上服务大厅\n3. 后勤服务大厅窗口\n\n"
        "补办需携带学生证，工本费 20 元。\n"
    )
    (out_dir / "02-一卡通挂失与补办指南.txt").write_text(txt, encoding="utf-8")
    add(Sample(".txt", "02-一卡通挂失与补办指南.txt", "generated", ["一卡通", "挂失"]))

    # 3. csv
    with (out_dir / "03-图书馆开放时间.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["区域", "开放时间", "备注"])
        writer.writerow(["中文社科阅览室", "08:00-22:00", "周一至周日"])
        writer.writerow(["自然科学阅览室", "08:00-17:30", "周末闭馆"])
    add(Sample(".csv", "03-图书馆开放时间.csv", "generated", ["阅览室", "开放时间"]))

    # 4. json（FAQ 问答对）
    faq = {
        "title": "宿舍生活FAQ",
        "dept": "后勤处",
        "ktype": "常见问题",
        "items": [
            {"q": "宿舍空调如何使用？", "a": "空调由后勤处统一管理，押金 200 元，电费自理。"},
            {"q": "宿舍报修怎么提交？", "a": "通过网上服务大厅提交报修工单，24 小时内响应。"},
        ],
    }
    (out_dir / "04-宿舍生活FAQ.json").write_text(
        json.dumps(faq, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    add(Sample(".json", "04-宿舍生活FAQ.json", "generated", ["空调", "报修"]))

    # 5. xlsx
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "差旅报销标准"
    ws.append(["职级", "住宿标准（元/天）", "交通补贴"])
    ws.append(["副教授及以下", "450", "80"])
    ws.append(["教授", "600", "120"])
    ws2 = wb.create_sheet("注意事项")
    ws2.append(["事项", "说明"])
    ws2.append(["发票", "发票丢失需提供复印件并填写说明"])
    wb.save(out_dir / "05-差旅费报销标准.xlsx")
    add(Sample(".xlsx", "05-差旅费报销标准.xlsx", "generated", ["差旅报销标准", "住宿标准"]))

    # 6. xls（旧版 Excel，xlwt 写入）
    try:
        import xlwt

        book = xlwt.Workbook(encoding="utf-8")
        sheet = book.add_sheet("固定资产分类")
        for col, head in enumerate(["资产类别", "使用年限", "归口部门"]):
            sheet.write(0, col, head)
        for row, data in enumerate(
            [["服务器", "5 年", "信息网络中心"], ["实验设备", "8 年", "国有资产管理处"]], start=1
        ):
            for col, val in enumerate(data):
                sheet.write(row, col, val)
        book.save(str(out_dir / "06-固定资产分类.xls"))
        add(Sample(".xls", "06-固定资产分类.xls", "generated", ["固定资产分类", "服务器"]))
    except ImportError:
        add(Sample(".xls", "06-固定资产分类.xls", "needs_soffice", [], "未安装 xlwt，无法生成 .xls"))

    # 7. docx（含内嵌图片）
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    doc.add_heading("招生咨询常见问题", level=1)
    doc.add_paragraph("本指南适用于本科招生咨询，最终以当年招生章程为准。")
    doc.add_heading("一、录取分数", level=2)
    doc.add_paragraph("录取分数以各省招生考试院公布为准，学校不另行划定。")
    table = doc.add_table(rows=3, cols=2)
    for r, (a, b) in enumerate(
        [("批次", "录取时间"), ("提前批", "7 月上旬"), ("本科一批", "7 月中下旬")]
    ):
        table.cell(r, 0).text = a
        table.cell(r, 1).text = b
    doc.add_paragraph("以下是招生宣传图片：")
    doc.add_picture(BytesIO(make_png_bytes()), width=Inches(4))
    doc.save(out_dir / "07-招生咨询常见问题.docx")
    add(
        Sample(
            ".docx",
            "07-招生咨询常见问题.docx",
            "generated",
            ["招生咨询常见问题", "录取分数", "本科一批"],
            "含 1 张内嵌图片，用于验证原图抽取",
        )
    )

    # 8. pptx（标题/正文/表格/图片/备注）
    from pptx import Presentation
    from pptx.util import Inches as PptInches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "能小智校园 AI 助手平台"
    slide.placeholders[1].text = "知识库建设与运维培训"
    slide.notes_slide.notes_text_frame.text = "开场先介绍平台定位。"

    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    slide2.shapes.title.text = "知识入库流程"
    box = slide2.shapes.add_textbox(PptInches(0.8), PptInches(1.6), PptInches(8), PptInches(2))
    tf = box.text_frame
    tf.text = "1. 上传原始文件"
    tf.add_paragraph().text = "2. 解析并切片"
    tf.add_paragraph().text = "3. 向量化入库"
    slide2.shapes.add_picture(
        BytesIO(make_png_bytes(("知识入库流程图",))),
        PptInches(0.8),
        PptInches(4),
        width=PptInches(4),
    )
    slide3 = prs.slides.add_slide(prs.slide_layouts[5])
    slide3.shapes.title.text = "切片参数"
    rows, cols = 3, 2
    tbl = slide3.shapes.add_table(rows, cols, PptInches(0.8), PptInches(1.8), PptInches(6), PptInches(2)).table
    for r, (a, b) in enumerate([("参数", "取值"), ("切片上限", "400 字"), ("重叠", "60 字")]):
        tbl.cell(r, 0).text = a
        tbl.cell(r, 1).text = b
    prs.save(out_dir / "08-知识库建设培训.pptx")
    add(
        Sample(
            ".pptx",
            "08-知识库建设培训.pptx",
            "generated",
            ["能小智校园 AI 助手平台", "知识入库流程", "切片上限", "备注"],
            "3 页，含表格/图片/演讲者备注",
        )
    )

    # 9. png（第 12 类：图片 → OCR）
    (out_dir / "09-校园网开户公告.png").write_bytes(make_png_bytes())
    add(
        Sample(
            ".png",
            "09-校园网开户公告.png",
            "generated",
            ["校园网", "029"],
            "图片型文档，走 OCR",
        )
    )

    # 10. jpg（同类，验证扩展名归一）
    from PIL import Image

    jpg_buf = BytesIO()
    Image.open(BytesIO(make_png_bytes())).convert("RGB").save(jpg_buf, format="JPEG", quality=92)
    (out_dir / "10-校园网开户公告.jpg").write_bytes(jpg_buf.getvalue())
    add(Sample(".jpg", "10-校园网开户公告.jpg", "generated", ["校园网"], "JPEG 变体"))

    # 11. pdf（复用仓库根目录的含文本层 PDF）
    source = Path(pdf_source) if pdf_source else PDF_SOURCE
    if source.is_file():
        shutil.copyfile(source, out_dir / "11-采购内容及要求.pdf")
        add(Sample(".pdf", "11-采购内容及要求.pdf", "copied", ["能小智"], f"复制自 {source.name}"))
    else:
        add(Sample(".pdf", "11-采购内容及要求.pdf", "needs_soffice", [], f"缺少源文件 {source}"))

    # 12. doc / ppt（OLE 二进制旧格式，需 LibreOffice 才能生成）
    add(
        Sample(
            ".doc",
            "12-招生咨询常见问题.doc",
            "needs_soffice",
            ["招生咨询常见问题"],
            "需 LibreOffice 由 .docx 转换生成",
        )
    )
    add(
        Sample(
            ".ppt",
            "13-知识库建设培训.ppt",
            "needs_soffice",
            ["知识入库流程"],
            "需 LibreOffice 由 .pptx 转换生成",
        )
    )

    return samples


def build_missing_via_soffice(samples: list[Sample], out_dir: Path) -> list[str]:
    """用 LibreOffice 补齐 ``needs_soffice`` 的样例（.doc/.ppt/.pdf）。

    :return: 成功补齐的格式列表；LibreOffice 不可用时返回空列表
    """
    from app.core.parser import office_convert

    if not office_convert.soffice_available():
        return []

    pairs = {
        ".doc": ("07-招生咨询常见问题.docx", "12-招生咨询常见问题.doc"),
        ".ppt": ("08-知识库建设培训.pptx", "13-知识库建设培训.ppt"),
    }
    done: list[str] = []
    for fmt, (src_name, dst_name) in pairs.items():
        src = out_dir / src_name
        if not src.is_file():
            continue
        try:
            data = office_convert.convert_bytes(src.read_bytes(), src.suffix, fmt)
        except office_convert.ConvertError as exc:
            print(f"  [WARN] {fmt} 生成失败：{exc}")
            continue
        (out_dir / dst_name).write_bytes(data)
        for sample in samples:
            if sample.fmt == fmt:
                sample.kind = "generated"
                sample.path = out_dir / dst_name
                sample.note = "由 LibreOffice 从同内容 docx/pptx 转换生成"
        done.append(fmt)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 12 格式样例文件（任务 1.10）")
    ap.add_argument("--out", default=str(SERVICE_DIR / "outputs" / "samples"), help="输出目录")
    ap.add_argument("--with-soffice", action="store_true", help="尝试用 LibreOffice 补齐 .doc/.ppt")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    samples = build_all(out_dir)
    if args.with_soffice:
        done = build_missing_via_soffice(samples, out_dir)
        print(f"LibreOffice 补齐：{done or '未执行（LibreOffice 不可用）'}")

    print(f"样例目录：{out_dir}")
    print(f"{'格式':<8}{'文件':<32}{'状态':<14}说明")
    for s in samples:
        print(f"{s.fmt:<8}{s.name:<32}{s.kind:<14}{s.note or ('期望关键词：' + '、'.join(s.expect))}")
    print(f"合计 {len(samples)} 个样例（12 类格式）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
