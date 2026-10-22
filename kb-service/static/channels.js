/* 能小智 · 渠道分身管理（任务 3.1 / M-C-01）
 *
 * 对接：
 *   GET    /api/admin/channels            清单 + 助手下拉
 *   POST   /api/admin/channels            新建
 *   PATCH  /api/admin/channels/{id}       更新 / 绑定助手 / 启停
 *   DELETE /api/admin/channels/{id}       删除
 *   GET    /api/channels/{code}           公开配置（对话页/门户读取）
 *
 * 嵌入代码由后端生成并随清单返回（iframeSnippet / floatSnippet），
 * 前端只负责展示与复制——避免两处拼接出不同的 URL。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const ROUTE_ROLES = ['访客', '本科生', '研究生', '教职工', '离退休人员'];
  const ROUTE_MODULES = [
    ['guideQuestions', '引导问题'],
    ['shortcuts', '快捷方式'],
    ['recommendedApps', '推荐应用'],
    ['appSquare', '应用广场'],
    ['serviceEntry', '客服入口'],
    ['feedback', '意见反馈'],
  ];
  const state = {
    items: [],
    agentOptions: [],
    editingId: null,
    editingTheme: {},
    routeItem: null,
    codeItem: null,
    codeKind: 'iframe',
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

  function agentName(id) {
    const hit = state.agentOptions.find((a) => Number(a.agentId) === Number(id));
    return hit ? hit.agentName : `#${id}`;
  }

  function agentChip(agent) {
    const off = Number(agent.status) !== 1;
    const cls = 'ch-agent-chip' + (off ? ' off' : '');
    const star = agent.isDefault ? '<b title="默认助手">★</b> ' : '';
    return `<span class="${cls}" title="${off ? '已下架' : '已上架'}">${star}${esc(agent.agentName)}</span>`;
  }

  function render() {
    const tbody = $('ch-tbody');
    const empty = $('ch-empty');
    tbody.innerHTML = '';
    empty.hidden = state.items.length > 0;
    $('ch-count').textContent = state.items.length;
    $('ch-enabled').textContent = state.items.filter((x) => x.status === 1).length;
    $('ch-agents').textContent = state.agentOptions.length;

    for (const item of state.items) {
      const tr = document.createElement('tr');
      const others = (item.agents || []).filter((a) => !a.isDefault);
      const theme = item.themeConfig || {};
      const bits = [];
      if (theme.assistantName) bits.push(`形象 ${theme.assistantName}`);
      if (Array.isArray(theme.faq) && theme.faq.length) bits.push(`FAQ ${theme.faq.length}`);
      if (Array.isArray(theme.shortcuts) && theme.shortcuts.length) bits.push(`快捷 ${theme.shortcuts.length}`);
      if (Array.isArray(theme.recommendedApps) && theme.recommendedApps.length) {
        bits.push(`应用 ${theme.recommendedApps.length}`);
      }
      if (theme.serviceEntry && theme.serviceEntry.enabled) bits.push('客服');
      if (theme.feedback && theme.feedback.enabled) bits.push('反馈');
      const routeCount = ((item.routeConfig || {}).routes || []).length;
      if (routeCount) bits.push(`路由 ${routeCount} 身份`);
      const statusBadge = item.status === 1
        ? '<span class="badge s2">启用</span>'
        : '<span class="badge s0">停用</span>';
      const authBadge = item.needAuth
        ? '<span class="ch-auth on">需认证</span>'
        : '<span class="ch-auth">访客可用</span>';
      tr.innerHTML = `
        <td>
          <div class="doc-title">${esc(item.channelName)}</div>
          <div class="doc-meta mono">${esc(item.channelCode)}</div>
          ${bits.length ? `<div class="ch-tags">${bits.map(esc).join(' · ')}</div>` : ''}
        </td>
        <td><span class="ch-default">${esc(item.defaultAgentName || '-')}</span></td>
        <td>${others.length ? others.map(agentChip).join('') : '<span class="cl-dim">仅默认助手</span>'}</td>
        <td>${authBadge}</td>
        <td>${statusBadge}</td>
        <td>${fmtTime(item.updatedAt)}</td>
        <td class="ch-ops">
          <button class="op-btn" data-op="edit" data-perm="channels.manage">编辑</button>
          <button class="op-btn" data-op="route" data-perm="channels.manage">路由</button>
          <button class="op-btn" data-op="copy" data-perm="channels.manage">复制</button>
          <button class="op-btn" data-op="code">嵌入代码</button>
          <button class="op-btn" data-op="auth" data-perm="channels.manage">${item.needAuth ? '关闭认证' : '开启认证'}</button>
          <button class="op-btn" data-op="toggle" data-perm="channels.manage">${item.status === 1 ? '停用' : '启用'}</button>
          <button class="op-btn danger" data-op="delete" data-perm="channels.manage">删除</button>
        </td>`;
      tr.querySelectorAll('[data-op]').forEach((btn) => {
        btn.onclick = () => handleRow(btn.dataset.op, item);
      });
      tbody.appendChild(tr);
    }
  }

  async function load() {
    msg('ch-note', '加载中…');
    try {
      const data = await api('/api/admin/channels');
      state.items = data.items || [];
      state.agentOptions = data.agentOptions || [];
      $('ch-note').textContent = `共 ${state.items.length} 个渠道 · 生成链接基于 ${data.baseUrl || location.origin}`;
      render();
    } catch (e) {
      $('ch-note').textContent = '加载失败：' + e.message;
      $('ch-empty').hidden = false;
      $('ch-empty').textContent = '渠道加载失败：' + e.message;
    }
  }

  async function handleRow(op, item) {
    if (op === 'edit') return openEditor(item);
    if (op === 'route') return openRoute(item);
    if (op === 'code') return openCode(item);
    if (op === 'copy') {
      try {
        const created = await api(
          `/api/admin/channels/${item.channelId}/copy`, jsonBody('POST', {}));
        await load();
        openEditor(created);
      } catch (e) { alert('复制失败：' + e.message); }
      return;
    }
    if (op === 'auth') {
      try {
        await api(`/api/admin/channels/${item.channelId}`,
          jsonBody('PATCH', { needAuth: item.needAuth ? 0 : 1 }));
        await load();
      } catch (e) { alert('操作失败：' + e.message); }
      return;
    }
    if (op === 'toggle') {
      try {
        await api(`/api/admin/channels/${item.channelId}`,
          jsonBody('PATCH', { status: item.status === 1 ? 0 : 1 }));
        await load();
      } catch (e) { alert('操作失败：' + e.message); }
      return;
    }
    if (op === 'delete') {
      if (!confirm(`确认删除渠道「${item.channelName}」？其嵌入代码将立即失效，渠道会话记录保留。`)) return;
      try {
        await api(`/api/admin/channels/${item.channelId}`, { method: 'DELETE' });
        await load();
      } catch (e) { alert('删除失败：' + e.message); }
    }
  }

  // ---------------------------------------------------------------- 新建 / 编辑
  function renderAgentChecks(selected, defaultId) {
    const box = $('ch-f-agents');
    box.innerHTML = '';
    if (!state.agentOptions.length) {
      box.innerHTML = '<p class="ch-hint">暂无可用助手，请先在「智能体管理」中创建助手。</p>';
      return;
    }
    const chosen = new Set((selected || []).map(Number));
    const def = Number(defaultId);
    chosen.add(def);
    for (const agent of state.agentOptions) {
      const id = Number(agent.agentId);
      const label = document.createElement('label');
      label.className = 'ch-agent-item';
      label.innerHTML = `
        <input type="checkbox" value="${id}" ${chosen.has(id) ? 'checked' : ''} ${id === def ? 'disabled' : ''}>
        <span>${esc(agent.iconUrl || '🤖')} ${esc(agent.agentName)}
          <em>${agent.status === 1 ? '' : '（已下架）'}</em></span>`;
      label.querySelector('input').dataset.default = id === def ? '1' : '0';
      box.appendChild(label);
    }
  }

  function fillDefaultSelect(defaultId) {
    const sel = $('ch-f-default');
    sel.innerHTML = state.agentOptions
      .map((a) => `<option value="${a.agentId}">${esc(a.agentName)}${a.status === 1 ? '' : '（已下架）'}</option>`)
      .join('');
    if (defaultId != null) sel.value = String(defaultId);
    sel.onchange = () => {
      const def = Number(sel.value);
      renderAgentChecks(readAgentChecks(), def);
    };
  }

  function readAgentChecks() {
    return [...$('ch-f-agents').querySelectorAll('input[type="checkbox"]:checked')]
      .map((x) => Number(x.value));
  }

  function inputLines(id) {
    return ($(id).value || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
  }

  function parseShortcuts() {
    const rows = inputLines('ch-f-shortcuts');
    if (rows.length > 8) throw new Error('快捷方式最多 8 个');
    const actions = { 问: 'ask', 提问: 'ask', ask: 'ask', 链: 'link', 链接: 'link', link: 'link' };
    return rows.map((line, i) => {
      const parts = line.split('|').map((x) => x.trim());
      if (parts.length < 3) throw new Error(`快捷方式第 ${i + 1} 行格式应为「图标|名称|动作|内容」`);
      const [icon, label, third] = parts;
      let action = 'ask';
      let content = third;
      if (parts.length >= 4) {
        action = actions[third.toLowerCase()] || actions[third] || '';
        content = parts.slice(3).join('|').trim();
      }
      if (!label) throw new Error(`快捷方式第 ${i + 1} 行缺少名称`);
      if (!action) throw new Error(`快捷方式第 ${i + 1} 行动作需为「问」或「链」`);
      if (!content) throw new Error(`快捷方式第 ${i + 1} 行缺少内容`);
      return { icon, label, action, content };
    });
  }

  function parseApps() {
    const rows = inputLines('ch-f-apps');
    if (rows.length > 8) throw new Error('推荐应用最多 8 个');
    return rows.map((line, i) => {
      const parts = line.split('|').map((x) => x.trim());
      if (parts.length < 4) throw new Error(`推荐应用第 ${i + 1} 行格式应为「图标|名称|说明|链接」`);
      const [icon, name, desc] = parts;
      const url = parts.slice(3).join('|').trim();
      if (!name) throw new Error(`推荐应用第 ${i + 1} 行缺少名称`);
      if (!url) throw new Error(`推荐应用第 ${i + 1} 行缺少链接`);
      return { icon, name, desc, url };
    });
  }

  function readEntry(prefix) {
    return {
      enabled: Number($(`ch-f-${prefix}-enabled`).value) || 0,
      label: $(`ch-f-${prefix}-label`).value.trim(),
      url: $(`ch-f-${prefix}-url`).value.trim(),
    };
  }

  function putTheme(theme, key, value) {
    if (Array.isArray(value) ? value.length : value) theme[key] = value;
    else delete theme[key];
  }

  function buildTheme() {
    const theme = Object.assign({}, state.editingTheme || {});
    theme.primaryColor = $('ch-f-color').value;
    theme.position = $('ch-f-position').value;
    putTheme(theme, 'launcherText', $('ch-f-launcher').value.trim());
    putTheme(theme, 'assistantName', $('ch-f-assistant').value.trim());
    putTheme(theme, 'avatarText', $('ch-f-avatar-text').value.trim());
    putTheme(theme, 'avatarUrl', $('ch-f-avatar-url').value.trim());
    putTheme(theme, 'faq', inputLines('ch-f-faq'));
    putTheme(theme, 'shortcuts', parseShortcuts());
    putTheme(theme, 'recommendedApps', parseApps());
    theme.serviceEntry = readEntry('service');
    theme.feedback = readEntry('feedback');
    return theme;
  }

  function shortcutLine(s) {
    const action = s && s.action === 'link' ? '链' : '问';
    return `${(s && s.icon) || ''}|${(s && s.label) || ''}|${action}|${(s && s.content) || ''}`;
  }

  function appLine(a) {
    return `${(a && a.icon) || ''}|${(a && a.name) || ''}|${(a && a.desc) || ''}|${(a && a.url) || ''}`;
  }

  function openEditor(item) {
    state.editingId = item ? item.channelId : null;
    $('ch-modal-title').textContent = item ? `编辑渠道 · ${item.channelName}` : '新建渠道';
    $('ch-f-code').value = item ? item.channelCode : '';
    $('ch-f-code').disabled = false;
    $('ch-f-name').value = item ? item.channelName : '';
    $('ch-f-welcome').value = item ? (item.welcomeText || '') : '';
    const theme = (item && item.themeConfig) || {};
    state.editingTheme = theme;
    $('ch-f-color').value = /^#[0-9a-f]{6}$/i.test(theme.primaryColor || '') ? theme.primaryColor : '#3f8d80';
    $('ch-f-position').value = theme.position === 'left' ? 'left' : 'right';
    $('ch-f-launcher').value = theme.launcherText || '';
    $('ch-f-assistant').value = theme.assistantName || '';
    $('ch-f-avatar-text').value = theme.avatarText || '';
    $('ch-f-avatar-url').value = theme.avatarUrl || '';
    $('ch-f-faq').value = (theme.faq || []).join('\n');
    $('ch-f-shortcuts').value = (theme.shortcuts || []).map(shortcutLine).join('\n');
    $('ch-f-apps').value = (theme.recommendedApps || []).map(appLine).join('\n');
    const service = theme.serviceEntry || {};
    $('ch-f-service-enabled').value = String(service.enabled ? 1 : 0);
    $('ch-f-service-label').value = service.label || '';
    $('ch-f-service-url').value = service.url || '';
    const feedback = theme.feedback || {};
    $('ch-f-feedback-enabled').value = String(feedback.enabled ? 1 : 0);
    $('ch-f-feedback-label').value = feedback.label || '';
    $('ch-f-feedback-url').value = feedback.url || '';
    $('ch-f-need-auth').value = item ? String(item.needAuth) : '0';
    $('ch-f-status').value = item ? String(item.status) : '1';
    fillDefaultSelect(item ? item.defaultAgentId : (state.agentOptions[0] || {}).agentId);
    renderAgentChecks(item ? item.agentIds : [], item ? item.defaultAgentId : (state.agentOptions[0] || {}).agentId);
    msg('ch-modal-msg', '');
    $('ch-modal').hidden = false;
  }

  function closeEditor() {
    $('ch-modal').hidden = true;
    state.editingId = null;
    state.editingTheme = {};
  }

  async function save() {
    const code = $('ch-f-code').value.trim().toLowerCase();
    const name = $('ch-f-name').value.trim();
    if (!code) return msg('ch-modal-msg', '请填写渠道编码', 'err');
    if (!name) return msg('ch-modal-msg', '请填写渠道名称', 'err');
    if (!state.agentOptions.length) return msg('ch-modal-msg', '暂无可用助手，请先创建助手', 'err');

    const agentIds = readAgentChecks();
    const defaultId = Number($('ch-f-default').value);
    if (!agentIds.includes(defaultId)) agentIds.unshift(defaultId);
    let themeConfig;
    try {
      themeConfig = buildTheme();
    } catch (e) {
      return msg('ch-modal-msg', e.message, 'err');
    }
    const payload = {
      channelCode: code,
      channelName: name,
      welcomeText: $('ch-f-welcome').value.trim(),
      defaultAgentId: defaultId,
      agentIds,
      needAuth: Number($('ch-f-need-auth').value),
      status: Number($('ch-f-status').value),
      themeConfig,
    };
    const btn = $('ch-save');
    btn.disabled = true;
    msg('ch-modal-msg', '保存中…');
    try {
      if (state.editingId) {
        await api(`/api/admin/channels/${state.editingId}`, jsonBody('PATCH', payload));
      } else {
        await api('/api/admin/channels', jsonBody('POST', payload));
      }
      closeEditor();
      await load();
    } catch (e) {
      msg('ch-modal-msg', '保存失败：' + e.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 角色路由（3.4）
  function routeEntry(item, role) {
    const routes = ((item.routeConfig || {}).routes) || [];
    return routes.find((r) => r && r.role === role) || null;
  }

  function routeAgentOptions(item, selected) {
    const rows = ['<option value="">跟随渠道默认</option>'];
    for (const agent of item.agents || []) {
      const off = Number(agent.status) !== 1 ? '（已下架）' : '';
      const star = agent.isDefault ? '（默认）' : '';
      rows.push(
        `<option value="${agent.agentId}" ${Number(selected) === Number(agent.agentId) ? 'selected' : ''}>${esc(agent.agentName)}${star}${off}</option>`
      );
    }
    return rows.join('');
  }

  function routeCard(item, role) {
    const route = routeEntry(item, role);
    const enabled = !!route;
    const modules = (route && route.modules) || {};
    const defaultOn = !route;
    const checks = ROUTE_MODULES.map(([key, label]) => {
      const on = defaultOn ? true : Number(modules[key]) !== 0;
      return `<label class="ch-route-mod"><input type="checkbox" data-route-module="${key}" ${on ? 'checked' : ''}> ${label}</label>`;
    }).join('');
    return `
      <div class="ch-route-card ${enabled ? '' : 'off'}" data-role="${esc(role)}">
        <div class="ch-route-head">
          <label class="ch-route-switch">
            <input type="checkbox" data-route-enabled ${enabled ? 'checked' : ''}> 单独配置
          </label>
          <b>${esc(role)}</b>
        </div>
        <div class="ch-route-body">
          <div class="field">
            <label>默认助手</label>
            <select data-route-agent>${routeAgentOptions(item, route && route.agentId)}</select>
          </div>
          <div class="field">
            <label>专属欢迎语（留空沿用渠道欢迎语）</label>
            <input type="text" data-route-welcome maxlength="1024" value="${esc(route ? route.welcomeText : '')}" placeholder="如：欢迎咨询，教职工事项可在这里直接办理。">
          </div>
          <div class="field">
            <label>引导问题（每行一条，最多 6 条）</label>
            <textarea rows="3" data-route-guides placeholder="如：差旅怎么报销？&#10;体检怎么预约？">${esc(route ? (route.guideQuestions || []).join('\n') : '')}</textarea>
          </div>
          <div class="ch-route-mods">${checks}</div>
        </div>
      </div>`;
  }

  function openRoute(item) {
    state.routeItem = item;
    $('ch-route-title').textContent = `角色路由 · ${item.channelName}`;
    $('ch-route-list').innerHTML = ROUTE_ROLES.map((role) => routeCard(item, role)).join('');
    document.querySelectorAll('#ch-route-list .ch-route-card').forEach((card) => {
      const toggle = card.querySelector('[data-route-enabled]');
      const sync = () => {
        card.classList.toggle('off', !toggle.checked);
        card.querySelectorAll('.ch-route-body input, .ch-route-body select, .ch-route-body textarea')
          .forEach((el) => { el.disabled = !toggle.checked; });
      };
      toggle.onchange = sync;
      sync();
    });
    msg('ch-route-msg', '');
    $('ch-route-modal').hidden = false;
  }

  function closeRoute() {
    $('ch-route-modal').hidden = true;
    state.routeItem = null;
  }

  async function saveRoute() {
    if (!state.routeItem) return;
    const routes = [];
    for (const card of document.querySelectorAll('#ch-route-list .ch-route-card')) {
      if (!card.querySelector('[data-route-enabled]').checked) continue;
      const role = card.dataset.role;
      const agentRaw = card.querySelector('[data-route-agent]').value;
      const guides = (card.querySelector('[data-route-guides]').value || '')
        .split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
      if (guides.length > 6) {
        return msg('ch-route-msg', `角色「${role}」的引导问题最多 6 条`, 'err');
      }
      const modules = {};
      card.querySelectorAll('[data-route-module]').forEach((input) => {
        modules[input.dataset.routeModule] = input.checked ? 1 : 0;
      });
      routes.push({
        role,
        agentId: agentRaw ? Number(agentRaw) : null,
        welcomeText: (card.querySelector('[data-route-welcome]').value || '').trim(),
        guideQuestions: guides,
        modules,
      });
    }
    const btn = $('ch-route-save');
    btn.disabled = true;
    msg('ch-route-msg', '保存中…');
    try {
      await api(
        `/api/admin/channels/${state.routeItem.channelId}`,
        jsonBody('PATCH', { routeConfig: { routes } })
      );
      closeRoute();
      await load();
    } catch (e) {
      msg('ch-route-msg', '保存失败：' + e.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 嵌入代码
  function openCode(item) {
    state.codeItem = item;
    state.codeKind = 'iframe';
    $('ch-code-title').textContent = `嵌入代码 · ${item.channelName}`;
    $('ch-url').textContent = item.channelUrl || '';
    document.querySelectorAll('#ch-code-tabs .ch-code-tab').forEach((b) => {
      b.classList.toggle('active', b.dataset.kind === state.codeKind);
    });
    $('ch-code-pre').textContent = item.iframeSnippet || '';
    $('ch-code-hint').textContent = '把 iframe 代码粘贴到页面固定区块；尺寸可按容器调整。';
    msg('ch-code-msg', '');
    $('ch-code-modal').hidden = false;
  }

  function closeCode() {
    $('ch-code-modal').hidden = true;
    state.codeItem = null;
  }

  async function copyText(text, msgId, okText) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      msg(msgId, okText || '已复制', 'ok');
    } catch (e) {
      msg(msgId, '复制失败，请手动选择代码复制', 'err');
    }
  }

  // ---------------------------------------------------------------- 事件绑定
  $('ch-new').onclick = () => openEditor(null);
  $('ch-refresh').onclick = () => load();
  $('ch-save').onclick = save;
  document.querySelectorAll('[data-ch-close]').forEach((el) => {
    el.onclick = closeEditor;
  });
  document.querySelectorAll('[data-ch-code-close]').forEach((el) => {
    el.onclick = closeCode;
  });
  $('ch-route-save').onclick = saveRoute;
  document.querySelectorAll('[data-ch-route-close]').forEach((el) => {
    el.onclick = closeRoute;
  });
  document.querySelectorAll('#ch-code-tabs .ch-code-tab').forEach((btn) => {
    btn.onclick = () => {
      state.codeKind = btn.dataset.kind;
      document.querySelectorAll('#ch-code-tabs .ch-code-tab').forEach((b) => {
        b.classList.toggle('active', b === btn);
      });
      if (!state.codeItem) return;
      if (state.codeKind === 'iframe') {
        $('ch-code-pre').textContent = state.codeItem.iframeSnippet;
        $('ch-code-hint').textContent = '把 iframe 代码粘贴到页面固定区块；尺寸可按容器调整。';
      } else if (state.codeKind === 'sdk') {
        $('ch-code-pre').textContent = state.codeItem.sdkSnippet || '';
        $('ch-code-hint').textContent =
          '把 SDK 代码放到 </body> 前：自动生成浮窗，支持 NengXiaoZhi.init/setToken/open 编程控制与身份透传。';
      } else {
        $('ch-code-pre').textContent = state.codeItem.floatSnippet;
        $('ch-code-hint').textContent = '把浮窗代码粘贴到 </body> 前，会在全站右下角生成常驻入口（无外部依赖）。';
      }
    };
  });
  $('ch-copy-url').onclick = () => {
    if (state.codeItem) copyText(state.codeItem.channelUrl, 'ch-code-msg', '链接已复制');
  };
  $('ch-copy-code').onclick = () => {
    if (state.codeItem) copyText($('ch-code-pre').textContent, 'ch-code-msg', '代码已复制');
  };
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('ch-modal').hidden) closeEditor();
    if (!$('ch-code-modal').hidden) closeCode();
    if (!$('ch-route-modal').hidden) closeRoute();
  });

  const ready = window.NXZAuth ? window.NXZAuth.ready() : Promise.resolve();
  ready.then(() => {
    const me = window.NXZAuth ? window.NXZAuth.me : null;
    if (me && me.authEnabled && !window.NXZAuth.can('channels.view')) return;
    load();
  }).catch(() => {});
})();
