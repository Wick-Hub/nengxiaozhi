import http from 'node:http';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { runQa, runQaStream, getStore, resetStore } from '../qa.js';
import { loadSession } from '../store/chatlog.js';
import { listDocs, updateDocStatus, deleteDoc, ingestUploaded } from '../store/admin.js';
import { syncKnowledgeBase } from '../sync.js';
import { KTYPE_NAME, STATUS_NAME } from '../store/mysql.js';
import { llmAvailable } from '../llm/client.js';
import { STORE_MODE } from '../config.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, 'public');
const PORT = +(process.env.WEB_PORT || 3000);
const HOST = process.env.WEB_HOST || '127.0.0.1';
const ROLES = ['访客', '本科生', '研究生', '教职工', '离退休人员'];

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

function sendJson(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    let tooBig = false;
    req.on('data', chunk => {
      data += chunk;
      if (data.length > 20 * 1024 * 1024) { tooBig = true; req.destroy(); }
    });
    req.on('end', () => {
      if (tooBig) return reject(new Error('请求体过大'));
      if (!data) return resolve({});
      try { resolve(JSON.parse(data)); }
      catch { reject(new Error('JSON 解析失败')); }
    });
    req.on('error', reject);
  });
}

function serveStatic(req, res, pathname) {
  let rel = pathname === '/' ? 'index.html' : pathname.slice(1);
  const filePath = path.normalize(path.join(PUBLIC_DIR, rel));
  if (!filePath.startsWith(PUBLIC_DIR)) { res.writeHead(403); res.end(); return; }
  fs.readFile(filePath, (err, buf) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('404 Not Found');
      return;
    }
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
    res.end(buf);
  });
}

function parseChatBody(body) {
  const question = String(body.question || '').trim();
  if (!question) return { error: '问题不能为空' };
  if (question.length > 500) return { error: '问题过长（≤500字）' };
  const role = body.role ? String(body.role).trim() : undefined;
  if (role && !ROLES.includes(role)) return { error: '未知角色' };
  const sessionId = body.sessionId ? String(body.sessionId).trim() : undefined;
  if (sessionId && !/^[A-Za-z0-9_-]{1,40}$/.test(sessionId)) return { error: '会话ID格式非法' };
  return { question, role, sessionId };
}

async function handleChat(req, res) {
  let body;
  try { body = await readBody(req); }
  catch (e) { return sendJson(res, 400, { ok: false, error: e.message }); }

  const parsed = parseChatBody(body);
  if (parsed.error) return sendJson(res, 400, { ok: false, error: parsed.error });
  if (!llmAvailable()) return sendJson(res, 503, { ok: false, error: '未配置 LLM_API_KEY（见 kb-engine/.env）' });

  const { question, role, sessionId } = parsed;
  try {
    const { results, gen, saved } = await runQa({
      question, role, sessionId, topK: 5, mode: 'hybrid',
      save: STORE_MODE === 'mysql',
    });
    if (!results.length) {
      return sendJson(res, 200, { ok: true, data: { sessionId, msgId: null, answer: null, refused: true, confidence: 0.15, citations: [], retrieved: 0, model: null, latencyMs: 0 } });
    }
    sendJson(res, 200, {
      ok: true,
      data: {
        sessionId: saved?.sessionId ?? sessionId,
        msgId: saved?.msgId ?? null,
        answer: gen.answer,
        refused: gen.refused,
        confidence: gen.confidence,
        citations: gen.citations.map((c, i) => ({ n: i + 1, ...c })),
        retrieved: results.length,
        model: gen.model,
        latencyMs: gen.latencyMs,
      },
    });
  } catch (e) {
    console.error('[chat]', e);
    sendJson(res, 500, { ok: false, error: '问答处理失败：' + e.message });
  }
}

function sse(res, obj) {
  res.write('data: ' + JSON.stringify(obj) + '\n\n');
}

