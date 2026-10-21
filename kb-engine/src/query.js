import { isVisible } from './retrieve/retriever.js';
import { llmAvailable } from './llm/client.js';
import { retrieve, answer, getStore } from './qa.js';
import { STORE_MODE } from './config.js';

function parseArgs(argv) {
  const opts = { role: undefined, top: 5, mode: 'hybrid', llm: true, save: true, session: undefined };
  let question = '';
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--role') opts.role = argv[++i];
    else if (a === '--top') opts.top = +argv[++i] || 5;
    else if (a === '--mode') opts.mode = argv[++i];
    else if (a === '--no-llm') opts.llm = false;
    else if (a === '--no-save') opts.save = false;
    else if (a === '--session') opts.session = argv[++i];
    else if (!question) question = a;
  }
  return { question, opts };
}

async function main() {
  const { question, opts } = parseArgs(process.argv.slice(2));
  if (!question) {
    console.log('用法：node src/query.js "问题" [--role 访客|本科生|研究生|教职工|离退休人员] [--top 5] [--mode hybrid|vector|bm25]');
    console.log('      [--no-llm 只检索] [--no-save 不落库] [--session 会话ID 继续同一会话]');
    process.exit(0);
  }

  const t0 = Date.now();
  const { store, results } = await retrieve(question, opts.role, { topK: opts.top, mode: opts.mode });

  console.log(`问题：${question}`);
  if (opts.role) {
    const visible = store.chunks.filter(c => isVisible(c, opts.role)).length;
    console.log(`角色：${opts.role}（权限过滤后可见 ${visible}/${store.chunks.length} 条切片）`);
  }

  if (!results.length) {
    console.log('\n未召回任何知识（当前角色下可能无可见内容，或问题超出知识范围）。');
    return;
  }

  for (const [i, r] of results.entries()) {
    const c = r.chunk;
    const doc = store.docs.find(d => d.docId === c.docId);
    console.log(`\n─ [${i + 1}] RRF ${r.rrf.toFixed(4)} ｜ 向量 ${r.cos.toFixed(3)} (#${r.vRank}) ｜ BM25 ${r.bm25.toFixed(2)}${r.bRank ? ` (#${r.bRank})` : ''}`);
    console.log(`  来源：${doc ? doc.file : c.docId} ｜ ${c.dept}/${c.ktype} ｜ ${doc ? doc.status : ''}`);
    if (c.titlePath) console.log(`  路径：${c.titlePath}`);
    console.log(`  切片：${c.chunkId} · ${c.blockType} · ${c.charCount}字`);
    const text = c.text.length > 280 ? c.text.slice(0, 280) + '…' : c.text;
    console.log('  ' + text.split('\n').join('\n  '));
  }

  console.log(`\n共召回 ${results.length} 条 ｜ 检索耗时 ${((Date.now() - t0) / 1000).toFixed(2)}s`);

  if (!opts.llm) {
    console.log('（--no-llm：仅检索，未生成回答）');
    return;
  }
  if (!llmAvailable()) {
    console.log('\n未配置 LLM_API_KEY，跳过回答生成。参照 .env.example 写入 kb-engine/.env 后重试。');
    return;
  }

  console.log('\n━━━━━━━━━━━━━━━━━━━━ 生成回答 ━━━━━━━━━━━━━━━━━━━━');
  const { gen, saved } = await answer({ question, role: opts.role, results, sessionId: opts.session, save: opts.save });
  console.log(`\n【回答】${gen.model} ｜ ${(gen.latencyMs / 1000).toFixed(1)}s ｜ 置信度 ${gen.confidence.toFixed(4)}${gen.refused ? ' ｜ 已拒答' : ''}\n`);
  console.log(gen.answer);

  if (gen.citations.length) {
    console.log('\n【引用】');
    for (const [i, c] of gen.citations.entries()) {
      console.log(`[${i + 1}] ${c.chunkId} ｜ ${c.dept}/${c.ktype}${c.titlePath ? ' · ' + c.titlePath : ''} ｜ 相似度 ${c.similarity.toFixed(4)}`);
    }
  }

  if (!opts.save) return;
  if (STORE_MODE !== 'mysql') {
    console.log('\n（当前为 json 存储模式，问答不落库）');
    return;
  }
  console.log(`\n【落库】会话 ${saved.sessionId} ｜ AI消息#${saved.msgId} ｜ 引用 ${gen.citations.length} 条 ｜ token ${gen.usage.total_tokens}${gen.refused ? ' ｜ 已入未识别问题池' : ''}`);
  if (!opts.session) console.log(`（后续提问加 --session ${saved.sessionId} 可继续同一会话）`);
}

main().catch(e => {
  console.error('查询失败：', e);
  process.exit(1);
});
