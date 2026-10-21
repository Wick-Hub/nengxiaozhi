import path from 'node:path';
import { KB_ROOT, KTYPE_DIRS, CHUNK_MAX_CHARS, CHUNK_CARRY, CHUNK_MIN_CHARS } from '../config.js';

function parseRoles(v) {
  if (!v) return ['全部'];
  const arr = String(v).replace(/[[\]]/g, '').split(/[,，、]/).map(s => s.trim()).filter(Boolean);
  return arr.length ? arr : ['全部'];
}

function inferFromPath(filePath) {
  const rel = path.relative(KB_ROOT, filePath).split(path.sep);
  let dept = '';
  let ktype = '';
  if (rel[0] && /^\d+-/.test(rel[0])) dept = rel[0].replace(/^\d+-/, '');
  if (KTYPE_DIRS.includes(rel[1] || '')) ktype = rel[1];
  return { dept, ktype };
}

export function blocksToChunks(blocks, docMeta, filePath) {
  const { dept: dDept, ktype: dKtype } = inferFromPath(filePath);
  const dept = docMeta.dept || dDept || '未分类';
  const ktype = docMeta.ktype || dKtype || '未分类';
  const visibleRoles = parseRoles(docMeta.visible_roles);
  const title = docMeta.title || path.basename(filePath, path.extname(filePath));

  const chunks = [];
  let stack = [];
  let cur = [];

  const emit = (text, blockType) => {
    const t = text.trim();
    if (!t) return;
    const titlePath = stack.filter(Boolean).join(' > ');
    chunks.push({ titlePath, blockType, text: t });
  };
  const flush = () => {
    if (cur.length) { emit(cur.join('\n'), 'para'); cur = []; }
  };

  for (const b of blocks) {
    if (b.type === 'heading') {
      flush();
      stack[b.level - 1] = b.text;
      stack.length = b.level;
    } else if (b.type === 'table') {
      flush();
      emit(b.text, 'table');
    } else if (b.type === 'qa') {
      flush();
      emit(b.text, 'faq');
    } else if (b.type === 'image') {
      cur.push(`[图片] ${b.text}`);
    } else if (b.type === 'para') {
      cur.push(b.text);
      if (cur.join('\n').length > CHUNK_MAX_CHARS) {
        const text = cur.join('\n');
        emit(text, 'para');
        cur = [text.slice(-CHUNK_CARRY)];
      }
    }
  }
  flush();

  if (chunks.length > 1) {
    const last = chunks[chunks.length - 1];
    if (last.text.length < CHUNK_MIN_CHARS) {
      chunks[chunks.length - 2].text += '\n' + last.text;
      chunks.pop();
    }
  }

  return chunks.map((c, i) => ({
    chunkId: '',
    docId: '',
    seqNo: i + 1,
    charCount: c.text.length,
    dept,
    ktype,
    visibleRoles,
    titlePath: c.titlePath,
    blockType: c.blockType,
    text: c.text,
    embedText: c.titlePath ? `${c.titlePath}\n${c.text}` : c.text,
  }));
}
