/* 能小智 · 通行分析（任务 4.4 / P-DQ-04、I-04）
 *
 * 对接：
 *   GET  /api/admin/access/overview
 *   GET  /api/admin/access/drilldown
 *   GET  /api/admin/access/events
 *   GET  /api/admin/access/export
 *   GET  /api/admin/access/report
 *   POST /api/admin/access/sync
 *
 * 所有服务端文本都用 textContent / DOM 节点写入，不拼接 innerHTML。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    data: null,
    page: 1,
    pageSize: 20,
    totalPages: 1,
    report: null,
    loading: false,
  };
  const numberFormat = new Intl.NumberFormat('zh-CN');

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

  function num(value) {
    return numberFormat.format(Number(value) || 0);
  }

  function fmtTime(raw, short) {
    if (!raw) return '-';
    const text = String(raw).replace(' ', 'T');
    const date = new Date(text);
    if (isNaN(date.getTime())) return String(raw);
    const pad = (n) => String(n).padStart(2, '0');
    const base = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
    return short ? base : `${date.getFullYear()}-${base}`;
  }

  function isoDay(date) {
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function initDates() {
    const end = new Date();
    const start = new Date(end.getFullYear(), end.getMonth(), end.getDate());
    start.setDate(start.getDate() - 29);
    $('ac-from').value = isoDay(start);
    $('ac-to').value = isoDay(end);
  }

  function msg(text, kind) {
    const line = $('ac-msg');
    line.textContent = text || '';
    line.className = 'msg-line' + (kind ? ' ' + kind : '');
  }

  function buildParams() {
    const params = new URLSearchParams();
    const add = (key, value) => {
      if (value !== '' && value !== null && value !== undefined) params.set(key, value);
    };
    add('dateFrom', $('ac-from').value);
    add('dateTo', $('ac-to').value);
    add('campusId', $('ac-campus').value);
    add('college', $('ac-college').value);
    add('gateId', $('ac-gate').value);
    add('role', $('ac-role').value);
    add('direction', $('ac-direction').value);
    add('abnormal', $('ac-abnormal-filter').value);
    add('q', $('ac-q').value.trim());
    return params;
  }

  function fillSelect(selectId, placeholder, items, valueOf, labelOf) {
    const select = $(selectId);
    const current = select.value;
    select.textContent = '';
    select.appendChild(new Option(placeholder, ''));
    items.forEach((item) => {
      select.appendChild(new Option(labelOf(item), String(valueOf(item))));
    });
    if (current && !Array.from(select.options).some((opt) => opt.value === current)) {
      select.appendChild(new Option(current, current));
    }
    select.value = current || '';
  }

  function renderOptions(options) {
    if (!options) return;
    fillSelect('ac-campus', '全部校区', options.campuses || [], (item) => item.campusId, (item) => item.campusName);
    fillSelect('ac-college', '全部学院', options.colleges || [], (item) => item, (item) => item);
    fillSelect(
      'ac-gate',
      '全部门禁点',
      options.gates || [],
      (item) => item.gateId,
      (item) => {
        const campus = (options.campuses || []).find((row) => String(row.campusId) === String(item.campusId));
        return campus ? `${campus.campusName} · ${item.gateName}` : item.gateName;
      },
    );
    fillSelect('ac-role', '全部身份', options.roles || [], (item) => item, (item) => item);
  }

  function renderMeta(data) {
    const meta = data.meta || {};
    const summary = data.summary || {};
    const window = data.window || {};
    const parts = [];
    if (meta.syncedAt) {
      parts.push(`数据源 ${meta.provider || '-'}`);
      parts.push(`最近同步 ${fmtTime(meta.syncedAt)}`);
      parts.push(`本地流水 ${num(meta.totalEvents)} 条`);
    } else {
      parts.push(`数据源 ${meta.provider || meta.configuredProvider || '-'}`);
      parts.push('本地分析库尚未同步');
    }
    if (window.from && window.to) parts.push(`当前区间 ${window.from} ~ ${window.to}`);
    if (summary.abnormalRate) parts.push(`异常率 ${(summary.abnormalRate * 100).toFixed(1)}%`);
    $('ac-meta').textContent = parts.join(' · ');
  }

  function setKpi(id, value) {
    $(id).textContent = value;
  }

  function renderKpis(data) {
    const summary = data.summary || {};
    setKpi('ac-total', num(summary.total));
    setKpi('ac-entries', num(summary.entries));
    setKpi('ac-exits', num(summary.exits));
    setKpi('ac-persons', num(summary.persons));
    setKpi('ac-abnormal', num(summary.abnormal));
    setKpi('ac-peak', summary.peakHour ? summary.peakHour.label : '-');
    $('ac-abnormal').title = summary.abnormalRate
      ? `异常率 ${(summary.abnormalRate * 100).toFixed(2)}%`
      : '';
    $('ac-peak').title = summary.peakHour ? `${summary.peakHour.total} 次` : '暂无数据';
  }

  function renderTrend(rows) {
    const box = $('ac-trend');
    box.textContent = '';
    if (!rows || !rows.length) {
      box.appendChild(el('div', 'ac-more', '暂无趋势数据'));
      return;
    }
    const max = Math.max(1, ...rows.map((row) => Math.max(row.entries || 0, row.exits || 0)));
    const labelEvery = Math.max(1, Math.ceil(rows.length / 9));
    rows.forEach((row, index) => {
      const column = el('div', 'ac-trend-col' + (row.total ? '' : ' empty-col'));
      column.title = `${row.date}：进入 ${row.entries}，离开 ${row.exits}，异常 ${row.abnormal}，去重 ${row.persons} 人`;
      const entries = el('div', 'ac-bar entries');
      const exits = el('div', 'ac-bar exits');
      entries.style.height = `${Math.max(row.entries ? 2 : 0, Math.round((row.entries || 0) / max * 100))}%`;
      exits.style.height = `${Math.max(row.exits ? 2 : 0, Math.round((row.exits || 0) / max * 100))}%`;
      column.append(entries, exits);
      if (index % labelEvery === 0 || index === rows.length - 1) {
        column.appendChild(el('span', 'ac-day-label', row.date.slice(5)));
      }
      box.appendChild(column);
    });
  }

  function renderHourly(rows, peak) {
    const box = $('ac-hourly');
    box.textContent = '';
    if (!rows || !rows.length) {
      box.appendChild(el('div', 'ac-more', '暂无小时数据'));
      return;
    }
    const max = Math.max(1, ...rows.map((row) => row.total || 0));
    rows.forEach((row) => {
      const column = el('div', 'ac-hour-col' + (peak && peak.hour === row.hour ? ' peak' : ''));
      column.title = `${row.label}：进入 ${row.entries}，离开 ${row.exits}，异常 ${row.abnormal}`;
      const bar = el('div', 'ac-bar');
      bar.style.height = `${Math.max(row.total ? 2 : 0, Math.round((row.total || 0) / max * 100))}%`;
      column.appendChild(bar);
      if (row.hour % 3 === 0) column.appendChild(el('span', 'ac-hour-label', row.label.slice(0, 2)));
      box.appendChild(column);
    });
    $('ac-hour-hint').textContent = peak && peak.total
      ? `高峰 ${peak.label} · ${num(peak.total)} 次`
      : '暂无高峰数据';
  }

  function renderBars(boxId, rows, options) {
    const box = $(boxId);
    box.textContent = '';
    const config = options || {};
    if (!rows || !rows.length) {
      box.appendChild(el('div', 'ac-more', config.emptyText || '暂无数据'));
      return;
    }
    const shown = config.limit ? rows.slice(0, config.limit) : rows;
    const max = Math.max(1, ...shown.map((row) => row.total || 0));
    shown.forEach((row) => {
      const line = el('div', 'ac-bar-row' + (config.onSelect ? ' clickable' : ''));
      const name = el('div', 'ac-bar-name', row[config.nameKey || 'name'] || '-');
      if (config.subKey && row[config.subKey]) {
        name.appendChild(el('small', '', row[config.subKey]));
      }
      const track = el('div', 'ac-bar-track');
      const fill = el('div', 'ac-bar-fill');
      fill.style.width = `${Math.max(2, Math.round((row.total || 0) / max * 100))}%`;
      track.appendChild(fill);
      const value = el('div', 'ac-bar-value', num(row.total));
      const share = typeof row.share === 'number' ? `${(row.share * 100).toFixed(1)}%` : '';
      value.appendChild(el('span', '', share));
      line.append(name, track, value);
      if (config.onSelect) {
        line.tabIndex = 0;
        line.setAttribute('role', 'button');
        line.title = `下钻查看「${row[config.nameKey || 'name'] || ''}」的构成`;
        line.addEventListener('click', () => config.onSelect(row));
        line.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            config.onSelect(row);
          }
        });
      }
      box.appendChild(line);
    });
    if (config.limit && rows.length > config.limit) {
      box.appendChild(el('div', 'ac-more', `仅显示前 ${config.limit} 项，共 ${rows.length} 项`));
    }
  }

  function renderPersons(rows) {
    const body = $('ac-person-rows');
    body.textContent = '';
    const empty = $('ac-person-empty');
    empty.hidden = Boolean(rows && rows.length);
    if (!rows || !rows.length) return;
    rows.forEach((row) => {
      const tr = el('tr');
      const rank = el('td');
      rank.appendChild(el('span', 'ac-rank' + (row.rank <= 3 ? ' top' : ''), row.rank));
      const name = el('td');
      name.appendChild(el('b', '', row.userName || '-'));
      const who = el('td');
      who.appendChild(el('div', '', row.role || '-'));
      who.appendChild(el('div', 'ac-sub', row.collegeName || '未标注学院'));
      const no = el('td', '', row.userNoMasked || '-');
      const total = el('td');
      total.appendChild(el('b', '', num(row.total)));
      const abnormal = el('td');
      abnormal.appendChild(el('span', 'badge ' + (row.abnormal ? 'ac-alert' : 'ac-ok'), row.abnormal ? `${row.abnormal} 次` : '正常'));
      const last = el('td', '', fmtTime(row.lastAt, true));
      tr.append(rank, name, who, no, total, abnormal, last);
      body.appendChild(tr);
    });
  }

  function renderAbnormal(detail) {
    const reasons = $('ac-reason-rows');
    reasons.textContent = '';
    const recent = $('ac-abnormal-recent');
    recent.textContent = '';
    const data = detail || {};
    $('ac-abnormal-hint').textContent = data.total
      ? `异常率 ${((data.total / Math.max(1, state.data?.summary?.total || 1)) * 100).toFixed(1)}% · 深夜 ${num(data.night)} 次`
      : '所选区间无异常';
    (data.byReason || []).forEach((row) => {
      const line = el('div', 'ac-reason-row');
      line.appendChild(el('span', '', row.reason));
      line.appendChild(el('b', '', `${num(row.count)} 次`));
      reasons.appendChild(line);
    });
    if (!(data.byReason || []).length) {
      reasons.appendChild(el('div', 'ac-more', '没有异常类型统计'));
    }
    (data.recent || []).forEach((row) => {
      const line = el('div', 'ac-recent-row');
      const who = el('div', 'who', `${row.userName || '-'} · ${row.gateName || '-'} · ${row.abnormalReason || '未标注'}`);
      line.appendChild(who);
      line.appendChild(el('span', 'time', fmtTime(row.eventTime, true)));
      recent.appendChild(line);
    });
    if (!(data.recent || []).length) {
      recent.appendChild(el('div', 'ac-more', '没有最近的异常记录'));
    }
  }

  function renderEvents(data) {
    const body = $('ac-event-rows');
    body.textContent = '';
    const rows = data.items || [];
    const empty = $('ac-event-empty');
    empty.hidden = Boolean(rows.length);
    $('ac-event-total').textContent = `${num(data.total)} 条`;
    state.page = data.page || 1;
    state.totalPages = Math.max(1, Math.ceil((data.total || 0) / (data.pageSize || state.pageSize)));
    $('ac-page-label').textContent = `第 ${state.page} / ${state.totalPages} 页`;
    $('ac-prev').disabled = state.page <= 1;
    $('ac-next').disabled = state.page >= state.totalPages;
    rows.forEach((row) => {
      const tr = el('tr');
      tr.appendChild(el('td', '', fmtTime(row.eventTime)));
      const direction = el('td');
      direction.appendChild(el('span', 'badge ' + (row.direction === 'in' ? 'ac-in' : 'ac-out'), row.directionLabel || (row.direction === 'in' ? '进入' : '离开')));
      tr.appendChild(direction);
      tr.appendChild(el('td', '', row.campusName || '-'));
      tr.appendChild(el('td', '', row.collegeName || '-'));
      tr.appendChild(el('td', '', row.gateName || '-'));
      const person = el('td');
      person.appendChild(el('b', '', row.userName || '-'));
      person.appendChild(el('div', 'ac-sub', row.role || '-'));
      tr.appendChild(person);
      tr.appendChild(el('td', '', row.userNoMasked || '-'));
      const abnormal = el('td');
      abnormal.appendChild(el('span', 'badge ' + (row.abnormal ? 'ac-alert' : 'ac-ok'), row.abnormal ? (row.abnormalReason || '异常') : '正常'));
      tr.appendChild(abnormal);
      body.appendChild(tr);
    });
  }

  function render(data) {
    renderMeta(data);
    renderKpis(data);
    renderTrend(data.trend || []);
    renderHourly(data.hourly || [], data.summary?.peakHour);
    renderBars('ac-campus-rows', data.byCampus || [], {
      nameKey: 'campusName',
      emptyText: '暂无校区数据',
      onSelect: (row) => openDrill('campus', { campusId: row.campusId }),
    });
    renderBars('ac-college-rows', data.topColleges || [], {
      nameKey: 'collegeName',
      limit: 5,
      emptyText: '暂无学院数据',
      onSelect: (row) => openDrill('college', { college: row.collegeName }),
    });
    renderBars('ac-gate-rows', data.byGate || [], {
      nameKey: 'gateName',
      subKey: 'campusName',
      limit: 8,
      emptyText: '暂无门禁点数据',
      onSelect: (row) => openDrill('gate', { gateId: row.gateId }),
    });
    renderPersons(data.topPersons || []);
    renderAbnormal(data.abnormal);
  }

  async function loadEvents(page) {
    const params = buildParams();
    params.set('page', String(page || 1));
    params.set('pageSize', String(state.pageSize));
    const data = await api(`/api/admin/access/events?${params.toString()}`);
    renderEvents(data);
  }

  function setBusy(busy) {
    ['ac-sync', 'ac-apply', 'ac-refresh', 'ac-report', 'ac-csv'].forEach((id) => {
      const node = $(id);
      if (node) node.disabled = Boolean(busy);
    });
  }

  async function refresh() {
    if (state.loading) return;
    state.loading = true;
    setBusy(true);
    msg('正在加载通行分析…');
    try {
      const params = buildParams();
      const data = await api(`/api/admin/access/overview?${params.toString()}`);
      state.data = data;
      renderOptions(data.options);
      render(data);
      await loadEvents(1);
      msg('');
    } catch (error) {
      msg(`加载失败：${error.message}`, 'err');
    } finally {
      state.loading = false;
      setBusy(false);
    }
  }

  async function syncData() {
    const button = $('ac-sync');
    const original = button.textContent;
    button.disabled = true;
    button.textContent = '同步中…';
    msg('正在从门禁数据源同步流水…');
    try {
      const data = await api('/api/admin/access/sync', { method: 'POST' });
      await refresh();
      msg(`同步完成：${data.provider === 'mock' ? '演示数据' : '门禁平台'} ${data.imported} 条（跳过 ${data.skipped} 条），区间 ${data.from} ~ ${data.to}`, 'ok');
    } catch (error) {
      msg(`同步失败：${error.message}`, 'err');
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function resetFilters() {
    initDates();
    ['ac-campus', 'ac-college', 'ac-gate', 'ac-role', 'ac-direction', 'ac-abnormal-filter'].forEach((id) => {
      $(id).value = '';
    });
    $('ac-q').value = '';
    state.page = 1;
    refresh();
  }

  function drillLabel(dimension) {
    return { campus: '校区', college: '学院', gate: '门禁点' }[dimension] || '维度';
  }

  function renderDrill(data) {
    $('ac-drill-title').textContent = `${drillLabel(data.dimension)}下钻 · ${data.title}`;
    const kpis = $('ac-drill-kpis');
    kpis.textContent = '';
    [
      ['通行总量', num(data.summary.total)],
      ['去重人数', num(data.summary.persons)],
      ['异常通行', num(data.summary.abnormal)],
      ['高峰时段', data.summary.peakHour ? data.summary.peakHour.label : '-'],
    ].forEach(([label, value]) => {
      const box = el('div', 'ac-kpi');
      box.appendChild(el('b', '', value));
      box.appendChild(el('span', '', label));
      kpis.appendChild(box);
    });
    renderBars('ac-drill-colleges', data.byCollege || [], {
      nameKey: 'collegeName',
      emptyText: '暂无学院构成',
    });
    renderBars('ac-drill-gates', data.byGate || [], {
      nameKey: 'gateName',
      subKey: 'campusName',
      emptyText: '暂无门禁点构成',
    });
    const persons = $('ac-drill-persons');
    persons.textContent = '';
    (data.topPersons || []).slice(0, 5).forEach((row) => {
      const line = el('div', 'ac-recent-row');
      const who = el('div', 'who', `${row.rank}. ${row.userName} · ${row.userNoMasked} · ${row.total} 次`);
      line.appendChild(who);
      line.appendChild(el('span', 'time', row.abnormal ? `异常 ${row.abnormal}` : '正常'));
      persons.appendChild(line);
    });
    if (!(data.topPersons || []).length) persons.appendChild(el('div', 'ac-more', '暂无人员排行'));
    $('ac-drill-card').hidden = false;
  }

  async function openDrill(dimension, payload) {
    const params = buildParams();
    params.set('dimension', dimension);
    if (payload.campusId !== undefined) params.set('campusId', String(payload.campusId));
    if (payload.college) params.set('college', payload.college);
    if (payload.gateId !== undefined) params.set('gateId', String(payload.gateId));
    msg('正在加载下钻数据…');
    try {
      const data = await api(`/api/admin/access/drilldown?${params.toString()}`);
      renderDrill(data);
      $('ac-drill-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
      msg('');
    } catch (error) {
      msg(`下钻失败：${error.message}`, 'err');
    }
  }

  async function previewReport() {
    msg('正在生成智能分析报告…');
    try {
      const params = buildParams();
      const data = await api(`/api/admin/access/report?${params.toString()}`);
      state.report = data;
      $('ac-report-title').textContent = '通行智能分析报告';
      $('ac-report-content').textContent = data.content;
      $('ac-report-msg').textContent = `${data.window.from} ~ ${data.window.to} · ${data.filename}`;
      $('ac-report-msg').className = 'msg-line';
      $('ac-report-modal').hidden = false;
      msg('');
    } catch (error) {
      msg(`报告生成失败：${error.message}`, 'err');
    }
  }

  function filenameFrom(resp, fallback) {
    const header = resp.headers.get('Content-Disposition') || '';
    const match = header.match(/filename\*=UTF-8''([^;]+)/i);
    if (match) {
      try { return decodeURIComponent(match[1]); } catch (_) { /* ignore */ }
    }
    return fallback;
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  async function downloadReport() {
    const button = $('ac-report-download');
    button.disabled = true;
    const params = buildParams();
    params.set('download', '1');
    try {
      const resp = await authFetch(`/api/admin/access/report?${params.toString()}`, { headers: { Accept: 'text/markdown' } });
      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const json = await resp.json();
          detail = json.detail || json.error || detail;
        } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      downloadBlob(await resp.blob(), filenameFrom(resp, state.report?.filename || '通行分析报告.md'));
      $('ac-report-msg').textContent = '报告已开始下载。';
      $('ac-report-msg').className = 'msg-line ok';
    } catch (error) {
      $('ac-report-msg').textContent = `下载失败：${error.message}`;
      $('ac-report-msg').className = 'msg-line err';
    } finally {
      button.disabled = false;
    }
  }

  async function exportCsv() {
    msg('正在导出通行明细…');
    const params = buildParams();
    try {
      const resp = await authFetch(`/api/admin/access/export?${params.toString()}`, { headers: { Accept: 'text/csv' } });
      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const json = await resp.json();
          detail = json.detail || json.error || detail;
        } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      downloadBlob(await resp.blob(), filenameFrom(resp, '通行流水.csv'));
      const truncated = resp.headers.get('X-Export-Truncated');
      msg(truncated ? '明细已导出（超过单次上限，结果已截断）。' : '明细已导出。', truncated ? 'err' : 'ok');
    } catch (error) {
      msg(`导出失败：${error.message}`, 'err');
    }
  }

  function bind() {
    $('ac-apply').addEventListener('click', () => { state.page = 1; refresh(); });
    $('ac-refresh').addEventListener('click', () => refresh());
    $('ac-reset').addEventListener('click', resetFilters);
    $('ac-sync').addEventListener('click', syncData);
    $('ac-report').addEventListener('click', previewReport);
    $('ac-csv').addEventListener('click', exportCsv);
    $('ac-report-download').addEventListener('click', downloadReport);
    $('ac-prev').addEventListener('click', () => {
      if (state.page > 1) loadEvents(state.page - 1).catch((error) => msg(`加载失败：${error.message}`, 'err'));
    });
    $('ac-next').addEventListener('click', () => {
      if (state.page < state.totalPages) loadEvents(state.page + 1).catch((error) => msg(`加载失败：${error.message}`, 'err'));
    });
    $('ac-drill-close').addEventListener('click', () => { $('ac-drill-card').hidden = true; });
    $('ac-q').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        state.page = 1;
        refresh();
      }
    });
    document.querySelectorAll('[data-ac-close]').forEach((node) => {
      node.addEventListener('click', () => { $('ac-report-modal').hidden = true; });
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !$('ac-report-modal').hidden) $('ac-report-modal').hidden = true;
    });
  }

  initDates();
  bind();
  refresh();
})();
