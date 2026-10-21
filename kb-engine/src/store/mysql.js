import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import mysql from 'mysql2/promise';
import { KB_ROOT, DB } from '../config.js';

// 原型中文知识类型 → 生产枚举(schema.sql kb_doc.ktype)
export const KTYPE_MAP = { '政策文件': 'policy', '办事指南': 'guide', '常见问题': 'faq', '服务事项': 'item', '组织机构': 'org' };
export const KTYPE_NAME = { policy: '政策文件', guide: '办事指南', faq: '常见问题', item: '服务事项', org: '组织机构' };
export const STATUS_MAP = { '草稿': 0, '待审核': 1, '已审核': 2, '已发布': 2, '已下线': 3 };
export const STATUS_NAME = ['草稿', '待审核', '已发布', '已下线'];

let pool;

function getPool() {
  if (!pool) {
    pool = mysql.createPool({
      host: DB.host, port: DB.port, user: DB.user, password: DB.password,
      database: DB.database, connectionLimit: 4, charset: 'utf8mb4',
    });
  }
  return pool;
}

// 连接池的空闲连接会让 Node 事件循环保持存活,导致 CLI 完成任务后进程不退出;
// 每次操作结束必须显式 end()
async function closePool() {
  if (pool) {
    const p = pool;
    pool = null;
    await p.end();
  }
}

function wrapDbError(e) {
  if (['ECONNREFUSED', 'ER_ACCESS_DENIED_ERROR', 'ER_BAD_DB_ERROR', 'ETIMEDOUT'].includes(e.code)) {
    return new Error(
      `无法连接 MySQL（${DB.host}:${DB.port} / ${DB.database}）：${e.message}\n` +
      `请先启动数据库：cd database && docker compose up -d\n` +
      `或临时退回 JSON 存储：KB_STORE=json npm run ask -- "问题"`
    );
  }
  return e;
}

export async function ensureOrg(conn, deptName, sortNo) {
  const [rows] = await conn.execute('SELECT org_id FROM sys_org WHERE org_code = ?', [deptName]);
  if (rows.length) return rows[0].org_id;
  const [res] = await conn.execute(
    'INSERT INTO sys_org (parent_id, org_code, org_name, org_path, sort_no) VALUES (0, ?, ?, ?, ?)',
    [deptName, deptName, '', sortNo]
  );
  await conn.execute('UPDATE sys_org SET org_path = ? WHERE org_id = ?', [`/${res.insertId}/`, res.insertId]);
  return res.insertId;
}

export async function ensureCategory(conn, deptId, ktypeRaw) {
  const ktype = KTYPE_MAP[ktypeRaw] || ktypeRaw;
  const [rows] = await conn.execute(
    'SELECT category_id FROM kb_category WHERE dept_id = ? AND ktype = ?', [deptId, ktype]
  );
  if (rows.length) return rows[0].category_id;
  const [res] = await conn.execute(
    'INSERT INTO kb_category (dept_id, ktype, category_name) VALUES (?, ?, ?)',
    [deptId, ktype, KTYPE_NAME[ktype] || ktypeRaw]
  );
  return res.insertId;
}

