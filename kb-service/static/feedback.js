/* 能小智 · 问答反馈记录（任务 4.9 / P-QA-08）
 *
 * 数据口径：
 *   - 一行 = 一条用户评价（点赞 / 点踩），按 session + msg 去重后 upsert；
 *   - 点踩必须带原因（inaccurate / incomplete / irrelevant / other）；
 *   - 标记处理 / 重新打开会写操作日志（FEEDBACK_HANDLE）；
 *   - CSV 导出沿用当前筛选条件，上限由服务端控制。
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 20;

  const state = {
    page: 1,
    pages: 0,
    total: 0,
    items: [],
    detail: null,
    filters: { rating: '', handled: '', dateFrom: '', dateTo: '', q: '' },
  };

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function authFetch(url, options) {
    return (window.NXZAuth ? window.NXZAuth.fetch : fetch)(url, options);
  }

  async function api(url, options) {
    const opts = Object.assign({ headers: { Accept: 'application/json' } }, options || {});
    const resp = await authFetch(url, opts);
    let json = null;
    try { json = await resp.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!resp.ok) {
      const msg = (json && (json.detail || json.error)) || ('HTTP ' + resp.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    if (!json) throw new Error('响应不是 JSON（HTTP ' + resp.status + '）');
    if (!json.ok) throw new Error(json.error || json.detail || '操作失败');
    return json.data;
  }

  function jsonBody(method, payload) {
    return {
      method: method,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload),
    };
  }

  function note(text, kind) {
    const el = $('fb-note');
    el.textContent = text || '';
    el.className = 'fb-note' + (kind ? ' ' + kind : '');
  }

  function detailMsg(text, kind) {
    const el = $('fb-detail-msg');
    el.textContent = text || '';
    el.className = 'fb-msg' + (kind ? ' ' + kind : '');
  }

  function fmtTime(value) {
    if (!value) return '—';
    return String(value).replace('T', ' ').slice(0, 19);
  }

  function fmtRate(value) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    return (Number(value) * 100).toFixed(1) + '%';
  }

  function ratingBadge(rating) {
    const like = Number(rating) === 1;
    return `<span class="fb-rating ${like ? 'like' : 'dislike'}">` +
      `${like ? '👍 点赞' : '👎 点踩'}</span>`;
  }

  function statusBadge(handled) {
    return Number(handled)
      ? '<span class="fb-status done">已处理</span>'
      : '<span class="fb-status pending">待处理</span>';
  }

  // ---------------------------------------------------------------- 列表
  function readFilters() {
    state.filters = {
      rating: $('fb-rating').value,
      handled: $('fb-handled').value,
      dateFrom: $('fb-from').value || '',
      dateTo: $('fb-to').value || '',
      q: $('fb-q').value.trim(),
    };
  }

  function buildQuery(includePage) {
    const params = new URLSearchParams();
    const f = state.filters;
    if (f.rating !== '') params.set('rating', f.rating);
    if (f.handled !== '') params.set('handled', f.handled);
    if (f.dateFrom) params.set('dateFrom', f.dateFrom);
    if (f.dateTo) params.set('dateTo', f.dateTo);
    if (f.q) params.set('q', f.q);
    if (includePage) {
      params.set('page', String(state.page));
      params.set('pageSize', String(PAGE_SIZE));
    }
    return params.toString();
  }

  function renderSummary(summary) {
    const data = summary || {};
    $('fb-kpi-total').textContent = String(data.total == null ? 0 : data.total);
    $('fb-kpi-likes').textContent = String(data.likes == null ? 0 : data.likes);
    $('fb-kpi-dislikes').textContent = String(data.dislikes == null ? 0 : data.dislikes);
    $('fb-kpi-pending').textContent = String(data.pending == null ? 0 : data.pending);
    $('fb-kpi-rate').textContent = fmtRate(data.likeRate);
  }

  function renderRows() {
    const body = $('fb-body');
    body.innerHTML = state.items.map((item) => {
      const reason = item.reasonLabel
        ? `<span class="fb-reason-code">${esc(item.reasonLabel)}</span>`
        : '<span class="muted">—</span>';
      const extra = item.reason
        ? `<p class="fb-reason-text">${esc(item.reason)}</p>`
        : '';
      return '<tr>' +
        `<td><span class="fb-id">#${esc(item.feedbackId)}</span>` +
        `<span class="fb-time">${esc(fmtTime(item.createdAt))}</span></td>` +
        `<td>${ratingBadge(item.rating)}</td>` +
        `<td>${reason}${extra}</td>` +
        `<td><p class="fb-question" title="${esc(item.question)}">${esc(item.question || '—')}</p></td>` +
        `<td><p class="fb-excerpt" title="${esc(item.answerExcerpt)}">${esc(item.answerExcerpt || '—')}</p></td>` +
        '<td>' +
        `<p class="fb-meta-line">${esc(item.role || '—')}</p>` +
        `<p class="fb-meta-line">${esc(item.channelCode || '默认渠道')}</p></td>` +
        `<td>${statusBadge(item.handled)}</td>` +
        `<td><div class="fb-row-ops">` +
        `<button type="button" class="btn-ghost" data-act="detail" data-id="${esc(item.feedbackId)}">详情</button>` +
        `<button type="button" class="btn-ghost" data-act="handle" data-id="${esc(item.feedbackId)}" data-perm="feedback.manage">处理</button>` +
        '</div></td>' +
        '</tr>';
    }).join('');
    $('fb-empty').hidden = state.items.length > 0;
    if (window.NXZAuth) window.NXZAuth.apply();
  }

  function renderPager() {
    const pager = $('fb-pager');
    if (state.pages <= 1) {
      pager.hidden = true;
      return;
    }
    pager.hidden = false;
    $('fb-page-info').textContent = `第 ${state.page} / ${state.pages} 页`;
    $('fb-prev').disabled = state.page <= 1;
    $('fb-next').disabled = state.page >= state.pages;
  }

  async function load() {
    note('加载中…');
    let data;
    try {
      data = await api('/api/admin/feedback?' + buildQuery(true));
    } catch (err) {
      note('加载失败：' + err.message, 'err');
      return;
    }
    state.items = data.items || [];
    state.total = data.total || 0;
    state.pages = data.pages || 0;
    state.page = data.page || state.page;
    renderSummary(data.summary);
    renderRows();
    renderPager();
    $('fb-count').textContent = `（共 ${state.total} 条）`;
    $('fb-hint').textContent = '点踩优先跟进 · 处理动作写入操作日志';
    note(state.total
      ? `当前筛选 ${state.total} 条 · 点赞 ${data.summary.likes} / 点踩 ${data.summary.dislikes} · 待处理 ${data.summary.pending}`
      : '当前筛选下暂无反馈记录');
  }

  // ---------------------------------------------------------------- 详情
  function renderDetail(item) {
    state.detail = item;
    $('fb-detail-title').textContent = '反馈详情 #' + item.feedbackId;
    const chips = [
      '时间 · ' + fmtTime(item.createdAt),
      item.role ? '身份 · ' + item.role : '身份 · —',
      item.channelCode ? '渠道 · ' + item.channelCode : '渠道 · 默认',
      '会话 · ' + (item.sessionId || '—'),
      '消息 · ' + (item.msgId == null ? '—' : '#' + item.msgId),
    ];
    if (item.handledAt) chips.push('处理时间 · ' + fmtTime(item.handledAt));
    $('fb-detail-meta').innerHTML = chips.map((t) => `<span>${esc(t)}</span>`).join('');

    $('fb-detail-question').textContent = item.question || '（无问题快照）';
    $('fb-detail-answer').textContent = item.answerExcerpt || '（无回答摘要）';

    const reason = item.reasonLabel
      ? `<p class="fb-detail-reason">原因：${esc(item.reasonLabel)}` +
        (item.reasonCode ? `（${esc(item.reasonCode)}）` : '') + '</p>'
      : '<p>原因：<span class="muted">用户未填写</span></p>';
    const extra = item.reason
      ? `<p>补充说明：${esc(item.reason)}</p>`
      : '<p>补充说明：<span class="muted">无</span></p>';
    const handle = item.handled
      ? `<p>处理备注：${esc(item.handleNote || '无')}</p>`
      : '<p>处理备注：<span class="muted">尚未处理</span></p>';
    $('fb-detail-feedback').innerHTML =
      ratingBadge(item.rating) + reason + extra + handle;
    $('fb-handle-note').value = item.handleNote || '';
    $('fb-handle').hidden = !!item.handled;
    $('fb-reopen').hidden = !item.handled;
    detailMsg('');
  }

  async function openDetail(id, focusHandle) {
    try {
      const item = await api('/api/admin/feedback/' + encodeURIComponent(id));
      renderDetail(item);
      $('fb-detail').hidden = false;
      if (focusHandle) $('fb-handle-note').focus();
    } catch (err) {
      note('详情加载失败：' + err.message, 'err');
    }
  }

  function closeDetail() {
    $('fb-detail').hidden = true;
    state.detail = null;
    detailMsg('');
  }

  async function handleDetail(handled) {
    if (!state.detail) return;
    const id = state.detail.feedbackId;
    const text = $('fb-handle-note').value.trim();
    if (handled && !text && Number(state.detail.rating) === -1) {
      // 点踩未写备注时允许处理，但提示建议补充跟进说明；不强制阻断
      detailMsg('提示：该点踩记录尚无处理备注，可直接处理或先补充说明。');
    }
    $('fb-handle').disabled = true;
    $('fb-reopen').disabled = true;
    try {
      const item = await api(
        '/api/admin/feedback/' + encodeURIComponent(id),
        jsonBody('PATCH', { handled: handled, note: text })
      );
      // 反馈可能因「处理状态」筛选而离开当前列表，重新拉取才能保证汇总与分页准确
      await load();
      note(`反馈 #${id} 已${handled ? '标记处理' : '重新打开'}`, 'ok');
      const stillListed = state.items.some(
        (x) => Number(x.feedbackId) === Number(id)
      );
      if (!stillListed && state.filters.handled !== '') {
        closeDetail();
        return;
      }
      renderDetail(item);
      detailMsg(handled ? '已标记处理' : '已重新打开', 'ok');
    } catch (err) {
      detailMsg('操作失败：' + err.message, 'err');
    } finally {
      $('fb-handle').disabled = false;
      $('fb-reopen').disabled = false;
    }
  }

  // ---------------------------------------------------------------- 导出
  async function exportCsv() {
    readFilters();
    if (!(window.NXZAuth && window.NXZAuth.download)) {
      note('导出不可用：鉴权脚本未加载', 'err');
      return;
    }
    const btn = $('fb-export');
    btn.disabled = true;
    note('正在导出…');
    try {
      await window.NXZAuth.download('/api/admin/feedback/export?' + buildQuery(false));
      note('导出完成（最多 5000 条，按当前筛选条件）', 'ok');
    } catch (err) {
      note('导出失败：' + err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 事件
  function bind() {
    $('fb-search').addEventListener('click', () => {
      readFilters();
      state.page = 1;
      load();
    });
    $('fb-reset').addEventListener('click', () => {
      $('fb-rating').value = '';
      $('fb-handled').value = '';
      $('fb-from').value = '';
      $('fb-to').value = '';
      $('fb-q').value = '';
      readFilters();
      state.page = 1;
      load();
    });
    $('fb-q').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        readFilters();
        state.page = 1;
        load();
      }
    });
    $('fb-export').addEventListener('click', exportCsv);
    $('fb-prev').addEventListener('click', () => {
      if (state.page <= 1) return;
      state.page -= 1;
      load();
    });
    $('fb-next').addEventListener('click', () => {
      if (state.page >= state.pages) return;
      state.page += 1;
      load();
    });
    $('fb-body').addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-act]');
      if (!btn) return;
      openDetail(btn.dataset.id, btn.dataset.act === 'handle');
    });
    $('fb-detail-close').addEventListener('click', closeDetail);
    $('fb-detail-close-btn').addEventListener('click', closeDetail);
    $('fb-detail-mask').addEventListener('click', closeDetail);
    $('fb-handle').addEventListener('click', () => handleDetail(true));
    $('fb-reopen').addEventListener('click', () => handleDetail(false));
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && !$('fb-detail').hidden) closeDetail();
    });
  }

  bind();
  load();
})();
