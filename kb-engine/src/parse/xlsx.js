import * as XLSX from 'xlsx';
import { toMdTable } from './markdown.js';

function sheetToRows(sheet) {
  return XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '' })
    .map(r => r.map(c => String(c)))
    .filter(r => r.some(c => c.trim() !== ''));
}

export function xlsxToBlocks(buffer) {
  const wb = XLSX.read(buffer, { type: 'buffer' });
  const blocks = [];
  for (const name of wb.SheetNames) {
    const rows = sheetToRows(wb.Sheets[name]);
    if (!rows.length) continue;
    blocks.push({ type: 'heading', level: 2, text: `工作表：${name}` });
    blocks.push({ type: 'table', rows, text: toMdTable(rows) });
  }
  return blocks;
}

export function csvToBlocks(buffer) {
  const wb = XLSX.read(buffer, { type: 'buffer' });
  const blocks = [];
  for (const name of wb.SheetNames) {
    const rows = sheetToRows(wb.Sheets[name]);
    if (rows.length) blocks.push({ type: 'table', rows, text: toMdTable(rows) });
  }
  return blocks;
}
