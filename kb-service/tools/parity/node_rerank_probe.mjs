// 精排一致性探针（Node 侧，任务 1.6 验收工具）。
// 读 kb-engine 的 store.json，取前 N 条切片作为候选，用 transformers.js 逐条打分，
// 输出每条候选的分数与精排后的 chunkId 序列。需在 kb-engine 目录下运行。

import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

console.log = (...args) => process.stderr.write(args.join(' ') + '\n');

const HERE = import.meta.dirname;
const KB_ENGINE = process.env.KB_ENGINE || path.resolve(HERE, '../../../kb-engine');
const STORE = process.env.KB_STORE_JSON || path.join(KB_ENGINE, 'data/store.json');
const HF_ENDPOINT = process.env.HF_ENDPOINT || 'https://hf-mirror.com';
const RERANK_MODEL = process.env.RERANK_MODEL || 'onnx-community/bge-reranker-v2-m3-ONNX';

const { AutoTokenizer, AutoModelForSequenceClassification, env } = await import(
  pathToFileURL(
    // Node 构建入口（web 构建不含 onnxruntime-node 绑定）
    path.join(KB_ENGINE, 'node_modules/@huggingface/transformers/dist/transformers.node.mjs')
  ).href
);
env.allowLocalModels = false;
env.remoteHost = HF_ENDPOINT;

const tokenizer = await AutoTokenizer.from_pretrained(RERANK_MODEL);
const model = await AutoModelForSequenceClassification.from_pretrained(RERANK_MODEL, {
  dtype: 'q8',
});

const cfg = JSON.parse(fs.readFileSync(path.join(HERE, 'rerank_cases.json'), 'utf8'));
const store = JSON.parse(fs.readFileSync(STORE, 'utf8'));
const all = store.chunks;

const out = [];
for (const c of cfg.cases) {
  const cands = all.slice(0, c.first);
  const scored = [];
  for (const chunk of cands) {
    const inputs = await tokenizer(c.query, {
      text_pair: chunk.text,
      truncation: true,
      max_length: 512,
    });
    const res = await model(inputs);
    scored.push({ chunkId: chunk.chunkId, score: res.logits.data[0] });
  }
  // 诊断：输出首条候选的 token ids，用于区分 tokenizer 差异与算子差异
  const probe = await tokenizer(c.query, {
    text_pair: cands[0].text,
    truncation: true,
    max_length: 512,
  });
  const ids = Array.from(probe.input_ids.data, (v) => Number(v));
  const ranked = [...scored].sort((a, b) => b.score - a.score).slice(0, c.topK);
  out.push({
    query: c.query,
    first: c.first,
    topK: c.topK,
    before: cands.map((x) => x.chunkId),
    after: ranked.map((x) => x.chunkId),
    scores: scored,
    firstIds: ids,
  });
}
process.stdout.write(JSON.stringify(out));
