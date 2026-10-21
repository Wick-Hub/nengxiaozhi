import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import mysql from 'mysql2/promise';
import { fileURLToPath } from 'node:url';
import { DB, EMBED_MODEL } from '../config.js';
import { parseFile, SUPPORTED } from '../parse/index.js';
import { blocksToChunks } from '../chunk/chunker.js';
import { embed } from '../embed/embedder.js';
import { ensureOrg, ensureCategory, KTYPE_MAP, KTYPE_NAME, STATUS_NAME } from './mysql.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const UPLOAD_DIR = path.resolve(__dirname, '../../uploads');

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

const ktypeName = k => KTYPE_NAME[k] || k;

export async function listDocs() {
  return withConn(async (conn) => {
    const [rows] = await conn.query(
      `SELECT d.doc_id, d.doc_title, d.ktype, d.file_format, d.file_key, d.file_size,
              d.file_hash, d.chunk_count, d.visible_roles, d.status, d.version_no,
              d.source_type, d.published_at, d.created_at, d.updated_at,
              o.org_name AS dept
       FROM kb_doc d JOIN sys_org o ON o.org_id = d.dept_id
       ORDER BY d.doc_id`
    );
    return rows.map(r => ({
      docId: r.doc_id,
      title: r.doc_title,
      dept: r.dept,
      ktype: ktypeName(r.ktype),
      fileFormat: r.file_format,
      fileKey: r.file_key,
      fileSize: r.file_size,
      fileHash: r.file_hash,
      chunkCount: r.chunk_count,
      visibleRoles: String(r.visible_roles || '全部').split(','),
      status: r.status,
      statusName: STATUS_NAME[r.status] ?? String(r.status),
      versionNo: r.version_no,
      sourceType: r.source_type,
      publishedAt: r.published_at,
      createdAt: r.created_at,
      updatedAt: r.updated_at,
    }));
  });
}

export async function updateDocStatus(docId, status) {
  return withConn(async (conn) => {
    const [res] = await conn.execute(
      `UPDATE kb_doc SET status = ?, published_at = IF(? = 2, NOW(), NULL) WHERE doc_id = ?`,
      [status, status, docId]
    );
    if (!res.affectedRows) return { ok: false, error: '文档不存在' };
    return { ok: true, docId, status, statusName: STATUS_NAME[status] };
  });
}

// 删除文档及其切片/向量/引用（需在事务内，供 deleteDoc 与 sync 共用）
export async function deleteDocOnConn(conn, docId) {
  await conn.execute('DELETE FROM answer_citation WHERE doc_id = ?', [docId]);
  await conn.execute(
    'DELETE v FROM kb_chunk_vec_proto v JOIN kb_chunk c ON c.chunk_id = v.chunk_id WHERE c.doc_id = ?',
    [docId]
  );
  await conn.execute('DELETE FROM kb_chunk WHERE doc_id = ?', [docId]);
  const [res] = await conn.execute('DELETE FROM kb_doc WHERE doc_id = ?', [docId]);
  return res.affectedRows > 0;
}

export async function deleteDoc(docId) {
  return withConn(async (conn) => {
    await conn.beginTransaction();
    try {
      const deleted = await deleteDocOnConn(conn, docId);
      await conn.commit();
      return { ok: true, deleted, docId };
    } catch (e) {
      await conn.rollback();
      throw e;
    }
  });
}

// 解析 + 切片 + 向量化（供上传与增量同步共用）
export async function parseDoc(filePath, overrides = {}) {
  const { meta, blocks } = await parseFile(filePath);
  const docMeta = {
    title: overrides.title || meta.title || path.basename(filePath, path.extname(filePath)),
    dept: overrides.dept || meta.dept || '未分类',
    ktype: overrides.ktype || meta.ktype || '未分类',
    visible_roles: Array.isArray(overrides.visibleRoles) && overrides.visibleRoles.length
      ? overrides.visibleRoles.join(',')
      : (meta.visible_roles || '全部'),
    status: meta.status || '',
    source: meta.source || '示例数据（待替换为学校真实文件）',
  };
  const chunks = blocksToChunks(blocks, docMeta, filePath);
  if (!chunks.length) return { ok: false, error: '文件解析后无有效内容' };
  const embeddings = await embed(chunks.map(c => c.embedText));
  return { ok: true, docMeta, chunks, embeddings };
}

