import fs from 'node:fs';
import path from 'node:path';
import { KB_ROOT, STORE_PATH, STORE_MODE, DB, EMBED_MODEL } from './config.js';
import { parseFile, SUPPORTED } from './parse/index.js';
import { blocksToChunks } from './chunk/chunker.js';
import { embed } from './embed/embedder.js';
import { saveStore } from './store/store.js';

function* walk(dir, depth = 0) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith('.') || entry.name.startsWith('00-')) continue;
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walk(p, depth + 1);
    } else if (entry.isFile() && depth >= 1) {
      yield p;
    }
  }
}

const rel = p => path.relative(KB_ROOT, p).split(path.sep).join('/');

async function main() {
  const t0 = Date.now();
  if (!fs.existsSync(KB_ROOT)) {
    console.error(`知识目录不存在：${KB_ROOT}`);
    process.exit(1);
  }

  const files = [...walk(KB_ROOT)].filter(f =>
    SUPPORTED.includes(path.extname(f).toLowerCase()) && !path.basename(f).startsWith('~$')
  );
  console.log(`扫描知识目录：${KB_ROOT}`);
  console.log(`发现 ${files.length} 个知识文件\n`);

  const docs = [];
  const chunks = [];
  const skipped = [];

  for (const f of files) {
    try {
      const { meta, blocks } = await parseFile(f);
      if (meta.status === '草稿') {
        skipped.push({ file: rel(f), reason: '草稿状态（待审核，未入库）' });
        continue;
      }
      const docChunks = blocksToChunks(blocks, meta, f);
      if (!docChunks.length) {
        skipped.push({ file: rel(f), reason: '解析后无有效内容' });
        continue;
      }
      const docId = `D${String(docs.length + 1).padStart(3, '0')}`;
      for (const c of docChunks) {
        c.docId = docId;
        c.chunkId = `${docId}-C${String(c.seqNo).padStart(3, '0')}`;
      }
      docs.push({
        docId,
        file: rel(f),
        title: meta.title || path.basename(f, path.extname(f)),
        dept: docChunks[0].dept,
        ktype: docChunks[0].ktype,
        status: meta.status || '已审核',
        updated: meta.updated || '',
        source: meta.source || '',
        chunkCount: docChunks.length,
      });
      chunks.push(...docChunks);
      console.log(`  ✓ ${rel(f)} → ${docChunks.length} 个切片`);
    } catch (e) {
      skipped.push({ file: rel(f), reason: `解析失败：${e.message}` });
    }
  }

  if (!chunks.length) {
    console.error('没有可入库的知识切片');
    process.exit(1);
  }

  console.log(`\n开始向量化（${chunks.length} 个切片，模型 ${EMBED_MODEL}）`);
  const embeddings = await embed(chunks.map(c => c.embedText));
  chunks.forEach((c, i) => { c.embedding = embeddings[i]; });

  await saveStore({
    version: 1,
    model: EMBED_MODEL,
    createdAt: new Date().toISOString(),
    docs,
    chunks,
  });

  const byDept = {};
  const byType = {};
  for (const c of chunks) {
    byDept[c.dept] = (byDept[c.dept] || 0) + 1;
    byType[c.blockType] = (byType[c.blockType] || 0) + 1;
  }
  const sizeMB = (fs.statSync(STORE_PATH).size / 1024 / 1024).toFixed(1);
  const storageLine = STORE_MODE === 'mysql'
    ? `  存储：MySQL ${DB.host}:${DB.port}/${DB.database}（生产表 kb_doc / kb_chunk / kb_chunk_vec_proto）`
    : `  存储：${STORE_PATH}（${sizeMB} MB）`;

  console.log(`\n✅ 知识库构建完成`);
  console.log(`  文档 ${docs.length} 篇 ｜ 切片 ${chunks.length} 条 ｜ 跳过 ${skipped.length} 个文件`);
  console.log(`  类型分布：${Object.entries(byType).map(([k, v]) => `${k} ${v}`).join(' · ')}`);
  console.log(`  部门分布：${Object.entries(byDept).map(([k, v]) => `${k} ${v}`).join(' · ')}`);
  console.log(storageLine);
  console.log(`  耗时：${((Date.now() - t0) / 1000).toFixed(1)}s`);
  if (skipped.length) {
    console.log(`  跳过明细：`);
    for (const s of skipped) console.log(`    - ${s.file}：${s.reason}`);
  }
}

main().catch(e => {
  console.error('构建失败：', e);
  process.exit(1);
});
