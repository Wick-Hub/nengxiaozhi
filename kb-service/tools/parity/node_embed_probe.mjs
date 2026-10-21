// 向量一致性探针（Node 侧，任务 1.4 验收工具）。
// 用 kb-engine 的 @huggingface/transformers 对固定文本集编码，输出 JSON 向量数组。
// 需在 kb-engine 目录下运行，以便命中 node_modules 内的模型缓存。

import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// 库（transformers.js）会往 stdout 打印加载日志，这里统一转到 stderr，
// 保证 stdout 只承载最终 JSON，便于 Python 侧直接解析。
console.log = (...args) => process.stderr.write(args.join(' ') + '\n');

const HERE = import.meta.dirname;
const KB_ENGINE = process.env.KB_ENGINE || path.resolve(HERE, '../../../kb-engine');

const { embed } = await import(
  pathToFileURL(path.join(KB_ENGINE, 'src/embed/embedder.js')).href
);

const texts = JSON.parse(fs.readFileSync(path.join(HERE, 'embed_texts.json'), 'utf8'));
const vecs = await embed(texts, 8);
process.stdout.write(JSON.stringify(vecs));
