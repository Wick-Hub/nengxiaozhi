// 生成层一致性探针（Node 侧，任务 1.7 验收工具）。
// 置 LLM_MOCK=1 走确定性模拟回答，调用原型的 generateAnswer / parseCitations，
// 输出回答、引用、拒答与置信度。需在 kb-engine 目录下运行。

import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

console.log = (...args) => process.stderr.write(args.join(' ') + '\n');
process.env.LLM_MOCK = '1';

const HERE = import.meta.dirname;
const KB_ENGINE = process.env.KB_ENGINE || path.resolve(HERE, '../../../kb-engine');
const STORE = process.env.KB_STORE_JSON || path.join(KB_ENGINE, 'data/store.json');

const { generateAnswer, parseCitations } = await import(
  pathToFileURL(path.join(KB_ENGINE, 'src/llm/generator.js')).href
);

const cfg = JSON.parse(fs.readFileSync(path.join(HERE, 'generate_cases.json'), 'utf8'));
const store = JSON.parse(fs.readFileSync(STORE, 'utf8'));
const cos = cfg.cos;
const chunks = store.chunks.slice(0, cos.length);
const results = chunks.map((c, i) => ({ chunk: c, cos: cos[i] }));

const cases = [];
for (const c of cfg.cases) {
  const res = await generateAnswer(c.question, results, {
    role: c.role ?? undefined,
    history: c.history ?? undefined,
  });
  cases.push({
    question: c.question,
    role: c.role ?? null,
    answer: res.answer,
    citations: res.citations,
    refused: res.refused,
    confidence: res.confidence,
    usage: res.usage,
  });
}

const citation = {
  answer: cfg.citationAnswer,
  citations: parseCitations(cfg.citationAnswer, results),
};

process.stdout.write(JSON.stringify({ cases, citation }));
