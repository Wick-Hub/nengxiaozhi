/* 能小智 · 未识别问题池（任务 3.11 / M-S-07～09）
 *
 * 数据口径：
 *   - 列表一行 = 一个归一化问题（questionKey）的聚合，频次 / 首末时间 /
 *     角色 / 渠道 / 来源 / 语义簇均由服务端算好；
 *   - 批量处理按 questionKey 提交，服务端再展开为该组全部记录；
 *   - 反哺只在 MySQL 模式可用：生成 status=0 的知识草稿，发布仍走 2.7 审核。
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 20;
  const STATE_LABELS = { 0: '待处理', 1: '已反哺', 2: '已忽略' };

  const state = {
    page: 1,
    total: 0,
    recordTotal: 0,
    items: [],
    selected: new Set(),
    enabled: true,
    feedbackAvailable: false,
    store: 'json',
    threshold: 0.35,
    window: null,
    detail: null,
    feedbackKey: null,
    feedbackIds: [],
    filters: { state: '', source: '', dept: '', role: '', channel: '', dateFrom: '', dateTo: '', q: '' },
    sort: 'recent',
  };

  // ---------------------------------------------------------------- 基础工具
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
    try { json = await resp.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!resp.ok) {
      const msg = (json && (json.detail || json.error)) || ('HTTP ' + resp.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    if (!json) throw new Error('响应不是 JSON（HTTP ' + resp.status + '）');
    if (!json.ok) throw new Error(json.error || json.detail || '操作失败');
    return json.data;
  }

  function msg(id, text, kind) {
    const el = $(id);
    if (!el) return;
    const base = id === 'un-note' ? 'un-note' : 'un-msg';
    el.textContent = text || '';
    el.className = base + (kind ? ' ' + kind : '');
  }

  function jsonBody(payload) {
    return {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload),
    };
  }

  function fmtTime(value) {
    if (!value) return '—';
    return String(value).replace('T', ' ').slice(0, 19);
  }

  function fmtConf(value) {
    if (value == null) return '—';
    return Number(value).toFixed(2);
  }

  function stateBadge(code) {
    const n = Number(code) || 0;
    return `<span class="un-state st-${n}">${esc(STATE_LABELS[n] || '未知')}</span>`;
  }

  // ---------------------------------------------------------------- 筛选项
  async function loadFilters() {
    let data;
    try {
      data = await api('/api/admin/unrecognized/filters');
    } catch (e) {
      msg('un-note', '筛选项加载失败：' + e.message, 'err');
      return false;
    }
    state.enabled = data.enabled !== false;
    state.store = data.store || 'json';
    state.feedbackAvailable = !!data.feedbackAvailable;
    state.threshold = data.threshold == null ? 0.35 : data.threshold;

    fillSelect('un-state', data.states, '全部状态');
    fillSelect('un-source', data.sources, '全部来源');
    fillSelect('un-dept', data.depts, '全部部门');
    fillSelect('un-role', data.roles, '全部角色');
    fillSelect('un-channel', data.channels, '全部渠道');
    return true;
  }

  function fillSelect(id, values, placeholder) {
    const sel = $(id);
    if (!sel) return;
    const keep = sel.value;
    sel.innerHTML = `<option value="">${esc(placeholder)}</option>` +
      (values || []).map((v) => {
        if (v && typeof v === 'object') {
          const code = v.value != null ? v.value : v.code;
          let label = v.label != null ? v.label : v.name;
          if (v.code != null && v.name != null && v.name !== v.code) label = v.name + '（' + v.code + '）';
          return `<option value="${esc(code)}">${esc(label == null ? code : label)}</option>`;
        }
        return `<option value="${esc(v)}">${esc(v)}</option>`;
      }).join('');
    if (keep) sel.value = keep;
  }

  // ---------------------------------------------------------------- 列表
  function buildQuery(includePage) {
    const params = new URLSearchParams();
    const f = state.filters;
    if (f.state !== '') params.set('state', f.state);
    if (f.source) params.set('source', f.source);
    if (f.dept) params.set('dept', f.dept);
    if (f.role) params.set('role', f.role);
    if (f.channel) params.set('channelCode', f.channel);
    if (f.dateFrom) params.set('dateFrom', f.dateFrom);
    if (f.dateTo) params.set('dateTo', f.dateTo);
    if (f.q) params.set('q', f.q);
    params.set('sort', state.sort);
    if (includePage) {
      params.set('page', String(state.page));
      params.set('pageSize', String(PAGE_SIZE));
    }
    return params.toString();
  }

  async function load() {
    msg('un-note', '加载中…');
    let data;
    try {
      data = await api('/api/admin/unrecognized?' + buildQuery(true));
    } catch (e) {
      msg('un-note', '加载失败：' + e.message, 'err');
      return;
    }
    state.items = data.items || [];
    state.total = data.total || 0;
    state.recordTotal = data.recordTotal || 0;
    state.enabled = data.enabled !== false;
    state.store = data.store || state.store;
    state.feedbackAvailable = !!data.feedbackAvailable;
    state.threshold = data.threshold == null ? state.threshold : data.threshold;
    state.window = data.window || null;
    state.selected.clear();

    const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
    if (state.page > pages) {
      state.page = pages;
      return load();
    }

    $('un-count').textContent = `（${state.total} 组 / ${state.recordTotal} 条记录）`;
    $('un-hint').textContent = state.feedbackAvailable
      ? '勾选问题组后可批量忽略 / 重新打开 / 归类或反哺知识草稿'
      : '当前为 JSON 只读知识模式：可查询、追溯、批量处理与导出 CSV；反哺需 MySQL';
    renderNote();
    renderClusters(data.clusters || []);
    render();
    renderPager(pages);
    syncBatch();
  }

  function renderNote() {
    const bits = [
      `触发来源：弱召回 / 模型拒答 / 低置信度（< ${state.threshold}）`,
      `数据源：${state.store === 'mysql' ? 'MySQL' : '本地 JSON'}`,
    ];
    if (state.window && state.window.truncated) {
      bits.push(`仅扫描最近 ${state.window.limit} 条记录`);
    }
    if (!state.enabled) {
      msg('un-note', '未识别问题池已关闭（UNRECOGNIZED_ENABLED=false）：不再记录新问题，历史数据仍可查询。', 'warn');
      return;
    }
    msg('un-note', bits.join(' · '));
  }

  function renderClusters(clusters) {
    const box = $('un-clusters');
    if (!clusters.length) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    box.hidden = false;
    box.innerHTML = '<span class="un-cluster-label">语义簇</span>' + clusters.map((c) => {
      const keys = (c.questionKeys || []).join('\n');
      return `<span class="un-cluster-chip" title="${esc(keys)}">` +
        `<span class="un-cluster-text">${esc(c.clusterLabel || c.clusterId)}</span>` +
        `<b>×${esc(c.questionCount)}</b></span>`;
    }).join('');
  }

  function render() {
    const body = $('un-body');
    body.innerHTML = state.items.map((item) => {
      const idText = (item.ids && item.ids.length) ? '#' + item.ids[0] + (item.idsTruncated ? '…' : '') : '—';
      const roles = (item.roles || []).join('、') || '—';
      const channels = (item.channelNames || []).join('、') || '—';
      const sources = (item.sourceLabels || []).join('、') || '—';
      const cluster = item.clusterLabel
        ? `<div class="un-cluster" title="${esc(item.clusterLabel)}">簇：${esc(item.clusterLabel)}</div>`
        : '';
      const reopen = Number(item.state) === 2;
      const feedback = state.feedbackAvailable && Number(item.state) !== 1;
      return `<tr data-key="${esc(item.questionKey)}">
        <td class="chk"><input type="checkbox" data-chk="${esc(item.questionKey)}"></td>
        <td>
          <div class="un-question">${esc(item.question)}</div>
          <div class="un-sub">${esc(idText)} · 首次 ${esc(fmtTime(item.firstAt))}</div>
        </td>
        <td><span class="un-freq">${esc(item.count)}<span>次</span></span></td>
        <td class="un-sub">${esc(sources)}</td>
        <td class="un-sub">${esc(roles)}<br>${esc(channels)}</td>
        <td>
          <span class="un-domain">${esc(item.domain || '其他')}</span>
          ${cluster}
        </td>
        <td>${stateBadge(item.state)}</td>
        <td><div class="un-time">${esc(fmtTime(item.lastAt))}</div></td>
        <td class="un-ops-cell">
          <button class="op-btn" data-detail="${esc((item.ids || [])[0])}">详情</button>
          <button class="op-btn" data-perm="unrecognized.manage" data-action="${reopen ? 'reopen' : 'ignore'}"
            data-key="${esc(item.questionKey)}">${reopen ? '重新打开' : '忽略'}</button>
          ${feedback ? `<button class="op-btn" data-perm="unrecognized.manage" data-feedback="${esc(item.questionKey)}">反哺</button>` : ''}
        </td>
      </tr>`;
    }).join('');

    $('un-empty').hidden = state.items.length > 0;

    body.querySelectorAll('[data-chk]').forEach((box) => {
      box.onchange = () => {
        const key = box.getAttribute('data-chk');
        if (box.checked) state.selected.add(key); else state.selected.delete(key);
        syncBatch();
      };
    });
    body.querySelectorAll('[data-detail]').forEach((btn) => {
      btn.onclick = () => openDetail(Number(btn.getAttribute('data-detail')));
    });
    body.querySelectorAll('[data-action]').forEach((btn) => {
      btn.onclick = () => {
        const key = btn.getAttribute('data-key');
        runRowAction(key, btn.getAttribute('data-action'));
      };
    });
    body.querySelectorAll('[data-feedback]').forEach((btn) => {
      const key = btn.getAttribute('data-feedback');
      btn.onclick = () => {
        const item = state.items.find((x) => x.questionKey === key);
        if (item) openFeedback({ questionKey: item.questionKey, question: item.question, dept: item.dept, ids: item.ids });
      };
    });
  }

  function renderPager(pages) {
    const pager = $('un-pager');
    pager.hidden = pages <= 1;
    $('un-page-info').textContent = `第 ${state.page} / ${pages} 页`;
    $('un-prev').disabled = state.page <= 1;
    $('un-next').disabled = state.page >= pages;
  }

  function syncBatch() {
    const bar = $('un-batch');
    const n = state.selected.size;
    bar.hidden = n === 0;
    $('un-batch-count').textContent = String(n);
    const all = $('un-chk-all');
    const boxes = Array.from(document.querySelectorAll('[data-chk]'));
    all.checked = boxes.length > 0 && boxes.every((b) => b.checked);
    all.indeterminate = !all.checked && boxes.some((b) => b.checked);
  }

  // ---------------------------------------------------------------- 批量处理
  async function runBatch(action, keys, dept) {
    const target = keys || Array.from(state.selected);
    if (!target.length) { msg('un-batch-msg', '请先勾选问题组', 'err'); return; }
    if (action === 'classify' && !dept) { msg('un-batch-msg', '归类需要填写归属部门', 'err'); return; }
    msg('un-batch-msg', '处理中…');
    try {
      const data = await api('/api/admin/unrecognized/batch', jsonBody({
        questionKeys: target,
        action: action,
        dept: dept || undefined,
      }));
      const label = action === 'ignore' ? '忽略' : action === 'reopen' ? '重新打开' : '归类';
      msg('un-batch-msg', `${label}成功：更新 ${data.updated} 条记录`, 'ok');
      state.selected.clear();
      await load();
      if (!$('un-detail').hidden && state.detail) await openDetail(state.detail.record.id, true);
    } catch (e) {
      msg('un-batch-msg', '操作失败：' + e.message, 'err');
    }
  }

  async function runRowAction(key, action) {
    const label = action === 'ignore' ? '忽略' : '重新打开';
    try {
      const data = await api('/api/admin/unrecognized/batch', jsonBody({
        questionKeys: [key],
        action: action,
      }));
      await load();
      msg('un-note', `已${label}：更新 ${data.updated} 条记录`, 'ok');
    } catch (e) {
      msg('un-note', '操作失败：' + e.message, 'err');
    }
  }

  // ---------------------------------------------------------------- 详情追溯
  async function openDetail(recordId, silent) {
    if (!recordId) { msg('un-batch-msg', '该问题组没有可追溯的记录', 'err'); return; }
    $('un-detail').hidden = false;
    if (!silent) msg('un-detail-msg', '加载中…');
    let data;
    try {
      data = await api('/api/admin/unrecognized/' + encodeURIComponent(recordId));
    } catch (e) {
      msg('un-detail-msg', '加载失败：' + e.message, 'err');
      return;
    }
    state.detail = data;
    const rec = data.record || {};
    $('un-detail-title').textContent = rec.question || '问题详情';
    $('un-detail-meta').innerHTML = [
      `<span class="un-tag">#${esc(rec.id)}</span>`,
      `<span class="un-tag">${esc(STATE_LABELS[Number(rec.state)] || '未知')}</span>`,
      `<span class="un-tag">${esc(rec.sourceLabel || sourceLabel(rec.source))}</span>`,
      `<span class="un-tag">置信度 ${esc(fmtConf(rec.confidence))}</span>`,
      `<span class="un-tag">角色 ${esc(rec.role || '—')}</span>`,
      `<span class="un-tag">渠道 ${esc(rec.channelCode || '—')}</span>`,
      `<span class="un-tag">部门 ${esc(rec.dept || '—')}</span>`,
      `<span class="un-tag">业务域 ${esc(data.domain || '其他')}</span>`,
      `<span class="un-tag">记录于 ${esc(fmtTime(rec.createdAt))}</span>`,
    ].join('');
    renderSession(data);
    renderVariants(data);
    if (!silent) msg('un-detail-msg', '');
  }

  function sourceLabel(code) {
    return { weak_recall: '弱召回', refused: '模型拒答', low_confidence: '低置信度' }[code] || code || '—';
  }

  function renderSession(data) {
    const box = $('un-detail-session');
    const rec = data.record || {};
    const sess = data.session;
    if (!sess) {
      box.innerHTML = '<div class="un-sub">原会话已被清理，无法回溯对话内容；问题原文仍保留在池中。</div>';
      return;
    }
    const q = data.questionMessage;
    const a = data.answerMessage;
    let html = `<div class="un-session-meta">会话 ${esc(sess.sessionId)} · ${esc(sess.role || '—')} · `
      + `${esc(fmtTime(sess.createdAt))} · ${esc(sess.messageCount)} 条消息 · 记录 #${esc(rec.id)}</div>`;
    if (q) {
      html += `<div class="un-bubble"><div class="un-bubble-role">用户 · ${esc(fmtTime(q.createdAt))}</div>`
        + `<div class="un-bubble-body">${esc(q.content || '')}</div></div>`;
    } else {
      html += '<div class="un-sub">未找到对应的用户消息。</div>';
    }
    if (a) {
      const refused = !!a.refused;
      html += `<div class="un-bubble ai${refused ? ' refused' : ''}">`
        + `<div class="un-bubble-role">AI · 置信度 ${esc(fmtConf(a.confidence))}`
        + `${refused ? ' · 拒答' : ''} · ${esc(fmtTime(a.createdAt))}</div>`
        + `<div class="un-bubble-body">${esc(a.content || '')}</div>`;
      const cites = a.citations || [];
      if (cites.length) {
        html += '<div class="un-cites">' + cites.map((c) => {
          const label = c.titlePath || c.docTitle || c.chunkId || '';
          return `<div class="un-cite"><b>[${esc(c.n == null ? '·' : c.n)}]</b> ${esc(label)}`
            + `${c.chunkId && label !== c.chunkId ? ` · ${esc(c.chunkId)}` : ''}</div>`;
        }).join('') + '</div>';
      }
      html += '</div>';
    } else {
      html += '<div class="un-sub">未找到对应的 AI 回答。</div>';
    }
    if (data.feedback && data.feedback.docId) {
      const f = data.feedback;
      const name = f.statusName ? ` · ${f.statusName}` : '';
      html += `<div class="un-feedback-info">已反哺知识草稿 ${esc(f.publicId || f.docId)}${esc(name)}`
        + `${f.title ? ' · ' + esc(f.title) : ''}</div>`;
    }
    box.innerHTML = html;
  }

  function renderVariants(data) {
    const rec = data.record || {};
    const variants = data.variants || [];
    $('un-variant-count').textContent = `（${data.variantCount || variants.length} 条）`;
    const box = $('un-variants');
    if (!variants.length) {
      box.innerHTML = '<div class="un-sub">暂无同义问法</div>';
      return;
    }
    box.innerHTML = variants.map((v) => {
      const current = v.id === rec.id;
      return `<div class="un-variant${current ? ' current' : ''}">${esc(v.question || '')}`
        + `<span class="un-variant-meta">#${esc(v.id)} · ${esc(STATE_LABELS[Number(v.state)] || '')} · `
        + `${esc(v.role || '—')} · ${esc(fmtTime(v.createdAt))}</span></div>`;
    }).join('');
  }

  async function detailBatch(action) {
    const rec = state.detail && state.detail.record;
    if (!rec) return;
    msg('un-detail-msg', '处理中…');
    try {
      const data = await api('/api/admin/unrecognized/batch', jsonBody({ ids: [rec.id], action: action }));
      await load();
      await openDetail(rec.id, true);
      msg('un-detail-msg', `${action === 'ignore' ? '已忽略' : '已重新打开'}（更新 ${data.updated} 条）`, 'ok');
    } catch (e) {
      msg('un-detail-msg', '操作失败：' + e.message, 'err');
    }
  }

  // ---------------------------------------------------------------- 反哺
  function openFeedback(opts) {
    const item = opts || {};
    state.feedbackKey = item.questionKey || null;
    state.feedbackIds = item.ids || [];
    $('un-fb-title').value = item.question || '';
    $('un-fb-dept').value = item.dept || '';
    $('un-fb-ktype').value = 'faq';
    $('un-fb-content').value = '';
    msg('un-feedback-msg', '');
    const banner = $('un-feedback-banner');
    if (!state.feedbackAvailable) {
      banner.hidden = false;
      banner.textContent = '当前知识存储为本地 JSON（只读静态索引），无法生成草稿。'
        + '请设置 KB_STORE=mysql 并启动数据库后重试；当前仍可导出 CSV 线下补充知识。';
    } else {
      banner.hidden = false;
      banner.textContent = '将生成「草稿」状态的知识文档，不会直接发布；'
        + '请在「知识文档」中补充标准答案并提交审核，发布后重建索引即可被检索。';
    }
    $('un-feedback-submit').disabled = !state.feedbackAvailable;
    $('un-feedback').hidden = false;
  }

  async function submitFeedback() {
    if (!state.feedbackAvailable) return;
    const title = $('un-fb-title').value.trim();
    const content = $('un-fb-content').value;
    if (!state.feedbackKey && !state.feedbackIds.length) {
      msg('un-feedback-msg', '缺少问题标识，请从列表重新发起', 'err');
      return;
    }
    const payload = {
      title: title || undefined,
      dept: $('un-fb-dept').value.trim() || undefined,
      ktype: $('un-fb-ktype').value.trim() || 'faq',
      content: content.trim() || undefined,
    };
    if (state.feedbackKey) payload.questionKey = state.feedbackKey;
    else payload.ids = state.feedbackIds;

    msg('un-feedback-msg', '生成中…');
    $('un-feedback-submit').disabled = true;
    try {
      const data = await api('/api/admin/unrecognized/feedback', jsonBody(payload));
      msg('un-feedback-msg',
        `已生成草稿 ${data.publicId || data.docId}（${data.statusName || '草稿'}），请到「知识文档」补充答案并提交审核`,
        'ok');
      await load();
      if (!$('un-detail').hidden && state.detail) await openDetail(state.detail.record.id, true);
    } catch (e) {
      msg('un-feedback-msg', '反哺失败：' + e.message, 'err');
      $('un-feedback-submit').disabled = false;
    }
  }

  // ---------------------------------------------------------------- 导出
  async function exportCsv() {
    const url = '/api/admin/unrecognized/export?' + buildQuery(false);
    msg('un-batch-msg', '导出中…');
    try {
      if (window.NXZAuth && window.NXZAuth.download) {
        await window.NXZAuth.download(url);
      } else {
        const resp = await authFetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const blob = await resp.blob();
        const objectUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objectUrl;
        a.download = '未识别问题.csv';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(objectUrl);
      }
      msg('un-batch-msg', `已导出当前筛选的 ${state.total} 组问题`, 'ok');
    } catch (e) {
      msg('un-batch-msg', '导出失败：' + e.message, 'err');
    }
  }

  // ---------------------------------------------------------------- 事件绑定
  function collectFilters() {
    state.filters = {
      state: $('un-state').value,
      source: $('un-source').value,
      dept: $('un-dept').value,
      role: $('un-role').value,
      channel: $('un-channel').value,
      dateFrom: $('un-from').value,
      dateTo: $('un-to').value,
      q: $('un-q').value.trim(),
    };
    state.sort = $('un-sort').value || 'recent';
  }

  function resetFilters() {
    ['un-state', 'un-source', 'un-dept', 'un-role', 'un-channel', 'un-from', 'un-to', 'un-q'].forEach((id) => {
      $(id).value = '';
    });
    $('un-sort').value = 'recent';
    state.filters = { state: '', source: '', dept: '', role: '', channel: '', dateFrom: '', dateTo: '', q: '' };
    state.sort = 'recent';
    state.page = 1;
    load();
  }

  function bind() {
    $('un-search').onclick = () => {
      collectFilters();
      state.page = 1;
      load();
    };
    $('un-q').onkeydown = (e) => { if (e.key === 'Enter') $('un-search').click(); };
    $('un-reset').onclick = resetFilters;
    $('un-export').onclick = exportCsv;
    $('un-sort').onchange = () => {
      state.sort = $('un-sort').value || 'recent';
      state.page = 1;
      load();
    };
    $('un-prev').onclick = () => { if (state.page > 1) { state.page -= 1; load(); } };
    $('un-next').onclick = () => { state.page += 1; load(); };
    $('un-chk-all').onchange = (e) => {
      state.selected.clear();
      document.querySelectorAll('[data-chk]').forEach((box) => {
        box.checked = e.target.checked;
        if (e.target.checked) state.selected.add(box.getAttribute('data-chk'));
      });
      syncBatch();
    };

    $('un-batch-ignore').onclick = () => runBatch('ignore');
    $('un-batch-reopen').onclick = () => runBatch('reopen');
    $('un-batch-classify').onclick = () => runBatch('classify', null, $('un-batch-dept').value.trim());
    $('un-batch-feedback').onclick = () => {
      if (state.selected.size !== 1) {
        msg('un-batch-msg', '反哺一次只针对一组问题，请只勾选一组', 'err');
        return;
      }
      const key = Array.from(state.selected)[0];
      const item = state.items.find((x) => x.questionKey === key);
      if (!item) { msg('un-batch-msg', '未找到所选问题组', 'err'); return; }
      openFeedback({ questionKey: item.questionKey, question: item.question, dept: item.dept, ids: item.ids });
    };
    $('un-batch-clear').onclick = () => {
      state.selected.clear();
      document.querySelectorAll('[data-chk]').forEach((box) => { box.checked = false; });
      msg('un-batch-msg', '');
      syncBatch();
    };

    $('un-detail-close').onclick = () => { $('un-detail').hidden = true; };
    $('un-detail-close-btn').onclick = () => { $('un-detail').hidden = true; };
    $('un-detail-mask').onclick = () => { $('un-detail').hidden = true; };
    $('un-detail-ignore').onclick = () => detailBatch('ignore');
    $('un-detail-reopen').onclick = () => detailBatch('reopen');
    $('un-detail-feedback').onclick = () => {
      if (!state.detail) return;
      const rec = state.detail.record || {};
      openFeedback({ questionKey: rec.questionKey, question: rec.question, dept: rec.dept, ids: [rec.id] });
    };

    $('un-feedback-close').onclick = () => { $('un-feedback').hidden = true; };
    $('un-feedback-cancel').onclick = () => { $('un-feedback').hidden = true; };
    $('un-feedback-mask').onclick = () => { $('un-feedback').hidden = true; };
    $('un-feedback-submit').onclick = submitFeedback;

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        $('un-detail').hidden = true;
        $('un-feedback').hidden = true;
      }
    });
  }

  // ---------------------------------------------------------------- 启动
  function boot() {
    bind();
    const ready = window.NXZAuth ? window.NXZAuth.ready() : Promise.resolve();
    ready.then(async () => {
      if (window.NXZAuth && window.NXZAuth.me.authEnabled && !window.NXZAuth.can('unrecognized.view')) return;
      const ok = await loadFilters();
      if (ok) await load();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 供浏览器验收工具断言页面初始化完成
  window.__NXZ_UNRECOGNIZED__ = {
    state: state,
    reload: load,
    openDetail: openDetail,
    openFeedback: openFeedback,
  };
})();
