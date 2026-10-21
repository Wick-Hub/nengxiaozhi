import { RERANK, HF_ENDPOINT } from '../config.js';

let rerankerPromise = null;
let loadFailed = false;

async function getReranker() {
  if (loadFailed) return null;
  if (!rerankerPromise) {
    rerankerPromise = (async () => {
      const proxy = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.ALL_PROXY;
      if (proxy) {
        const { setGlobalDispatcher, ProxyAgent } = await import('undici');
        setGlobalDispatcher(new ProxyAgent(proxy));
      }
      const { AutoTokenizer, AutoModelForSequenceClassification, env } = await import('@huggingface/transformers');
      env.allowLocalModels = false;
      env.remoteHost = HF_ENDPOINT;
      console.log(`加载精排模型 ${RERANK.model}（${RERANK.dtype}，首次运行需下载约 280MB，请耐心等待）…`);
      const tokenizer = await AutoTokenizer.from_pretrained(RERANK.model);
      const model = await AutoModelForSequenceClassification.from_pretrained(RERANK.model, { dtype: RERANK.dtype });
      return { tokenizer, model };
    })();
    rerankerPromise.catch(() => { loadFailed = true; });
  }
  return rerankerPromise;
}

// 交叉编码精排：对召回候选逐条打分，取 topK。模型加载失败时降级返回原序。
export async function rerankCandidates(question, items, topK) {
  if (!RERANK.enabled || items.length <= topK) return items;
  let rr;
  try {
    rr = await getReranker();
  } catch (e) {
    console.warn(`精排模型不可用，降级为不精排：${e.message}`);
    loadFailed = true;
    return items.slice(0, topK);
  }
  if (!rr) return items.slice(0, topK);

  const scored = [];
  for (const it of items) {
    const inputs = await rr.tokenizer(question, { text_pair: it.chunk.text, truncation: true, max_length: 512 });
    const out = await rr.model(inputs);
    scored.push({ it, score: out.logits.data[0] });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, topK).map(s => s.it);
}
