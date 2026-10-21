// 中文 BM25：CJK 按 bigram 切分，ASCII 按词切分，规避原生分词依赖
export function tokenize(text) {
  const t = String(text).toLowerCase();
  const tokens = [];
  const ascii = t.match(/[a-z0-9]+/g) || [];
  tokens.push(...ascii);
  const cjk = t.match(/[\u4e00-\u9fff]+/g) || [];
  for (const seg of cjk) {
    for (let i = 0; i < seg.length - 1; i++) {
      tokens.push(seg.slice(i, i + 2));
    }
  }
  return tokens;
}

const K1 = 1.5;
const B = 0.75;

export class BM25 {
  constructor(docs) {
    // docs: [{ id, text }]
    this.entries = docs.map(d => ({ id: d.id, tokens: tokenize(d.text) }));
    this.docCount = this.entries.length || 1;
    this.avgLen = this.entries.reduce((s, e) => s + e.tokens.length, 0) / this.docCount;
    this.df = new Map();
    for (const e of this.entries) {
      for (const term of new Set(e.tokens)) {
        this.df.set(term, (this.df.get(term) || 0) + 1);
      }
    }
  }

  search(query, topK) {
    const qt = tokenize(query);
    const scored = [];
    for (const e of this.entries) {
      const tf = new Map();
      for (const t of e.tokens) tf.set(t, (tf.get(t) || 0) + 1);
      let s = 0;
      for (const t of qt) {
        const f = tf.get(t);
        if (!f) continue;
        const df = this.df.get(t) || 0;
        const idf = Math.log(1 + (this.docCount - df + 0.5) / (df + 0.5));
        s += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * e.tokens.length / this.avgLen));
      }
      if (s > 0) scored.push({ id: e.id, score: s });
    }
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, topK);
  }
}
