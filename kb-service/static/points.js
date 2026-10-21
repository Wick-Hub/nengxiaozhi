/* 能小智 · 知识点管理（任务 2.8 / M-K-10）
 *
 * 知识点 = 检索切片（kb_chunk），对外编号 D001-C003。
 * 本页承担：按「部门 × 类型 × 块类型 × 关键词」集约化浏览、检索、
 * 增删改、再加工（追加文本 / 删内容 / 插图）与批量导出。
 *
 * 与 admin.js 的约定一致：所有接口都带 /api 前缀（漏掉会 404，
 * 因为 /api 是 include_router 的挂载点，不是静态路径）。
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 20;

  const state = {
    page: 1,
    total: 0,
    editable: true,
    dept: '',
    ktype: '',
    blockType: '',
    q: '',
    selected: new Set(),
    items: [],
    current: null,
  };

  // ---------------------------------------------------------------- 基础工具
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function authFetch(url, options) {
    return (window.NXZAuth ? window.NXZAuth.fetch : fetch)(url, options);
  }

  async function api(url, options) {
    const resp = await authFetch(url, Object.assign({ headers: { Accept: 'application/json' } }, options || {}));
    let json = null;
    try { json = await resp.json(); } catch (_) { /* 非 JSON（导出接口不走这里） */ }
    if (!resp.ok) {
      const msg = (json && (json.detail || json.error)) || ('HTTP ' + resp.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    if (!json) throw new Error('响应不是 JSON（HTTP ' + resp.status + '）');
    if (!json.ok) throw new Error(json.error || json.detail || '操作失败');
    return json.data;
  }

  function msg(id, text, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'pt-msg' + (kind ? ' ' + kind : '');
  }

  function jsonBody(payload) {
    return {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload),
    };
  }

  // ---------------------------------------------------------------- 筛选下拉
  async function loadFilters() {
    let data;
    try {
      data = await api('/api/knowledge/points?size=1');
    } catch (e) {
      msg('pt-note', '加载失败：' + e.message, 'err');
      return;
    }
    const f = data.filters || {};
    fillSelect('pt-dept', f.depts, '全部部门');
    fillSelect('pt-ktype', f.ktypes, '全部类型');
    fillSelect('pt-block', f.blockTypes, '全部块类型');
  }

  function fillSelect(id, values, placeholder) {
    const sel = $(id);
    if (!sel) return;
    const keep = sel.value;
    sel.innerHTML = `<option value="">${esc(placeholder)}</option>` +
      (values || []).map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
    if (keep) sel.value = keep;
  }

  // ---------------------------------------------------------------- 列表
  function buildQuery() {
    const params = new URLSearchParams();
    if (state.dept) params.set('dept', state.dept);
    if (state.ktype) params.set('ktype', state.ktype);
    if (state.blockType) params.set('blockType', state.blockType);
    if (state.q) params.set('q', state.q);
    params.set('page', String(state.page));
    params.set('size', String(PAGE_SIZE));
    return params.toString();
  }

  async function load() {
    msg('pt-note', '加载中…');
    let data;
    try {
      data = await api('/api/knowledge/points?' + buildQuery());
    } catch (e) {
      msg('pt-note', '加载失败：' + e.message, 'err');
      return;
    }
    state.items = data.items || [];
    state.total = data.total || 0;
    state.editable = data.editable !== false;
    state.selected.clear();

    const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
    if (state.page > pages) {
      state.page = pages;
      return load();
    }

    $('pt-count').textContent = `（${state.total} 条）`;
    $('pt-hint').textContent = state.editable
      ? '勾选后可批量导出；点击「编辑」进入再加工'
      : '当前为 JSON 只读模式（未启用 MySQL），仅支持浏览与导出';
    msg('pt-note', state.editable
      ? '数据源：MySQL（kb_chunk）· 修改后立即重算该切片向量并刷新检索索引'
      : '数据源：本地索引（只读）· 如需编辑请在 .env 设置 KB_STORE=mysql 并启动数据库');

    render();
    renderPager(pages);
    syncBatch();
  }

  function render() {
    const body = $('pt-body');
    body.innerHTML = state.items.map((p) => {
      const summary = (p.excerpt || '').replace(/\s+/g, ' ').slice(0, 160);
      return `<tr data-chunk="${esc(p.chunkId)}">
        <td class="chk"><input type="checkbox" data-chk="${esc(p.chunkId)}"></td>
        <td><span class="pt-id">${esc(p.chunkId)}</span><div class="doc-meta">序号 ${esc(p.seqNo)}</div></td>
        <td><div class="doc-title">${esc(p.docTitle || '(未命名)')}</div>
            <div class="doc-meta">${esc(p.docId)} · ${esc(p.dept || '-')} / ${esc(p.ktype || '-')}</div></td>
        <td><span class="pt-block">${esc(p.blockType || '-')}</span></td>
        <td class="pt-path">${esc(p.titlePath || '—')}</td>
        <td>${esc(p.charCount)}</td>
        <td class="pt-summary">${esc(summary)}${(p.excerpt || '').length > 160 ? '…' : ''}</td>
        <td class="pt-ops-cell">
          <button class="op-btn" data-perm="points.edit" data-edit="${esc(p.chunkId)}">编辑</button>
          <button class="op-btn danger" data-perm="points.edit" data-del="${esc(p.chunkId)}">删除</button>
        </td>
      </tr>`;
    }).join('');

    $('pt-empty').hidden = state.items.length > 0;

    body.querySelectorAll('[data-chk]').forEach((box) => {
      box.onchange = () => {
        const id = box.getAttribute('data-chk');
        if (box.checked) state.selected.add(id); else state.selected.delete(id);
        syncBatch();
      };
    });
    body.querySelectorAll('[data-edit]').forEach((btn) => {
      btn.onclick = () => openEditor(btn.getAttribute('data-edit'));
    });
    body.querySelectorAll('[data-del]').forEach((btn) => {
      btn.onclick = () => removePoint(btn.getAttribute('data-del'));
    });
  }

  function renderPager(pages) {
    const pager = $('pt-pager');
    pager.hidden = pages <= 1;
    $('pt-page-info').textContent = `第 ${state.page} / ${pages} 页`;
    $('pt-prev').disabled = state.page <= 1;
    $('pt-next').disabled = state.page >= pages;
  }

  function syncBatch() {
    const bar = $('pt-batch');
    const n = state.selected.size;
    bar.hidden = n === 0;
    $('pt-batch-count').textContent = String(n);
    const all = $('pt-chk-all');
    const boxes = Array.from(document.querySelectorAll('[data-chk]'));
    all.checked = boxes.length > 0 && boxes.every((b) => b.checked);
    all.indeterminate = !all.checked && boxes.some((b) => b.checked);
  }

  // ---------------------------------------------------------------- 导出
  async function exportSelected(fmt) {
    const ids = Array.from(state.selected);
    if (!ids.length) return;
    msg('pt-batch-msg', '导出中…');
    try {
      const resp = await authFetch('/api/knowledge/export', jsonBody({ chunkIds: ids, format: fmt }));
      if (!resp.ok) {
        let detail = 'HTTP ' + resp.status;
        try { const j = await resp.json(); detail = j.detail || j.error || detail; } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      const blob = await resp.blob();
      const disp = resp.headers.get('Content-Disposition') || '';
      const m = /filename="([^"]+)"/.exec(disp);
      const name = m ? m[1] : `knowledge-points.${fmt}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      msg('pt-batch-msg', `已导出 ${ids.length} 条（${fmt}）`, 'ok');
    } catch (e) {
      msg('pt-batch-msg', '导出失败：' + e.message, 'err');
    }
  }

  // ---------------------------------------------------------------- 编辑弹窗
  function openModal(id) { $(id).hidden = false; }
  function closeModal(id) { $(id).hidden = true; }

  async function openEditor(chunkId) {
    msg('pt-modal-msg', '加载中…');
    openModal('pt-modal');
    ['pt-op-append', 'pt-op-remove', 'pt-op-image'].forEach((id) => { $(id).hidden = true; });
    let point;
    try {
      point = await api('/api/knowledge/points/' + encodeURIComponent(chunkId));
    } catch (e) {
      msg('pt-modal-msg', '加载失败：' + e.message, 'err');
      return;
    }
    state.current = point;
    $('pt-modal-title').textContent = '编辑知识点 · ' + point.chunkId;
    $('pt-modal-meta').innerHTML =
      `<span class="pt-tag">${esc(point.docId)}</span>` +
      `<span class="pt-tag">${esc(point.dept || '-')} / ${esc(point.ktype || '-')}</span>` +
      `<span class="pt-tag">${esc(point.docTitle || '(未命名)')}</span>` +
      `<span class="pt-tag">向量${point.vecSynced ? '已同步' : '待重算'}</span>`;
    $('pt-f-title').value = point.titlePath || '';
    $('pt-f-block').value = point.blockType || 'para';
    $('pt-f-images').value = (point.imageKeys || []).join(', ');
    $('pt-f-content').value = point.content || '';
    $('pt-append-text').value = '';
    $('pt-remove-text').value = '';
    $('pt-remove-start').value = '';
    $('pt-remove-end').value = '';
    $('pt-image-key').value = '';
    $('pt-save').disabled = !state.editable;
    $('pt-delete').disabled = !state.editable;
    msg('pt-modal-msg', state.editable ? '' : '只读模式：仅可浏览，不能保存');
  }

  function splitKeys(raw) {
    return (raw || '').split(',').map((s) => s.trim()).filter(Boolean);
  }

  async function save() {
    const point = state.current;
    if (!point) return;
    msg('pt-modal-msg', '保存中…');
    try {
      const data = await api('/api/knowledge/points/' + encodeURIComponent(point.chunkId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          content: $('pt-f-content').value,
          titlePath: $('pt-f-title').value,
          blockType: $('pt-f-block').value,
          imageKeys: splitKeys($('pt-f-images').value),
        }),
      });
      const warn = data.warning ? '（' + data.warning + '）' : '';
      msg('pt-modal-msg', `已保存，重算向量${data.vecSynced ? '成功' : '失败'}${warn}`,
        data.vecSynced ? 'ok' : 'warn');
      state.current = data.point || state.current;
      load();
    } catch (e) {
      msg('pt-modal-msg', '保存失败：' + e.message, 'err');
    }
  }

  async function appendText() {
    const point = state.current;
    if (!point) return;
    const text = $('pt-append-text').value;
    if (!text) { msg('pt-modal-msg', '请填写要追加的文本', 'err'); return; }
    const pos = (document.querySelector('input[name="pt-append-pos"]:checked') || {}).value || 'end';
    msg('pt-modal-msg', '追加中…');
    try {
      const data = await api('/api/knowledge/points/' + encodeURIComponent(point.chunkId) + '/append-text',
        jsonBody({ text: text, position: pos }));
      $('pt-f-content').value = (data.point || {}).content || $('pt-f-content').value;
      $('pt-append-text').value = '';
      msg('pt-modal-msg', `已追加 ${data.appended} 字，正文 ${data.point.charCount} 字`, 'ok');
      load();
    } catch (e) {
      msg('pt-modal-msg', '追加失败：' + e.message, 'err');
    }
  }

  async function removeContent() {
    const point = state.current;
    if (!point) return;
    const text = $('pt-remove-text').value;
    const start = $('pt-remove-start').value.trim();
    const end = $('pt-remove-end').value.trim();
    const payload = {};
    if (text) payload.text = text;
    else if (start !== '' && end !== '') { payload.start = Number(start); payload.end = Number(end); }
    else { msg('pt-modal-msg', '请填写删除片段，或同时填写起始/结束字符位置', 'err'); return; }
    msg('pt-modal-msg', '删除中…');
    try {
      const data = await api('/api/knowledge/points/' + encodeURIComponent(point.chunkId) + '/remove-content',
        jsonBody(payload));
      $('pt-f-content').value = (data.point || {}).content || '';
      $('pt-remove-text').value = '';
      $('pt-remove-start').value = '';
      $('pt-remove-end').value = '';
      msg('pt-modal-msg', `已删除 ${data.removed} 字（区间 ${data.range.join('-')}）`, 'ok');
      load();
    } catch (e) {
      msg('pt-modal-msg', '删除失败：' + e.message, 'err');
    }
  }

  async function insertImage() {
    const point = state.current;
    if (!point) return;
    const key = $('pt-image-key').value.trim();
    if (!key) { msg('pt-modal-msg', '请填写图片对象键', 'err'); return; }
    msg('pt-modal-msg', '插入中…');
    try {
      const data = await api('/api/knowledge/points/' + encodeURIComponent(point.chunkId) + '/insert-image',
        jsonBody({ key: key }));
      $('pt-f-images').value = (data.imageKeys || []).join(', ');
      msg('pt-modal-msg', data.added ? '已插入图片' : '该图片已存在，未重复插入', data.added ? 'ok' : 'warn');
      load();
    } catch (e) {
      msg('pt-modal-msg', '插入失败：' + e.message, 'err');
    }
  }

  async function removePoint(chunkId) {
    if (!window.confirm(`确认删除知识点 ${chunkId}？该切片及其向量会被移除，且不可恢复。`)) return;
    try {
      await api('/api/knowledge/points/' + encodeURIComponent(chunkId), { method: 'DELETE' });
      closeModal('pt-modal');
      load();
    } catch (e) {
      msg('pt-modal-msg', '删除失败：' + e.message, 'err');
    }
  }

  // ---------------------------------------------------------------- 新建
  async function createPoint() {
    const docId = $('pt-new-doc').value.trim();
    const content = $('pt-new-content').value;
    if (!docId) { msg('pt-create-msg', '请填写所属文档编号', 'err'); return; }
    if (!content.trim()) { msg('pt-create-msg', '正文不能为空', 'err'); return; }
    msg('pt-create-msg', '创建中…');
    try {
      const data = await api('/api/knowledge/points', jsonBody({
        docId: docId,
        content: content,
        titlePath: $('pt-new-title').value,
        blockType: $('pt-new-block').value,
      }));
      msg('pt-create-msg', `已创建 ${data.point.chunkId}（序号 ${data.seqNo}）`, 'ok');
      $('pt-new-content').value = '';
      closeModal('pt-create');
      load();
    } catch (e) {
      msg('pt-create-msg', '创建失败：' + e.message, 'err');
    }
  }

  // ---------------------------------------------------------------- 事件绑定
  function bind() {
    $('pt-search').onclick = () => {
      state.dept = $('pt-dept').value;
      state.ktype = $('pt-ktype').value;
      state.blockType = $('pt-block').value;
      state.q = $('pt-q').value.trim();
      state.page = 1;
      load();
    };
    $('pt-q').onkeydown = (e) => { if (e.key === 'Enter') $('pt-search').click(); };
    $('pt-reset').onclick = () => {
      $('pt-dept').value = ''; $('pt-ktype').value = ''; $('pt-block').value = ''; $('pt-q').value = '';
      state.dept = ''; state.ktype = ''; state.blockType = ''; state.q = ''; state.page = 1;
      load();
    };
    $('pt-prev').onclick = () => { if (state.page > 1) { state.page -= 1; load(); } };
    $('pt-next').onclick = () => { state.page += 1; load(); };
    $('pt-chk-all').onchange = (e) => {
      state.selected.clear();
      document.querySelectorAll('[data-chk]').forEach((box) => {
        box.checked = e.target.checked;
        if (e.target.checked) state.selected.add(box.getAttribute('data-chk'));
      });
      syncBatch();
    };
    $('pt-export-csv').onclick = () => exportSelected('csv');
    $('pt-export-md').onclick = () => exportSelected('md');
    $('pt-export-json').onclick = () => exportSelected('json');
    $('pt-batch-clear').onclick = () => {
      state.selected.clear();
      document.querySelectorAll('[data-chk]').forEach((box) => { box.checked = false; });
      msg('pt-batch-msg', '');
      syncBatch();
    };

    $('pt-new').onclick = () => { msg('pt-create-msg', ''); openModal('pt-create'); };
    $('pt-create-close').onclick = () => closeModal('pt-create');
    $('pt-create-cancel').onclick = () => closeModal('pt-create');
    $('pt-create-mask').onclick = () => closeModal('pt-create');
    $('pt-create-submit').onclick = createPoint;

    $('pt-close').onclick = () => closeModal('pt-modal');
    $('pt-cancel').onclick = () => closeModal('pt-modal');
    $('pt-modal-mask').onclick = () => closeModal('pt-modal');
    $('pt-save').onclick = save;
    $('pt-delete').onclick = () => state.current && removePoint(state.current.chunkId);

    $('pt-op-append-toggle').onclick = () => { $('pt-op-append').hidden = !$('pt-op-append').hidden; };
    $('pt-op-remove-toggle').onclick = () => { $('pt-op-remove').hidden = !$('pt-op-remove').hidden; };
    $('pt-op-image-toggle').onclick = () => { $('pt-op-image').hidden = !$('pt-op-image').hidden; };
    $('pt-append-run').onclick = appendText;
    $('pt-remove-run').onclick = removeContent;
    $('pt-image-run').onclick = insertImage;

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { closeModal('pt-modal'); closeModal('pt-create'); }
    });
  }

  // ---------------------------------------------------------------- 启动
  function boot() {
    bind();
    const ready = window.NXZAuth ? window.NXZAuth.ready() : Promise.resolve();
    ready.then(() => {
      if (window.NXZAuth && window.NXZAuth.me.authEnabled && !window.NXZAuth.can('points.view')) return;
      loadFilters().then(load);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 供浏览器验收工具断言页面确实初始化完成
  window.__NXZ_POINTS__ = { state: state, reload: load };
})();