export async function saveStoreMysql(store) {
  const conn = await getPool().getConnection().catch(e => { throw wrapDbError(e); });
  try {
    // TRUNCATE 隐式提交且重置自增,使 doc_id 从 1 重新编号,
    // 保持 chunk_id「D001-C001」与 doc_id 的编号对齐(与原型编号规则一致)
    await conn.query('TRUNCATE TABLE kb_chunk_vec_proto');
    await conn.query('TRUNCATE TABLE kb_chunk');
    await conn.query('TRUNCATE TABLE kb_doc');

    const orgMap = new Map();
    let sort = 0;
    for (const dept of new Set(store.docs.map(d => d.dept))) {
      orgMap.set(dept, await ensureOrg(conn, dept, sort++));
    }
    const catMap = new Map();
    for (const doc of store.docs) {
      const ktype = KTYPE_MAP[doc.ktype] || doc.ktype;
      const key = `${doc.dept}::${ktype}`;
      if (!catMap.has(key)) catMap.set(key, await ensureCategory(conn, orgMap.get(doc.dept), doc.ktype));
    }

    const rowIdByDocId = new Map();
    for (const doc of store.docs) {
      const ktype = KTYPE_MAP[doc.ktype] || doc.ktype;
      const buf = fs.readFileSync(path.join(KB_ROOT, doc.file));
      const roles = [...new Set(store.chunks.filter(c => c.docId === doc.docId).flatMap(c => c.visibleRoles || []))];
      const [res] = await conn.execute(
        `INSERT INTO kb_doc
         (doc_title, category_id, dept_id, ktype, file_format, file_key, file_size, file_hash,
          chunk_count, visible_roles, status, version_no, source_type, published_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'import', ?)`,
        [
          doc.title, catMap.get(`${doc.dept}::${ktype}`), orgMap.get(doc.dept), ktype,
          path.extname(doc.file).slice(1).toLowerCase(), doc.file, buf.length,
          crypto.createHash('sha256').update(buf).digest('hex'),
          doc.chunkCount, roles.join(','), STATUS_MAP[doc.status] ?? 2,
          doc.updated || null,
        ]
      );
      rowIdByDocId.set(doc.docId, res.insertId);
    }

    const chunkRows = [];
    const vecRows = [];
    for (const c of store.chunks) {
      chunkRows.push([
        c.chunkId, rowIdByDocId.get(c.docId), c.seqNo, c.blockType, c.titlePath || '',
        c.text, c.charCount, (c.visibleRoles || ['全部']).join(','), 2, 1,
      ]);
      vecRows.push([c.chunkId, JSON.stringify(c.embedding), c.embedding.length, store.model]);
    }
    if (chunkRows.length) {
      await conn.query(
        `INSERT INTO kb_chunk
         (chunk_id, doc_id, seq_no, block_type, title_path, content, char_count, visible_roles, status, vec_synced)
         VALUES ?`, [chunkRows]
      );
      await conn.query(
        `INSERT INTO kb_chunk_vec_proto (chunk_id, embedding, dim, model) VALUES ?`, [vecRows]
      );
    }
    return { docs: store.docs.length, chunks: store.chunks.length };
  } finally {
    conn.release();
    await closePool();
  }
}

export async function loadStoreMysql() {
  try {
    const [docRows] = await getPool().query(
      `SELECT d.doc_id, d.doc_title, d.file_key, d.ktype, d.chunk_count, d.status,
              DATE_FORMAT(d.published_at, '%Y-%m-%d') AS updated, o.org_name AS dept
       FROM kb_doc d JOIN sys_org o ON o.org_id = d.dept_id
       WHERE d.status = 2
       ORDER BY d.doc_id`
    );
    const [chunkRows] = await getPool().query(
      `SELECT c.chunk_id, c.doc_id, c.seq_no, c.block_type, c.title_path, c.content,
              c.char_count, c.visible_roles, o.org_name AS dept, d.ktype AS doc_ktype,
              v.embedding, v.model AS vec_model
       FROM kb_chunk c
       JOIN kb_doc d ON d.doc_id = c.doc_id
       JOIN sys_org o ON o.org_id = d.dept_id
       JOIN kb_chunk_vec_proto v ON v.chunk_id = c.chunk_id
       WHERE d.status = 2
       ORDER BY c.doc_id, c.seq_no`
    );
    if (!docRows.length) {
      throw new Error(`MySQL（${DB.database}）中无知识数据，请先运行：npm run ingest`);
    }
    const docIdByRow = new Map(docRows.map(r => [r.doc_id, 'D' + String(r.doc_id).padStart(3, '0')]));
    return {
      version: 1,
      model: chunkRows.length ? chunkRows[0].vec_model : '',
      createdAt: new Date().toISOString(),
      docs: docRows.map(r => ({
        docId: docIdByRow.get(r.doc_id),
        file: r.file_key,
        title: r.doc_title,
        dept: r.dept,
        ktype: KTYPE_NAME[r.ktype] || r.ktype,
        status: STATUS_NAME[r.status] ?? '',
        updated: r.updated || '',
        source: '',
        chunkCount: r.chunk_count,
      })),
      chunks: chunkRows.map(r => {
        const titlePath = r.title_path || '';
        const emb = typeof r.embedding === 'string' ? JSON.parse(r.embedding) : r.embedding;
        return {
          chunkId: r.chunk_id,
          docId: docIdByRow.get(r.doc_id),
          seqNo: r.seq_no,
          charCount: r.char_count,
          dept: r.dept,
          ktype: KTYPE_NAME[r.doc_ktype] || r.doc_ktype,
          visibleRoles: String(r.visible_roles || '全部').split(','),
          titlePath,
          blockType: r.block_type,
          text: r.content,
          embedText: titlePath ? `${titlePath}\n${r.content}` : r.content,
          embedding: emb,
        };
      }),
    };
  } catch (e) {
    throw wrapDbError(e);
  } finally {
    await closePool();
  }
}
