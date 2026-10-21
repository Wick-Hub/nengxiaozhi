import { chat, chatStream } from './client.js';

const SYSTEM_PROMPT = `你是「能小智」校园 AI 助手，为学校师生解答校园事务问题。严格遵守：
1. 只能依据用户消息中给出的知识资料回答，不得使用资料外的信息，不得编造数字、政策或流程；
2. 引用来源用 [编号] 标注（编号对应资料块开头的序号），可多处引用；
3. 资料不足以回答时，只回复："抱歉，知识库中未找到相关信息，建议联系相关部门或转人工咨询。" 不要硬答；
4. 用简体中文，简洁分点，控制在 300 字以内；
5. 涉及个人账号、密码、缴费等敏感操作，提醒通过学校官方渠道办理。`;

function buildContext(results) {
  return results.map((r, i) => {
    const c = r.chunk;
    const head = `[${i + 1}] ${c.dept}/${c.ktype}${c.titlePath ? ' · ' + c.titlePath : ''}`;
    return `${head}\n${c.text}`;
  }).join('\n\n');
}

function buildMessages(question, results, { role, history } = {}) {
  const messages = [{ role: 'system', content: SYSTEM_PROMPT }];
  if (history && history.length) messages.push(...history);
  messages.push({
    role: 'user',
    content: `知识资料：\n${buildContext(results)}\n\n问题：${question}${role ? `\n（提问者身份：${role}，只能依据其可见资料回答）` : ''}`,
  });
  return messages;
}

export function parseCitations(content, results) {
  const used = new Set();
  for (const m of content.matchAll(/\[(\d{1,2})\]/g)) {
    const n = +m[1];
    if (n >= 1 && n <= results.length) used.add(n - 1);
  }
  return [...used].sort((a, b) => a - b).map(i => ({
    chunkId: results[i].chunk.chunkId,
    docId: parseInt(String(results[i].chunk.docId).replace(/\D/g, ''), 10) || 0,
    similarity: +results[i].cos.toFixed(4),
    titlePath: results[i].chunk.titlePath,
    dept: results[i].chunk.dept,
    ktype: results[i].chunk.ktype,
  }));
}

function toResult(resp, results) {
  const citations = parseCitations(resp.content, results);
  const refused = /未找到相关信息|无法回答|没有相关资料/.test(resp.content);
  const confidence = refused ? 0.15 : +(results[0]?.cos ?? 0).toFixed(4);
  return {
    answer: resp.content.trim(),
    citations,
    refused,
    confidence,
    usage: resp.usage,
    latencyMs: resp.latencyMs,
    firstTokenMs: resp.firstTokenMs,
    model: resp.model,
  };
}

export async function generateAnswer(question, results, { role, history } = {}) {
  const messages = buildMessages(question, results, { role, history });
  const resp = await chat(messages);
  return toResult(resp, results);
}

export async function generateAnswerStream(question, results, { role, history, onDelta } = {}) {
  const messages = buildMessages(question, results, { role, history });
  const resp = await chatStream(messages, { onDelta });
  return toResult(resp, results);
}
