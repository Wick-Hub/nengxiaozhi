import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const HERE = import.meta.dirname;
const KB_ENGINE = process.env.KB_ENGINE || path.resolve(HERE, '../../../kb-engine');
const KB_ROOT = process.env.KB_ROOT || path.resolve(KB_ENGINE, '../knowledge-base');

const { parseFile } = await import(pathToFileURL(path.join(KB_ENGINE, 'src/parse/index.js')).href);
const { blocksToChunks } = await import(
  pathToFileURL(path.join(KB_ENGINE, 'src/chunk/chunker.js')).href
);

const SUPPORTED = new Set(['.md', '.txt', '.docx', '.pdf', '.xlsx', '.xls', '.csv', '.json']);

const files = [];
const walk = (d) => {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    if (e.name.startsWith('.')) continue;
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p);
    else files.push(p);
  }
};
walk(KB_ROOT);

const out = [];
for (const f of files.sort()) {
  const ext = path.extname(f).toLowerCase();
  if (!SUPPORTED.has(ext)) continue;
  const rel = path.relative(KB_ROOT, f).split(path.sep).join('/');
  if (rel.startsWith('00-') || !rel.includes('/')) continue;
  const { meta, blocks } = await parseFile(f);
  const chunks = blocksToChunks(blocks, meta, f);
  out.push({
    rel,
    blocks: blocks.length,
    chunks: chunks.length,
    meta,
    blockTypes: blocks.map((b) => b.type),
    chunkChars: chunks.map((c) => c.charCount),
  });
}
process.stdout.write(JSON.stringify(out, null, 2));
