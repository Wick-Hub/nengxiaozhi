import { BM25 } from './bm25.js';

export function isVisible(chunk, role) {
  if (!role) return true;
  const vr = chunk.visibleRoles || ['全部'];
  return vr.includes('全部') || vr.includes(role);
}

function dot(a, b) {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

export function hybridSearch(store, queryEmb, question, { role, topK = 5, mode = 'hybrid' } = {}) {
  const pool = store.chunks.filter(c => Array.isArray(c.embedding) && isVisible(c, role));
  if (!pool.length) return [];

  const scored = pool.map(c => ({ chunk: c, cos: dot(queryEmb, c.embedding) }));
  scored.sort((a, b) => b.cos - a.cos);
  scored.forEach((s, i) => { s.vRank = i + 1; });

  const bm25 = new BM25(pool.map(c => ({ id: c.chunkId, text: c.embedText })));
  const bm25Hits = bm25.search(question, pool.length);
  const bm25Map = new Map(bm25Hits.map((h, i) => [h.id, { score: h.score, rank: i + 1 }]));

  const K = 60;
  for (const s of scored) {
    const b = bm25Map.get(s.chunk.chunkId);
    s.bm25 = b ? b.score : 0;
    s.bRank = b ? b.rank : null;
    s.rrf = 1 / (K + s.vRank) + (b ? 1 / (K + b.rank) : 0);
  }

  const sorters = {
    hybrid: (a, b) => b.rrf - a.rrf,
    vector: (a, b) => b.cos - a.cos,
    bm25: (a, b) => b.bm25 - a.bm25,
  };
  return scored.sort(sorters[mode] || sorters.hybrid).slice(0, topK);
}
