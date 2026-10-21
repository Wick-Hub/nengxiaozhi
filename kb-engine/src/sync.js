import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import mysql from 'mysql2/promise';
import { fileURLToPath } from 'node:url';
import { KB_ROOT, DB } from './config.js';
import { SUPPORTED } from './parse/index.js';
import { parseDoc, writeDoc, deleteDocOnConn } from './store/admin.js';
import { STATUS_MAP } from './store/mysql.js';

function* walk(dir, depth = 0) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith('.') || entry.name.startsWith('00-')) continue;
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(p, depth + 1);
    else if (entry.isFile() && depth >= 1) yield p;
  }
}

const rel = p => path.relative(KB_ROOT, p).split(path.sep).join('/');
const sha256 = buf => crypto.createHash('sha256').update(buf).digest('hex');

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

// 增量同步：按文件哈希比对，只重算新增/变化的文档，删除已不存在的文档
export async function syncKnowledgeBase() {
  const files = [...walk(KB_ROOT)].filter(f =>
    SUPPORTED.includes(path.extname(f).toLowerCase()) && !path.basename(f).startsWith('~$')
  );
  const summary = { added: 0, updated: 0, deleted: 0, skipped: 0, errors: [] };

  await withConn(async (conn) => {
    const [rows] = await conn.query("SELECT doc_id, file_key, file_hash FROM kb_doc WHERE file_key NOT LIKE 'uploads/%'");
    const dbByKey = new Map(rows.map(r => [r.file_key, r]));
    const seen = new Set();

    for (const f of files) {
      const key = rel(f);
      seen.add(key);
      const buf = fs.readFileSync(f);
      const hash = sha256(buf);
      const row = dbByKey.get(key);
      if (row && row.file_hash === hash) { summary.skipped++; continue; }
      try {
        const parsed = await parseDoc(f);
        if (!parsed.ok) { summary.errors.push(`${key}：${parsed.error}`); continue; }
        const status = STATUS_MAP[parsed.docMeta.status] ?? 2;
        await writeDoc({
          conn, fileKey: key, fileBuf: buf,
          docMeta: parsed.docMeta, chunks: parsed.chunks, embeddings: parsed.embeddings,
          status, docId: row?.doc_id ?? null,
        });
        if (row) summary.updated++; else summary.added++;
      } catch (e) {
        summary.errors.push(`${key}：${e.message}`);
      }
    }

    for (const [key, row] of dbByKey) {
      if (!seen.has(key)) {
        try {
          await deleteDocOnConn(conn, row.doc_id);
          summary.deleted++;
        } catch (e) {
          summary.errors.push(`${key}：删除失败 ${e.message}`);
        }
      }
    }
  });

  return summary;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const t0 = Date.now();
  syncKnowledgeBase()
    .then(s => {
      console.log(`✅ 增量同步完成：新增 ${s.added} ｜ 更新 ${s.updated} ｜ 删除 ${s.deleted} ｜ 跳过 ${s.skipped} ｜ 耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
      for (const e of s.errors) console.log('  ✗ ' + e);
      process.exit(0);
    })
    .catch(e => { console.error('同步失败：', e); process.exit(1); });
}
