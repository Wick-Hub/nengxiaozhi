/* 智能体 / 助手管理页（任务 2.2 M-A-01·02·03 + 任务 2.3 M-A-04·05·09）
 *
 * 对接接口（/api/admin 前缀，AUTH_ENABLED=true 时需管理员）：
 *   GET    /api/admin/agents                      列表（含 agentTypes / presets / defaultSystemPrompt）
 *   GET    /api/admin/agents/presets              场景预设
 *   GET    /api/admin/agents/{id}                 详情
 *   POST   /api/admin/agents                      创建（M-A-02）
 *   POST   /api/admin/agents/presets/{sceneKey}   一键搭建场景助手（M-A-01）
 *   PATCH  /api/admin/agents/{id}                 基础配置 + 提示词（M-A-03/04）
 *   POST   /api/admin/agents/{id}/status          上架/下架（M-A-03）
 *   DELETE /api/admin/agents/{id}                 删除
 *   GET    /api/admin/kb-folders                  知识文件夹清单（M-A-05）
 *   GET    /api/admin/agents/{id}/retrieval       检索配置（M-A-05）
 *   PUT    /api/admin/agents/{id}/retrieval       保存检索配置（M-A-05）
 *   POST   /api/admin/agents/{id}/test            对话测试（M-A-09，不落库）
 *
 * 弹窗分四个页签：基础信息 / 提示词 / 检索配置 / 对话测试。
 * 「保存」一次提交三类配置：基础字段与提示词走 PATCH，检索配置走 PUT，
 * 新建时合成一个 POST 请求体——**检索配置走独立接口**是为了让「知识范围」
 * 那批勾选项有明确的增量语义（未勾选 ≠ 清空，见后端 merge_retrieval）。
 *
 * 由 admin.js 的页签切换调用 window.initAgentPage()，只初始化一次（懒加载）。
 */
