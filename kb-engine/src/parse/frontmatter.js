export function parseFrontmatter(raw) {
  const meta = {};
  let body = raw;
  const m = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (m) {
    for (const line of m[1].split(/\r?\n/)) {
      const kv = line.match(/^([A-Za-z_\u4e00-\u9fff-]+)\s*:\s*(.*)$/);
      if (kv) meta[kv[1].trim()] = kv[2].trim();
    }
    body = m[2];
  }
  return { meta, body };
}
