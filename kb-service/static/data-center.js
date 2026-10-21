/* 能小智 · 数据中台（任务 4.2 / I-02、M-SYS-02）
 *
 * 对接：
 *   GET  /api/admin/data-center/status
 *   GET  /api/admin/data-center/reports | reports/{batchId}
 *   GET  /api/admin/data-center/jobs
 *   GET  /api/admin/data-center/orgs | users
 *   POST /api/admin/data-center/preview | report | sync | run-due
 *
 * 所有服务端文本都用 textContent 写入，不拼接 innerHTML。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const DATE_FMT = new Intl.DateTimeFormat('zh-CN', { hour12: false });
  const ROLE_LABEL = { 1: '本科生', 2: '研究生', 3: '教职工', 4: '离退休', 5: '访客' };

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

  function fmtTime(raw, short) {
    if (!raw) return '-';
    const date = new Date(String(raw).replace(' ', 'T'));
    if (isNaN(date.getTime())) return String(raw).slice(0, short ? 16 : 19);
    const text = DATE_FMT.format(date);
    return short ? text.replace(' ', ' ').slice(5) : text;
  }

  function msg(text, kind) {
    const line = $('dc-msg');
    line.textContent = text || '';
    line.className = 'msg-line' + (kind ? ' ' + kind : '');
  }

  function yesterday() {
    const day = new Date();
    day.setDate(day.getDate() - 1);
    const pad = (n) => String(n).padStart(2, '0');
    return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
  }

  function showDetail(title, body) {
    $('dc-detail-title').textContent = title;
    $('dc-detail-body').textContent = body;
    $('dc-detail').hidden = false;
    $('dc-detail-mask').hidden = false;
  }

  function closeDetail() {
    $('dc-detail').hidden = true;
    $('dc-detail-mask').hidden = true;
  }

  function renderStatus(data) {
    $('dc-provider').textContent = data.provider === 'mock' ? 'mock' : 'http';
    $('dc-orgs').textContent = String((data.mirror || {}).orgs || 0);
    $('dc-users').textContent = String((data.mirror || {}).users || 0);
    const state = data.state || {};
    $('dc-last-report').textContent = state.lastReportDate || (state.lastReportAt ? fmtTime(state.lastReportAt, true) : '-');
    $('dc-last-sync').textContent = state.lastIncrementalAt ? fmtTime(state.lastIncrementalAt, true) : (state.orgCursor || '-');
    const scheduler = data.scheduler || {};
    $('dc-scheduler').textContent = scheduler.enabled ? '开' : '关';
    $('dc-scheduler').title = scheduler.enabled
      ? `每 ${scheduler.interval}s 检查；上报 ${scheduler.reportHour}:00 / 增量 ${scheduler.incrementalHour}:00 / 全量 周${scheduler.fullWeekday} ${scheduler.fullHour}:00`
      : '生产可通过 DATA_CENTER_SCHEDULER_ENABLED=1 开启，或由外部调度器调用接口';
  }

  function renderReports(items) {
    const tbody = $('dc-report-rows');
    tbody.replaceChildren();
    $('dc-report-empty').hidden = items.length > 0;
    for (const item of items) {
      const tr = document.createElement('tr');
      tr.append(
        el('td', 'dc-mono', item.batchId),
        el('td', '', item.kind === 'stats' ? '统计' : '聊天日志'),
        el('td', '', item.date),
        el('td', '', `v${item.revision}`),
        el('td', '', item.rows),
        el('td', '', item.status === 'delivered' ? '已投递' : (item.status === 'failed' ? '失败' : '待投递')),
        el('td', 'dc-mono', String(item.checksum || '').slice(0, 12)),
        el('td', '', fmtTime(item.updatedAt, true)),
      );
      const ops = document.createElement('td');
      const btn = el('button', 'btn-ghost dc-mini', '详情');
      btn.addEventListener('click', async () => {
        try {
          const data = await api(`/api/admin/data-center/reports/${encodeURIComponent(item.batchId)}`);
          showDetail(`批次 ${item.batchId}`, JSON.stringify(data.payload || data, null, 2));
        } catch (err) { msg(err.message, 'error'); }
      });
      ops.append(btn);
      tr.append(ops);
      tbody.append(tr);
    }
  }

  function renderJobs(items) {
    const tbody = $('dc-job-rows');
    tbody.replaceChildren();
    $('dc-job-empty').hidden = items.length > 0;
    for (const item of items) {
      const tr = document.createElement('tr');
      const statusLabel = { success: '成功', running: '执行中', failed: '失败', skipped: '跳过' }[item.status] || item.status;
      tr.append(
        el('td', '', item.jobId),
        el('td', '', item.jobType === 'report' ? '上报' : '组织同步'),
        el('td', '', item.mode),
        el('td', '', statusLabel),
        el('td', '', item.created),
        el('td', '', item.updated),
        el('td', '', item.skipped),
        el('td', '', item.failed),
        el('td', '', fmtTime(item.startedAt, true)),
        el('td', '', fmtTime(item.finishedAt, true)),
      );
      if (item.error) tr.title = item.error;
      tbody.append(tr);
    }
  }

  function renderOrgs(items) {
    const tbody = $('dc-org-rows');
    tbody.replaceChildren();
    $('dc-org-empty').hidden = items.length > 0;
    for (const item of items) {
      const tr = document.createElement('tr');
      tr.append(
        el('td', 'dc-mono', item.orgCode),
        el('td', '', item.orgName),
        el('td', 'dc-mono', item.parentCode || '—'),
        el('td', 'dc-mono', item.orgPath || '—'),
        el('td', '', item.sortNo),
        el('td', '', item.status ? '启用' : '停用'),
        el('td', '', fmtTime(item.syncedAt, true)),
      );
      tbody.append(tr);
    }
  }

  function renderUsers(items) {
    const tbody = $('dc-user-rows');
    tbody.replaceChildren();
    $('dc-user-empty').hidden = items.length > 0;
    for (const item of items) {
      const tr = document.createElement('tr');
      tr.append(
        el('td', 'dc-mono', item.userNo),
        el('td', '', item.realName),
        el('td', '', ROLE_LABEL[item.idType] || item.idType),
        el('td', 'dc-mono', item.orgCode || '—'),
        el('td', '', item.email || '—'),
        el('td', '', item.status ? '在册' : '注销'),
        el('td', '', fmtTime(item.syncedAt || item.updatedAt, true)),
      );
      tbody.append(tr);
    }
  }

  async function loadAll() {
    try {
      const [status, reports, jobs, orgs, users] = await Promise.all([
        api('/api/admin/data-center/status'),
        api('/api/admin/data-center/reports?limit=30'),
        api('/api/admin/data-center/jobs?limit=30'),
        api('/api/admin/data-center/orgs?limit=200'),
        api('/api/admin/data-center/users?limit=200'),
      ]);
      renderStatus(status);
      renderReports(reports.items || []);
      renderJobs(jobs.items || []);
      renderOrgs(orgs.items || []);
      renderUsers(users.items || []);
    } catch (err) {
      msg(err.message, 'error');
    }
  }

  async function runAction(button, task, label) {
    if (button) button.disabled = true;
    msg(`${label}中…`);
    try {
      const data = await task();
      msg(`${label}完成`, 'ok');
      await loadAll();
      return data;
    } catch (err) {
      msg(`${label}失败：${err.message}`, 'error');
      return null;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function bind() {
    $('dc-report-date').value = yesterday();

    $('dc-preview').addEventListener('click', () => runAction($('dc-preview'), async () => {
      const data = await api('/api/admin/data-center/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: $('dc-report-date').value, kind: $('dc-report-kind').value }),
      });
      const summary = (data.items || []).map((item) => ({
        kind: item.kind,
        batchId: item.batchId,
        rows: item.rows,
        counts: item.counts,
        statistics: item.statistics,
        checksum: item.checksum,
        sample: item.sample,
      }));
      showDetail(`上报预览 · ${data.date}`, JSON.stringify({ schema: data.schema, truncated: data.truncated, includeContent: data.includeContent, items: summary }, null, 2));
      return data;
    }, '预览'));

    $('dc-run-report').addEventListener('click', () => runAction($('dc-run-report'), async () => {
      const data = await api('/api/admin/data-center/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: $('dc-report-date').value, kind: $('dc-report-kind').value }),
      });
      const lines = (data.batches || []).map((b) => `${b.kind}: ${b.skipped ? '已是最新（跳过）' : '已投递'} · v${b.revision} · ${b.rows} 条`);
      showDetail(`上报任务 #${data.jobId}`, lines.join('\n'));
      return data;
    }, '上报'));

    $('dc-sync-inc').addEventListener('click', () => runAction($('dc-sync-inc'), () => api('/api/admin/data-center/sync', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'incremental' }),
    }), '增量同步'));

    $('dc-sync-full').addEventListener('click', () => runAction($('dc-sync-full'), () => api('/api/admin/data-center/sync', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'full' }),
    }), '全量同步'));

    $('dc-run-due').addEventListener('click', () => runAction($('dc-run-due'), () => api('/api/admin/data-center/run-due', { method: 'POST' }), '补跑'));

    $('dc-refresh').addEventListener('click', () => loadAll());
    $('dc-detail-close').addEventListener('click', closeDetail);
    $('dc-detail-mask').addEventListener('click', closeDetail);
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDetail(); });

    document.querySelectorAll('.dc-tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.dc-tab').forEach((t) => t.classList.toggle('active', t === tab));
        document.querySelectorAll('.dc-panel').forEach((panel) => {
          panel.classList.toggle('active', panel.id === `dc-panel-${tab.dataset.panel}`);
        });
      });
    });
  }

  bind();
  loadAll();
})();
