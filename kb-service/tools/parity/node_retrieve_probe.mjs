// 检索一致性探针（Node 侧，任务 1.5 验收工具）。
// 读 kb-engine 的 store.json，对固定用例调用原型的 hybridSearch，输出 JSON。
// 需在 kb-engine 目录下运行。

import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

console.log = (...args) => process.stderr.write(args.join(' ') + '\n');

const HERE = import.meta.dirname;
const KB_ENGINE = process.env.KB_ENGINE || path.resolve(HERE, '../../../kb-engine');
const STORE = process.env.KB_STORE_JSON || path.join(KB_ENGINE, 'data/store.json');

const { hybridSearch } = await import(
  pathToFileURL(path.join(KB_ENGINE, 'src/retrieve/retriever.js')).href
);
const { embed } = await import(
  pathToFileURL(path.join(KB_ENGINE, 'src/embed/embedder.js')).href
);

const cfg = JSON.parse(fs.readFileSync(path.join(HERE, 'retrieve_cases.json'), 'utf8'));
const store = JSON.parse(fs.readFileSync(STORE, 'utf8'));

const out = [];
for (const c of cfg.cases) {
  const [qEmb] = await embed([c.question]);
  const res = hybridSearch(store, qEmb, c.question, {
    role: c.role ?? undefined,
    topK: c.topK,
    mode: c.mode,
  });
  out.push({
    question: c.question,
    role: c.role ?? null,
    mode: c.mode,
    hits: res.map((r) => ({
      chunkId: r.chunk.chunkId,
      cos: r.cos,
      bm25: r.bm25,
      rrf: r.rrf,
      vRank: r.vRank,
      bRank: r.bRank,
    })),
  });
}
process.stdout.write(JSON.stringify(out));
