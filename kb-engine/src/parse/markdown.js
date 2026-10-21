function splitRow(line) {
  return line.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
}

function isSeparator(cells) {
  return cells.length > 0 && cells.every(c => /^:?-{2,}:?$/.test(c));
}

export function toMdTable(rows) {
  if (!rows.length) return '';
  const sep = rows[0].map(() => '---');
  return [rows[0], sep, ...rows.slice(1)].map(r => '| ' + r.join(' | ') + ' |').join('\n');
}

export function mdToBlocks(md) {
  const lines = md.split(/\r?\n/);
  const blocks = [];
  let para = [];
  let tableLines = [];

  const flushPara = () => {
    const t = para.join('\n').trim();
    if (t) blocks.push({ type: 'para', text: t });
    para = [];
  };
  const flushTable = () => {
    if (!tableLines.length) return;
    const rows = tableLines.map(splitRow).filter(cells => !isSeparator(cells));
    if (rows.length) blocks.push({ type: 'table', rows, text: toMdTable(rows) });
    tableLines = [];
  };

  for (const line of lines) {
    const t = line.trim();
    if (/^\|.*\|$/.test(t)) {
      if (!tableLines.length) flushPara();
      tableLines.push(t);
      continue;
    }
    flushTable();
    if (!t) { flushPara(); continue; }
    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushPara(); blocks.push({ type: 'heading', level: h[1].length, text: h[2].trim() }); continue; }
    if (/^!\[[^\]]*\]\([^)]*\)/.test(t)) { flushPara(); blocks.push({ type: 'image', text: t }); continue; }
    para.push(t);
  }
  flushTable();
  flushPara();
  return blocks;
}
