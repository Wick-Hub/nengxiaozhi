/* 统计与运营分析（任务 3.10 / M-T-01～09）
 *
 * 接口（/api/admin 前缀，AUTH_ENABLED=true 时需 stats.view / stats.export）：
 *   GET /api/admin/stats/filters    渠道/助手/角色下拉
 *   GET /api/admin/stats/overview   指标卡 + 按天趋势 + 渠道/助手统计
 *   GET /api/admin/stats/rankings   热门知识/分类/设备/情绪/应用
 *   GET /api/admin/stats/export     按当前筛选导出 CSV
 *
 * 口径：与「会话日志」共用同一筛选与扫描窗口，总数可对账；无数据维度显示空态。
 * 由 admin.js 页签切换调用 window.initStatsPage()，只初始化一次。
 */
(() => {
  'use strict';

  const state = { agentId: '', channelId: '', role: '', dateFrom: '', dateTo: '' };
  let booted = false;

  // ---------------------------------------------------------------- 工具
  function el(id) {
    return document.getElementById(id);
  }

  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function num(v) {
    return v == null ? '—' : Number(v).toLocaleString('zh-CN');
  }

  function pct(v) {
    return v == null ? '—' : (Number(v) * 100).toFixed(1) + '%';
  }

  function authFetch(url, opts) {
    return (window.NXZAuth ? window.NXZAuth.fetch : fetch)(url, opts);
  }

  async function api(url) {
    const resp = await authFetch(url, { credentials: 'same-origin' });
    if (resp.status === 401) throw new Error('未登录或登录已失效，请先登录管理员账号');
    if (resp.status === 403) throw new Error('当前账号无统计查看权限');
    let json;
    try {
      json = await resp.json();
    } catch (e) {
      throw new Error(`响应不是合法 JSON（HTTP ${resp.status}）`);
    }
    if (!json.ok) throw new Error(json.error || `请求失败（HTTP ${resp.status}）`);
    return json.data;
  }

  function dayStr(d) {
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  function queryString() {
    const p = new URLSearchParams();
    for (const key of ['agentId', 'channelId', 'role', 'dateFrom', 'dateTo']) {
      if (state[key] !== '') p.set(key, state[key]);
    }
    return p.toString();
  }

  function fillSelect(node, options, placeholder) {
    if (!node) return;
    const keep = node.value;
    node.innerHTML = `<option value="">${esc(placeholder)}</option>` +
      options.map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');
    node.value = keep;
  }

  // ---------------------------------------------------------------- 渲染
  function renderCards(s) {
    const box = el('st-cards');
    if (!box) return;
    const empty = !s || s.replies === 0;
    const cards = [
      { label: '提问', value: empty ? '—' : num(s.questions), sub: empty ? '暂无数据' : '一问一答计一次' },
      { label: '回复', value: empty ? '—' : num(s.replies), sub: empty ? '暂无数据' : 'AI 消息数' },
      { label: '未答', value: empty ? '—' : num(s.unanswered), sub: empty ? '暂无数据' : '拒答 / 兜底回复' },
      { label: '访问', value: empty ? '—' : num(s.visits), sub: empty ? '暂无数据' : '会话数' },
      {
        label: '人次',
        value: empty ? '—' : num(s.persons),
        sub: empty ? '暂无数据' : (s.persons === s.visits ? '按会话计' : '用户 / 访客去重'),
      },
      {
        label: '应答成功率',
        value: empty ? '—' : pct(s.successRate),
        sub: empty ? '暂无数据' : `已答 ${num(s.answered)}`,
      },
    ];
    box.innerHTML = cards.map(c => `
      <div class="st-card">
        <span class="st-card-label">${esc(c.label)}</span>
        <b class="st-card-value">${esc(c.value)}</b>
        <span class="st-card-sub">${esc(c.sub)}</span>
      </div>`).join('');
  }

  function renderTrend(rows) {
    const box = el('st-trend');
    const emptyEl = el('st-trend-empty');
    const noteEl = el('st-trend-note');
    if (!box) return;
    const has = (rows || []).length > 0;
    if (emptyEl) emptyEl.hidden = has;
    box.hidden = !has;
    if (!has) {
      box.innerHTML = '';
      if (noteEl) noteEl.textContent = '';
      return;
    }
    const max = Math.max(...rows.map(r => r.questions), 1);
    box.innerHTML = rows.map(r => {
      const h = r.questions > 0 ? Math.max(6, Math.round((r.questions / max) * 100)) : 2;
      return `<div class="st-bar-col" title="${esc(r.date)} · 提问 ${r.questions} · 未答 ${r.unanswered} · 会话 ${r.sessions}">
        <div class="st-bar-wrap"><div class="st-bar" style="height:${h}%"><span>${r.questions}</span></div></div>
        <em>${esc(String(r.date).slice(5))}</em>
      </div>`;
    }).join('');
    if (noteEl) noteEl.textContent = `共 ${rows.length} 天 · 柱高按当天提问数`;
  }

  function renderTable(tbodyId, emptyId, rows, columns) {
    const tbody = el(tbodyId);
    const emptyEl = el(emptyId);
    if (!tbody) return;
    const items = rows || [];
    if (emptyEl) emptyEl.hidden = items.length > 0;
    tbody.innerHTML = items.map(row => `<tr>${columns.map(c => `<td>${c.f(row)}</td>`).join('')}</tr>`).join('');
  }

  function renderRankings(overview, rankings) {
    const channels = overview.channels || [];
    renderTable('st-channels', 'st-channels-empty', channels, [
      { f: r => esc(r.name) },
      { f: r => num(r.questions) },
      { f: r => num(r.unanswered) },
      { f: r => num(r.sessions) },
      { f: r => pct(r.successRate) },
      { f: r => pct(r.share) },
    ]);

    const agents = overview.agents || [];
    renderTable('st-agents', 'st-agents-empty', agents, [
      { f: r => esc(r.name) },
      { f: r => num(r.questions) },
      { f: r => num(r.unanswered) },
      { f: r => num(r.sessions) },
      { f: r => pct(r.successRate) },
      { f: r => pct(r.share) },
    ]);

    const knowledge = rankings.knowledge || [];
    renderTable('st-knowledge', 'st-knowledge-empty', knowledge, [
      { f: r => esc(r.docTitle || `文档 #${r.docId}`) },
      { f: r => esc(`${r.dept || '未分类'} / ${r.ktype || '未分类'}`) },
      { f: r => num(r.citations) },
      { f: r => num(r.sessions) },
      { f: r => pct(r.share) },
    ]);
    if (el('st-kn-count')) el('st-kn-count').textContent = `共 ${rankings.knowledgeTotal || 0} 篇`;
    if (el('st-kn-note')) {
      el('st-kn-note').textContent = rankings.notes && rankings.notes.knowledge
        ? rankings.notes.knowledge
        : `筛选范围内共 ${num(rankings.citations)} 次引用`;
    }

    const categories = rankings.categories || [];
    renderTable('st-categories', 'st-categories-empty', categories, [
      { f: r => esc(r.label || `${r.dept} / ${r.ktype}`) },
      { f: r => num(r.citations) },
      { f: r => num(r.sessions) },
      { f: r => pct(r.share) },
    ]);
    if (el('st-cat-count')) el('st-cat-count').textContent = `共 ${rankings.categoryTotal || 0} 类`;
  }

  function renderBars(containerId, emptyId, noteId, rows, note, valueKey, labelKey) {
    const box = el(containerId);
    const emptyEl = el(emptyId);
    const noteEl = el(noteId);
    const items = rows || [];
    if (box) {
      box.innerHTML = items.map(r => `
        <div class="st-bar-row">
          <span class="st-bar-label">${esc(r[labelKey])}</span>
          <div class="st-bar-track"><i style="width:${Math.max(2, Math.round((r.share || 0) * 100))}%"></i></div>
          <b>${num(r[valueKey])}</b>
          <em>${pct(r.share)}</em>
        </div>`).join('');
    }
    if (emptyEl) emptyEl.hidden = items.length > 0;
    if (noteEl) noteEl.textContent = note || '';
  }

  function renderDistributions(data) {
    renderBars('st-devices', 'st-devices-empty', 'st-devices-note',
      data.devices, (data.notes || {}).devices, 'count', 'label');
    renderBars('st-sentiments', 'st-sentiments-empty', 'st-sentiments-note',
      data.sentiments, (data.notes || {}).sentiments, 'count', 'label');
    renderBars('st-apps', 'st-apps-empty', 'st-apps-note',
      data.apps, (data.notes || {}).apps, 'count', 'name');
  }

  function renderNote(overview, rankings) {
    const box = el('st-note');
    if (!box) return;
    const window_ = overview.window || {};
    const storeName = window_.store === 'mysql' ? 'MySQL' : 'JSON 文件';
    const parts = [
      `存储：${storeName}`,
      `扫描消息 ${window_.scanned || 0} / 上限 ${window_.scanLimit || 0}`,
    ];
    if (window_.truncated) parts.push('已达扫描上限，请用时间或渠道收窄');
    for (const note of overview.notes || []) parts.push(note);
    box.className = 'cl-note' + (window_.truncated ? ' warn' : '');
    box.textContent = parts.join(' · ');
    if (el('st-sub')) {
      el('st-sub').textContent = `共 ${num(overview.summary && overview.summary.replies)} 条问答`;
    }
    if (rankings && rankings.window && rankings.window.truncated && !window_.truncated) {
      box.className = 'cl-note warn';
      box.textContent += ' · 排行与概览共用同一扫描窗口';
    }
  }

  function renderError(message) {
    for (const id of ['st-cards', 'st-trend']) {
      const node = el(id);
      if (node) node.innerHTML = '';
    }
    for (const id of ['st-trend-empty', 'st-channels-empty', 'st-agents-empty',
      'st-knowledge-empty', 'st-categories-empty', 'st-devices-empty',
      'st-sentiments-empty', 'st-apps-empty']) {
      const node = el(id);
      if (node) node.hidden = false;
    }
    const box = el('st-note');
    if (box) {
      box.className = 'cl-note warn';
      box.textContent = '加载失败：' + message;
    }
  }

  // ---------------------------------------------------------------- 取数
  async function load() {
    try {
      const qs = queryString();
      const suffix = qs ? `?${qs}` : '';
      const [overview, rankings] = await Promise.all([
        api('/api/admin/stats/overview' + suffix),
        api('/api/admin/stats/rankings' + suffix + (qs ? '&' : '?') + 'limit=10'),
      ]);
      renderNote(overview, rankings);
      renderCards(overview.summary || {});
      renderTrend(overview.trend || []);
      renderRankings(overview, rankings);
      renderDistributions(rankings);
    } catch (e) {
      renderError(e.message);
    }
  }

  async function loadFilters() {
    try {
      const data = await api('/api/admin/stats/filters');
      fillSelect(
        el('st-agent'),
        (data.agents || []).map(a => ({
          value: a.agentId == null ? '' : String(a.agentId),
          label: a.agentName || `智能体 ${a.agentId}`,
        })),
        '全部助手'
      );
      fillSelect(
        el('st-channel'),
        (data.channels || []).map(c => ({
          value: c.channelId == null ? '' : String(c.channelId),
          label: c.channelName || `渠道 ${c.channelId}`,
        })),
        '全部渠道'
      );
      fillSelect(
        el('st-role'),
        (data.roles || []).map(r => ({ value: r, label: r })),
        '全部角色'
      );
    } catch (e) {
      const box = el('st-note');
      if (box) {
        box.className = 'cl-note warn';
        box.textContent = '筛选项加载失败：' + e.message;
      }
    }
  }

  // ---------------------------------------------------------------- 操作
  function readFilters() {
    state.agentId = el('st-agent') ? el('st-agent').value : '';
    state.channelId = el('st-channel') ? el('st-channel').value : '';
    state.role = el('st-role') ? el('st-role').value : '';
    state.dateFrom = el('st-from') ? el('st-from').value : '';
    state.dateTo = el('st-to') ? el('st-to').value : '';
  }

  function exportCsv(kind) {
    const qs = queryString();
    const url = `/api/admin/stats/export?kind=${encodeURIComponent(kind)}${qs ? '&' + qs : ''}`;
    if (window.NXZAuth && window.NXZAuth.download) {
      window.NXZAuth.download(url).catch(e => alert('导出失败：' + e.message));
    } else {
      window.location.href = url;
    }
  }

  function toggleFullscreen() {
    const board = el('st-board');
    if (!board) return;
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
      return;
    }
    const request = board.requestFullscreen || board.webkitRequestFullscreen;
    if (!request) {
      board.classList.toggle('st-fallback-big');
      return;
    }
    Promise.resolve(request.call(board)).catch(() => board.classList.toggle('st-fallback-big'));
  }

  function bind() {
    const search = el('st-search');
    const reset = el('st-reset');
    const full = el('st-full');
    if (search) search.onclick = () => { readFilters(); load(); };
    if (reset) {
      reset.onclick = () => {
        for (const id of ['st-agent', 'st-channel', 'st-role', 'st-from', 'st-to']) {
          const node = el(id);
          if (node) node.value = '';
        }
        readFilters();
        load();
      };
    }
    document.querySelectorAll('[data-kind]').forEach(btn => {
      btn.onclick = () => exportCsv(btn.dataset.kind);
    });
    const summaryBtn = el('st-export-summary');
    if (summaryBtn) summaryBtn.onclick = () => exportCsv('summary');
    if (full) full.onclick = toggleFullscreen;
    document.addEventListener('fullscreenchange', () => {
      if (full) full.textContent = document.fullscreenElement ? '退出全屏' : '全屏大屏';
    });
  }

  window.initStatsPage = function initStatsPage() {
    if (booted) return Promise.resolve();
    booted = true;
    const today = new Date();
    const from = new Date(today.getTime() - 6 * 24 * 3600 * 1000);
    if (el('st-from')) el('st-from').value = dayStr(from);
    if (el('st-to')) el('st-to').value = dayStr(today);
    readFilters();
    bind();
    return loadFilters().then(load);
  };
})();
