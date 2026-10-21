/* 能小智 · 集成中心（任务 4.8 / C-05、M-A-07/10）
 *
 * 对接：
 *   GET   /api/admin/integrations/overview
 *   CRUD  /api/admin/integrations/local-models | mcp-servers | agent-apps
 *   POST  .../{id}/test | .../{id}/activate | /seed-examples
 *
 * 所有服务端文本都用 textContent 写入，不拼接 innerHTML。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    overview: null,
    local: [],
    mcp: [],
    agents: [],
    formKind: '',
    editing: null,
  };

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function authFetch(url, options) {
    return (window.NXZAuth ? window.NXZAuth.fetch : fetch)(url, options);
  }

  async function api(url, options) {
    const opts = Object.assign({ headers: { Accept: 'application/json' } }, options || {});
    const resp = await authFetch(url, opts);
    let json = null;
    try { json = await resp.json(); } catch (_) { /* ignore */ }
    if (!resp.ok) {
      const detail = json && (json.detail || json.error);
      throw new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
    }
    if (!json || !json.ok) throw new Error((json && json.error) || '请求失败');
    return json.data;
  }

  function canManage() {
    if (!window.NXZAuth || typeof window.NXZAuth.can !== 'function') return true;
    return window.NXZAuth.can('integrations.manage');
  }

  function msg(text, kind) {
    const line = $('ig-msg');
    line.textContent = text || '';
    line.className = 'msg-line' + (kind ? ' ' + kind : '');
  }

  function formMsg(text, kind) {
    const line = $('ig-form-msg');
    line.textContent = text || '';
    line.className = 'msg-line' + (kind ? ' ' + kind : '');
  }

  function fmtTime(raw) {
    if (!raw) return '-';
    const date = new Date(String(raw).replace(' ', 'T'));
    if (isNaN(date.getTime())) return String(raw).slice(0, 19);
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date);
  }

  function setVal(id, value) {
    const node = $(id);
    if (!node) return;
    if (node.type === 'checkbox') node.checked = Boolean(value);
    else node.value = value === null || value === undefined ? '' : String(value);
  }

  // ------------------------------------------------------------------ 渲染
  function statusBadge(item) {
    return item.status
      ? el('span', 'ig-badge active', '启用')
      : el('span', 'ig-badge off', '停用');
  }

  function testBadge(item) {
    const stateName = item.lastTestState;
    let badge;
    if (stateName === 'ok') badge = el('span', 'ig-badge ok', '通过');
    else if (stateName === 'error') badge = el('span', 'ig-badge error', '失败');
    else if (stateName === 'config-only') badge = el('span', 'ig-badge warn', '仅配置');
    else badge = el('span', 'ig-badge', '未测试');
    if (stateName) {
      badge.title = `${fmtTime(item.lastTestAt)} · ${item.lastLatencyMs || 0}ms`
        + (item.lastTestError ? `\n${item.lastTestError}` : '');
    }
    return badge;
  }

  function testCell(item) {
    const wrap = el('span');
    wrap.append(testBadge(item));
    if (item.lastTestAt) {
      wrap.append(el('span', 'ig-sub', `${fmtTime(item.lastTestAt)} · ${item.lastLatencyMs || 0}ms`));
    }
    return wrap;
  }

  function opButton(label, onClick, options) {
    const opts = options || {};
    const btn = el('button', opts.danger ? 'danger' : '', label);
    btn.type = 'button';
    btn.onclick = onClick;
    return btn;
  }

  function renderOverview() {
    const data = state.overview;
    if (!data) return;
    const localMode = data.llmProvider === 'local';
    $('ig-provider').textContent = localMode ? '本地模型' : '网关';
    $('ig-provider').title = localMode
      ? 'LLM_PROVIDER=local：问答直连当前启用的本地模型'
      : 'LLM_PROVIDER=gateway：模型清单由 campus-ai-gateway 下发';
    const active = data.activeLocalModel;
    $('ig-active').textContent = active ? active.name : '-';
    $('ig-active').title = active ? `${active.code} · ${active.remoteModel}` : '尚未启用本地模型';
    $('ig-local-count').textContent = String(data.localModelCount || 0);
    $('ig-mcp-count').textContent = String(data.mcpServerCount || 0);
    $('ig-agent-count').textContent = String(data.agentAppCount || 0);
    const m = data.mcpTestProvider === 'live' ? 'live' : 'mock';
    const a = data.agentTestProvider === 'live' ? 'live' : 'mock';
    $('ig-test-mode').textContent = m === a ? m : `${m}/${a}`;
    $('ig-test-mode').title = 'mock = 内置确定性结果；live = 真实请求外部服务（INTEGRATION_*_PROVIDER）';
    $('ig-hint').textContent = data.hint || '';
  }

  function renderLocal() {
    const tbody = $('ig-local-rows');
    tbody.replaceChildren();
    $('ig-local-empty').hidden = state.local.length > 0;
    for (const item of state.local) {
      const tr = document.createElement('tr');
      const name = el('td');
      name.append(el('span', 'ig-name', item.name));
      name.append(el('span', 'ig-code', item.code));

      const url = el('td');
      url.append(el('span', 'ig-mono', item.baseUrl));
      url.append(el('span', 'ig-sub', `超时 ${item.timeout}s`));

      const model = el('td', 'ig-mono', item.remoteModel);
      const key = el('td', '', item.hasKey ? '已配置' : '无（可选）');
      const active = el('td');
      active.append(item.active
        ? el('span', 'ig-badge active', '当前启用')
        : el('span', 'ig-badge off', '未启用'));

      const ops = el('td');
      if (canManage()) {
        const box = el('div', 'ig-ops');
        box.append(opButton('测试', (ev) => runTest('local', item, ev.currentTarget)));
        if (!item.active && item.status) {
          box.append(opButton('启用', () => activateLocal(item)));
        }
        box.append(opButton('编辑', () => openForm('local', item)));
        box.append(opButton('删除', () => removeItem('local', item), { danger: true }));
        ops.append(box);
      }

      tr.append(name, url, model, key, statusBadgeCell(item), active, testCell(item), ops);
      tbody.append(tr);
    }
  }

  function statusBadgeCell(item) {
    const td = el('td');
    td.append(statusBadge(item));
    return td;
  }

  function renderMcp() {
    const tbody = $('ig-mcp-rows');
    tbody.replaceChildren();
    $('ig-mcp-empty').hidden = state.mcp.length > 0;
    for (const item of state.mcp) {
      const tr = document.createElement('tr');
      const name = el('td');
      name.append(el('span', 'ig-name', item.name));
      name.append(el('span', 'ig-code', item.code));

      const transport = el('td');
      transport.append(el('span', 'ig-badge', item.transport.toUpperCase()));

      const target = el('td');
      if (item.transport === 'stdio') {
        target.append(el('span', 'ig-mono', [item.command].concat(item.args || []).join(' ')));
        target.append(el('span', 'ig-sub', 'stdio 仅保存配置'));
      } else {
        target.append(el('span', 'ig-mono', item.endpoint));
        if (item.headerNames && item.headerNames.length) {
          target.append(el('span', 'ig-sub', `Header：${item.headerNames.join(', ')}`));
        }
      }

      const tools = el('td');
      tools.append(el('span', 'ig-name', String(item.toolCount || 0)));
      tools.append(el('span', 'ig-sub', 'tools'));

      const ops = el('td');
      if (canManage()) {
        const box = el('div', 'ig-ops');
        box.append(opButton('测试', (ev) => runTest('mcp', item, ev.currentTarget)));
        box.append(opButton('编辑', () => openForm('mcp', item)));
        box.append(opButton('删除', () => removeItem('mcp', item), { danger: true }));
        ops.append(box);
      }

      tr.append(name, transport, target, tools, statusBadgeCell(item), testCell(item), ops);
      tbody.append(tr);
    }
  }

  function renderAgents() {
    const tbody = $('ig-agent-rows');
    tbody.replaceChildren();
    $('ig-agent-empty').hidden = state.agents.length > 0;
    for (const item of state.agents) {
      const tr = document.createElement('tr');
      const name = el('td');
      name.append(el('span', 'ig-name', item.name));
      name.append(el('span', 'ig-code', item.code));

      const kind = el('td');
      kind.append(el('span', 'ig-badge', item.kindLabel || item.kind));

      const url = el('td', 'ig-mono', item.baseUrl);
      const remote = el('td', 'ig-mono', item.remoteAppId || '-');
      const token = el('td', '', item.hasToken ? '已配置' : '未配置');

      const ops = el('td');
      if (canManage()) {
        const box = el('div', 'ig-ops');
        box.append(opButton('测试', (ev) => runTest('agent', item, ev.currentTarget)));
        box.append(opButton('编辑', () => openForm('agent', item)));
        box.append(opButton('删除', () => removeItem('agent', item), { danger: true }));
        ops.append(box);
      }

      tr.append(name, kind, url, remote, token, statusBadgeCell(item), testCell(item), ops);
      tbody.append(tr);
    }
  }

  function renderAll() {
    renderOverview();
    renderLocal();
    renderMcp();
    renderAgents();
  }

  // ------------------------------------------------------------------ 数据
  async function loadAll(silent) {
    if (!silent) msg('正在加载…');
    try {
      const [overview, local, mcp, agents] = await Promise.all([
        api('/api/admin/integrations/overview'),
        api('/api/admin/integrations/local-models'),
        api('/api/admin/integrations/mcp-servers'),
        api('/api/admin/integrations/agent-apps'),
      ]);
      state.overview = overview;
      state.local = local.items || [];
      state.mcp = mcp.items || [];
      state.agents = agents.items || [];
      renderAll();
      if (!silent) msg('已加载');
    } catch (err) {
      msg(`加载失败：${err.message}`, 'error');
    }
  }

  function replaceItem(kind, item) {
    const list = kind === 'local' ? state.local : kind === 'mcp' ? state.mcp : state.agents;
    const idKey = kind === 'local' ? 'localId' : kind === 'mcp' ? 'serverId' : 'appId';
    const index = list.findIndex((row) => row[idKey] === item[idKey]);
    if (index >= 0) list[index] = item;
    renderAll();
  }

  // ------------------------------------------------------------------ 抽屉
  function openDrawer(id, maskId) {
    $(id).hidden = false;
    $(maskId).hidden = false;
  }

  function closeDrawer(id, maskId) {
    $(id).hidden = true;
    $(maskId).hidden = true;
  }

  function closeForm() {
    closeDrawer('ig-form', 'ig-form-mask');
    state.formKind = '';
    state.editing = null;
    formMsg('');
  }

  function closeResult() {
    closeDrawer('ig-result', 'ig-result-mask');
  }

  function showResult(title, data) {
    $('ig-result-title').textContent = title;
    const view = Object.assign({}, data);
    delete view.item;
    const lines = [];
    const stateText = view.state === 'ok' ? '通过'
      : view.state === 'config-only' ? '仅配置（未联网探测）' : '失败';
    lines.push(`状态：${stateText}${view.mock ? '（mock）' : ''}`);
    if (view.latencyMs !== undefined) lines.push(`延迟：${view.latencyMs} ms`);
    if (view.reply) lines.push(`回复：${view.reply}`);
    if (view.toolCount !== undefined) lines.push(`工具数：${view.toolCount}`);
    if (view.error) lines.push(`错误：${view.error}`);
    if (view.message) lines.push(`说明：${view.message}`);
    lines.push('');
    lines.push(JSON.stringify(view, null, 2));
    $('ig-result-body').textContent = lines.join('\n');
    openDrawer('ig-result', 'ig-result-mask');
  }

  // ------------------------------------------------------------------ 表单
  function syncMcpFields() {
    const transport = $('ig-mcp-transport').value;
    $('ig-mcp-endpoint-wrap').hidden = transport === 'stdio';
    $('ig-mcp-stdio-wrap').hidden = transport !== 'stdio';
  }

  function openForm(kind, item) {
    state.formKind = kind;
    state.editing = item || null;
    for (const name of ['local', 'mcp', 'agent']) {
      $(`ig-fields-${name}`).hidden = name !== kind;
    }
    const label = kind === 'local' ? '本地模型' : kind === 'mcp' ? 'MCP 服务' : '第三方智能体';
    $('ig-form-title').textContent = `${item ? '编辑' : '新增'}${label}`;
    formMsg('');

    if (kind === 'local') {
      setVal('ig-local-code', item && item.code);
      setVal('ig-local-name', item && item.name);
      setVal('ig-local-url', item && item.baseUrl);
      setVal('ig-local-model', item && item.remoteModel);
      setVal('ig-local-key', '');
      setVal('ig-local-timeout', item ? item.timeout : 30);
      setVal('ig-local-status', item ? item.status : 1);
      setVal('ig-local-active', false);
      setVal('ig-local-remark', item && item.remark);
      $('ig-local-key').placeholder = item && item.hasKey
        ? '已配置；留空保持不变，输入新值覆盖'
        : '可选；本地服务通常无需 Key';
      $('ig-local-active-wrap').hidden = Boolean(item);
    } else if (kind === 'mcp') {
      setVal('ig-mcp-code', item && item.code);
      setVal('ig-mcp-name', item && item.name);
      setVal('ig-mcp-transport', item ? item.transport : 'http');
      setVal('ig-mcp-endpoint', item && item.endpoint);
      setVal('ig-mcp-command', item && item.command);
      setVal('ig-mcp-args', item ? (item.args || []).join('\n') : '');
      setVal('ig-mcp-headers', '');
      setVal('ig-mcp-clear-headers', false);
      setVal('ig-mcp-status', item ? item.status : 1);
      setVal('ig-mcp-remark', item && item.remark);
      $('ig-mcp-headers').placeholder = item && item.hasHeaders
        ? '留空保持不变；每行一条 Name: Value'
        : '每行一条：X-Token: abc';
      $('ig-mcp-header-hint').textContent = item && item.hasHeaders
        ? `已配置 Header：${(item.headerNames || []).join(', ') || '(名称未解析)'}；留空保持不变`
        : '尚未配置自定义 Header';
      syncMcpFields();
    } else {
      setVal('ig-agent-code', item && item.code);
      setVal('ig-agent-name', item && item.name);
      setVal('ig-agent-kind', item ? item.kind : 'dify');
      setVal('ig-agent-url', item && item.baseUrl);
      setVal('ig-agent-remote', item && item.remoteAppId);
      setVal('ig-agent-token', '');
      setVal('ig-agent-status', item ? item.status : 1);
      setVal('ig-agent-remark', item && item.remark);
      $('ig-agent-token').placeholder = item && item.hasToken
        ? '已配置；留空保持不变，输入新值覆盖'
        : 'Dify / Coze 通常必填';
    }
    openDrawer('ig-form', 'ig-form-mask');
  }

  function parseHeaders(text) {
    const headers = {};
    for (const raw of String(text || '').split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) continue;
      const index = line.indexOf(':');
      if (index <= 0) throw new Error(`Header 行格式应为「名称: 值」：${line}`);
      headers[line.slice(0, index).trim()] = line.slice(index + 1).trim();
    }
    return headers;
  }

  function formPayload() {
    const kind = state.formKind;
    if (kind === 'local') {
      const payload = {
        code: $('ig-local-code').value.trim(),
        name: $('ig-local-name').value.trim(),
        baseUrl: $('ig-local-url').value.trim(),
        remoteModel: $('ig-local-model').value.trim(),
        timeout: Number($('ig-local-timeout').value || 30),
        status: Number($('ig-local-status').value),
        remark: $('ig-local-remark').value.trim(),
      };
      const key = $('ig-local-key').value;
      if (key) payload.apiKey = key;
      if (!state.editing) payload.active = $('ig-local-active').checked;
      return payload;
    }
    if (kind === 'mcp') {
      const transport = $('ig-mcp-transport').value;
      const payload = {
        code: $('ig-mcp-code').value.trim(),
        name: $('ig-mcp-name').value.trim(),
        transport,
        status: Number($('ig-mcp-status').value),
        remark: $('ig-mcp-remark').value.trim(),
      };
      if (transport === 'stdio') {
        payload.command = $('ig-mcp-command').value.trim();
        payload.args = $('ig-mcp-args').value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      } else {
        payload.endpoint = $('ig-mcp-endpoint').value.trim();
      }
      const headerText = $('ig-mcp-headers').value.trim();
      if ($('ig-mcp-clear-headers').checked) payload.headers = {};
      else if (headerText) payload.headers = parseHeaders(headerText);
      return payload;
    }
    const payload = {
      code: $('ig-agent-code').value.trim(),
      name: $('ig-agent-name').value.trim(),
      kind: $('ig-agent-kind').value,
      baseUrl: $('ig-agent-url').value.trim(),
      remoteAppId: $('ig-agent-remote').value.trim(),
      status: Number($('ig-agent-status').value),
      remark: $('ig-agent-remark').value.trim(),
    };
    const token = $('ig-agent-token').value;
    if (token) payload.token = token;
    return payload;
  }

  function endpointOf(kind, id) {
    if (kind === 'local') return `/api/admin/integrations/local-models/${id}`;
    if (kind === 'mcp') return `/api/admin/integrations/mcp-servers/${id}`;
    return `/api/admin/integrations/agent-apps/${id}`;
  }

  async function submitForm(event) {
    event.preventDefault();
    const kind = state.formKind;
    if (!kind) return;
    const save = $('ig-form-save');
    save.disabled = true;
    formMsg('正在保存…');
    try {
      const payload = formPayload();
      const editing = state.editing;
      const url = editing ? endpointOf(kind, idOf(kind, editing)) : endpointOf(kind, '').replace(/\/$/, '');
      const data = await api(url, {
        method: editing ? 'PATCH' : 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      closeForm();
      msg(`已保存「${data.name}」`, 'ok');
      await loadAll(true);
    } catch (err) {
      formMsg(err.message, 'error');
    } finally {
      save.disabled = false;
    }
  }

  function idOf(kind, item) {
    return kind === 'local' ? item.localId : kind === 'mcp' ? item.serverId : item.appId;
  }

  // ------------------------------------------------------------------ 动作
  async function runTest(kind, item, button) {
    const label = item.name;
    if (button) {
      button.disabled = true;
      button.textContent = '测试中…';
    }
    try {
      const data = await api(`${endpointOf(kind, idOf(kind, item))}/test`, {
        method: 'POST',
        headers: { Accept: 'application/json' },
      });
      if (data.item) replaceItem(kind, data.item);
      const title = kind === 'local' ? '本地模型测试' : kind === 'mcp' ? 'MCP 连通性测试' : '第三方智能体测试';
      showResult(`${title} · ${label}`, data);
      msg(data.state === 'ok' ? '测试通过' : data.state === 'config-only' ? '配置已保存（未联网探测）' : '测试失败',
        data.state === 'ok' ? 'ok' : data.state === 'config-only' ? '' : 'error');
    } catch (err) {
      msg(`测试失败：${err.message}`, 'error');
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = '测试';
      }
    }
  }

  async function activateLocal(item) {
    try {
      await api(`${endpointOf('local', item.localId)}/activate`, {
        method: 'POST',
        headers: { Accept: 'application/json' },
      });
      msg(`已切换当前本地模型为「${item.name}」`, 'ok');
      await loadAll(true);
    } catch (err) {
      msg(`切换失败：${err.message}`, 'error');
    }
  }

  async function removeItem(kind, item) {
    const label = kind === 'local' ? '本地模型' : kind === 'mcp' ? 'MCP 服务' : '第三方智能体';
    if (!window.confirm(`确定删除${label}「${item.name}」？`)) return;
    try {
      await api(endpointOf(kind, idOf(kind, item)), {
        method: 'DELETE',
        headers: { Accept: 'application/json' },
      });
      msg(`已删除「${item.name}」`, 'ok');
      await loadAll(true);
    } catch (err) {
      msg(`删除失败：${err.message}`, 'error');
    }
  }

  async function seedExamples() {
    const button = $('ig-seed');
    button.disabled = true;
    try {
      const data = await api('/api/admin/integrations/seed-examples', {
        method: 'POST',
        headers: { Accept: 'application/json' },
      });
      const created = data.created || {};
      const total = (created.localModels || 0) + (created.mcpServers || 0) + (created.agentApps || 0);
      msg(total ? `已写入 ${total} 条示例配置` : '示例配置已存在，未重复写入', 'ok');
      await loadAll(true);
    } catch (err) {
      msg(`写入失败：${err.message}`, 'error');
    } finally {
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------------ 初始化
  function initTabs() {
    document.querySelectorAll('.ig-tab').forEach((button) => {
      button.onclick = () => {
        const tab = button.dataset.tab;
        document.querySelectorAll('.ig-tab').forEach((b) => b.classList.toggle('active', b === button));
        document.querySelectorAll('.ig-panel').forEach((panel) => {
          panel.classList.toggle('active', panel.id === `ig-panel-${tab}`);
        });
      };
    });
  }

  function initDrawers() {
    $('ig-form-close').onclick = closeForm;
    $('ig-form-cancel').onclick = closeForm;
    $('ig-form-mask').onclick = closeForm;
    $('ig-result-close').onclick = closeResult;
    $('ig-result-mask').onclick = closeResult;
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (!$('ig-result').hidden) closeResult();
      else if (!$('ig-form').hidden) closeForm();
    });
  }

  function boot() {
    initTabs();
    initDrawers();
    $('ig-form-el').addEventListener('submit', submitForm);
    $('ig-mcp-transport').addEventListener('change', syncMcpFields);
    $('ig-add-local').onclick = () => openForm('local', null);
    $('ig-add-mcp').onclick = () => openForm('mcp', null);
    $('ig-add-agent').onclick = () => openForm('agent', null);
    $('ig-refresh').onclick = () => loadAll(false);
    $('ig-seed').onclick = seedExamples;
    loadAll(false);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
