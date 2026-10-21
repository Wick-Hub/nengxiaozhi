import { loadStore } from './store/store.js';
import { embed } from './embed/embedder.js';
import { hybridSearch } from './retrieve/retriever.js';
import { rerankCandidates } from './retrieve/rerank.js';
import { generateAnswer, generateAnswerStream } from './llm/generator.js';
import { saveQa, loadSession, newTraceId } from './store/chatlog.js';
import { RERANK } from './config.js';

let storePromise = null;
export function getStore() {
  if (!storePromise) storePromise = loadStore();
  return storePromise;
}

// 知识库增删改后调用，使缓存的 store 失效、下次检索重新加载
export function resetStore() {
  storePromise = null;
}

// 读取会话最近若干轮消息作为多轮上下文（ai → assistant，喂给大模型）
async function getHistory(sessionId, limit = 8) {
  if (!sessionId) return [];
  try {
    const data = await loadSession(sessionId);
    if (!data) return [];
    return data.slice(-limit).map(m => ({
      role: m.role === 'ai' ? 'assistant' : 'user',
      content: m.content,
    }));
  } catch {
    return [];
  }
}

// 检索：加载/复用 store，向量化问题，混合召回（可选精排）
export async function retrieve(question, role, { topK = 5, mode = 'hybrid' } = {}) {
  const store = await getStore();
  const qEmb = (await embed([question]))[0];
  const recallK = RERANK.enabled ? Math.max(RERANK.topN, topK) : topK;
  let results = hybridSearch(store, qEmb, question, { role, topK: recallK, mode });
  if (RERANK.enabled && results.length > topK) {
    results = await rerankCandidates(question, results, topK);
  }
  return { store, results };
}

// 生成 + 落库（基于已有检索结果，带多轮历史）
export async function answer({ question, role, results, sessionId, save = true }) {
  if (!results.length) return { gen: null, saved: null };
  const history = await getHistory(sessionId);
  const gen = await generateAnswer(question, results, { role, history });
  const saved = save ? await saveQa({ question, role, gen, sessionId, traceId: newTraceId() }) : null;
  return { gen, saved };
}

export async function answerStream({ question, role, results, sessionId, save = true, onDelta }) {
  if (!results.length) return { gen: null, saved: null };
  const history = await getHistory(sessionId);
  const gen = await generateAnswerStream(question, results, { role, history, onDelta });
  const saved = save ? await saveQa({ question, role, gen, sessionId, traceId: newTraceId() }) : null;
  return { gen, saved };
}

// 一次完整问答（Web 端非流式使用）
export async function runQa({ question, role, sessionId, topK = 5, mode = 'hybrid', save = true }) {
  const { store, results } = await retrieve(question, role, { topK, mode });
  const { gen, saved } = await answer({ question, role, results, sessionId, save });
  return { store, results, gen, saved };
}

// 一次完整问答（流式：检索 → 逐 token 生成 → 落库）
export async function runQaStream({ question, role, sessionId, topK = 5, mode = 'hybrid', save = true, onDelta }) {
  const { store, results } = await retrieve(question, role, { topK, mode });
  const { gen, saved } = await answerStream({ question, role, results, sessionId, save, onDelta });
  return { store, results, gen, saved };
}
