"""解析层 / 切片层回归测试（任务 1.2 / 1.3）。

基线数值来自 Python ↔ Node 一致性比对（tools/parity/compare.py），
两侧在 9 个样例上完全一致后锁定，用于防止后续改动导致行为漂移。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.core.chunker import blocks_to_chunks, infer_from_path, parse_roles, split_windows
from app.core.parser import Block, parse_file
from app.core.parser.frontmatter import parse_frontmatter
from app.core.parser.markdown import md_to_blocks

KB = Path(settings.knowledge_dir).resolve()

# 相对路径 -> (blocks, chunks)，与 Node 版一致
BASELINE: dict[str, tuple[int, int]] = {
    "01-信网中心/办事指南/校园网开户与报修指南.md": (11, 5),
    "02-财务处/办事指南/差旅费报销指南.md": (16, 8),
    "02-财务处/常见问题/财务报销常见问题.xlsx": (2, 1),
    "03-后勤处/办事指南/一卡通挂失与补办指南.md": (11, 5),
    "03-后勤处/常见问题/宿舍生活FAQ.json": (2, 2),
    "03-后勤处/常见问题/宿舍空调使用FAQ.md": (7, 2),
    "04-国有资产管理处/政策文件/固定资产处置管理办法.md": (16, 6),
    "05-本科生招生办/常见问题/招生咨询FAQ.md": (20, 8),
    "06-图书馆/办事指南/图书馆借阅流程与规则指南.md": (10, 5),
}


@pytest.mark.parametrize("rel,expected", list(BASELINE.items()))
def test_sample_baseline(rel: str, expected: tuple[int, int]) -> None:
    f = KB / rel
    assert f.exists(), f"样例缺失：{f}"
    doc = parse_file(f)
    chunks = blocks_to_chunks(doc.blocks, doc.meta, f)
    assert (len(doc.blocks), len(chunks)) == expected


def test_frontmatter_parsing() -> None:
    meta, body = parse_frontmatter("---\ntitle: A\ndept: B\n---\n正文内容")
    assert meta == {"title": "A", "dept": "B"}
    assert body.strip() == "正文内容"


def test_markdown_blocks_order() -> None:
    blocks = md_to_blocks("# 标题\n\n段落\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n")
    assert [b.type for b in blocks] == ["heading", "para", "table"]
    assert blocks[0].level == 1
    assert blocks[2].text.startswith("| a | b |")


def test_parse_roles_variants() -> None:
    assert parse_roles("") == ["全部"]
    assert parse_roles("[教职工, 研究生]") == ["教职工", "研究生"]
    assert parse_roles("全部、本科生") == ["全部", "本科生"]


def test_meta_overrides_path_inference() -> None:
    f = KB / "01-信网中心/办事指南/校园网开户与报修指南.md"
    doc = parse_file(f)
    chunks = blocks_to_chunks(doc.blocks, doc.meta, f)
    # frontmatter dept=信息网络中心 覆盖路径推断的「信网中心」
    assert chunks[0].dept == "信息网络中心"
    assert chunks[0].ktype == "办事指南"


def test_path_inference_without_meta() -> None:
    dept, ktype = infer_from_path(KB / "01-信网中心/办事指南/某文档.md")
    assert dept == "信网中心"
    assert ktype == "办事指南"


def test_visible_roles_default_all() -> None:
    chunks = blocks_to_chunks(
        [Block(type="para", text="x" * 50)], {}, KB / "01-信网中心/办事指南/t.md"
    )
    assert chunks[0].visible_roles == ["全部"]


def test_long_paragraph_hard_split() -> None:
    """修复原型「400 字非硬切」：单条切片不超过 chunk_max。"""
    chunks = blocks_to_chunks(
        [Block(type="para", text="长" * 1200)], {}, KB / "01-信网中心/办事指南/t.md"
    )
    assert max(c.char_count for c in chunks) <= settings.chunk_max
    assert len(chunks) >= 3


def test_split_windows_short_text_unchanged() -> None:
    assert split_windows("短文本", 400, 60) == ["短文本"]


def test_title_path_from_heading_stack() -> None:
    blocks = [
        Block(type="heading", level=1, text="一级"),
        Block(type="heading", level=2, text="二级"),
        Block(type="para", text="正文" * 30),
    ]
    chunks = blocks_to_chunks(blocks, {}, KB / "01-信网中心/办事指南/t.md")
    assert chunks[0].title_path == "一级 > 二级"
    assert chunks[0].embed_text.startswith("一级 > 二级\n")
