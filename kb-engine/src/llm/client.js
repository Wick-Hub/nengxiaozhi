import { LLM } from '../config.js';

let dispatcherPromise = null;
async function getDispatcher() {
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.ALL_PROXY;
  if (!proxy) return undefined;
  if (!dispatcherPromise) {
    dispatcherPromise = (async () => {
      const { ProxyAgent } = await import('undici');
      return new ProxyAgent(proxy);
    })();
  }
  return dispatcherPromise;
}

export function llmAvailable() {
  return LLM.mock || !!LLM.apiKey;
}

function mockChat(messages) {
  const user = [...messages].reverse().find(m => m.role === 'user')?.content || '';
  const ids = [...new Set([...user.matchAll(/\[(\d{1,2})\]/g)].map(m => +m[1]))].slice(0, 2);
  const marks = ids.map(i => `[${i}]`).join('');
  const content = /火星|量子|彩票|哪个星球/.test(user)
    ? '抱歉，知识库中未找到相关信息，建议联系相关部门或转人工咨询。'
    : `（模拟回答，未真实调用大模型）根据知识库资料${marks}：这是基于召回切片拼装的占位答案，用于验证生成层全链路。配置 LLM_API_KEY 后将由大模型基于资料生成真实回答。`;
  return {
    content,
    usage: { prompt_tokens: 620, completion_tokens: 46, total_tokens: 666 },
    latencyMs: 1,
    firstTokenMs: null,
    model: 'mock',
  };
}

async function mockChatStream(messages, onDelta) {
  const r = mockChat(messages);
  if (onDelta) {
    for (let i = 0; i < r.content.length; i += 3) {
      onDelta(r.content.slice(i, i + 3));
      await new Promise(res => setTimeout(res, 6));
    }
  }
  return r;
}

// 流式调用：逐 token 回调 onDelta(delta)，返回完整结果（含首 token 延迟与 usage）
export async function chatStream(messages, { model = LLM.model, temperature = LLM.temperature, maxTokens = LLM.maxTokens, onDelta } = {}) {
  if (LLM.mock) return mockChatStream(messages, onDelta);
  if (!LLM.apiKey) {
    throw new Error('未配置 LLM_API_KEY。请写入 kb-engine/.env（参照 .env.example），或设 LLM_MOCK=1 体验模拟回答。');
  }

  const t0 = Date.now();
  const res = await fetch(`${LLM.baseUrl}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${LLM.apiKey}` },
    body: JSON.stringify({ model, messages, temperature, max_tokens: maxTokens, stream: true, stream_options: { include_usage: true } }),
    dispatcher: await getDispatcher(),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`LLM 调用失败 HTTP ${res.status}：${text.slice(0, 300)}`);
  }

  const decoder = new TextDecoder();
  let buf = '';
  let content = '';
  let firstTokenMs = null;
  let usage = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
  let respModel = model;

  for await (const chunk of res.body) {
    buf += decoder.decode(chunk, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') continue;
      let obj;
      try { obj = JSON.parse(payload); } catch { continue; }
      if (obj.model) respModel = obj.model;
      const delta = obj.choices?.[0]?.delta?.content;
      if (delta) {
        if (firstTokenMs == null) firstTokenMs = Date.now() - t0;
        content += delta;
        if (onDelta) onDelta(delta);
      }
      if (obj.usage) usage = obj.usage;
    }
  }

  return {
    content,
    usage: { ...usage, total_tokens: usage.total_tokens ?? usage.prompt_tokens + usage.completion_tokens },
    latencyMs: Date.now() - t0,
    firstTokenMs,
    model: respModel,
  };
}

export async function chat(messages, { model = LLM.model, temperature = LLM.temperature, maxTokens = LLM.maxTokens } = {}) {
  if (LLM.mock) return mockChat(messages);

  if (!LLM.apiKey) {
    throw new Error('未配置 LLM_API_KEY。请写入 kb-engine/.env（参照 .env.example），或设 LLM_MOCK=1 体验模拟回答。');
  }
  const t0 = Date.now();
  const res = await fetch(`${LLM.baseUrl}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${LLM.apiKey}` },
    body: JSON.stringify({ model, messages, temperature, max_tokens: maxTokens }),
    dispatcher: await getDispatcher(),
  });
  const latencyMs = Date.now() - t0;
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`LLM 调用失败 HTTP ${res.status}：${text.slice(0, 300)}`);
  }
  const data = await res.json();
  const usage = data.usage || { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
  return {
    content: data.choices?.[0]?.message?.content ?? '',
    usage: { ...usage, total_tokens: usage.total_tokens ?? usage.prompt_tokens + usage.completion_tokens },
    latencyMs,
    firstTokenMs: null,
    model: data.model || model,
  };
}
