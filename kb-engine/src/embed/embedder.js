import { EMBED_MODEL, HF_ENDPOINT, EMBED_DTYPE } from '../config.js';

let extractorPromise = null;

async function getExtractor() {
  if (!extractorPromise) {
    extractorPromise = (async () => {
      const proxy = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.ALL_PROXY;
      if (proxy) {
        const { setGlobalDispatcher, ProxyAgent } = await import('undici');
        setGlobalDispatcher(new ProxyAgent(proxy));
        console.log(`已启用代理：${proxy}`);
      }
      const { pipeline, env } = await import('@huggingface/transformers');
      env.allowLocalModels = false;
      env.remoteHost = HF_ENDPOINT;
      console.log(`加载嵌入模型 ${EMBED_MODEL}（下载源 ${HF_ENDPOINT}，首次运行需下载约 90MB，请耐心等待）…`);
      return pipeline('feature-extraction', EMBED_MODEL, { dtype: EMBED_DTYPE });
    })();
  }
  return extractorPromise;
}

export async function embed(texts, batchSize = 16) {
  const extractor = await getExtractor();
  const out = [];
  for (let i = 0; i < texts.length; i += batchSize) {
    const batch = texts.slice(i, i + batchSize);
    const res = await extractor(batch, { pooling: 'mean', normalize: true });
    out.push(...res.tolist());
    if (texts.length > batchSize) {
      console.log(`  向量化进度 ${Math.min(i + batchSize, texts.length)}/${texts.length}`);
    }
  }
  return out;
}