(() => {
  'use strict';

  const TYPE_LABELS = { chat: '问答', db: '问数', guide: '导办', access: '通行', custom: '自定义' };
  const TAB_IDS = { basic: 'ag-pane-basic', prompt: 'ag-pane-prompt', retrieval: 'ag-pane-retrieval', test: 'ag-pane-test' };

  let booted = false;
  let allAgents = [];
  let presets = [];
  let roles = [];
  let statusFilter = '';
  let keyword = '';
  let editing = null; // null = 新建；对象 = 编辑该助手
  let activeTab = 'basic';
  let defaultPrompt = '';
  let retrievalDefaults = null;
  let retrievalRanges = null;
  let folders = { depts: [], ktypes: [] };
  let foldersLoaded = false;
  //: 知识范围的当前勾选。**不能只从 DOM 读**：选项是异步拉的，
  //: 首次渲染时列表还是空的，只读 DOM 会把助手已保存的范围丢掉。
  let retrievalSel = { depts: [], ktypes: [] };
  // 任务 2.4（M-A-06）：模型下拉与生成参数
  let modelChoices = [];
  let modelDefaults = null;
  let modelRanges = null;

  const el = id => document.getElementById(id);

  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function authFetch(url, opts) {
    return (window.NXZAuth ? window.NXZAuth.fetch : fetch)(url, opts);
  }

  async function api(url, opts) {
    const resp = await authFetch(url, { credentials: 'same-origin', ...(opts || {}) });
    if (resp.status === 401) throw new Error('未登录或登录已失效，请先登录管理员账号');
    if (resp.status === 403) throw new Error('当前账号无管理端权限');
    let json;
    try {
      json = await resp.json();
    } catch (e) {
      throw new Error(`响应不是合法 JSON（HTTP ${resp.status}）`);
    }
    if (!json.ok) throw new Error(json.error || `请求失败（HTTP ${resp.status}）`);
    return json.data;
  }

  function jsonBody(obj) {
    return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(obj) };
  }

  // ---------------------------------------------------------------- 渲染
  function renderScenes() {
    const box = el('ag-scenes');
    if (!box) return;
    box.innerHTML = '';
    for (const p of presets) {
      const card = document.createElement('div');
      card.className = 'scene-card' + (p.created ? ' done' : '');
      card.innerHTML = `
        <div class="sc-top">
          <span class="sc-icon">${esc(p.iconUrl || '🤖')}</span>
          <div>
            <div class="sc-name">${esc(p.agentName)}</div>
            <div class="sc-code mono">${esc(p.agentCode)}</div>
          </div>
        </div>
        <div class="sc-desc">${esc(p.description)}</div>
        <button class="btn-ghost sc-btn" data-perm="agents.manage" ${p.created ? 'disabled' : ''}>
          ${p.created ? '✓ 已搭建' : '一键搭建'}
        </button>`;
      const btn = card.querySelector('.sc-btn');
      if (btn && !p.created) btn.onclick = () => buildPreset(p);
      box.appendChild(card);
    }
  }

  function iconCell(a) {
    const icon = String(a.iconUrl || '').trim();
    if (!icon) return '<span class="ag-icon ag-icon-empty">🤖</span>';
    if (/^https?:\/\//i.test(icon)) return `<img class="ag-icon" src="${esc(icon)}" alt="">`;
    return `<span class="ag-icon">${esc(icon)}</span>`;
  }

  /** 提示词/检索配置是否已自定义（列表里一眼看出哪些助手「配过」）。 */
  function configuredTags(a) {
    const out = [];
    if (String(a.systemPrompt || '').trim()) out.push('<span class="tag tag-on">提示词</span>');
    const rc = a.retrievalConfig || {};
    if (rc.topK || rc.rerankTopn || rc.minScore || (rc.depts || []).length || (rc.ktypes || []).length) {
      out.push('<span class="tag tag-on">检索</span>');
    }
    // 任务 2.4：绑定了非默认模型 → 标出模型编码，一眼看出这个助手走哪个模型
    const mc = String(a.modelCode || 'default');
    if (mc && mc !== 'default') {
      out.push(`<span class="tag tag-on">模型 ${esc(mc)}</span>`);
    }
    if (a.temperature != null || a.topP != null || a.maxTokens != null) {
      out.push('<span class="tag tag-on">参数</span>');
    }
    return out.length ? out.join('') : '<span class="cl-dim">默认</span>';
  }

  function renderTable() {
    const tbody = el('ag-tbody');
    const emptyEl = el('ag-empty');
    tbody.innerHTML = '';
    emptyEl.hidden = allAgents.length > 0;
    emptyEl.textContent = '暂无助手';

    for (const a of allAgents) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${iconCell(a)}</td>
        <td>
          <div class="doc-title">${esc(a.agentName)}</div>
          <div class="doc-meta mono">${esc(a.agentCode)}</div>
        </td>
        <td><span class="tag">${esc(TYPE_LABELS[a.agentType] || a.agentType)}</span></td>
        <td class="cl-q">${a.description ? esc(a.description) : '<span class="cl-dim">—</span>'}</td>
        <td>${(a.visibleRoles || []).map(r => `<span class="tag">${esc(r)}</span>`).join('')}</td>
        <td>${configuredTags(a)}</td>
        <td>${a.streamEnabled ? '<span class="badge s2">流式</span>' : '<span class="badge s0">整段</span>'}</td>
        <td>${a.status === 1
          ? '<span class="badge s2">已上架</span>'
          : '<span class="badge s3">已下架</span>'}</td>
        <td class="ag-ops"></td>`;
      const ops = tr.querySelector('.ag-ops');
      ops.appendChild(opBtn('编辑', 'edit', a, ''));
      ops.appendChild(opBtn('测试', 'test', a, ''));
      ops.appendChild(opBtn(a.status === 1 ? '下架' : '上架', 'toggle', a, ''));
      ops.appendChild(opBtn('删除', 'delete', a, 'danger'));
      tbody.appendChild(tr);
    }
  }

  function opBtn(label, op, agent, cls) {
    const b = document.createElement('button');
    b.className = 'op-btn' + (cls ? ' ' + cls : '');
    b.dataset.perm = op === 'test' ? 'agents.view' : 'agents.manage';
    b.textContent = label;
    b.onclick = () => {
      if (op === 'edit') openModal(agent, 'basic');
      else if (op === 'test') openModal(agent, 'test');
      else if (op === 'toggle') toggleStatus(agent);
      else remove(agent);
    };
    return b;
  }

  function renderCount() {
    const c = el('ag-count');
    if (c) c.textContent = `共 ${allAgents.length} 个`;
  }

  function renderStatusTabs() {
    const box = el('ag-status-tabs');
    if (!box) return;
    box.innerHTML = '';
    for (const [v, label] of [['', '全部'], ['1', '已上架'], ['0', '已下架']]) {
      const b = document.createElement('button');
      b.className = 'f' + (statusFilter === v ? ' active' : '');
      b.textContent = label;
      b.onclick = () => { statusFilter = v; renderStatusTabs(); load(); };
      box.appendChild(b);
    }
  }

  // ---------------------------------------------------------------- 取数
  async function load() {
    try {
      const qs = new URLSearchParams();
      if (statusFilter !== '') qs.set('status', statusFilter);
      if (keyword) qs.set('q', keyword);
      const data = await api(`/api/admin/agents?${qs.toString()}`);
      allAgents = data.items || [];
      presets = data.presets || [];
      defaultPrompt = data.defaultSystemPrompt || '';
      retrievalDefaults = data.retrievalDefaults || retrievalDefaults;
      retrievalRanges = data.retrievalRanges || retrievalRanges;
      // 任务 2.4（M-A-06）：模型下拉与生成参数的平台默认值。
      // 平台默认值来自后端下发——前端**不写死第二份**，否则改 .env 后显示会不一致。
      modelDefaults = data.modelDefaults || modelDefaults;
      modelRanges = data.modelRanges || modelRanges;
      modelChoices = data.modelChoices || modelChoices;
      renderCount();
      renderScenes();
      renderTable();
      const note = el('ag-note');
      if (note) {
        const total = presets.length;
        const done = presets.filter(p => p.created).length;
        note.className = 'cl-note';
        note.textContent = `场景智能体已搭建 ${done} / ${total} · 助手共 ${allAgents.length} 个`
          + (keyword || statusFilter !== '' ? '（当前为筛选结果）' : '');
      }
    } catch (e) {
      const emptyEl = el('ag-empty');
      emptyEl.hidden = false;
      emptyEl.textContent = '加载失败：' + e.message;
    }
  }

  async function loadRoles() {
    try {
      const meta = await api('/api/meta');
      roles = meta.roles || [];
    } catch (e) {
      roles = ['全部'];
    }
  }

  /** 知识文件夹（部门 / 类型）——只在首次打开「检索配置」页签时拉取。 */
  async function ensureFolders() {
    if (foldersLoaded) return;
    try {
      folders = await api('/api/admin/kb-folders');
      foldersLoaded = true;
    } catch (e) {
      folders = { depts: [], ktypes: [] };
      const note = el('ag-f-ret-note');
      if (note) note.textContent = '知识文件夹加载失败：' + e.message;
    }
  }

  // ---------------------------------------------------------------- 动作
  async function buildPreset(preset) {
    if (!confirm(`按预设搭建「${preset.agentName}」？将创建一条独立的助手记录。`)) return;
    try {
      const data = await api(`/api/admin/agents/presets/${encodeURIComponent(preset.sceneKey)}`,
        { method: 'POST' });
      await load();
      if (data.reused) alert(`「${data.agentName}」已存在，已为你复用原记录。`);
    } catch (e) {
      alert('搭建失败：' + e.message);
    }
  }

  async function toggleStatus(agent) {
    const next = agent.status === 1 ? 0 : 1;
    try {
      await api(`/api/admin/agents/${agent.agentId}/status`, jsonBody({ status: next }));
      await load();
    } catch (e) {
      alert((next === 1 ? '上架' : '下架') + '失败：' + e.message);
    }
  }

  async function remove(agent) {
    if (!confirm(`确认删除助手「${agent.agentName}」？删除后不可恢复。`)) return;
    try {
      await api(`/api/admin/agents/${agent.agentId}`, { method: 'DELETE' });
      await load();
    } catch (e) {
      alert('删除失败：' + e.message);
    }
  }

  // ---------------------------------------------------------------- 弹窗·页签
  function switchTab(name) {
    activeTab = name;
    for (const [key, paneId] of Object.entries(TAB_IDS)) {
      const pane = el(paneId);
      if (pane) pane.hidden = key !== name;
    }
    el('ag-tabs').querySelectorAll('.ag-tab').forEach(b => {
      b.classList.toggle('active', b.dataset.agt === name);
    });
    if (name === 'retrieval') ensureFolders().then(renderRetrievalPickers);
    if (name === 'test') renderTestPane();
  }

  // ---------------------------------------------------------------- 弹窗·检索配置
  function renderCheckGroup(boxId, items, selected, kind) {
    const box = el(boxId);
    if (!box) return;
    const chosen = new Set(selected || []);
    if (!items || !items.length) {
      box.innerHTML = '<span class="cl-dim">暂无可选项（知识库为空）</span>';
      return;
    }
    box.innerHTML = items.map(it => {
      // 部门用名称本身做值；知识类型用编码（索引里既有 'policy' 也有 '政策文件'，
      // 两者显示名相同但取值不同，因此编码与显示名不一致时把编码一并标出来，
      // 否则运营会看到两个一模一样的「常见问题」而不知该勾哪个）。
      const value = kind === 'dept' ? it.name : it.code;
      const name = kind === 'dept' ? it.name : (it.name || it.code);
      const extra = kind === 'dept' || !it.name || it.name === it.code
        ? '' : ` <span class="cl-dim mono">${esc(it.code)}</span>`;
      return `<label><input type="checkbox" value="${esc(value)}" ${chosen.has(value) ? 'checked' : ''}> `
        + `${esc(name)}${extra} <span class="cl-dim">${it.chunkCount}</span></label>`;
    }).join('');
  }

  /** 重渲染前把 DOM 里已有的勾选回收进状态（选项还没渲染出来时不动，避免误清空）。 */
  function syncRetrievalSel() {
    const dbox = el('ag-f-depts');
    const kbox = el('ag-f-ktypes');
    if (dbox && dbox.querySelector('input')) {
      retrievalSel.depts = [...dbox.querySelectorAll('input:checked')].map(c => c.value);
    }
    if (kbox && kbox.querySelector('input')) {
      retrievalSel.ktypes = [...kbox.querySelectorAll('input:checked')].map(c => c.value);
    }
  }

  function renderRetrievalPickers() {
    if (!el('ag-f-depts')) return;
    syncRetrievalSel();
    renderCheckGroup('ag-f-depts', folders.depts || [], retrievalSel.depts, 'dept');
    renderCheckGroup('ag-f-ktypes', folders.ktypes || [], retrievalSel.ktypes, 'ktype');

    const note = el('ag-f-ret-note');
    if (note) {
      const d = retrievalDefaults || {};
      note.className = 'cl-note';
      note.textContent = `留空 / 填 0 表示沿用平台默认：召回 ${d.topK} 条、精排候选 ${d.rerankTopn} 条`
        + `（精排${d.rerankEnabled ? '已开启' : '已关闭'}，检索模式 ${d.retrieveMode || 'hybrid'}）。`
        + '知识范围过滤发生在召回之前，勾选后助手只会看到所选文件夹的切片。';
    }
    // 上下限与后端校验共用同一组常量（retrievalRanges），避免前端能填出后端会拒的值
    const rg = retrievalRanges || {};
    if (rg.topK) el('ag-f-topk').max = String(rg.topK.max);
    if (rg.rerankTopn) el('ag-f-rerank').max = String(rg.rerankTopn.max);
    if (rg.minScore) el('ag-f-minscore').max = String(rg.minScore.max);
    updateRetrievalHints();
  }

  function updateRetrievalHints() {
    const d = retrievalDefaults || {};
    const topk = el('ag-f-topk');
    const rerank = el('ag-f-rerank');
    const hintK = el('ag-f-topk-hint');
    const hintR = el('ag-f-rerank-hint');
    if (hintK && topk) {
      const v = parseInt(topk.value, 10) || 0;
      hintK.textContent = v > 0 ? `生效：${v} 条（助手配置）` : `0 / 留空 → 沿用全局（${d.topK} 条）`;
    }
    if (hintR && rerank) {
      const v = parseInt(rerank.value, 10) || 0;
      hintR.textContent = v > 0
        ? `生效：${Math.max(v, parseInt(topk && topk.value, 10) || 0)} 条参与精排`
        : `0 / 留空 → 沿用全局（${d.rerankTopn} 条）`;
    }
  }

  /** 当前勾选（选项未渲染时回落到状态，保证「没打开过检索页签」也不会清空已存配置）。 */
  function checkedIn(boxId, fallback) {
    const box = el(boxId);
    if (!box || !box.querySelector('input')) return (fallback || []).slice();
    return [...box.querySelectorAll('input:checked')].map(c => c.value);
  }

  function currentRetrieval() {
    const topk = parseInt(el('ag-f-topk').value, 10) || 0;
    const rerank = parseInt(el('ag-f-rerank').value, 10) || 0;
    const minScore = parseFloat(el('ag-f-minscore').value) || 0;
    return {
      topK: topk,
      rerankTopn: rerank,
      minScore: minScore,
      depts: checkedIn('ag-f-depts', retrievalSel.depts),
      ktypes: checkedIn('ag-f-ktypes', retrievalSel.ktypes),
    };
  }

  // ---------------------------------------------------------------- 弹窗·模型与生成参数
  /** 渲染模型下拉。停用的模型后端已不下发（选了只会回退全局默认，更难排查）。 */
  function renderModelOptions(selected) {
    const sel = el('ag-f-model');
    if (!sel) return;
    sel.innerHTML = modelChoices.map(m => {
      const suffix = m.isDefault ? '' : `（${m.vendorLabel}${m.available ? '' : ' · 未配置 Key'}）`;
      return `<option value="${esc(m.modelCode)}">${esc(m.modelName)}${esc(suffix)}</option>`;
    }).join('');
    sel.value = selected || 'default';
    if (!sel.value) sel.value = 'default'; // 选中的模型已停用/删除 → 回落全局默认
    updateModelHints();
  }

  /** 生成参数与模型的「生效情况」提示——让运营知道「留空到底等于什么」。 */
  function updateModelHints() {
    const d = modelDefaults || {};
    const sel = el('ag-f-model');
    const code = sel ? sel.value : 'default';
    const chosen = modelChoices.find(m => m.modelCode === code);
    const hint = el('ag-f-model-hint');
    if (hint) {
      if (!chosen || chosen.isDefault) {
        hint.textContent = `沿用全局默认（.env）：${d.remoteModel || '-'}`
          + (d.hasApiKey ? '' : ' · 未配置 API Key')
          + (d.baseUrl ? ` · ${d.baseUrl}` : '');
      } else {
        hint.textContent = `厂商模型名 ${chosen.remoteModel || '-'}`
          + (chosen.isLocal ? ' · 本地模型' : (chosen.available ? '' : ' · ⚠ 未配置 API Key，问答会失败'));
      }
    }

    const t = el('ag-f-temp');
    const p = el('ag-f-topp');
    const m = el('ag-f-maxtok');
    const ph = el('ag-f-param-hint');
    if (!ph) return;
    const parts = [];
    parts.push(t && t.value !== ''
      ? `温度 ${t.value}（助手配置）`
      : `温度 ${d.temperature}（全局默认）`);
    parts.push(p && p.value !== ''
      ? `topP ${p.value}（助手配置）`
      : 'topP 跟随厂商默认');
    parts.push(m && m.value !== ''
      ? `最大输出 ${m.value}（助手配置）`
      : `最大输出 ${d.maxTokens}（全局默认）`);
    ph.textContent = '生效参数：' + parts.join(' · ');
  }

  /** 供 model.js 在模型增删改后调用：重新拉下拉，避免列到已删除的模型。 */
  window.refreshModelChoices = async function refreshModelChoices() {
    try {
      const data = await api('/api/admin/models/choices');
      modelChoices = data.items || [];
      const cur = el('ag-f-model') ? el('ag-f-model').value : 'default';
      renderModelOptions(cur);
    } catch (e) { /* 模型页未就绪或权限不足时静默，不影响助手页 */ }
  };

  // ---------------------------------------------------------------- 弹窗·对话测试
  function renderTestRoleOptions() {
    const sel = el('ag-test-role');
    if (!sel) return;
    const keep = sel.value;
    sel.innerHTML = '<option value="">（不指定，按访客处理）</option>'
      + roles.map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join('');
    if (keep) sel.value = keep;
  }

  function renderTestPane() {
    renderTestRoleOptions();
    const msg = el('ag-test-msg');
    if (!editing) {
      msg.className = 'cl-note';
      msg.textContent = '新建助手尚未保存，请先「保存」后再进行对话测试。';
      el('ag-test-run').disabled = true;
      return;
    }
    el('ag-test-run').disabled = false;
    msg.className = 'cl-note';
    msg.textContent = '测试使用「已保存」的提示词与检索配置，且不会写入会话日志。'
      + '若刚改过配置，请先保存再测试。';
  }

  function renderTestResult(d) {
    const box = el('ag-test-result');
    const ret = d.retrieval || {};
    const mdl = d.model || {};
    const sources = d.sources || [];
    const cits = (d.citations || []).map(c => `<span class="tag tag-on">[${c.n}] ${esc(c.titlePath || c.chunkId)}</span>`).join('');
    box.innerHTML = `
      <div class="tr-head">
        <span class="badge ${d.refused ? 's3' : 's2'}">${d.refused ? '拒答' : '已作答'}</span>
        <span class="tag">置信度 ${d.confidence}</span>
        <span class="tag">召回 ${d.retrieved}</span>
        <span class="tag">耗时 ${d.latencyMs}ms</span>
        <span class="tag">模型 ${esc(d.model || '—')}</span>
      </div>
      <div class="tr-answer">${esc(d.answer || '')}</div>
      <div class="tr-meta mono">
        生效检索参数：topK=${ret.topK}（${esc(ret.topKSource || '')}） ·
        rerankTopn=${ret.rerankTopn}（${esc(ret.rerankTopnSource || '')}） ·
        召回窗口 ${ret.recallK} · 最低相似度 ${ret.minScore} ·
        部门 [${(ret.depts || []).join('、') || '不限'}] · 类型 [${(ret.ktypes || []).join('、') || '不限'}] ·
        提示词 ${d.agent && d.agent.hasSystemPrompt ? '自定义' : '平台默认'}
      </div>
      <div class="tr-meta mono">
        生效模型参数：模型 ${esc(mdl.modelCode || 'default')}（${esc(mdl.sourceLabel || '')}） ·
        厂商模型名 ${esc(mdl.remoteModel || '—')} ·
        温度 ${esc(mdl.temperature)}（${esc(mdl.temperatureSource || '')}） ·
        topP ${mdl.topP == null ? '跟随厂商默认' : esc(mdl.topP)}（${esc(mdl.topPSource || '')}） ·
        最大输出 ${esc(mdl.maxTokens)}（${esc(mdl.maxTokensSource || '')}）
      </div>
      ${cits ? `<div class="tr-cites">引用：${cits}</div>` : ''}
      <table class="docs cl-table tr-table">
        <thead><tr><th>#</th><th>切片</th><th>部门</th><th>类型</th><th>标题路径</th><th>相似度</th></tr></thead>
        <tbody>${sources.map(s => `<tr>
          <td>${s.n}</td><td class="mono">${esc(s.chunkId)}</td><td>${esc(s.dept)}</td>
          <td>${esc(s.ktype)}</td><td class="cl-q">${esc(s.titlePath || '—')}</td><td>${s.cos}</td>
        </tr>`).join('')}</tbody>
      </table>`;
    box.hidden = false;
  }

  async function runTest() {
    const msg = el('ag-test-msg');
    const q = el('ag-test-q').value.trim();
    if (!editing) return;
    if (!q) { msg.className = 'cl-note err'; msg.textContent = '请输入测试问题'; return; }

    const btn = el('ag-test-run');
    btn.disabled = true;
    msg.className = 'cl-note';
    msg.textContent = '测试中…（首次调用需加载向量与精排模型，可能较慢）';
    try {
      const data = await api(`/api/admin/agents/${editing.agentId}/test`,
        jsonBody({ question: q, role: el('ag-test-role').value || null }));
      renderTestResult(data);
      msg.textContent = '测试完成（未写入会话日志）。';
    } catch (e) {
      msg.className = 'cl-note err';
      msg.textContent = '测试失败：' + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 弹窗·基础
  function renderRoleChecks(selected) {
    const box = el('ag-f-roles');
    const chosen = new Set(selected && selected.length ? selected : ['全部']);
    box.innerHTML = roles
      .map(r => `<label><input type="checkbox" value="${esc(r)}" ${chosen.has(r) ? 'checked' : ''}> ${esc(r)}</label>`)
      .join('');
  }

  function renderPromptPane(agent) {
    const ta = el('ag-f-prompt');
    ta.value = agent ? (agent.systemPrompt || '') : '';
    const def = el('ag-f-prompt-default');
    def.textContent = defaultPrompt || '（未取到平台默认提示词）';
    def.hidden = true;
    el('ag-prompt-default-toggle').textContent = '查看平台默认提示词';
    updatePromptCount();
  }

  function updatePromptCount() {
    const ta = el('ag-f-prompt');
    const c = el('ag-f-prompt-count');
    if (!ta || !c) return;
    const n = ta.value.length;
    c.textContent = n === 0
      ? `未填写（0 字符）→ 使用平台默认提示词（${defaultPrompt.length} 字符）`
      : `${n} / 20000 字符`;
  }

  function openModal(agent, tab) {
    editing = agent;
    el('ag-modal-title').textContent = agent ? `编辑助手 · ${agent.agentName}` : '新建助手';
    el('ag-modal-msg').textContent = '';
    el('ag-modal-msg').className = 'msg-line';

    el('ag-f-code').value = agent ? agent.agentCode : '';
    el('ag-f-code').disabled = false;
    el('ag-f-name').value = agent ? agent.agentName : '';
    el('ag-f-type').value = agent ? agent.agentType : 'chat';
    el('ag-f-desc').value = agent ? agent.description : '';
    el('ag-f-guides').value = agent && Array.isArray(agent.guideQuestions)
      ? agent.guideQuestions.join('\n') : '';
    el('ag-f-icon').value = agent ? agent.iconUrl : '🤖';
    el('ag-f-stream').checked = agent ? !!agent.streamEnabled : true;
    el('ag-f-status').checked = agent ? agent.status === 1 : true;
    el('ag-f-risk').value = agent ? agent.riskReply : '';
    el('ag-f-fallback').value = agent ? agent.fallbackReply : '';
    renderRoleChecks(agent ? agent.visibleRoles : ['全部']);
    renderPromptPane(agent);

    // 任务 2.4（M-A-06）：模型绑定与生成参数。
    // 三个参数为 null 时**留空输入框**（而不是显示平台默认值）——
    // 显示具体数字会让运营以为「这个助手已显式配置」，从而看不出「沿用全局」的语义。
    renderModelOptions(agent ? agent.modelCode : 'default');
    el('ag-f-temp').value = agent && agent.temperature != null ? agent.temperature : '';
    el('ag-f-topp').value = agent && agent.topP != null ? agent.topP : '';
    el('ag-f-maxtok').value = agent && agent.maxTokens != null ? agent.maxTokens : '';
    updateModelHints();

    const rc = (agent && agent.retrievalConfig) || {};
    el('ag-f-topk').value = rc.topK || 0;
    el('ag-f-rerank').value = rc.rerankTopn || 0;
    el('ag-f-minscore').value = rc.minScore || 0;
    retrievalSel = { depts: (rc.depts || []).slice(), ktypes: (rc.ktypes || []).slice() };
    renderRetrievalPickers();

    el('ag-test-q').value = '';
    el('ag-test-result').hidden = true;
    el('ag-test-result').innerHTML = '';

    el('ag-modal').hidden = false;
    switchTab(tab || 'basic');
    if (tab !== 'test') el('ag-f-name').focus();
  }

  function closeModal() {
    el('ag-modal').hidden = true;
    editing = null;
  }

  function collectForm() {
    const selectedRoles = [...el('ag-f-roles').querySelectorAll('input:checked')].map(c => c.value);
    // 生成参数（任务 2.4）：空输入框 → **显式 null**（= 沿用平台默认）。
    // 必须显式传 null，因为后端 PATCH 是「未传即不改」的增量语义——
    // 不传的话「清空参数」这个动作根本表达不出来（见 AgentUpdateRequest.NULLABLE_KEYS）。
    const num = v => (v === '' ? null : Number(v));
    return {
      agentCode: el('ag-f-code').value.trim(),
      agentName: el('ag-f-name').value.trim(),
      agentType: el('ag-f-type').value,
      description: el('ag-f-desc').value.trim(),
      guideQuestions: el('ag-f-guides').value
        .split('\n').map(s => s.trim()).filter(Boolean),
      iconUrl: el('ag-f-icon').value.trim(),
      visibleRoles: selectedRoles,
      streamEnabled: el('ag-f-stream').checked ? 1 : 0,
      status: el('ag-f-status').checked ? 1 : 0,
      riskReply: el('ag-f-risk').value.trim(),
      fallbackReply: el('ag-f-fallback').value.trim(),
      systemPrompt: el('ag-f-prompt').value,
      modelCode: el('ag-f-model').value || 'default',
      temperature: num(el('ag-f-temp').value),
      topP: num(el('ag-f-topp').value),
      maxTokens: num(el('ag-f-maxtok').value),
    };
  }

  async function save() {
    const body = collectForm();
    const retrieval = currentRetrieval();
    const msg = el('ag-modal-msg');
    if (!body.agentName) { msg.textContent = '请填写助手名称'; msg.className = 'msg-line err'; switchTab('basic'); return; }
    if (!body.agentCode) { msg.textContent = '请填写助手编码'; msg.className = 'msg-line err'; switchTab('basic'); return; }
    if (!body.visibleRoles.length) { msg.textContent = '请至少选择一个可见角色'; msg.className = 'msg-line err'; switchTab('basic'); return; }
    if (body.guideQuestions.length > 6) { msg.textContent = '场景引导问题最多 6 条'; msg.className = 'msg-line err'; switchTab('basic'); return; }
    if (body.guideQuestions.some(q => q.length > 120)) { msg.textContent = '引导问题单条最长 120 字'; msg.className = 'msg-line err'; switchTab('basic'); return; }

    const btn = el('ag-save');
    btn.disabled = true;
    msg.textContent = '保存中…';
    msg.className = 'msg-line';
    try {
      if (editing) {
        await api(`/api/admin/agents/${editing.agentId}`,
          { ...jsonBody(body), method: 'PATCH' });
        await api(`/api/admin/agents/${editing.agentId}/retrieval`,
          { ...jsonBody(retrieval), method: 'PUT' });
      } else {
        await api('/api/admin/agents', jsonBody({ ...body, retrievalConfig: retrieval }));
      }
      closeModal();
      await load();
    } catch (e) {
      msg.textContent = '保存失败：' + e.message;
      msg.className = 'msg-line err';
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 绑定
  function bind() {
    const modal = el('ag-modal');
    modal.querySelectorAll('[data-close]').forEach(node => { node.onclick = closeModal; });
    document.addEventListener('keydown', ev => {
      if (ev.key === 'Escape' && !modal.hidden) closeModal();
    });

    el('ag-tabs').querySelectorAll('.ag-tab').forEach(b => {
      b.onclick = () => switchTab(b.dataset.agt);
    });

    el('ag-save').onclick = save;
    el('ag-new').onclick = () => openModal(null, 'basic');
    el('ag-test-run').onclick = runTest;
    el('ag-f-prompt').oninput = updatePromptCount;
    el('ag-f-topk').oninput = updateRetrievalHints;
    el('ag-f-rerank').oninput = updateRetrievalHints;
    // 任务 2.4：模型与生成参数的实时提示
    el('ag-f-model').onchange = updateModelHints;
    el('ag-f-temp').oninput = updateModelHints;
    el('ag-f-topp').oninput = updateModelHints;
    el('ag-f-maxtok').oninput = updateModelHints;

    el('ag-prompt-default-toggle').onclick = () => {
      const def = el('ag-f-prompt-default');
      def.hidden = !def.hidden;
      el('ag-prompt-default-toggle').textContent = def.hidden ? '查看平台默认提示词' : '收起平台默认提示词';
    };

    const q = el('ag-q');
    q.onkeydown = ev => {
      if (ev.key === 'Enter') { keyword = q.value.trim(); load(); }
    };
    q.oninput = () => {
      // 清空输入框即时恢复全量，避免用户以为「卡住」
      if (q.value.trim() === '' && keyword !== '') { keyword = ''; load(); }
    };
  }

  // ---------------------------------------------------------------- 入口
  window.initAgentPage = function initAgentPage() {
    // 返回 Promise：admin.js 的页签切换据此显示/收起「加载中」提示
    if (booted) return Promise.resolve();
    booted = true;
    renderStatusTabs();
    bind();
    return loadRoles().then(load);
  };
})();