// 流式问答：SSE 逐 token 推送 delta，结束发 done（含引用/落库信息）
async function handleChatStream(req, res) {
  let body;
  try { body = await readBody(req); }
  catch (e) { return sendJson(res, 400, { ok: false, error: e.message }); }

  const parsed = parseChatBody(body);
  if (parsed.error) return sendJson(res, 400, { ok: false, error: parsed.error });
  if (!llmAvailable()) return sendJson(res, 503, { ok: false, error: '未配置 LLM_API_KEY（见 kb-engine/.env）' });

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });

  let closed = false;
  res.on('close', () => { closed = true; });

  const { question, role, sessionId } = parsed;
  try {
    const { results, gen, saved } = await runQaStream({
      question, role, sessionId, topK: 5, mode: 'hybrid',
      save: STORE_MODE === 'mysql',
      onDelta: text => { if (!closed) sse(res, { type: 'delta', text }); },
    });
    if (closed) return;
    if (!results.length || !gen) {
      sse(res, { type: 'done', answer: null, refused: true, confidence: 0.15, citations: [], retrieved: 0, sessionId: sessionId ?? null, msgId: null, model: null, latencyMs: 0 });
    } else {
      sse(res, {
        type: 'done',
        answer: gen.answer,
        refused: gen.refused,
        confidence: gen.confidence,
        citations: gen.citations.map((c, i) => ({ n: i + 1, ...c })),
        retrieved: results.length,
        sessionId: saved?.sessionId ?? sessionId,
        msgId: saved?.msgId ?? null,
        model: gen.model,
        latencyMs: gen.latencyMs,
      });
    }
  } catch (e) {
    console.error('[chat-stream]', e);
    if (!closed) sse(res, { type: 'error', error: e.message });
  } finally {
    if (!closed) res.end();
  }
}

async function handleHistory(req, res, sessionId) {
  try {
    const data = await loadSession(sessionId);
    if (!data) return sendJson(res, 404, { ok: false, error: '会话不存在' });
    sendJson(res, 200, { ok: true, data });
  } catch (e) {
    console.error('[history]', e);
    sendJson(res, 500, { ok: false, error: '读取历史失败：' + e.message });
  }
}

async function handleHealth(res) {
  try {
    const store = await getStore();
    sendJson(res, 200, {
      ok: true,
      data: {
        llm: llmAvailable(),
        store: STORE_MODE,
        chunks: store.chunks.length,
        docs: store.docs.length,
        roles: ROLES,
      },
    });
  } catch (e) {
    sendJson(res, 500, { ok: false, error: e.message });
  }
}

async function handleListDocs(res) {
  try {
    const docs = await listDocs();
    sendJson(res, 200, { ok: true, data: docs });
  } catch (e) {
    console.error('[docs]', e);
    sendJson(res, 500, { ok: false, error: '读取文档列表失败：' + e.message });
  }
}

async function handleUploadDoc(req, res) {
  let body;
  try { body = await readBody(req); }
  catch (e) { return sendJson(res, 400, { ok: false, error: e.message }); }

  const filename = String(body.filename || '').trim();
  const dept = String(body.dept || '').trim();
  const ktype = String(body.ktype || '').trim();
  const title = body.title ? String(body.title).trim() : '';
  const status = Number.isInteger(body.status) ? body.status : 0;
  const visibleRoles = Array.isArray(body.visibleRoles) && body.visibleRoles.length ? body.visibleRoles : ['全部'];

  if (!filename) return sendJson(res, 400, { ok: false, error: '缺少文件名' });
  if (!dept) return sendJson(res, 400, { ok: false, error: '缺少部门' });
  if (!ktype) return sendJson(res, 400, { ok: false, error: '缺少知识类型' });
  if (!body.data) return sendJson(res, 400, { ok: false, error: '缺少文件内容' });

  let fileBuf;
  try { fileBuf = Buffer.from(body.data, 'base64'); }
  catch { return sendJson(res, 400, { ok: false, error: '文件内容 base64 解码失败' }); }
  if (!fileBuf.length) return sendJson(res, 400, { ok: false, error: '文件内容为空' });

  const r = await ingestUploaded({ fileBuf, filename, dept, ktype, status, visibleRoles, title });
  if (!r.ok) return sendJson(res, 400, { ok: false, error: r.error });
  resetStore();
  sendJson(res, 200, { ok: true, data: r });
}

