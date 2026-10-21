"""XLSX / XLS / CSV 解析（对齐 kb-engine/src/parse/xlsx.js）。

xlsx 用 openpyxl，xls 用 xlrd，csv 用标准库；均输出「工作表标题 + 表格」块。
"""

from __future__ import annotations

import csv
import io

from app.core.parser.block import Block
from app.core.parser.markdown import to_md_table


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _read_sheets(data: bytes, ext: str) -> list[tuple[str, list[list[str]]]]:
    if ext == ".xls":
        import xlrd

        wb = xlrd.open_workbook(file_contents=data)
        out: list[tuple[str, list[list[str]]]] = []
        for sheet in wb.sheets():
            rows: list[list[str]] = []
            for r in range(sheet.nrows):
                cells = [_cell_str(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
                if any(c.strip() for c in cells):
                    rows.append(cells)
            out.append((sheet.name, rows))
        return out

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = [_cell_str(c) for c in row]
            if any(c.strip() for c in cells):
                rows.append(cells)
        out.append((ws.title, rows))
    return out


def xlsx_to_blocks(data: bytes, ext: str = ".xlsx") -> list[Block]:
    blocks: list[Block] = []
    for title, rows in _read_sheets(data, ext):
        if not rows:
            continue
        blocks.append(Block(type="heading", level=2, text=f"工作表：{title}"))
        blocks.append(Block(type="table", text=to_md_table(rows), rows=rows))
    return blocks


def csv_to_blocks(data: bytes) -> list[Block]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [[c.strip() for c in r] for r in reader]
    rows = [r for r in rows if any(c for c in r)]
    if rows:
        return [Block(type="table", text=to_md_table(rows), rows=rows)]
    return []
