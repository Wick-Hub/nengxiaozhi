/* 能小智 · 用户端人工客服（任务 4.6 / P-B-09～13、M-S-03·04）
 *
 * 对话页顶栏「转人工」→ 面板内创建工单 / 离线留言 / 继续会话 / 结束会话。
 * 用户侧只持有创建时下发的 accessKey（localStorage 按会话保存），
 * 所有查询与发言都带该凭证；页面不渲染任何 HTML 字符串，避免注入。
 */
(() => {
  const btn = document.getElementById('support-open');
  const panel = document.getElementById('support-panel');
  const mask = document.getElementById('support-mask');
  const closeBtn = document.getElementById('support-close');
  const thread = document.getElementById('support-thread');
  const input = document.getElementById('support-input');
  const sendBtn = document.getElementById('support-send');
  const endBtn = document.getElementById('support-end');
  const stateEl = document.getElementById('support-state');
  const noticeEl = document.getElementById('support-notice');
  const msgEl = document.getElementById('support-msg');
  const badgeEl = document.getElementById('support-badge');
  if (!btn || !panel || !thread || !input || !sendBtn) return;

  const LS_TOKEN = 'nxz_token';
  const LS_SESSION = 'nxz.sessionId';
  const LS_TICKET = 'nxz.support.ticket';
  const POLL_MS = 4000;
  const STATE_LABELS = { 1: '待接入', 2: '接待中', 3: '离线留言', 4: '已回复', 5: '已结束' };

  let enabled = false;
  let busy = false;
  let ticket = null;
  let accessKey = '';
  let timer = null;
  let noticeTimer = null;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function message(text) {
    if (!msgEl) return;
    msgEl.textContent = text || '';
    msgEl.classList.toggle('error', !!text);
  }

  function authHeaders() {
    const token = localStorage.getItem(LS_TOKEN);
    return token ? { Authorization: 'Bearer ' + token } : {};
  }

  async function api(path, options = {}) {
    const headers = Object.assign({}, authHeaders(), options.headers || {});
    if (options.body) headers['Content-Type'] = 'application/json';
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    let body = null;
    try { body = await resp.json(); } catch { /* 非 JSON 错误页 */ }
    if (!resp.ok) {
      const text = (body && (body.error || body.detail)) || '';
      const err = new Error(typeof text === 'string' && text ? text : '请求失败 HTTP ' + resp.status);
      err.status = resp.status;
      throw err;
    }
    return body && body.data !== undefined ? body.data : body;
  }

  function loadStored() {
    try {
      const raw = JSON.parse(localStorage.getItem(LS_TICKET) || 'null');
      if (raw && raw.ticketId && raw.accessKey) {
        ticket = { ticketId: Number(raw.ticketId) };
        accessKey = String(raw.accessKey);
        return true;
      }
    } catch { /* 损坏的本地缓存直接丢弃 */ }
    return false;
  }

  function saveStored() {
    if (!ticket) {
      localStorage.removeItem(LS_TICKET);
      return;
    }
    localStorage.setItem(LS_TICKET, JSON.stringify({
      ticketId: ticket.ticketId,
      accessKey,
      sessionId: ticket.sessionId || currentSession(),
    }));
  }

  function currentSession() {
    return localStorage.getItem(LS_SESSION) || '';
  }

  function role() {
    const node = document.getElementById('role');
    return node ? node.value : '访客';
  }

  function channelCode() {
    return new URLSearchParams(location.search).get('channel') || '';
  }

  function showNotice(text) {
    if (!noticeEl) return;
    noticeEl.textContent = text || '';
    noticeEl.hidden = !text;
    if (noticeTimer) clearTimeout(noticeTimer);
    if (text) noticeTimer = setTimeout(() => { noticeEl.hidden = true; }, 6000);
  }

  function setState(value) {
    const label = STATE_LABELS[value] || '';
    stateEl.textContent = label;
    stateEl.hidden = !label;
    stateEl.dataset.state = String(value || '');
    endBtn.hidden = !ticket || Number(value) === 5;
  }

  function render(t) {
    if (!t) return;
    ticket = Object.assign({}, ticket, t);
    if (t.accessKey) accessKey = String(t.accessKey);
    thread.textContent = '';
    const messages = Array.isArray(t.messages) ? t.messages : [];
    if (!messages.length) {
      thread.appendChild(el('div', 'support-empty', '说说你遇到的问题，客服上线后会尽快回复。'));
    }
    for (const m of messages) {
      const kind = Number(m.senderType) === 1 ? 'user' : Number(m.senderType) === 2 ? 'agent' : 'system';
      const row = el('div', 'support-message ' + kind);
      if (kind !== 'user') {
        row.appendChild(el('div', 'support-sender', m.senderName || (kind === 'agent' ? '客服' : '系统')));
      }
      row.appendChild(el('div', 'support-bubble', m.content || ''));
      if (m.createdAt) {
        row.appendChild(el('div', 'support-time', String(m.createdAt).replace('T', ' ').slice(5, 16)));
      }
      thread.appendChild(row);
    }
    thread.scrollTop = thread.scrollHeight;
    setState(Number(t.state || 0));
  }

  function stopPolling() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  function startPolling() {
    stopPolling();
    timer = setInterval(refresh, POLL_MS);
  }

  async function refresh() {
    if (!ticket || !accessKey || busy) return;
    try {
      const data = await api(
        '/api/support/tickets/' + encodeURIComponent(ticket.ticketId) +
        '?accessKey=' + encodeURIComponent(accessKey)
      );
      render(data.ticket);
      if (data.notice && data.notice.text) showNotice(data.notice.text);
      if (Number(data.ticket.state) === 5) {
        stopPolling();
        saveStored();
      }
    } catch (err) {
      if (err.status === 403 || err.status === 404) {
        ticket = null;
        accessKey = '';
        saveStored();
        thread.textContent = '';
        setState(0);
        message('原会话凭证已失效，请重新发送以发起转人工。');
        stopPolling();
      }
    }
  }

  async function open() {
    panel.hidden = false;
    document.body.classList.add('support-open-body');
    btn.classList.remove('attention');
    if (badgeEl) badgeEl.hidden = true;
    message('');
    if (!ticket) loadStored();
    if (ticket) {
      await refresh();
      if (ticket && Number(ticket.state) !== 5) startPolling();
    } else {
      prefill();
      startPolling();
    }
    input.focus();
  }

  function closePanel() {
    panel.hidden = true;
    document.body.classList.remove('support-open-body');
    stopPolling();
  }

  function prefill() {
    if (input.value.trim()) return;
    const rows = document.querySelectorAll('.msg.user .bubble');
    const last = rows.length ? rows[rows.length - 1].textContent.trim() : '';
    input.value = last ? last.slice(0, 500) : '我想转人工咨询';
    input.select();
  }

  async function createTicket(content) {
    const data = await api('/api/support/tickets', {
      method: 'POST',
      body: JSON.stringify({
        sessionId: currentSession(),
        question: content,
        role: role(),
        channelCode: channelCode(),
      }),
    });
    ticket = data.ticket;
    accessKey = data.accessKey;
    saveStored();
    render(data.ticket);
    startPolling();
  }

  async function send() {
    const content = input.value.trim();
    if (!content || busy) return;
    if (!enabled) { message('客服功能暂未启用。'); return; }
    if (ticket && Number(ticket.state) === 5) {
      ticket = null;
      accessKey = '';
      saveStored();
    }
    busy = true;
    sendBtn.disabled = true;
    message('发送中…');
    try {
      if (!ticket) {
        await createTicket(content);
      } else {
        const data = await api(
          '/api/support/tickets/' + encodeURIComponent(ticket.ticketId) + '/messages',
          {
            method: 'POST',
            body: JSON.stringify({ accessKey, content }),
          }
        );
        render(data.ticket);
      }
      input.value = '';
      message('');
    } catch (err) {
      message(err.message || '发送失败，请稍后重试。');
    } finally {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  async function endTicket() {
    if (!ticket || !accessKey) return;
    if (!window.confirm('结束本次人工会话？')) return;
    busy = true;
    try {
      const data = await api(
        '/api/support/tickets/' + encodeURIComponent(ticket.ticketId) + '/close',
        { method: 'POST', body: JSON.stringify({ accessKey, reason: '用户主动结束' }) }
      );
      render(data);
      stopPolling();
      saveStored();
      showNotice('本次人工会话已结束。');
    } catch (err) {
      message(err.message || '结束会话失败。');
    } finally {
      busy = false;
    }
  }

  async function init() {
    try {
      const status = await api('/api/support/status');
      enabled = !!status.enabled;
      if (!enabled) return;
      btn.hidden = false;
      btn.title = status.onlineSeats > 0
        ? `转接人工客服（${status.onlineSeats} 位在线）`
        : '客服当前离线，可留言，上线后回复';
      if (loadStored()) {
        api('/api/support/tickets/' + encodeURIComponent(ticket.ticketId) +
          '?accessKey=' + encodeURIComponent(accessKey))
          .then((data) => {
            render(data.ticket);
            const pending = Number(data.newReplies || 0);
            if (pending > 0) {
              if (badgeEl) { badgeEl.textContent = String(pending); badgeEl.hidden = false; }
              btn.classList.add('attention');
            }
          })
          .catch(() => { ticket = null; accessKey = ''; saveStored(); });
      }
    } catch { /* 客服状态不可用时静默隐藏入口 */ }
  }

  btn.addEventListener('click', open);
  closeBtn.addEventListener('click', closePanel);
  mask.addEventListener('click', closePanel);
  sendBtn.addEventListener('click', send);
  endBtn.addEventListener('click', endTicket);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !panel.hidden) closePanel();
  });
  document.addEventListener('nxz:handoff', () => {
    if (!enabled) return;
    btn.classList.add('attention');
    if (badgeEl && badgeEl.hidden) {
      badgeEl.textContent = '1';
      badgeEl.hidden = false;
    }
    btn.title = '识别到人工客服诉求，点击转人工';
  });

  init();
})();