async function handleUpdateDoc(req, res, docId) {
  let body;
  try { body = await readBody(req); }
  catch (e) { return sendJson(res, 400, { ok: false, error: e.message }); }
  if (!Number.isInteger(body.status) || body.status < 0 || body.status > 3) {
    return sendJson(res, 400, { ok: false, error: '状态非法（0草稿/1待审核/2已发布/3已下线）' });
  }
  try {
    const r = await updateDocStatus(+docId, body.status);
    if (!r.ok) return sendJson(res, 404, { ok: false, error: r.error });
    resetStore();
    sendJson(res, 200, { ok: true, data: r });
  } catch (e) {
    console.error('[doc-patch]', e);
    sendJson(res, 500, { ok: false, error: '更新失败：' + e.message });
  }
}

async function handleDeleteDoc(req, res, docId) {
  try {
    const r = await deleteDoc(+docId);
    resetStore();
    sendJson(res, 200, { ok: true, data: r });
  } catch (e) {
    console.error('[doc-delete]', e);
    sendJson(res, 500, { ok: false, error: '删除失败：' + e.message });
  }
}

async function handleSync(req, res) {
  try {
    const summary = await syncKnowledgeBase();
    resetStore();
    sendJson(res, 200, { ok: true, data: summary });
  } catch (e) {
    console.error('[sync]', e);
    sendJson(res, 500, { ok: false, error: '增量同步失败：' + e.message });
  }
}

async function handleMeta(res) {
  try {
    const docs = await listDocs();
    sendJson(res, 200, {
      ok: true,
      data: {
        depts: [...new Set(docs.map(d => d.dept))],
        ktypes: Object.values(KTYPE_NAME),
        statuses: STATUS_NAME.map((label, i) => ({ value: i, label })),
        roles: ['全部', ...ROLES],
      },
    });
  } catch (e) {
    sendJson(res, 500, { ok: false, error: e.message });
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const p = url.pathname;

  if (req.method === 'POST' && p === '/api/chat') return handleChat(req, res);
  if (req.method === 'POST' && p === '/api/chat/stream') return handleChatStream(req, res);
  if (req.method === 'GET' && p === '/api/health') return handleHealth(res);
  if (req.method === 'GET' && p === '/api/docs') return handleListDocs(res);
  if (req.method === 'POST' && p === '/api/docs') return handleUploadDoc(req, res);
  if (req.method === 'POST' && p === '/api/sync') return handleSync(req, res);
  if (req.method === 'GET' && p === '/api/meta') return handleMeta(res);
  const dm = p.match(/^\/api\/docs\/(\d+)$/);
  if (dm && req.method === 'PATCH') return handleUpdateDoc(req, res, dm[1]);
  if (dm && req.method === 'DELETE') return handleDeleteDoc(req, res, dm[1]);
  const m = p.match(/^\/api\/sessions\/([A-Za-z0-9_-]{1,40})\/messages$/);
  if (req.method === 'GET' && m) return handleHistory(req, res, m[1]);
  if (req.method === 'GET' && !p.startsWith('/api/')) {
    if (p === '/admin' || p === '/admin/') return serveStatic(req, res, '/admin.html');
    return serveStatic(req, res, p);
  }
  sendJson(res, 404, { ok: false, error: 'Not Found' });
});

server.listen(PORT, HOST, async () => {
  console.log(`能小智 Web 服务已启动：http://${HOST}:${PORT}`);
  console.log('正在加载知识库与嵌入模型（首次约需数秒）…');
  try {
    const store = await getStore();
    console.log(`知识库就绪：${store.docs.length} 份文档 / ${store.chunks.length} 条切片 ｜ LLM：${llmAvailable() ? '已配置' : '未配置（见 .env）'}`);
  } catch (e) {
    console.error('知识库加载失败：', e);
  }
});
