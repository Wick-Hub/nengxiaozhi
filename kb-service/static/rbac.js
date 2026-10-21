/* 能小智 · 角色权限与管理账号（任务 2.9 / M-SYS-01·03）
 *
 * 挂载在「系统管理」页内（不新增顶级页签，避免破坏既有 5 页签的验收脚本）：
 * - 角色矩阵：角色 × 菜单/按钮权限点，改动对已登录账号即时生效；
 * - 管理员账号：新建时必须设密码，历史演示账号（password_hash 为空）仍可免密登录。
 *
 * 依赖 auth.js：所有请求走 NXZAuth.fetch（等待登录完成并带 Bearer）。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    roles: [],
    catalog: [],
    admins: [],
    adminRoles: [],
    roleEditable: false,
    adminEditable: false,
    currentRole: null,
    currentAdmin: null,
    loaded: false,
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function authFetch(url, opts) {
    return (window.NXZAuth ? window.NXZAuth.fetch : fetch)(url, opts);
  }

  function can(code) {
    return !window.NXZAuth || window.NXZAuth.can(code);
  }

  async function api(url, opts) {
    const resp = await authFetch(url, opts);
    let json = null;
    try { json = await resp.json(); } catch (_) { /* 非 JSON */ }
    if (!resp.ok) {
      throw new Error((json && (json.detail || json.error)) || ('HTTP ' + resp.status));
    }
    if (!json || !json.ok) throw new Error((json && json.error) || '操作失败');
    return json.data;
  }

  function send(url, method, body) {
    return api(url, {
      method,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  function msg(id, text, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg-line' + (kind ? ' ' + kind : '');
  }

  function fmtTime(s) {
    if (!s) return '—';
    const d = new Date(s);
    if (isNaN(d)) return String(s);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ` +
      `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  function labelOf(code) {
    for (const g of state.catalog) {
      const p = (g.perms || []).find((x) => x.code === code);
      if (p) return p.label;
    }
    return code;
  }

  // ---------------------------------------------------------------- 角色矩阵
  function renderRoles(data) {
    state.roles = data.items || [];
    state.catalog = data.catalog || [];
    state.roleEditable = !!data.editable;

    const note = $('rbac-note');
    note.textContent = data.note || (state.roleEditable
      ? '「管理员」角色恒有全部权限；内置角色不可重命名/删除。权限点变更立即生效，已登录账号刷新菜单即可看到。'
      : '');

    const newBtn = $('rbac-role-new');
    if (newBtn) newBtn.hidden = !state.roleEditable;

    const box = $('rbac-roles');
    box.innerHTML = state.roles.map((r) => {
      const chips = r.permissions.slice(0, 8)
        .map((c) => `<span class="rbac-chip" title="${esc(labelOf(c))}">${esc(c)}</span>`);
      if (r.permissions.length > 8) {
        chips.push(`<span class="rbac-chip more">+${r.permissions.length - 8}</span>`);
      }
      const badges = [
        r.locked ? '<span class="rbac-badge locked">全部权限 · 只读</span>' : '',
        r.builtin && !r.locked ? '<span class="rbac-badge builtin">内置</span>' : '',
      ].join('');
      return `<div class="rbac-card" data-role="${r.roleId}">
        <div class="rbac-card-head">
          <div class="rbac-card-title">
            <b>${esc(r.roleName)}</b>
            <span class="rbac-code">${esc(r.roleCode)}</span>
          </div>
          ${badges}
        </div>
        <div class="rbac-remark">${esc(r.remark || '—')}</div>
        <div class="rbac-perm-chips">${chips.join('') || '<span class="rbac-chip more">无权限点</span>'}</div>
        <div class="rbac-actions">
          <button class="op-btn" data-perm="rbac.manage" data-role-edit="${r.roleId}"
            ${r.locked || !state.roleEditable ? 'disabled' : ''}>编辑权限</button>
          ${r.builtin ? '' : `<button class="op-btn danger" data-perm="rbac.manage" data-role-del="${r.roleId}"
            ${state.roleEditable ? '' : 'disabled'}>删除</button>`}
        </div>
      </div>`;
    }).join('');
    $('rbac-roles-empty').hidden = state.roles.length > 0;

    box.querySelectorAll('[data-role-edit]').forEach((b) => {
      b.onclick = () => openRoleModal(state.roles.find((r) => r.roleId === +b.dataset.roleEdit));
    });
    box.querySelectorAll('[data-role-del]').forEach((b) => {
      b.onclick = () => removeRole(state.roles.find((r) => r.roleId === +b.dataset.roleDel));
    });
    if (window.NXZAuth) window.NXZAuth.apply();
  }

  async function loadRoles() {
    try {
      const data = await api('/api/admin/roles');
      renderRoles(data);
    } catch (e) {
      $('rbac-note').textContent = '角色清单加载失败：' + e.message;
    }
  }

  function buildPermCatalog() {
    const host = $('rbac-role-perms');
    host.innerHTML = state.catalog.map((g) => `
      <div class="rbac-group">
        <div class="rbac-group-title">
          <span>${esc(g.name)}</span>
          <span>
            <button type="button" data-group-all="1">全选</button>
            <button type="button" data-group-none="1">清空</button>
          </span>
        </div>
        <div class="rbac-checks">
          ${(g.perms || []).map((p) => `
            <label>
              <input type="checkbox" value="${esc(p.code)}">
              <span>${esc(p.label)}<br><span class="code">${esc(p.code)}</span></span>
            </label>`).join('')}
        </div>
      </div>`).join('');

    host.querySelectorAll('.rbac-group').forEach((group) => {
      const boxes = () => Array.from(group.querySelectorAll('input[type="checkbox"]'));
      const all = group.querySelector('[data-group-all]');
      const none = group.querySelector('[data-group-none]');
      if (all) all.onclick = () => boxes().forEach((b) => { b.checked = true; });
      if (none) none.onclick = () => boxes().forEach((b) => { b.checked = false; });
    });
  }

  function openRoleModal(role) {
    state.currentRole = role || null;
    msg('rbac-role-msg', '');
    $('rbac-role-title').textContent = role ? `编辑角色 · ${role.roleName}` : '新建角色';
    $('rbac-role-code').value = role ? role.roleCode : '';
    $('rbac-role-code').disabled = !!role;
    $('rbac-role-name').value = role ? role.roleName : '';
    $('rbac-role-remark').value = role ? (role.remark || '') : '';
    if (!state.catalog.length && role) {
      // 目录缺失时无权渲染勾选（正常流程 loadRoles 已填）
      state.catalog = [];
    }
    buildPermCatalog();
    const selected = new Set(role ? role.permissions : []);
    $('rbac-role-perms').querySelectorAll('input[type="checkbox"]').forEach((b) => {
      b.checked = selected.has(b.value);
    });
    $('rbac-role-perms').querySelectorAll('input[type="checkbox"]').forEach((b) => {
      b.disabled = !state.roleEditable;
    });
    $('rbac-role-save').disabled = !state.roleEditable;
    $('rbac-role-modal').hidden = false;
  }

  function closeRoleModal() {
    $('rbac-role-modal').hidden = true;
  }

  async function saveRole() {
    const role = state.currentRole;
    const name = $('rbac-role-name').value.trim();
    const code = $('rbac-role-code').value.trim();
    const permissions = Array.from(
      $('rbac-role-perms').querySelectorAll('input[type="checkbox"]:checked')
    ).map((b) => b.value);
    if (!role && !code) { msg('rbac-role-msg', '请填写角色编码', 'err'); return; }
    if (!name) { msg('rbac-role-msg', '请填写角色名称', 'err'); return; }

    msg('rbac-role-msg', '保存中…');
    const btn = $('rbac-role-save');
    btn.disabled = true;
    try {
      if (role) {
        await send(`/api/admin/roles/${role.roleId}`, 'PATCH', {
          roleName: name,
          remark: $('rbac-role-remark').value.trim(),
          permissions,
        });
      } else {
        await send('/api/admin/roles', 'POST', {
          roleCode: code,
          roleName: name,
          remark: $('rbac-role-remark').value.trim(),
          permissions,
        });
      }
      closeRoleModal();
      await loadRoles();
      if (window.NXZAuth && window.NXZAuth.refresh) await window.NXZAuth.refresh();
    } catch (e) {
      msg('rbac-role-msg', '保存失败：' + e.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  async function removeRole(role) {
    if (!role) return;
    if (!window.confirm(`确认删除角色「${role.roleName}」？仍绑定账号时将无法删除。`)) return;
    try {
      await send(`/api/admin/roles/${role.roleId}`, 'DELETE');
      await loadRoles();
    } catch (e) {
      alert('删除失败：' + e.message);
    }
  }

  // ---------------------------------------------------------------- 管理员账号
  function selfUserNo() {
    return window.NXZAuth && window.NXZAuth.me ? window.NXZAuth.me.userNo : '';
  }

  function renderAdmins(data) {
    state.admins = data.items || [];
    state.adminRoles = data.roles || [];
    state.adminEditable = !!data.editable;

    const note = $('rbac-admin-note');
    note.textContent = data.note || (state.adminEditable
      ? `启用中的管理员 ${data.activeAdmins == null ? '—' : data.activeAdmins} 位。新建账号必须设密码（至少 8 位）；历史演示账号无密码时仍可免密登录。至少保留一个启用状态的管理员。`
      : '');
    $('rbac-admin-new').hidden = !state.adminEditable;
    $('rbac-admin-count').textContent = `（${state.admins.length} 个）`;

    const tbody = $('rbac-admin-tbody');
    const me = selfUserNo();
    tbody.innerHTML = state.admins.map((a) => {
      const roles = (a.roles || []).map((r) => `<span class="rbac-role-tag">${esc(r.roleName)}</span>`).join('');
      const isSelf = me && a.userNo === me;
      return `<tr>
        <td><span class="mono">${esc(a.userNo)}</span>${isSelf ? ' <span class="rbac-self">（当前）</span>' : ''}</td>
        <td>${esc(a.realName || '—')}</td>
        <td>${roles || '<span class="cl-dim">—</span>'}</td>
        <td>${a.hasPassword ? '已设置' : '<span class="rbac-pwd-hint">演示账号（免密）</span>'}</td>
        <td>${a.status === 1 ? '<span class="badge s2">启用</span>' : '<span class="badge s3">停用</span>'}</td>
        <td>${fmtTime(a.lastLoginAt)}</td>
        <td>
          <button class="op-btn" data-perm="rbac.manage" data-admin-edit="${a.userId}"
            ${state.adminEditable ? '' : 'disabled'}>编辑</button>
          <button class="op-btn danger" data-perm="rbac.manage" data-admin-del="${a.userId}"
            ${isSelf || !state.adminEditable ? 'disabled' : ''}>移除</button>
        </td>
      </tr>`;
    }).join('');
    $('rbac-admin-empty').hidden = state.admins.length > 0;

    tbody.querySelectorAll('[data-admin-edit]').forEach((b) => {
      b.onclick = () => openAdminModal(state.admins.find((a) => a.userId === +b.dataset.adminEdit));
    });
    tbody.querySelectorAll('[data-admin-del]').forEach((b) => {
      b.onclick = () => removeAdmin(state.admins.find((a) => a.userId === +b.dataset.adminDel));
    });
    if (window.NXZAuth) window.NXZAuth.apply();
  }

  async function loadAdmins() {
    try {
      const data = await api('/api/admin/admins');
      renderAdmins(data);
    } catch (e) {
      $('rbac-admin-note').textContent = '管理员清单加载失败：' + e.message;
    }
  }

  function openAdminModal(item) {
    state.currentAdmin = item || null;
    msg('rbac-admin-msg', '');
    $('rbac-admin-title').textContent = item ? `编辑管理员 · ${item.realName || item.userNo}` : '新建管理员';
    $('rbac-admin-no').value = item ? item.userNo : '';
    $('rbac-admin-no').disabled = !!item;
    $('rbac-admin-name').value = item ? (item.realName || '') : '';
    const pass = $('rbac-admin-pass');
    pass.value = '';
    pass.placeholder = item ? '留空则不修改（至少 8 位）' : '至少 8 位';
    $('rbac-admin-status').checked = item ? item.status === 1 : true;
    const isSelf = !!(item && selfUserNo() && item.userNo === selfUserNo());
    $('rbac-admin-status').disabled = isSelf;

    const host = $('rbac-admin-roles');
    host.innerHTML = state.adminRoles.map((r) => `
      <label>
        <input type="checkbox" value="${r.roleId}" ${item && (item.roleIds || []).includes(r.roleId) ? 'checked' : ''}>
        ${esc(r.roleName)} <span class="rbac-code">${esc(r.roleCode)}</span>
      </label>`).join('');
    host.querySelectorAll('input').forEach((b) => { b.disabled = !state.adminEditable; });
    $('rbac-admin-save').disabled = !state.adminEditable;
    $('rbac-admin-modal').hidden = false;
  }

  function closeAdminModal() {
    $('rbac-admin-modal').hidden = true;
  }

  async function saveAdmin() {
    const item = state.currentAdmin;
    const userNo = $('rbac-admin-no').value.trim();
    const realName = $('rbac-admin-name').value.trim();
    const password = $('rbac-admin-pass').value;
    const roleIds = Array.from(
      $('rbac-admin-roles').querySelectorAll('input:checked')
    ).map((b) => +b.value);
    const status = $('rbac-admin-status').checked ? 1 : 0;

    if (!item && !userNo) { msg('rbac-admin-msg', '请填写学工号', 'err'); return; }
    if (!realName) { msg('rbac-admin-msg', '请填写姓名', 'err'); return; }
    if (!item && !password) { msg('rbac-admin-msg', '新建账号必须设置密码', 'err'); return; }
    if (!roleIds.length) { msg('rbac-admin-msg', '请至少选择一个管理角色', 'err'); return; }

    msg('rbac-admin-msg', '保存中…');
    const btn = $('rbac-admin-save');
    btn.disabled = true;
    try {
      if (item) {
        const body = { realName, roleIds, status };
        if (password) body.password = password;
        await send(`/api/admin/admins/${item.userId}`, 'PATCH', body);
      } else {
        await send('/api/admin/admins', 'POST', { userNo, realName, password, roleIds, status });
      }
      closeAdminModal();
      await loadAdmins();
    } catch (e) {
      msg('rbac-admin-msg', '保存失败：' + e.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  async function removeAdmin(item) {
    if (!item) return;
    if (!window.confirm(`确认移除「${item.realName || item.userNo}」的全部管理角色？其普通问答账号不受影响。`)) return;
    try {
      await send(`/api/admin/admins/${item.userId}`, 'DELETE');
      await loadAdmins();
    } catch (e) {
      alert('移除失败：' + e.message);
    }
  }

  // ---------------------------------------------------------------- 启动
  async function initRbacPage() {
    if (window.NXZAuth && window.NXZAuth.me.authEnabled && !can('rbac.view')) return;
    await Promise.all([loadRoles(), loadAdmins()]);
    state.loaded = true;
  }

  function bind() {
    $('rbac-role-new').onclick = () => openRoleModal(null);
    $('rbac-role-save').onclick = saveRole;
    $('rbac-admin-new').onclick = () => openAdminModal(null);
    $('rbac-admin-save').onclick = saveAdmin;
    document.querySelectorAll('[data-rbac-close]').forEach((node) => {
      node.onclick = () => { closeRoleModal(); closeAdminModal(); };
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { closeRoleModal(); closeAdminModal(); }
    });
  }

  bind();

  const baseInit = window.initSystemPage;
  window.initSystemPage = function initSystemPageWithRbac() {
    const tasks = [];
    if (typeof baseInit === 'function') tasks.push(baseInit());
    tasks.push(initRbacPage());
    return Promise.all(tasks);
  };

  window.__NXZ_RBAC__ = { state, reload: initRbacPage };
})();
