/* 能小智 · 应用广场管理（任务 3.3 / M-C-05·06）
 *
 * 对接：
 *   GET    /api/admin/apps           清单 + 助手下拉 + 角色/分类字典
 *   POST   /api/admin/apps           新建
 *   PATCH  /api/admin/apps/{id}      更新 / 角色授权 / 启停
 *   DELETE /api/admin/apps/{id}      删除
 *   GET    /api/apps?role=…          用户端广场（按角色过滤）
 *
 * 角色授权语义：勾「全部」则清空具体角色；勾具体角色则取消「全部」。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    items: [],
    agentOptions: [],
    roleOptions: [],
    categoryOptions: [],
    editingId: null,
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function safeUrl(raw) {
    const url = String(raw == null ? '' : raw).trim();
    if (!url) return '';
    const compact = url.replace(/[\u0000-\u0020\u007f]+/g, '').toLowerCase();
    if (/^(?:javascript|vbscript|data|file|blob):/.test(compact)) return '';
    if (/^(?:\/|#|\.\/|\.\.\/)/.test(url)) return url;
    if (/^https?:\/\//.test(compact)) return url;
    return '';
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
    if (isNaN(d)) return raw;
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function msg(id, text, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg-line' + (kind ? ' ' + kind : '');
  }

  function iconHtml(item) {
    const raw = String(item.iconUrl || '').trim();
    const url = safeUrl(raw);
    if (url && /^https?:\/\//i.test(url)) {
      return `<span class="ap-icon"><img src="${esc(url)}" alt="" loading="lazy"></span>`;
    }
    return `<span class="ap-icon">${esc(raw || '🧩')}</span>`;
  }

  function roleChips(roles) {
    const list = Array.isArray(roles) && roles.length ? roles : ['全部'];
    return list.map((r) => `<span class="ap-role${r === '全部' ? ' all' : ''}">${esc(r)}</span>`).join('');
  }

  function render() {
    const tbody = $('ap-tbody');
    const empty = $('ap-empty');
    tbody.innerHTML = '';
    empty.hidden = state.items.length > 0;
    $('ap-count').textContent = state.items.length;
    $('ap-enabled').textContent = state.items.filter((x) => x.status === 1).length;
    $('ap-cats').textContent = new Set(state.items.map((x) => x.category).filter(Boolean)).size;

    for (const item of state.items) {
      const tr = document.createElement('tr');
      let source = '';
      if (item.sourceType === 'link') {
        const href = safeUrl(item.linkUrl);
        source = href
          ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(item.linkUrl)}</a>`
          : `<span class="ap-missing">链接无效：${esc(item.linkUrl || '(空)')}</span>`;
      } else if (item.agentMissing) {
        source = `<span class="ap-missing">助手已删除（#${esc(item.agentId)}）</span>`;
      } else {
        const off = item.agentStatus !== 1 ? ' · 已下架' : '';
        source = `智能体 · ${esc(item.agentName || `#${item.agentId}`)}${off}`;
      }
      const statusBadge = item.status === 1
        ? '<span class="badge s2">上架</span>'
        : '<span class="badge s0">下架</span>';
      tr.innerHTML = `
        <td>
          <div class="ap-name">
            ${iconHtml(item)}
            <span>
              <b>${esc(item.appName)}</b>
              <small>${esc(item.category || '未分类')}</small>
            </span>
          </div>
          ${item.description ? `<div class="doc-meta">${esc(item.description)}</div>` : ''}
        </td>
        <td class="ap-source">${source}</td>
        <td>${roleChips(item.visibleRoles)}</td>
        <td class="ap-sort">${esc(item.sortNo)}</td>
        <td>${statusBadge}</td>
        <td>${fmtTime(item.updatedAt)}</td>
        <td class="ap-ops">
          <button class="op-btn" data-op="edit" data-perm="apps.manage">编辑</button>
          <button class="op-btn" data-op="toggle" data-perm="apps.manage">${item.status === 1 ? '下架' : '上架'}</button>
          <button class="op-btn danger" data-op="delete" data-perm="apps.manage">删除</button>
        </td>`;
      tr.querySelectorAll('[data-op]').forEach((btn) => {
        btn.onclick = () => handleRow(btn.dataset.op, item);
      });
      tbody.appendChild(tr);
    }
    if (window.NXZAuth) window.NXZAuth.apply();
  }

  async function load() {
    msg('ap-note', '加载中…');
    try {
      const data = await api('/api/admin/apps');
      state.items = data.items || [];
      state.agentOptions = data.agentOptions || [];
      state.roleOptions = data.roleOptions || ['全部'];
      state.categoryOptions = data.categoryOptions || [];
      msg('ap-note', `共 ${data.total} 个应用`);
      render();
    } catch (e) {
      msg('ap-note', '加载失败：' + e.message, 'error');
    }
  }

  function renderAgentOptions(selected) {
    const sel = $('ap-f-agent');
    sel.innerHTML = '';
    if (!state.agentOptions.length) {
      sel.innerHTML = '<option value="">（暂无可用助手）</option>';
      return;
    }
    for (const agent of state.agentOptions) {
      const opt = document.createElement('option');
      opt.value = String(agent.agentId);
      opt.textContent = agent.agentName + (agent.status === 1 ? '' : '（已下架）');
      if (Number(selected) === Number(agent.agentId)) opt.selected = true;
      sel.appendChild(opt);
    }
  }

  function renderCategoryOptions() {
    const list = $('ap-cat-list');
    list.innerHTML = '';
    for (const name of state.categoryOptions) {
      const opt = document.createElement('option');
      opt.value = name;
      list.appendChild(opt);
    }
  }

  function renderRoles(selected) {
    const box = $('ap-f-roles');
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
    return Array.from($('ap-f-roles').querySelectorAll('input:checked')).map((x) => x.value);
  }

  function toggleSource() {
    const isAgent = $('ap-f-source').value === 'agent';
    document.querySelector('.ap-agent-field').hidden = !isAgent;
    document.querySelector('.ap-link-field').hidden = isAgent;
  }

  function openEditor(item) {
    state.editingId = item ? item.appId : null;
    $('ap-modal-title').textContent = item ? '编辑应用' : '新建应用';
    $('ap-f-name').value = item ? item.appName : '';
    $('ap-f-source').value = item ? item.sourceType : 'agent';
    $('ap-f-link').value = item ? (item.linkUrl || '') : '';
    $('ap-f-category').value = item ? (item.category || '') : '';
    $('ap-f-icon').value = item ? (item.iconUrl || '') : '';
    $('ap-f-desc').value = item ? (item.description || '') : '';
    $('ap-f-sort').value = item ? item.sortNo : 0;
    $('ap-f-status').value = item ? String(item.status) : '1';
    renderAgentOptions(item ? item.agentId : null);
    renderRoles(item ? item.visibleRoles : ['全部']);
    toggleSource();
    msg('ap-modal-msg', '');
    $('ap-modal').hidden = false;
    $('ap-f-name').focus();
  }

  function closeEditor() {
    $('ap-modal').hidden = true;
    state.editingId = null;
  }

  async function save() {
    const name = $('ap-f-name').value.trim();
    const category = $('ap-f-category').value.trim();
    const roles = selectedRoles();
    if (!name) { msg('ap-modal-msg', '请填写应用名称', 'error'); return; }
    if (!category) { msg('ap-modal-msg', '请填写应用分类', 'error'); return; }
    if (!roles.length) { msg('ap-modal-msg', '请至少选择一个可见角色', 'error'); return; }
    const payload = {
      appName: name,
      sourceType: $('ap-f-source').value,
      agentId: $('ap-f-source').value === 'agent' ? Number($('ap-f-agent').value) || null : null,
      linkUrl: $('ap-f-source').value === 'link' ? $('ap-f-link').value.trim() : '',
      category,
      description: $('ap-f-desc').value.trim(),
      iconUrl: $('ap-f-icon').value.trim(),
      sortNo: Number($('ap-f-sort').value) || 0,
      visibleRoles: roles,
      status: Number($('ap-f-status').value),
    };
    $('ap-save').disabled = true;
    try {
      if (state.editingId) {
        await api(`/api/admin/apps/${state.editingId}`, jsonBody('PATCH', payload));
      } else {
        await api('/api/admin/apps', jsonBody('POST', payload));
      }
      closeEditor();
      await load();
    } catch (e) {
      msg('ap-modal-msg', e.message, 'error');
    } finally {
      $('ap-save').disabled = false;
    }
  }

  async function handleRow(op, item) {
    if (op === 'edit') { openEditor(item); return; }
    if (op === 'delete') {
      if (!confirm(`确定删除应用「${item.appName}」？`)) return;
      try {
        await api(`/api/admin/apps/${item.appId}`, { method: 'DELETE' });
        await load();
      } catch (e) {
        msg('ap-note', '删除失败：' + e.message, 'error');
      }
      return;
    }
    if (op === 'toggle') {
      try {
        await api(`/api/admin/apps/${item.appId}`,
          jsonBody('PATCH', { status: item.status === 1 ? 0 : 1 }));
        await load();
      } catch (e) {
        msg('ap-note', '操作失败：' + e.message, 'error');
      }
    }
  }

  $('ap-new').onclick = () => openEditor(null);
  $('ap-refresh').onclick = load;
  $('ap-f-source').onchange = toggleSource;
  $('ap-save').onclick = save;
  document.querySelectorAll('[data-ap-close]').forEach((el) => {
    el.onclick = closeEditor;
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('ap-modal').hidden) closeEditor();
  });

  renderCategoryOptions();
  const ready = window.NXZAuth ? window.NXZAuth.ready() : Promise.resolve();
  ready.then(() => {
    const me = window.NXZAuth ? window.NXZAuth.me : null;
    if (me && me.authEnabled && !window.NXZAuth.can('apps.view')) return;
    load();
  }).catch(() => {});
})();
