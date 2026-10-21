/* 能小智 · 客服工作台（任务 4.6 / P-B-09～13、M-S-02～06）
 *
 * 对接：
 *   GET   /api/admin/support/overview
 *   GET   /api/admin/support/filters
 *   GET   /api/admin/support/tickets
 *   GET   /api/admin/support/tickets/{id}
 *   POST  /api/admin/support/tickets/{id}/claim | reply | close | assign
 *   GET   /api/admin/support/reply-logs
 *   GET/POST/PATCH/DELETE /api/admin/support/groups
 *   GET/POST/PATCH/DELETE /api/admin/support/seats
 *   POST  /api/admin/support/seats/{id}/status
 *   WS    /api/support/ws?seatId=
 *
 * 所有服务端文本都用 textContent 写入，不拼接 innerHTML。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const LS_SEAT = 'nxz.supportSeat';
  const view = {
    page: 1,
    pageSize: 20,
    total: 0,
    current: null,
    groups: [],
    seats: [],
    seatId: null,
    ticketId: null,
    ws: null,
    wsReady: false,
    wsTimer: null,
    wsGen: 0,
    refreshTimer: null,
    editingGroup: null,
    editingSeat: null,
    loading: false,
  };

  const STATE_CLASS = { 1: 'queued', 2: 'active', 3: 'offline', 4: 'replied', 5: 'closed' };
  const STATE_LABEL = { 1: '待接入', 2: '接待中', 3: '离线留言', 4: '已回复', 5: '已关闭' };

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
    if (isNaN(date.getTime())) return String(raw);
    const pad = (n) => String(n).padStart(2, '0');
    const base = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
    return short ? base : `${date.getFullYear()}-${base}`;
  }

  function msg(text, kind) {
    const line = $('sp-msg');
    line.textContent = text || '';
    line.className = 'msg-line' + (kind ? ' ' + kind : '');
  }

  function opMsg(text, kind) {
    const line = $('sp-action-msg');
    line.textContent = text || '';
    line.className = 'sp-hint' + (kind === 'err' ? ' err' : '');
  }

  function toast(text) {
    const box = el('div', 'nxz-toast', text);
    document.body.appendChild(box);
    setTimeout(() => box.classList.add('show'), 10);
    setTimeout(() => {
      box.classList.remove('show');
      setTimeout(() => box.remove(), 220);
    }, 2600);
  }

  function seat() {
    return view.seats.find((s) => Number(s.seatId) === Number(view.seatId)) || null;
  }

  function seatOnline() {
    const current = seat();
    return !!(current && current.online);
  }

  function option(value, label, selected) {
    const opt = document.createElement('option');
    opt.value = String(value);
    opt.textContent = label;
    if (selected) opt.selected = true;
    return opt;
  }

  function fillSelect(select, items, placeholder, valueKey, label, keep) {
    const previous = keep === undefined ? select.value : keep;
    select.textContent = '';
    if (placeholder !== null) select.appendChild(option('', placeholder));
    for (const item of items) {
      select.appendChild(option(item[valueKey], label(item), String(previous) === String(item[valueKey])));
    }
    if (placeholder !== null && previous === '') select.value = '';
  }

  // ------------------------------------------------------------------ 概览 / 筛选
  function renderOverview(data) {
    $('sp-total').textContent = data.total != null ? data.total : 0;
    $('sp-queued').textContent = data.queued != null ? data.queued : 0;
    $('sp-active').textContent = data.active != null ? data.active : 0;
    $('sp-offline').textContent = data.offline != null ? data.offline : 0;
    $('sp-replied').textContent = data.replied != null ? data.replied : 0;
    $('sp-negative').textContent = data.negativeOpen != null ? data.negativeOpen : 0;
    $('sp-closed-today').textContent = data.closedToday != null ? data.closedToday : 0;
    $('sp-online-seats').textContent = `${data.onlineSeats || 0}/${data.totalSeats || 0}`;
    $('sp-meta').textContent = `未读用户消息 ${data.unreadAgent || 0} 条 · 在线连接 ${data.wsConnections || 0}`;
  }

  async function loadOverview() {
    const data = await api('/api/admin/support/overview');
    renderOverview(data);
  }

  function renderGroupsAndSeats(data) {
    view.groups = Array.isArray(data.groups) ? data.groups : [];
    view.seats = Array.isArray(data.seats) ? data.seats : [];

    const filterGroup = $('sp-group');
    fillSelect(filterGroup, view.groups, '全部分组', 'groupId', (g) => g.groupName);
    const filterSeat = $('sp-seat-filter');
    fillSelect(filterSeat, view.seats, '全部坐席', 'seatId', (s) => s.seatName);

    const assignGroup = $('sp-assign-group');
    const activeGroups = view.groups.filter((g) => Number(g.status) === 1);
    fillSelect(assignGroup, activeGroups, '转交分组', 'groupId', (g) => g.groupName);
    fillAssignSeats();

    const formGroup = $('sps-group');
    fillSelect(formGroup, view.groups, '未分组', 'groupId', (g) => g.groupName, formGroup.value);

    const picker = $('sp-seat');
    const previous = view.seatId || Number(localStorage.getItem(LS_SEAT) || 0);
    picker.textContent = '';
    for (const item of view.seats) {
      picker.appendChild(option(item.seatId, `${item.seatName}${item.online ? ' · 在线' : ' · 离线'}`, Number(item.seatId) === Number(previous)));
    }
    if (!view.seats.length) {
      picker.appendChild(option('', '暂无坐席', true));
      view.seatId = null;
    } else {
      const exists = view.seats.some((s) => Number(s.seatId) === Number(previous));
      if (exists) view.seatId = Number(previous);
      else {
        const online = view.seats.find((s) => s.online) || view.seats[0];
        view.seatId = Number(online.seatId);
      }
      picker.value = String(view.seatId);
    }

    renderGroupRows();
    renderSeatRows();
  }

  function fillAssignSeats() {
    const groupId = Number($('sp-assign-group').value || 0);
    const list = groupId
      ? view.seats.filter((s) => Number(s.groupId) === groupId)
      : view.seats;
    fillSelect($('sp-assign-seat'), list, '转交坐席', 'seatId', (s) => s.seatName);
  }

  async function loadFilters() {
    const data = await api('/api/admin/support/filters');
    const stateSel = $('sp-state');
    const previous = stateSel.value;
    stateSel.textContent = '';
    stateSel.appendChild(option('', '全部状态'));
    for (const item of data.states || []) {
      stateSel.appendChild(option(item.value, item.label, String(previous) === String(item.value)));
    }
    renderGroupsAndSeats(data);
    await ensureSeatOnline();
    connectWs();
  }

  // ------------------------------------------------------------------ 工单列表
  function queryParams() {
    const params = new URLSearchParams();
    if ($('sp-state').value) params.set('state', $('sp-state').value);
    if ($('sp-sentiment').value) params.set('sentiment', $('sp-sentiment').value);
    if ($('sp-group').value) params.set('groupId', $('sp-group').value);
    if ($('sp-seat-filter').value) params.set('seatId', $('sp-seat-filter').value);
    if ($('sp-channel').value.trim()) params.set('channelCode', $('sp-channel').value.trim());
    if ($('sp-q').value.trim()) params.set('q', $('sp-q').value.trim());
    params.set('page', String(view.page));
    params.set('pageSize', String(view.pageSize));
    return params;
  }

  function ticketBadge(state) {
    const cls = STATE_CLASS[state] || '';
    return el('span', 'sp-badge ' + cls, STATE_LABEL[state] || '未知');
  }

  function renderTicketList(data) {
    view.total = Number(data.total) || 0;
    const list = $('sp-ticket-list');
    list.textContent = '';
    const items = Array.isArray(data.items) ? data.items : [];
    $('sp-ticket-empty').hidden = items.length > 0;
    $('sp-list-count').textContent = `${view.total} 条`;

    for (const ticket of items) {
      const row = el('button', 'sp-ticket');
      row.type = 'button';
      if (Number(ticket.ticketId) === Number(view.ticketId)) row.classList.add('active');
      const top = el('div', 'sp-ticket-top');
      top.appendChild(el('span', 'sp-ticket-no', ticket.ticketNo || `#${ticket.ticketId}`));
      top.appendChild(ticketBadge(Number(ticket.state)));
      if (Number(ticket.sentiment) === 1) top.appendChild(el('span', 'sp-badge danger', '负面'));
      row.appendChild(top);
      row.appendChild(el('div', 'sp-ticket-subject', ticket.subject || '（无问题描述）'));
      const foot = el('div', 'sp-ticket-foot');
      foot.appendChild(el('span', null, ticket.userName || ticket.userNo || '匿名'));
      foot.appendChild(el('span', null, ticket.seatName || '未分配'));
      foot.appendChild(el('span', null, fmtTime(ticket.updatedAt, true)));
      if (Number(ticket.unreadAgent) > 0) {
        foot.appendChild(el('span', 'sp-unread', ticket.unreadAgent > 99 ? '99+' : String(ticket.unreadAgent)));
      }
      row.appendChild(foot);
      row.onclick = () => openTicket(Number(ticket.ticketId));
      list.appendChild(row);
    }

    const totalPages = Math.max(1, Math.ceil(view.total / view.pageSize));
    if (view.page > totalPages) view.page = totalPages;
    $('sp-page-label').textContent = `第 ${view.page} / ${totalPages} 页`;
    $('sp-prev').disabled = view.page <= 1;
    $('sp-next').disabled = view.page >= totalPages;
  }

  async function loadTickets() {
    if (view.loading) return;
    view.loading = true;
    try {
      const data = await api('/api/admin/support/tickets?' + queryParams().toString());
      renderTicketList(data);
      if (data.overview) renderOverview(data.overview);
      msg('');
    } catch (err) {
      msg(err.message || '加载工单失败', 'err');
    } finally {
      view.loading = false;
    }
  }

  // ------------------------------------------------------------------ 会话详情
  function renderThread(ticket) {
    const thread = $('sp-thread');
    thread.textContent = '';
    const messages = Array.isArray(ticket.messages) ? ticket.messages : [];
    if (!messages.length) {
      thread.appendChild(el('div', 'sp-thread-empty', '该工单还没有消息。'));
      return;
    }
    for (const item of messages) {
      const type = Number(item.senderType);
      const role = type === 1 ? 'user' : type === 2 ? 'agent' : 'system';
      const row = el('div', 'sp-msg ' + role);
      const who = item.senderName || (role === 'user' ? ticket.userName : role === 'agent' ? '客服' : '系统');
      row.appendChild(el('div', 'sp-msg-meta', `${who} · ${fmtTime(item.createdAt, true)}`));
      row.appendChild(el('div', 'sp-msg-body', item.content || ''));
      let sent = '';
      if (role === 'agent') sent = item.pushed ? '用户已读' : '待用户查看';
      else if (role === 'user') sent = item.read ? '坐席已读' : '未读';
      if (sent) row.appendChild(el('div', 'sp-msg-sent', sent));
      thread.appendChild(row);
    }
    thread.scrollTop = thread.scrollHeight;
  }

  function setBadge(node, state, text, cls) {
    node.hidden = false;
    node.className = 'sp-badge ' + (cls || '');
    node.textContent = text;
  }

  function renderTicket(ticket) {
    view.current = ticket;
    const closed = Number(ticket.state) === 5;
    $('sp-thread-title').textContent = ticket.subject || `工单 ${ticket.ticketNo || ''}`;
    $('sp-thread-sub').textContent = `${ticket.ticketNo || ''} · ${ticket.userName || ticket.userNo || '匿名用户'}`;
    setBadge($('sp-thread-state'), ticket.state, STATE_LABEL[Number(ticket.state)] || '未知', STATE_CLASS[Number(ticket.state)]);
    $('sp-thread-sentiment').hidden = Number(ticket.sentiment) !== 1;

    $('sp-ticket-meta').hidden = false;
    $('sp-meta-user').textContent = `用户：${ticket.userName || '-'}（${ticket.userNo || '-'} · ${ticket.role || '访客'}）`;
    $('sp-meta-group').textContent = `分组：${ticket.groupName || '未分组'}`;
    $('sp-meta-channel').textContent = `渠道：${ticket.channelCode || '未标注'}`;
    $('sp-meta-time').textContent = `创建：${fmtTime(ticket.createdAt)}`;

    renderThread(ticket);

    $('sp-ticket-ops').hidden = false;
    $('sp-composer').hidden = false;
    const mine = seat();
    const assigned = ticket.seatId != null;
    const mineAssigned = assigned && Number(ticket.seatId) === Number(view.seatId);
    const claimable = !closed && (!assigned || mineAssigned) && [1, 3].includes(Number(ticket.state));
    $('sp-claim').hidden = !claimable;
    $('sp-claim').disabled = !seatOnline();
    $('sp-assign-group').value = ticket.groupId != null ? String(ticket.groupId) : '';
    fillAssignSeats();
    if (ticket.seatId != null) $('sp-assign-seat').value = String(ticket.seatId);
    $('sp-assign').disabled = closed;
    $('sp-close').hidden = closed;
    $('sp-close').disabled = !seatOnline();

    let hint = '';
    if (closed) hint = '会话已结束，不能继续回复。';
    else if (!mine) hint = '请先在上方选择客服坐席。';
    else if (!mine.online) hint = '当前坐席未上线，请点击顶栏坐席或等待工作台连接。';
    else if (assigned && !mineAssigned) hint = `工单由「${ticket.seatName || '其他坐席'}」接待，可直接转交给自己。`;
    $('sp-send').disabled = closed || !mine || !mine.online || (assigned && !mineAssigned);
    $('sp-send').title = hint || '';
    opMsg(hint);
    if (!closed && !$('sp-reply').disabled) $('sp-reply').placeholder = '输入回复内容，Ctrl + Enter 发送';
  }

  async function openTicket(ticketId) {
    view.ticketId = Number(ticketId);
    try {
      const ticket = await api(`/api/admin/support/tickets/${ticketId}`);
      renderTicket(ticket);
      await loadTickets();
    } catch (err) {
      opMsg(err.message || '加载工单失败', 'err');
    }
  }

  // ------------------------------------------------------------------ 操作
  async function ensureSeatOnline() {
    const current = seat();
    if (!current || current.online) return;
    try {
      await api(`/api/admin/support/seats/${current.seatId}/status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ online: true }),
      });
      current.online = true;
    } catch (err) {
      msg('坐席上线失败：' + (err.message || err), 'err');
    }
  }

  function connectWs() {
    if (!view.seatId) return;
    const generation = ++view.wsGen;
    if (view.ws) {
      try { view.ws.onclose = null; view.ws.close(); } catch (_) { /* ignore */ }
      view.ws = null;
    }
    if (view.wsTimer) { clearTimeout(view.wsTimer); view.wsTimer = null; }

    const wsUrl = new URL('/api/support/ws', location.origin);
    wsUrl.protocol = wsUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    wsUrl.searchParams.set('seatId', String(view.seatId));
    const token = window.NXZAuth ? window.NXZAuth.token : '';
    const protocols = token ? ['nxz', token] : ['nxz'];

    let socket;
    try {
      socket = new WebSocket(wsUrl.toString(), protocols);
    } catch (_) {
      setWs(false, '连接失败');
      return;
    }
    view.ws = socket;
    setWs(false, '连接中…');

    socket.onopen = () => {
      if (generation !== view.wsGen) return;
      view.wsReady = true;
      setWs(true, '实时在线');
    };
    socket.onmessage = (event) => {
      let data = null;
      try { data = JSON.parse(event.data); } catch (_) { return; }
      if (!data || data.type === 'ping') return;
      if (data.type === 'hello') {
        view.wsReady = true;
        setWs(true, '实时在线');
        return;
      }
      if (data.type === 'ticket.created') {
        const t = data.ticket || {};
        toast(`新工单：${t.subject || t.ticketNo || ''}`);
      }
      scheduleRefresh();
    };
    socket.onerror = () => {
      if (generation === view.wsGen) setWs(false, '连接异常');
    };
    socket.onclose = () => {
      if (generation !== view.wsGen) return;
      view.wsReady = false;
      setWs(false, view.seatId ? '已断开，重连中…' : '未连接');
      view.wsTimer = setTimeout(() => {
        if (generation === view.wsGen && view.seatId) connectWs();
      }, 4000);
    };
  }

  function setWs(online, text) {
    const node = $('sp-ws');
    node.textContent = text || (online ? '实时在线' : '未连接');
    node.className = 'sp-ws ' + (online ? 'online' : 'offline');
  }

  function scheduleRefresh() {
    if (view.refreshTimer) return;
    view.refreshTimer = setTimeout(async () => {
      view.refreshTimer = null;
      await loadTickets();
      if (view.ticketId) {
        try {
          const ticket = await api(`/api/admin/support/tickets/${view.ticketId}`);
          if (Number(ticket.ticketId) === Number(view.ticketId)) renderTicket(ticket);
        } catch (_) { /* ignore */ }
      }
      loadLogs();
    }, 280);
  }

  async function reply() {
    const content = $('sp-reply').value.trim();
    if (!content || !view.ticketId) return;
    const current = seat();
    if (!current) { opMsg('请先选择客服坐席', 'err'); return; }
    $('sp-send').disabled = true;
    opMsg('发送中…');
    try {
      const data = await api(`/api/admin/support/tickets/${view.ticketId}/reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, seatId: current.seatId }),
      });
      $('sp-reply').value = '';
      if (data && data.ticket) renderTicket(data.ticket);
      opMsg('已回复用户。', 'ok');
      await loadTickets();
      loadLogs();
    } catch (err) {
      opMsg(err.message || '发送失败', 'err');
      $('sp-send').disabled = false;
    }
  }

  async function claim() {
    if (!view.ticketId) return;
    const current = seat();
    if (!current) { opMsg('请先选择客服坐席', 'err'); return; }
    $('sp-claim').disabled = true;
    opMsg('接入中…');
    try {
      const ticket = await api(
        `/api/admin/support/tickets/${view.ticketId}/claim?seatId=${encodeURIComponent(current.seatId)}`,
        { method: 'POST' }
      );
      renderTicket(ticket);
      opMsg('已接入工单。', 'ok');
      await loadTickets();
    } catch (err) {
      opMsg(err.message || '接入失败', 'err');
      $('sp-claim').disabled = false;
    }
  }

  async function closeTicket() {
    if (!view.ticketId) return;
    const current = seat();
    try {
      const ticket = await api(`/api/admin/support/tickets/${view.ticketId}/close`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seatId: current ? current.seatId : null, reason: '客服结束' }),
      });
      renderTicket(ticket);
      opMsg('会话已结束。', 'ok');
      await loadTickets();
    } catch (err) {
      opMsg(err.message || '结束失败', 'err');
    }
  }

  async function assignTicket() {
    if (!view.ticketId) return;
    const groupId = Number($('sp-assign-group').value || 0);
    const seatId = Number($('sp-assign-seat').value || 0);
    if (!groupId && !seatId) { opMsg('请选择要转交的分组或坐席', 'err'); return; }
    try {
      const ticket = await api(`/api/admin/support/tickets/${view.ticketId}/assign`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ groupId: groupId || null, seatId: seatId || null }),
      });
      renderTicket(ticket);
      opMsg('已转交。', 'ok');
      await loadTickets();
    } catch (err) {
      opMsg(err.message || '转交失败', 'err');
    }
  }

  // ------------------------------------------------------------------ 分组管理
  function renderGroupRows() {
    const box = $('sp-group-rows');
    box.textContent = '';
    for (const group of view.groups) {
      const row = el('div', 'sp-row');
      const main = el('div', 'sp-row-main');
      main.appendChild(el('b', null, group.groupName));
      const scope = group.channelIds ? `渠道：${group.channelIds}` : '渠道：全部（兜底）';
      main.appendChild(el('small', null, `${scope} · 坐席 ${group.seatCount || 0}（在线 ${group.onlineSeats || 0}）${group.status ? '' : ' · 已停用'}`));
      row.appendChild(main);
      const ops = el('div', 'sp-row-ops');
      const edit = el('button', null, '编辑');
      edit.type = 'button';
      edit.onclick = () => editGroup(group);
      ops.appendChild(edit);
      const del = el('button', 'danger', '删除');
      del.type = 'button';
      del.onclick = () => removeGroup(group.groupId);
      ops.appendChild(del);
      row.appendChild(ops);
      box.appendChild(row);
    }
    if (!view.groups.length) box.appendChild(el('div', 'empty', '暂无分组'));
  }

  function resetGroupForm() {
    view.editingGroup = null;
    $('spg-name').value = '';
    $('spg-channels').value = '';
    $('spg-desc').value = '';
    $('spg-status').value = '1';
    $('sp-group-form-title').textContent = '新建客服分组';
    $('spg-submit').textContent = '新建分组';
    $('spg-cancel').hidden = true;
    $('spg-msg').textContent = '';
  }

  function editGroup(group) {
    view.editingGroup = Number(group.groupId);
    $('spg-name').value = group.groupName || '';
    $('spg-channels').value = group.channelIds || '';
    $('spg-desc').value = group.description || '';
    $('spg-status').value = String(group.status || 0);
    $('sp-group-form-title').textContent = `编辑分组 · ${group.groupName}`;
    $('spg-submit').textContent = '保存分组';
    $('spg-cancel').hidden = false;
    $('spg-name').focus();
  }

  async function submitGroup() {
    const payload = {
      groupName: $('spg-name').value.trim(),
      channelIds: $('spg-channels').value.trim(),
      description: $('spg-desc').value.trim(),
      status: Number($('spg-status').value),
    };
    try {
      if (view.editingGroup) {
        await api(`/api/admin/support/groups/${view.editingGroup}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      } else {
        await api('/api/admin/support/groups', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      }
      resetGroupForm();
      await loadFilters();
      await loadTickets();
      toast('分组已保存');
    } catch (err) {
      $('spg-msg').textContent = err.message || '保存失败';
    }
  }

  async function removeGroup(groupId) {
    try {
      await api(`/api/admin/support/groups/${groupId}`, { method: 'DELETE' });
      if (view.editingGroup === Number(groupId)) resetGroupForm();
      await loadFilters();
      toast('分组已删除');
    } catch (err) {
      $('spg-msg').textContent = err.message || '删除失败';
    }
  }

  // ------------------------------------------------------------------ 坐席管理
  function renderSeatRows() {
    const box = $('sp-seat-rows');
    box.textContent = '';
    for (const item of view.seats) {
      const row = el('div', 'sp-row');
      const main = el('div', 'sp-row-main');
      main.appendChild(el('b', null, `${item.seatName}${item.online ? ' · 在线' : ''}`));
      main.appendChild(el('small', null, `${item.userNo} · ${item.groupName || '未分组'} · 接待 ${item.activeCount || 0}/${item.maxSessions}`));
      row.appendChild(main);
      const ops = el('div', 'sp-row-ops');
      const toggle = el('button', item.online ? 'online' : '', item.online ? '下线' : '上线');
      toggle.type = 'button';
      toggle.onclick = () => toggleSeat(item);
      ops.appendChild(toggle);
      const edit = el('button', null, '编辑');
      edit.type = 'button';
      edit.onclick = () => editSeat(item);
      ops.appendChild(edit);
      const del = el('button', 'danger', '删除');
      del.type = 'button';
      del.onclick = () => removeSeat(item.seatId);
      ops.appendChild(del);
      row.appendChild(ops);
      box.appendChild(row);
    }
    if (!view.seats.length) box.appendChild(el('div', 'empty', '暂无坐席'));
  }

  function resetSeatForm() {
    view.editingSeat = null;
    $('sps-user').value = '';
    $('sps-name').value = '';
    $('sps-group').value = '';
    $('sps-max').value = '5';
    $('sps-online').checked = false;
    $('sp-seat-form-title').textContent = '新增客服坐席';
    $('sps-submit').textContent = '新增坐席';
    $('sps-cancel').hidden = true;
    $('sps-msg').textContent = '';
  }

  function editSeat(item) {
    view.editingSeat = Number(item.seatId);
    $('sps-user').value = item.userNo || '';
    $('sps-name').value = item.seatName || '';
    $('sps-group').value = item.groupId != null ? String(item.groupId) : '';
    $('sps-max').value = String(item.maxSessions || 5);
    $('sps-online').checked = !!item.online;
    $('sp-seat-form-title').textContent = `编辑坐席 · ${item.seatName}`;
    $('sps-submit').textContent = '保存坐席';
    $('sps-cancel').hidden = false;
    $('sps-user').focus();
  }

  async function submitSeat() {
    const payload = {
      userNo: $('sps-user').value.trim(),
      seatName: $('sps-name').value.trim(),
      groupId: $('sps-group').value ? Number($('sps-group').value) : null,
      maxSessions: Number($('sps-max').value || 5),
      online: $('sps-online').checked,
    };
    try {
      if (view.editingSeat) {
        await api(`/api/admin/support/seats/${view.editingSeat}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      } else {
        await api('/api/admin/support/seats', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      }
      resetSeatForm();
      await loadFilters();
      await loadTickets();
      toast('坐席已保存');
    } catch (err) {
      $('sps-msg').textContent = err.message || '保存失败';
    }
  }

  async function toggleSeat(item) {
    try {
      await api(`/api/admin/support/seats/${item.seatId}/status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ online: !item.online }),
      });
      await loadFilters();
      await loadTickets();
      toast(item.online ? '坐席已下线' : '坐席已上线');
    } catch (err) {
      $('sps-msg').textContent = err.message || '操作失败';
    }
  }

  async function removeSeat(seatId) {
    try {
      await api(`/api/admin/support/seats/${seatId}`, { method: 'DELETE' });
      if (view.editingSeat === Number(seatId)) resetSeatForm();
      if (Number(view.seatId) === Number(seatId)) view.seatId = null;
      await loadFilters();
      await loadTickets();
      toast('坐席已删除');
    } catch (err) {
      $('sps-msg').textContent = err.message || '删除失败';
    }
  }

  // ------------------------------------------------------------------ 回复日志
  async function loadLogs() {
    try {
      const rows = await api('/api/admin/support/reply-logs?limit=50');
      const body = $('sp-log-rows');
      body.textContent = '';
      $('sp-log-count').textContent = `${rows.length} 条`;
      $('sp-log-empty').hidden = rows.length > 0;
      for (const row of rows) {
        const tr = document.createElement('tr');
        tr.appendChild(el('td', null, row.ticketNo || row.ticketId));
        tr.appendChild(el('td', null, row.subject || ''));
        tr.appendChild(el('td', null, row.userNo || ''));
        tr.appendChild(el('td', null, row.seatName || ''));
        tr.appendChild(el('td', null, row.messageCount || 0));
        tr.appendChild(el('td', null, fmtTime(row.lastReplyAt, true)));
        const stateCell = document.createElement('td');
        stateCell.appendChild(ticketBadge(Number(row.state)));
        tr.appendChild(stateCell);
        body.appendChild(tr);
      }
    } catch (err) {
      msg('回复日志加载失败：' + (err.message || err), 'err');
    }
  }

  // ------------------------------------------------------------------ 事件
  $('sp-apply').onclick = () => { view.page = 1; loadTickets(); };
  $('sp-refresh').onclick = () => { loadFilters().then(loadTickets); loadLogs(); };
  $('sp-reset').onclick = () => {
    $('sp-state').value = '';
    $('sp-sentiment').value = '';
    $('sp-group').value = '';
    $('sp-seat-filter').value = '';
    $('sp-channel').value = '';
    $('sp-q').value = '';
    view.page = 1;
    loadTickets();
  };
  $('sp-q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { view.page = 1; loadTickets(); }
  });
  $('sp-prev').onclick = () => { if (view.page > 1) { view.page -= 1; loadTickets(); } };
  $('sp-next').onclick = () => { view.page += 1; loadTickets(); };
  $('sp-assign-group').onchange = () => fillAssignSeats();
  $('sp-seat').onchange = async () => {
    view.seatId = Number($('sp-seat').value) || null;
    try { localStorage.setItem(LS_SEAT, String(view.seatId || '')); } catch (_) { /* ignore */ }
    await ensureSeatOnline();
    connectWs();
    await loadTickets();
    if (view.current) renderTicket(view.current);
  };
  $('sp-claim').onclick = claim;
  $('sp-send').onclick = reply;
  $('sp-close').onclick = closeTicket;
  $('sp-assign').onclick = assignTicket;
  $('sp-reply').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); reply(); }
  });
  ['spg-name', 'spg-channels', 'spg-desc'].forEach((id) => {
    $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') submitGroup(); });
  });
  $('spg-submit').onclick = submitGroup;
  $('spg-cancel').onclick = resetGroupForm;
  $('sps-submit').onclick = submitSeat;
  $('sps-cancel').onclick = resetSeatForm;
  window.addEventListener('beforeunload', () => {
    view.wsGen += 1;
    if (view.ws) { try { view.ws.close(); } catch (_) { /* ignore */ } }
  });

  async function boot() {
    try {
      await loadFilters();
      await loadTickets();
      await loadLogs();
    } catch (err) {
      msg('初始化失败：' + (err.message || err), 'err');
    }
    // 工作台每 30 秒兜底刷新：WebSocket 断开时仍能看到新工单
    setInterval(() => { if (document.visibilityState === 'visible') loadTickets(); }, 30000);
  }

  boot();
})();
