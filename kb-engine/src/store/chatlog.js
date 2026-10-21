import crypto from 'node:crypto';
import mysql from 'mysql2/promise';
import { DB } from '../config.js';

async function withConn(fn) {
  const conn = await mysql.createConnection({
    host: DB.host, port: DB.port, user: DB.user, password: DB.password,
    database: DB.database, charset: 'utf8mb4',
  });
  try {
    return await fn(conn);
  } finally {
    await conn.end();
  }
}

export async function ensureSeed() {
  return withConn(async (conn) => {
    let [rows] = await conn.query("SELECT agent_id FROM agent WHERE agent_code = 'campus-default'");
    let agentId = rows[0]?.agent_id;
    if (!agentId) {
      const [r] = await conn.query(
        "INSERT INTO agent (agent_code, agent_name, model_code, temperature, visible_roles) VALUES ('campus-default', '能小智·校园百事通', 'default', 0.30, '全部')"
      );
      agentId = r.insertId;
    }
    [rows] = await conn.query("SELECT channel_id FROM channel WHERE channel_code = 'portal-web'");
    let channelId = rows[0]?.channel_id;
    if (!channelId) {
      const [r] = await conn.query(
        "INSERT INTO channel (channel_code, channel_name, default_agent_id, need_auth, status) VALUES ('portal-web', '门户Web', ?, 0, 1)",
        [agentId]
      );
      channelId = r.insertId;
    }
    return { agentId, channelId };
  });
}

export function newTraceId() {
  return 'T' + Date.now().toString(36).toUpperCase() + crypto.randomBytes(3).toString('hex').toUpperCase();
}

export function newSessionId() {
  return 'S' + Date.now().toString(36).toUpperCase() + crypto.randomBytes(2).toString('hex').toUpperCase();
}

// 读取某会话的消息历史（含每条 AI 消息的引用来源），用于 Web 端恢复会话
export async function loadSession(sessionId) {
  return withConn(async (conn) => {
    const [msgs] = await conn.query(
      `SELECT m.msg_id, m.msg_role, m.content, m.confidence, m.created_at
       FROM chat_message m
       WHERE m.session_id = ? ORDER BY m.msg_id`,
      [sessionId]
    );
    if (!msgs.length) return null;

    const [cites] = await conn.query(
      `SELECT c.msg_id, c.chunk_id, c.similarity,
              k.title_path, k.block_type, d.doc_title, d.ktype, o.org_name AS dept
       FROM answer_citation c
       JOIN kb_chunk k ON k.chunk_id = c.chunk_id
       JOIN kb_doc d ON d.doc_id = k.doc_id
       LEFT JOIN sys_org o ON o.org_id = d.dept_id
       WHERE c.msg_id IN (?)`,
      [msgs.map(m => m.msg_id)]
    );
    const citeMap = new Map();
    for (const c of cites) {
      if (!citeMap.has(c.msg_id)) citeMap.set(c.msg_id, []);
      citeMap.get(c.msg_id).push(c);
    }

    return msgs.map(m => ({
      msgId: m.msg_id,
      role: m.msg_role === 1 ? 'user' : 'ai',
      content: m.content,
      confidence: m.confidence,
      createdTime: m.created_at,
      citations: (citeMap.get(m.msg_id) || []).map(c => ({
        chunkId: c.chunk_id,
        similarity: c.similarity,
        titlePath: c.title_path,
        blockType: c.block_type,
        docTitle: c.doc_title,
        ktype: c.ktype,
        dept: c.dept,
      })),
    }));
  });
}

// 一次问答全链路落库：会话 + 用户消息 + AI消息 + 引用 + token计量；拒答时进未识别问题池
export async function saveQa({ question, role, gen, sessionId, traceId }) {
  return withConn(async (conn) => {
    const { agentId, channelId } = await ensureSeed();
    const sid = sessionId || newSessionId();

    await conn.query(
      'INSERT IGNORE INTO chat_session (session_id, channel_id, agent_id, role_snapshot) VALUES (?,?,?,?)',
      [sid, channelId, agentId, role || '访客']
    );
    await conn.query(
      'INSERT INTO chat_message (session_id, msg_role, content, trace_id) VALUES (?,1,?,?)',
      [sid, question, traceId]
    );
    const [m] = await conn.query(
      'INSERT INTO chat_message (session_id, msg_role, content, trace_id, confidence) VALUES (?,2,?,?,?)',
      [sid, gen.answer, traceId, gen.confidence]
    );
    const msgId = m.insertId;

    for (const c of gen.citations) {
      await conn.query(
        'INSERT INTO answer_citation (msg_id, chunk_id, doc_id, similarity) VALUES (?,?,?,?)',
        [msgId, c.chunkId, c.docId, c.similarity]
      );
    }
    await conn.query(
      `INSERT INTO token_usage
       (trace_id, channel_id, agent_id, model_code, prompt_tokens, completion_tokens, total_tokens, latency_ms, first_token_ms, call_state)
       VALUES (?,?,?,?,?,?,?,?,?,?)`,
      [traceId, channelId, agentId, gen.model,
        gen.usage.prompt_tokens, gen.usage.completion_tokens, gen.usage.total_tokens,
        gen.latencyMs, gen.firstTokenMs, 1]
    );
    if (gen.refused) {
      await conn.query(
        'INSERT INTO unrecognized_question (session_id, question) VALUES (?,?)',
        [sid, question]
      );
    }
    return { sessionId: sid, msgId };
  });
}
