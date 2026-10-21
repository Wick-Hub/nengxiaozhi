/* 文档预览与对比（任务 1.11 / 关联 M-K-08）
 *
 * 左栏「原文」、右栏「切片」，同一份关键词做高亮，用于核对切片是否忠实于原文。
 * 任务 1.10 追加：图片型文档与内嵌图片的缩略图条（`/api/docs/{id}/image?key=`），
 * 切片卡片里也会带出它引用的原图，便于「图 ↔ 切片」对照。
 *
 * 前后端同源约定：本文件的 findRanges / parseTerms 与后端
 * app/services/doc_preview.py 的 find_ranges / parse_terms **逐字符等价**
 * （忽略大小写、非重叠步进、每词最多 50 段），由 tools/verify_preview.py 交叉校验。
 */
(() => {
  'use strict';

  const STATUS_CLS = ['s0', 's1', 's2', 's3'];
  const MAX_RANGES_PER_TERM = 50;

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function fmtSize(n) {
    if (n == null) return '-';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function authFetch(url, opts) {
    return (globalThis.NXZAuth ? globalThis.NXZAuth.fetch : fetch)(url, opts);
  }

  function authOn() {
    const me = globalThis.NXZAuth && globalThis.NXZAuth.me;
    return !!(me && me.authEnabled);
  }

  async function api(url) {
    const resp = await authFetch(url, { headers: { Accept: 'application/json' } });
    let json = null;
    try { json = await resp.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!json) throw new Error('HTTP ' + resp.status);
    if (!json.ok) throw new Error(json.error || json.detail || ('HTTP ' + resp.status));
    return json.data;
  }

  // 审核流（任务 2.7 / M-K-05）用的写请求：PATCH /api/docs/{id} {status}
  async function send(url, method, body) {
    const resp = await authFetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body || {}),
    });
    let json = null;
    try { json = await resp.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!json) throw new Error('HTTP ' + resp.status);
    if (!json.ok) throw new Error(json.error || json.detail || ('HTTP ' + resp.status));
    return json.data;
  }

  // ---------------------------------------------------------------- 检索
  function parseTerms(query) {
    const seen = new Set();
    const out = [];
    for (const t of String(query == null ? '' : query).trim().split(/\s+/)) {
      if (!t || seen.has(t)) continue;
      seen.add(t);
      out.push(t);
    }
    return out;
  }

  function findRanges(haystack, term) {
    const text = String(haystack == null ? '' : haystack);
    const needle = String(term == null ? '' : term);
    if (!text || !needle) return [];
    const lowH = text.toLowerCase();
    const lowT = needle.toLowerCase();
    const out = [];
    const step = Math.max(1, needle.length);
    let start = 0;
    while (out.length < MAX_RANGES_PER_TERM) {
      const i = lowH.indexOf(lowT, start);
      if (i < 0) break;
      out.push([i, i + needle.length]);
      start = i + step;
    }
    return out;
  }

  function rangesFor(text, terms) {
    const all = [];
    for (const t of terms) all.push(...findRanges(text, t));
    all.sort((a, b) => a[0] - b[0]);
    return all;
  }

  function highlight(text, ranges) {
    const s = String(text == null ? '' : text);
    if (!ranges || !ranges.length) return esc(s);
    let out = '';
    let pos = 0;
    for (const [a, b] of ranges) {
      if (a < pos || a >= b || b > s.length) continue; // 重叠 / 越界防御
      out += esc(s.slice(pos, a)) + '<mark class="hit">' + esc(s.slice(a, b)) + '</mark>';
      pos = b;
    }
    return out + esc(s.slice(pos));
  }

  // ---------------------------------------------------------------- 图片（任务 1.10）
  function imageUrl(docId, key) {
    return `/api/docs/${encodeURIComponent(docId)}/image?key=${encodeURIComponent(key)}`;
  }

  // 媒体鉴权（任务 2.9）：AUTH_ENABLED=true 时 <img>/<a> 无法携带 Bearer，
  // 图片改由 NXZAuth.authorizeImages 取 blob 回填，链接点击走 openAuthed。
  function imgAttr(url) {
    return authOn() ? `data-auth-src="${esc(url)}"` : `src="${esc(url)}"`;
  }

  function authorizeMedia() {
    if (globalThis.NXZAuth && authOn()) globalThis.NXZAuth.authorizeImages(document);
  }

  async function openAuthed(url) {
    const win = window.open('', '_blank');
    try {
      const resp = await authFetch(url);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const objectUrl = URL.createObjectURL(await resp.blob());
      if (win) win.location.href = objectUrl;
      else window.location.href = objectUrl;
    } catch (e) {
      if (win) win.close();
      alert('打开失败：' + e.message);
    }
  }

  function imageCard(docId, img) {
    const role = img.role === 'original' ? '原件' : '内嵌图';
    const dims = img.width && img.height ? `${img.width}×${img.height}` : '';
    const refs = (img.chunkSeqs || []).length
      ? `<div class="thumb-refs">切片 ${img.chunkSeqs.map(s => '#' + s).join(' ')}</div>`
      : '';
    if (!img.exists) {
      return `<figure class="thumb broken" title="${esc(img.error || '图片缺失')}">
        <div class="thumb-box"><span>图片缺失</span></div>
        <figcaption><span class="tag mono">${esc(img.name || '')}</span></figcaption>
      </figure>`;
    }
    const url = imageUrl(docId, img.key);
    return `<figure class="thumb" title="${esc(img.name || '')}">
      <a class="thumb-box" href="${esc(url)}" target="_blank" rel="noopener" data-auth-href="${esc(url)}">
        <img ${imgAttr(url)} alt="${esc(img.name || '')}" loading="lazy">
      </a>
      <figcaption>
        <span class="tag">${esc(role)}</span>
        <span class="mono">${esc(dims)}</span>
        <span class="mono">${esc(fmtSize(img.size))}</span>
      </figcaption>
      ${refs}
    </figure>`;
  }

  function renderImages(d) {
    const strip = $('images-strip');
    const list = d.images || [];
    const docId = (d.doc || {}).docId;
    strip.hidden = !list.length;
    if (!list.length) return;
    $('images-count').textContent = `共 ${list.length} 张`;
    $('images-meta').textContent = d.isImage ? '图片型文档（文字来自 OCR）' : '文档内嵌图片';
    $('images-body').innerHTML = list.map(img => imageCard(docId, img)).join('');
    authorizeMedia();
  }

  // ---------------------------------------------------------------- 渲染
  function renderHead(doc, original) {
    $('doc-title').textContent = doc.title || '(未命名)';
    const tags = [
      ['#' + (doc.docId == null ? '' : doc.docId), 'mono'],
      [doc.dept, ''],
      [doc.ktype, ''],
      [doc.statusName, ''],
      [doc.fileFormat ? String(doc.fileFormat).toUpperCase() : '', 'mono'],
      [fmtSize(doc.fileSize), ''],
      [`${doc.chunkCount == null ? 0 : doc.chunkCount} 切片`, ''],
    ];
    if (original) {
      tags.push([original.fileName || '', 'mono']);
      tags.push([original.root === 'uploads' ? '上传件' : '知识目录', '']);
      if (!original.exists) tags.push(['原件缺失', '']);
    }
    $('doc-tags').innerHTML = tags
      .filter(t => t[0])
      .map(([t, cls]) => `<span class="tag ${cls}">${esc(t)}</span>`)
      .join('');

    const openRaw = $('open-raw');
    const dlRaw = $('download-raw');
    const ok = original && original.exists;
    openRaw.hidden = !(ok && original.inline);
    dlRaw.hidden = !ok;
    if (ok) {
      const base = `/api/docs/${encodeURIComponent(doc.docId == null ? '' : doc.docId)}/raw`;
      openRaw.href = base;
      openRaw.dataset.authHref = base;
      dlRaw.href = base + '?download=1';
    }

    // 审核流（任务 2.7 / M-K-05）：待审（草稿/审核中）才给操作按钮，
    // 已发布（2）/已归档（3）不展示，避免重复操作。
    $('audit-msg').textContent = '';
    const pending = doc.status === 0 || doc.status === 1;
    $('audit-approve').hidden = !pending;
    $('audit-reject').hidden = !pending;
  }

  function renderText(d) {
    const meta = $('text-meta');
    const body = $('text-body');
    const tm = d.textMeta || {};
    if (!tm.ok) {
      body.innerHTML = `<div class="empty">${esc(tm.error || '原文不可用')}</div>`;
      meta.textContent = '';
      return;
    }
    const terms = parseTerms(d.query);
    body.innerHTML = highlight(d.text, rangesFor(d.text, terms));
    const src = tm.source === 'raw' ? '原件文本' : '解析文本';
    meta.textContent = `${tm.chars} 字 · ${tm.lines} 行 · ${src}`;
  }

  function renderChunks(d) {
    const body = $('chunk-body');
    const onlyHit = $('only-hit').checked;
    const docId = (d.doc || {}).docId;
    const byKey = {};
    for (const img of d.images || []) byKey[img.key] = img;
    const list = (d.chunks || []).filter(
      c => !onlyHit || (c.matches || []).length || (c.titleMatches || []).length
    );

    if (!list.length) {
      body.innerHTML = `<div class="empty">${
        (d.terms || []).length ? '没有命中的切片' : '该文档暂无切片'
      }</div>`;
    } else {
      body.innerHTML = list
        .map(c => {
          const titleHtml = (c.titleMatches || []).length
            ? highlight(c.titlePath || '', c.titleMatches)
            : esc(c.titlePath || '');
          const titleHit = (c.titleMatches || []).length
            ? '<span class="chunk-hint">标题命中</span>'
            : '';
          // 切片引用的原图（任务 1.10）：小图 + 跳转，便于「图 ↔ 切片」对照
          const keys = c.imageKeys || [];
          const imgs = keys.length
            ? `<div class="chunk-imgs">${keys.map(k => {
                const info = byKey[k] || { key: k, name: k, exists: true };
                const u = imageUrl(docId, k);
                return `<a class="chunk-img" href="${esc(u)}" data-auth-href="${esc(u)}"
                  target="_blank" rel="noopener" title="${esc(info.name || k)}">
                  <img ${imgAttr(u)} alt="${esc(info.name || k)}" loading="lazy">
                </a>`;
              }).join('')}</div>`
            : '';
          return `<div class="chunk${c.matched ? '' : ' dim'}">
            <div class="chunk-head">
              <span class="chunk-seq">#${c.seqNo}</span>
              <span class="chunk-type">${esc(c.blockType || 'para')}</span>
              <span>${c.charCount} 字</span>
              <span class="mono">${esc(c.chunkId || '')}</span>
              ${titleHit}
              ${c.titlePath ? `<div class="chunk-title-path">${titleHtml}</div>` : ''}
            </div>
            ${imgs}
            <div class="chunk-text">${highlight(c.text || '', c.matches || [])}</div>
          </div>`;
        })
        .join('');
    }
    $('chunk-meta').textContent = (d.terms || []).length
      ? `命中 ${d.matchCount} / ${d.total} 条`
      : `共 ${d.total} 条`;
    authorizeMedia();
  }

  let hits = [];
  let hitIdx = -1;

  function refreshHits(d) {
    hits = Array.from(document.querySelectorAll('mark.hit'));
    hitIdx = -1;
    const terms = (d && d.terms) || [];
    if (!terms.length) {
      $('hit-stat').innerHTML = d ? `共 <b>${d.total}</b> 条切片` : '';
      return;
    }
    $('hit-stat').innerHTML =
      `命中 <b>${hits.length}</b> 处 · <b>${d.matchCount}</b>/<b>${d.total}</b> 条切片`;
  }

  function gotoHit(delta) {
    if (!hits.length) return;
    for (const h of hits) h.classList.remove('active');
    hitIdx = (hitIdx + delta + hits.length) % hits.length;
    const el = hits[hitIdx];
    el.classList.add('active');
    if (el.scrollIntoView) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  function renderAll(d) {
    renderHead(d.doc || {}, d.original);
    renderImages(d);
    renderText(d);
    renderChunks(d);
    refreshHits(d);
  }

  // ---------------------------------------------------------------- 审核（任务 2.7 / M-K-05）
  let auditing = false;

  async function audit(status) {
    if (auditing || !state.docId) return;
    auditing = true;
    const msg = $('audit-msg');
    msg.textContent = status === 2 ? '提交中…' : '驳回中…';
    try {
      const d = await send(`/api/docs/${encodeURIComponent(state.docId)}`, 'PATCH', { status });
      const failed = (d && d.failed) || [];
      if (failed.length) throw new Error(failed.join('；'));
      // 重新拉取预览：状态徽标、可检索性与高亮切片都会同步
      await load();
      msg.textContent = status === 2 ? '已审核通过并发布' : '已驳回为草稿';
    } catch (e) {
      msg.textContent = '操作失败：' + e.message;
    } finally {
      auditing = false;
    }
  }

  // ---------------------------------------------------------------- 流程
  const state = { docId: '', query: '' };

  function syncUrl() {
    const p = new URLSearchParams();
    if (state.docId) p.set('doc', state.docId);
    if (state.query) p.set('q', state.query);
    const qs = p.toString();
    history.replaceState(null, '', qs ? `/preview?${qs}` : '/preview');
  }

  async function load() {
    if (!state.docId) { await showPicker(); return; }

    $('picker').hidden = true;
    $('doc-head').hidden = false;
    $('toolbar').hidden = false;
    $('split').hidden = false;
    $('q').value = state.query;

    let d;
    try {
      const url = `/api/docs/${encodeURIComponent(state.docId)}/preview`
        + (state.query ? `?q=${encodeURIComponent(state.query)}` : '');
      d = await api(url);
    } catch (e) {
      $('images-strip').hidden = true;
      $('text-meta').textContent = '';
      $('chunk-meta').textContent = '';
      $('hit-stat').textContent = '';
      // 预览失败时文档本身未确认，审核按钮一并隐藏（避免对不存在/无权限文档误操作）
      $('audit-approve').hidden = true;
      $('audit-reject').hidden = true;
      $('audit-msg').textContent = '';
      $('text-body').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
      $('chunk-body').innerHTML = '';
      return;
    }
    renderAll(d);
  }

  async function showPicker() {
    $('picker').hidden = false;
    $('doc-head').hidden = true;
    $('images-strip').hidden = true;
    $('toolbar').hidden = true;
    $('split').hidden = true;
    try {
      const docs = await api('/api/docs');
      $('picker-count').textContent = `共 ${docs.length} 篇`;
      $('picker-empty').hidden = docs.length > 0;
      $('picker-body').innerHTML = docs
        .map(d => `<tr>
          <td>${esc(d.docId)}</td>
          <td>
            <div class="doc-title" title="${esc(d.title)}">${esc(d.title)}</div>
            <div class="doc-meta">${esc(d.fileKey || d.file || '')}</div>
          </td>
          <td>${esc(d.dept || '')}</td>
          <td>${esc(d.ktype || '')}</td>
          <td><span class="badge ${STATUS_CLS[d.status] || 's0'}">${esc(d.statusName || '')}</span></td>
          <td>${d.chunkCount == null ? 0 : d.chunkCount}</td>
          <td><a class="op-btn" href="/preview?doc=${encodeURIComponent(d.docId)}">预览</a></td>
        </tr>`)
        .join('');
    } catch (e) {
      $('picker-empty').hidden = false;
      $('picker-empty').textContent = '加载失败：' + e.message;
    }
  }

  function init() {
    const params = new URLSearchParams(location.search);
    state.docId = params.get('doc') || '';
    state.query = params.get('q') || '';

    $('search').onclick = () => {
      state.query = $('q').value.trim();
      syncUrl();
      load();
    };
    $('q').addEventListener('keydown', ev => {
      if (ev.key === 'Enter') { ev.preventDefault(); $('search').click(); }
    });
    $('clear').onclick = () => {
      $('q').value = '';
      state.query = '';
      syncUrl();
      load();
    };
    $('only-hit').onchange = () => {
      // 只影响切片列表，无需重新请求
      if (state.docId) load();
    };
    $('prev-hit').onclick = () => gotoHit(-1);
    $('next-hit').onclick = () => gotoHit(1);
    $('audit-approve').onclick = () => audit(2);
    $('audit-reject').onclick = () => audit(0);
    // 媒体鉴权：链接点击改为取 blob 打开；原件下载带 Bearer
    document.addEventListener('click', (ev) => {
      const a = ev.target.closest('[data-auth-href]');
      if (!a || !authOn()) return;
      ev.preventDefault();
      openAuthed(a.dataset.authHref);
    });
    $('download-raw').addEventListener('click', (ev) => {
      if (!authOn() || !globalThis.NXZAuth) return;
      ev.preventDefault();
      const name = ($('doc-title').textContent || 'document') + '';
      globalThis.NXZAuth.download($('download-raw').href, name).catch((e) => alert('下载失败：' + e.message));
    });
    document.addEventListener('keydown', ev => {
      if (!ev.shiftKey) return;
      if (ev.key === 'ArrowDown') { ev.preventDefault(); gotoHit(1); }
      if (ev.key === 'ArrowUp') { ev.preventDefault(); gotoHit(-1); }
    });

    const ready = globalThis.NXZAuth ? globalThis.NXZAuth.ready() : Promise.resolve();
    ready.then(() => {
      if (globalThis.NXZAuth && globalThis.NXZAuth.me.authEnabled && !globalThis.NXZAuth.can('docs.view')) return;
      load();
    });
  }

  // Node 探针（tools/verify_preview.py）只取纯函数，浏览器无副作用
  if (globalThis.__NXZ_EXPORT__) {
    globalThis.__NXZ_PREVIEW__ = {
      esc, findRanges, parseTerms, rangesFor, highlight, imageUrl, imageCard,
    };
  } else {
    init();
  }
})();
