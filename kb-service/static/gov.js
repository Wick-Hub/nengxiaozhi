/* 能小智 · 导办事项管理（任务 4.5 / M-G-01、I-03）
 *
 * 对接：
 *   GET    /api/admin/gov/items?q=&status=
 *   POST   /api/admin/gov/items
 *   PATCH  /api/admin/gov/items/{id}
 *   DELETE /api/admin/gov/items/{id}
 *   POST   /api/admin/gov/sync
 *
 * 生效时段留空 = 长期有效；显式清空会向服务端传 null。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    items: [],
    deptOptions: [],
    roleOptions: ['全部'],
    summary: {},
    sync: {},
    ticketTtlSeconds: 120,
    editingId: null,
  };

  const STATE_BADGE = {
    active: ['s2', '生效中'],
    scheduled: ['s1', '未生效'],
    expired: ['s3', '已过期'],
    disabled: ['s0', '已停用'],
  };

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
    try { json = await resp.json(); } catch (_) { /* ignore */ }
    if (!resp.ok) {
      const detail = json && (json.detail || json.error);
      throw new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
    }
    if (!json || !json.ok) throw new Error((json && json.error) || '请求失败');
    return json.data;
  }

  function jsonBody(method, payload) {
    return {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    };
  }

  function fmtTime(raw) {
    if (!raw) return '-';
    const d = new Date(raw);
    if (isNaN(d.getTime())) return String(raw);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function toLocalInput(raw) {
    if (!raw) return '';
    return String(raw).replace(' ', 'T').slice(0, 16);
  }

  function msg(id, text, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg-line' + (kind ? ' ' + kind : '');
  }

  function windowText(item) {
    const from = item.effectiveFrom ? fmtTime(item.effectiveFrom) : '';
    const to = item.effectiveTo ? fmtTime(item.effectiveTo) : '';
    if (!from && !to) return '长期有效';
    if (from && to) return `${from} ~ ${to}`;
    if (from) return `${from} 起`;
    return `至 ${to}`;
  }

  function syncNote(text, kind) {
    const box = $('gv-sync-note');
    box.innerHTML = '';
    const span = document.createElement('span');
    if (kind) span.className = kind;
    span.textContent = text;
    box.appendChild(span);
  }

  function defaultSyncNote() {
    const sync = state.sync || {};
    const provider = sync.provider === 'mock'
      ? `内置演示数据（${sync.mockCount || 0} 条）`
      : `一网通办接口${sync.urlConfigured ? '（已配置）' : '（未配置 GOV_SYNC_URL）'}`;
    syncNote(`同步源：${provider} · 票据有效期 ${state.ticketTtlSeconds} 秒，一次性消费 · 同步不会覆盖本地启停状态与生效时段`);
  }

  function render() {
    const tbody = $('gv-tbody');
    tbody.innerHTML = '';
    $('gv-empty').hidden = state.items.length > 0;
    $('gv-total').textContent = state.summary.total || 0;
    $('gv-enabled').textContent = state.summary.enabled || 0;
    $('gv-active').textContent = state.summary.active || 0;
    $('gv-ticket').textContent = state.summary.needTicket || 0;
    $('gv-note').textContent = `当前筛选 ${state.items.length} 条`;

    for (const item of state.items) {
      const tr = document.createElement('tr');
      const badge = STATE_BADGE[item.effectiveState] || ['s0', item.effectiveLabel || '未知'];
      const roles = Array.isArray(item.visibleRoles) ? item.visibleRoles : ['全部'];
      const tags = Array.isArray(item.keywordTags) ? item.keywordTags : [];
      tr.innerHTML = `
        <td>
          <div class="gv-name">
            <b>${esc(item.itemName)}</b>
            <span class="gv-id">#${esc(item.itemId)} · 可见：${esc(roles.join(' / '))}</span>
            ${tags.length ? `<div class="gv-tags">${tags.slice(0, 6).map((t) => `<span class="gv-tag">${esc(t)}</span>`).join('')}</div>` : ''}
          </div>
        </td>
        <td>${esc(item.deptName || '-')}</td>
        <td><span class="gv-url" title="${esc(item.hallUrl || '')}"><code>${esc(item.hallUrl || '未配置')}</code></span></td>
        <td>${item.needTicket ? '<span class="badge s1">需票据</span>' : '<span class="badge s0">直达</span>'}</td>
        <td><div class="gv-window">${esc(windowText(item))}</div></td>
        <td><div class="gv-state"><span class="badge ${badge[0]}">${esc(badge[1])}</span></div></td>
        <td>${fmtTime(item.updatedAt)}</td>
        <td class="gv-ops">
          <button class="op-btn" data-op="edit" data-perm="gov.manage">编辑</button>
          <button class="op-btn" data-op="toggle" data-perm="gov.manage">${item.status === 1 ? '停用' : '启用'}</button>
          <button class="op-btn danger" data-op="delete" data-perm="gov.manage">删除</button>
        </td>`;
      tr.querySelectorAll('[data-op]').forEach((btn) => {
        btn.onclick = () => handleRow(btn.dataset.op, item);
      });
      tbody.appendChild(tr);
    }
    if (window.NXZAuth) window.NXZAuth.apply();
  }

  async function load() {
    msg('gv-note', '加载中…');
    try {
      const params = new URLSearchParams();
      const q = $('gv-q').value.trim();
      const status = $('gv-status').value;
      if (q) params.set('q', q);
      if (status) params.set('status', status);
      const query = params.toString();
      const data = await api('/api/admin/gov/items' + (query ? '?' + query : ''));
      state.items = data.items || [];
      state.summary = data.summary || {};
      state.deptOptions = data.deptOptions || [];
      state.roleOptions = data.roleOptions || ['全部'];
      state.sync = data.sync || {};
      state.ticketTtlSeconds = data.ticketTtlSeconds || 120;
      render();
      defaultSyncNote();
    } catch (e) {
      msg('gv-note', '加载失败：' + e.message, 'error');
      syncNote('加载失败：' + e.message, 'err');
    }
  }

  function renderDeptOptions(selected) {
    const sel = $('gv-f-dept');
    sel.innerHTML = '';
    for (const dept of state.deptOptions) {
      const opt = document.createElement('option');
      opt.value = String(dept.deptId);
      opt.textContent = dept.deptName;
      if (Number(selected) === Number(dept.deptId)) opt.selected = true;
      sel.appendChild(opt);
    }
  }

  function renderRoles(selected) {
    const box = $('gv-f-roles');
    box.innerHTML = '';
    const picked = Array.isArray(selected) && selected.length ? selected : ['全部'];
    for (const role of state.roleOptions) {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = role;
      input.checked = picked.includes(role);
      input.onchange = () => {
        if (role === '全部' && input.checked) {
          box.querySelectorAll('input').forEach((x) => { if (x !== input) x.checked = false; });
        } else if (role !== '全部' && input.checked) {
          const all = box.querySelector('input[value="全部"]');
          if (all) all.checked = false;
        }
      };
      label.appendChild(input);
      label.appendChild(document.createTextNode(role));
      box.appendChild(label);
    }
  }

  function selectedRoles() {
    return Array.from($('gv-f-roles').querySelectorAll('input:checked')).map((x) => x.value);
  }

  function openEditor(item) {
    state.editingId = item ? item.itemId : null;
    $('gv-modal-title').textContent = item ? '编辑导办事项' : '新建导办事项';
    $('gv-f-name').value = item ? item.itemName : '';
    renderDeptOptions(item ? item.deptId : (state.deptOptions[0] || {}).deptId);
    $('gv-f-url').value = item ? (item.hallUrl || '') : '/hall';
    $('gv-f-status').value = item ? String(item.status) : '1';
    $('gv-f-ticket').checked = item ? !!item.needTicket : true;
    $('gv-f-from').value = toLocalInput(item && item.effectiveFrom);
    $('gv-f-to').value = toLocalInput(item && item.effectiveTo);
    $('gv-f-tags').value = item && Array.isArray(item.keywordTags) ? item.keywordTags.join('，') : '';
    $('gv-f-conditions').value = item ? (item.conditions || '') : '';
    $('gv-f-materials').value = item ? (item.materials || '') : '';
    renderRoles(item ? item.visibleRoles : ['全部']);
    msg('gv-modal-msg', '');
    $('gv-modal').hidden = false;
    $('gv-f-name').focus();
  }

  function closeEditor() {
    $('gv-modal').hidden = true;
    state.editingId = null;
  }

  async function save() {
    const name = $('gv-f-name').value.trim();
    const hallUrl = $('gv-f-url').value.trim();
    const roles = selectedRoles();
    const from = $('gv-f-from').value;
    const to = $('gv-f-to').value;
    if (!name) { msg('gv-modal-msg', '请填写事项名称', 'error'); return; }
    if (!hallUrl) { msg('gv-modal-msg', '请填写办理入口地址', 'error'); return; }
    if (!roles.length) { msg('gv-modal-msg', '请至少选择一个可见角色', 'error'); return; }
    if (from && to && new Date(to) <= new Date(from)) {
      msg('gv-modal-msg', '生效结束时间必须晚于开始时间', 'error');
      return;
    }
    const tags = $('gv-f-tags').value
      .split(/[,，、\s]+/)
      .map((x) => x.trim())
      .filter(Boolean);
    const payload = {
      itemName: name,
      deptId: Number($('gv-f-dept').value),
      hallUrl,
      needTicket: $('gv-f-ticket').checked,
      conditions: $('gv-f-conditions').value.trim(),
      materials: $('gv-f-materials').value.trim(),
      keywordTags: tags,
      visibleRoles: roles,
      status: Number($('gv-f-status').value),
      effectiveFrom: from || null,
      effectiveTo: to || null,
    };
    $('gv-save').disabled = true;
    try {
      if (state.editingId) {
        await api(`/api/admin/gov/items/${state.editingId}`, jsonBody('PATCH', payload));
      } else {
        await api('/api/admin/gov/items', jsonBody('POST', payload));
      }
      closeEditor();
      await load();
    } catch (e) {
      msg('gv-modal-msg', e.message, 'error');
    } finally {
      $('gv-save').disabled = false;
    }
  }

  async function handleRow(op, item) {
    if (op === 'edit') { openEditor(item); return; }
    if (op === 'delete') {
      if (!confirm(`确定删除事项「${item.itemName}」？已签发的票据将无法兑换。`)) return;
      try {
        await api(`/api/admin/gov/items/${item.itemId}`, { method: 'DELETE' });
        await load();
      } catch (e) {
        syncNote('删除失败：' + e.message, 'err');
      }
      return;
    }
    if (op === 'toggle') {
      try {
        await api(`/api/admin/gov/items/${item.itemId}`,
          jsonBody('PATCH', { status: item.status === 1 ? 0 : 1 }));
        await load();
      } catch (e) {
        syncNote('操作失败：' + e.message, 'err');
      }
    }
  }

  async function syncItems() {
    const btn = $('gv-sync');
    btn.disabled = true;
    btn.textContent = '同步中…';
    try {
      const data = await api('/api/admin/gov/sync', { method: 'POST' });
      await load();
      syncNote(
        `同步完成（${data.provider}）：新增 ${data.created} · 更新 ${data.updated} · 跳过 ${data.skipped}，共 ${data.total} 条`,
        'ok'
      );
    } catch (e) {
      syncNote('同步失败：' + e.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '同步一网通办';
    }
  }

  $('gv-new').onclick = () => openEditor(null);
  $('gv-refresh').onclick = load;
  $('gv-sync').onclick = syncItems;
  $('gv-save').onclick = save;
  $('gv-status').onchange = load;
  $('gv-q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); load(); }
  });
  document.querySelectorAll('[data-gv-close]').forEach((el) => {
    el.onclick = closeEditor;
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('gv-modal').hidden) closeEditor();
  });

  const ready = window.NXZAuth ? window.NXZAuth.ready() : Promise.resolve();
  ready.then(() => {
    const me = window.NXZAuth ? window.NXZAuth.me : null;
    if (me && me.authEnabled && !window.NXZAuth.can('gov.view')) return;
    load();
  }).catch(() => {});
})();
