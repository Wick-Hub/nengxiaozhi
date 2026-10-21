/* 会话日志管理页（任务 2.1）
 *
 * 对接接口（全部在 /api/admin 前缀下，AUTH_ENABLED=true 时需管理员）：
 *   GET /api/admin/chatlogs/filters  筛选项
 *   GET /api/admin/chatlogs          问答视图（一行 = 一问一答）
 *   GET /api/admin/chatlogs/users    咨询用户视图（一行 = 一个会话）
 *   GET /api/admin/chatlogs/export   CSV 导出（与页面同筛选口径）
 *
 * 响应契约：{ok, data, error}；data = {items,total,page,pageSize,totalPages,scanned,scanLimit,store}
 *
 * 由 admin.js 的页签切换调用 window.initChatlogPage()，只初始化一次（懒加载）。
 */
(() => {
  'use strict';

  const VIEWS = [
    ['qa', '问答明细'],
    ['users', '咨询用户'],
  ];

  // 列定义：{k 取值键, t 表头, cls 单元格附加类, f 渲染函数}
  const COLUMNS = {
    qa: [
      { t: '回答时间', f: r => fmtTime(r.answeredAt) },
      { t: '咨询用户', f: r => esc(r.role || '—') },
      { t: '智能体', f: r => esc(r.agentName || '—') },
      { t: '问题', cls: 'cl-q', f: r => cellText(r.question) },
      { t: '回答', cls: 'cl-a', f: r => cellText(r.answer) },
      { t: '置信度', f: r => (r.confidence == null ? '—' : Number(r.confidence).toFixed(3)) },
      { t: '引用', f: r => (r.citationCount == null ? '—' : r.citationCount) },
      { t: '拒答', f: r => (r.refused
        ? '<span class="badge s3">是</span>'
        : '<span class="badge s2">否</span>') },
      { t: '总token', f: r => (r.totalTokens == null ? '—' : r.totalTokens) },
      { t: '模型', f: r => esc(r.model || '—') },
      { t: '耗时ms', f: r => (r.latencyMs == null ? '—' : r.latencyMs) },
    ],
    users: [
      { t: '会话ID', cls: 'mono', f: r => esc(r.sessionId) },
      { t: '咨询用户', f: r => esc(r.role || '—') },
      { t: '智能体', f: r => esc(r.agentName || '—') },
      { t: '渠道', f: r => esc(r.channelName || '—') },
      { t: '首次时间', f: r => fmtTime(r.createdAt) },
      { t: '末次时间', f: r => fmtTime(r.lastAt) },
      { t: '问数', f: r => r.questionCount },
      { t: '拒答数', f: r => r.refusedCount },
      { t: '总token', f: r => (r.totalTokens == null ? '—' : r.totalTokens) },
      { t: '最后提问', cls: 'cl-q', f: r => cellText(r.lastQuestion) },
    ],
  };

  const state = {
    view: 'qa',
    agentId: '',
    channelId: '',
    role: '',
    dateFrom: '',
    dateTo: '',
    q: '',
    page: 1,
    pageSize: 20,
  };

  let booted = false;
  let lastMeta = { scanned: 0, scanLimit: 0, store: '—', total: 0, totalPages: 1 };

  // ---------------------------------------------------------------- 工具
  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function fmtTime(s) {
    if (!s) return '—';
    const d = new Date(s);
    if (isNaN(d)) return esc(s);
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  // 长文本单元格：截断显示，完整内容放 title，避免撑破表格
  function cellText(s) {
    const text = String(s ?? '');
    if (!text) return '<span class="cl-dim">—</span>';
    const short = text.length > 60 ? text.slice(0, 60) + '…' : text;
    return `<span class="cl-text" title="${esc(text)}">${esc(short)}</span>`;
  }

  function authFetch(url, opts) {
    return (window.NXZAuth ? window.NXZAuth.fetch : fetch)(url, opts);
  }

  async function api(url) {
    const resp = await authFetch(url, { credentials: 'same-origin' });
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

  function el(id) {
    return document.getElementById(id);
  }

  function queryString(extra) {
    const p = new URLSearchParams();
    if (state.agentId !== '') p.set('agentId', state.agentId);
    if (state.channelId !== '') p.set('channelId', state.channelId);
    if (state.role) p.set('role', state.role);
    if (state.dateFrom) p.set('dateFrom', state.dateFrom);
    if (state.dateTo) p.set('dateTo', state.dateTo);
    if (state.q) p.set('q', state.q);
    if (extra) for (const [k, v] of Object.entries(extra)) p.set(k, v);
    return p.toString();
  }

  // ---------------------------------------------------------------- 渲染
  function renderViewTabs() {
    const box = el('cl-views');
    if (!box) return;
    box.innerHTML = '';
    for (const [v, label] of VIEWS) {
      const b = document.createElement('button');
      b.className = 'f' + (state.view === v ? ' active' : '');
      b.textContent = label;
      b.onclick = () => {
        if (state.view === v) return;
        state.view = v;
        state.page = 1;
        renderViewTabs();
        load();
      };
      box.appendChild(b);
    }
  }

  function renderNote(meta) {
    const box = el('cl-note');
    if (!box) return;
    const truncated = meta.scanned >= meta.scanLimit;
    const storeName = meta.store === 'mysql' ? 'MySQL' : 'JSON 文件';
    const parts = [
      `存储：${esc(storeName)}`,
      `本次扫描消息 ${meta.scanned} / 上限 ${meta.scanLimit}`,
      `命中 ${meta.total} 条`,
    ];
    if (truncated) parts.push('已达扫描上限，请用时间范围或筛选项收窄窗口');
    if (meta.store !== 'mysql') parts.push('JSON 模式无智能体/渠道/模型/token 落库，对应列显示 —');
    box.className = 'cl-note' + (truncated ? ' warn' : '');
    box.textContent = parts.join(' · ');
  }

  function renderTable(items) {
    const thead = el('cl-thead');
    const tbody = el('cl-tbody');
    const emptyEl = el('cl-empty');
    const cols = COLUMNS[state.view];

    thead.innerHTML = `<tr>${cols.map(c => `<th>${c.t}</th>`).join('')}</tr>`;
    tbody.innerHTML = '';
    emptyEl.hidden = items.length > 0;

    for (const row of items) {
      const tr = document.createElement('tr');
      tr.innerHTML = cols
        .map(c => `<td${c.cls ? ` class="${c.cls}"` : ''}>${c.f(row)}</td>`)
        .join('');
      tbody.appendChild(tr);
    }
  }

  function renderPager(meta) {
    const box = el('cl-pager');
    if (!box) return;
    const page = meta.page;
    const pages = meta.totalPages;
    box.innerHTML = `
      <button class="btn-ghost" id="cl-prev" ${page <= 1 ? 'disabled' : ''}>← 上一页</button>
      <span class="cl-page-info">第 ${page} / ${pages} 页</span>
      <button class="btn-ghost" id="cl-next" ${page >= pages ? 'disabled' : ''}>下一页 →</button>`;
    const prev = el('cl-prev');
    const next = el('cl-next');
    if (prev) prev.onclick = () => { state.page = Math.max(1, page - 1); load(); };
    if (next) next.onclick = () => { state.page = Math.min(pages, page + 1); load(); };
  }

  function renderCount(meta) {
    const c = el('cl-count');
    if (c) c.textContent = `共 ${meta.total} 条`;
  }

  // ---------------------------------------------------------------- 取数
  async function load() {
    const tbody = el('cl-tbody');
    const emptyEl = el('cl-empty');
    emptyEl.hidden = true;
    tbody.innerHTML = `<tr><td class="cl-loading" colspan="12">加载中…</td></tr>`;
    try {
      const path = state.view === 'users' ? '/api/admin/chatlogs/users' : '/api/admin/chatlogs';
      const qs = queryString({ page: state.page, pageSize: state.pageSize });
      const data = await api(`${path}?${qs}`);
      lastMeta = data;
      renderCount(data);
      renderNote(data);
      renderTable(data.items || []);
      renderPager(data);
    } catch (e) {
      tbody.innerHTML = '';
      emptyEl.hidden = false;
      emptyEl.textContent = '加载失败：' + e.message;
    }
  }

  // ---------------------------------------------------------------- 筛选条
  function fillSelect(node, options, placeholder) {
    if (!node) return;
    const keep = node.value;
    node.innerHTML = `<option value="">${esc(placeholder)}</option>` +
      options.map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');
    node.value = keep;
  }

  async function loadFilters() {
    try {
      const data = await api('/api/admin/chatlogs/filters');
      fillSelect(
        el('cl-agent'),
        (data.agents || []).map(a => ({
          value: a.agentId == null ? '' : String(a.agentId),
          label: a.agentName || `智能体 ${a.agentId}`,
        })),
        '全部智能体'
      );
      fillSelect(
        el('cl-channel'),
        (data.channels || []).map(c => ({
          value: c.channelId == null ? '' : String(c.channelId),
          label: c.channelName || `渠道 ${c.channelId}`,
        })),
        '全部渠道'
      );
      fillSelect(
        el('cl-role'),
        (data.roles || []).map(r => ({ value: r, label: r })),
        '全部角色'
      );
    } catch (e) {
      const note = el('cl-note');
      if (note) {
        note.className = 'cl-note warn';
        note.textContent = '筛选项加载失败：' + e.message;
      }
    }
  }

  function readFilters() {
    state.agentId = el('cl-agent') ? el('cl-agent').value : '';
    state.channelId = el('cl-channel') ? el('cl-channel').value : '';
    state.role = el('cl-role') ? el('cl-role').value : '';
    state.dateFrom = el('cl-from') ? el('cl-from').value : '';
    state.dateTo = el('cl-to') ? el('cl-to').value : '';
    state.q = el('cl-q') ? el('cl-q').value.trim() : '';
  }

  function exportCsv() {
    // 带鉴权下载：fetch + blob 落盘（AUTH_ENABLED=true 时导航请求无法携带 Bearer）
    const qs = queryString({ view: state.view });
    const url = `/api/admin/chatlogs/export?${qs}`;
    if (window.NXZAuth) {
      window.NXZAuth.download(url).catch((e) => alert('导出失败：' + e.message));
    } else {
      window.location.href = url;
    }
  }

  function bind() {
    const search = el('cl-search');
    const reset = el('cl-reset');
    const exp = el('cl-export');
    const q = el('cl-q');
    if (search) {
      search.onclick = () => { readFilters(); state.page = 1; load(); };
    }
    if (q) {
      q.onkeydown = ev => {
        if (ev.key === 'Enter') { readFilters(); state.page = 1; load(); }
      };
    }
    if (reset) {
      reset.onclick = () => {
        for (const id of ['cl-agent', 'cl-channel', 'cl-role', 'cl-from', 'cl-to', 'cl-q']) {
          const n = el(id);
          if (n) n.value = '';
        }
        readFilters();
        state.page = 1;
        load();
      };
    }
    if (exp) exp.onclick = exportCsv;
  }

  // ---------------------------------------------------------------- 入口
  window.initChatlogPage = function initChatlogPage() {
    // 返回 Promise：admin.js 的页签切换据此显示/收起「加载中」提示
    if (booted) return Promise.resolve();
    booted = true;
    renderViewTabs();
    bind();
    return loadFilters().then(load);
  };
})();