// 写入单个文档：docId 为空则自增插入，否则原地更新（删旧切片/向量，保留 doc_id 与 version+1）
export async function writeDoc({ conn, fileKey, fileBuf, docMeta, chunks, embeddings, status, docId = null }) {
  const [orgRows] = await conn.execute('SELECT COALESCE(MAX(sort_no), 0) + 1 AS n FROM sys_org');
  const orgId = await ensureOrg(conn, docMeta.dept, orgRows[0].n);
  const categoryId = await ensureCategory(conn, orgId, docMeta.ktype);
  const ktypeEnum = KTYPE_MAP[docMeta.ktype] || docMeta.ktype;
  const ext = path.extname(fileKey).toLowerCase();
  const hash = crypto.createHash('sha256').update(fileBuf).digest('hex');

  if (docId != null) {
    await conn.execute(
      'DELETE v FROM kb_chunk_vec_proto v JOIN kb_chunk c ON c.chunk_id = v.chunk_id WHERE c.doc_id = ?',
      [docId]
    );
    await conn.execute('DELETE FROM kb_chunk WHERE doc_id = ?', [docId]);
    await conn.execute(
      `UPDATE kb_doc SET doc_title=?, category_id=?, dept_id=?, ktype=?, file_format=?, file_key=?,
        file_size=?, file_hash=?, chunk_count=?, visible_roles=?, status=?, version_no=version_no+1,
        published_at=IF(?=2, NOW(), published_at)
       WHERE doc_id=?`,
      [docMeta.title, categoryId, orgId, ktypeEnum, ext.slice(1).toLowerCase(), fileKey,
        fileBuf.length, hash, chunks.length, docMeta.visible_roles, status, status, docId]
    );
  } else {
    const [res] = await conn.execute(
      `INSERT INTO kb_doc
       (doc_title, category_id, dept_id, ktype, file_format, file_key, file_size, file_hash,
        chunk_count, visible_roles, status, version_no, source_type, published_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'import', IF(? = 2, NOW(), NULL))`,
      [docMeta.title, categoryId, orgId, ktypeEnum, ext.slice(1).toLowerCase(), fileKey,
        fileBuf.length, hash, chunks.length, docMeta.visible_roles, status, status]
    );
    docId = res.insertId;
  }

  const chunkRows = [];
  const vecRows = [];
  for (const c of chunks) {
    const chunkId = `D${String(docId).padStart(3, '0')}-C${String(c.seqNo).padStart(3, '0')}`;
    chunkRows.push([
      chunkId, docId, c.seqNo, c.blockType, c.titlePath || '', c.text, c.charCount,
      docMeta.visible_roles, 2, 1,
    ]);
    vecRows.push([chunkId, JSON.stringify(embeddings[c.seqNo - 1]), embeddings[c.seqNo - 1].length, EMBED_MODEL]);
  }
  await conn.query(
    `INSERT INTO kb_chunk (chunk_id, doc_id, seq_no, block_type, title_path, content, char_count, visible_roles, status, vec_synced) VALUES ?`,
    [chunkRows]
  );
  await conn.query(
    `INSERT INTO kb_chunk_vec_proto (chunk_id, embedding, dim, model) VALUES ?`, [vecRows]
  );
  return { docId, chunkCount: chunks.length };
}

// 上传入库：写 staging 文件 → 解析 → 切片 → 向量化 → 单文档插入（不影响其他文档）
export async function ingestUploaded({ fileBuf, filename, dept, ktype, status = 0, visibleRoles = ['全部'], title }) {
  const ext = path.extname(filename).toLowerCase();
  if (!SUPPORTED.includes(ext)) {
    return { ok: false, error: `不支持的文件类型 ${ext}（支持 ${SUPPORTED.join(' ')}）` };
  }
  if (!KTYPE_MAP[ktype]) {
    return { ok: false, error: `未知知识类型 ${ktype}` };
  }

  fs.mkdirSync(UPLOAD_DIR, { recursive: true });
  const safeName = `${Date.now()}-${path.basename(filename).replace(/[^\w.\u4e00-\u9fff-]/g, '_')}`;
  const stagingPath = path.join(UPLOAD_DIR, safeName);
  fs.writeFileSync(stagingPath, fileBuf);

  try {
    const parsed = await parseDoc(stagingPath, { title, dept, ktype, visibleRoles });
    if (!parsed.ok) return { ok: false, error: parsed.error };

    const result = await withConn(conn =>
      writeDoc({ conn, fileKey: 'uploads/' + safeName, fileBuf, docMeta: parsed.docMeta, chunks: parsed.chunks, embeddings: parsed.embeddings, status })
    );
    return { ok: true, docId: result.docId, chunkCount: result.chunkCount, title: parsed.docMeta.title, dept: parsed.docMeta.dept, ktype: parsed.docMeta.ktype };
  } catch (e) {
    return { ok: false, error: '入库失败：' + e.message };
  } finally {
    fs.rmSync(stagingPath, { force: true });
  }
}
