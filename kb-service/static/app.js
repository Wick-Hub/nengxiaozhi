(() => {
  const chatEl = document.getElementById('chat');
  const inputEl = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const micBtn = document.getElementById('mic');
  const roleEl = document.getElementById('role');
  const agentSelectEl = document.getElementById('agent-select');
  const newChatBtn = document.getElementById('new-chat');
  const brandTitleEl = document.getElementById('brand-title');
  const brandSubtitleEl = document.getElementById('brand-subtitle');
  const historyPanel = document.getElementById('history-panel');
  const historyList = document.getElementById('history-list');
  const historyToggle = document.getElementById('history-toggle');
  const historyOverlay = document.getElementById('history-overlay');
  const historyClose = document.getElementById('history-close');
  const historyClear = document.getElementById('history-clear');
  const authBox = document.getElementById('auth-box');
  const loginModal = document.getElementById('login-modal');
  const loginForm = document.getElementById('login-form');
  const lmUser = document.getElementById('lm-user');
  const lmPass = document.getElementById('lm-pass');
  const lmRole = document.getElementById('lm-role');
  const lmMsg = document.getElementById('lm-msg');
  const lmSubmit = document.getElementById('lm-submit');
  const lmSso = document.getElementById('lm-sso');
  const fbModal = document.getElementById('fb-modal');
  const fbReasons = document.getElementById('fb-reasons');
  const fbNote = document.getElementById('fb-note');
  const fbMsg = document.getElementById('fb-msg');
  const fbSubmit = document.getElementById('fb-submit');
  const fbCancel = document.getElementById('fb-cancel');

  const LS_SESSION = 'nxz.sessionId';
  const LS_ROLE = 'nxz.role';
  const LS_HISTORY = 'nxz.history';
  const LS_TOKEN = 'nxz_token';   // 与 admin/auth.js 共用，门户/后台一次登录
  const HISTORY_MAX = 30;
  const SAMPLES = [
    '校园网怎么开通？',
    '一卡通丢了怎么办？',
    '研究生怎么申请奖学金？',
    '图书馆借书流程是什么？',
  ];
  // 任务 4.9：点踩原因（与后端 share_store.REASON_CODES 保持一致）
  const FB_REASONS = [
    { code: 'inaccurate', label: '内容不准确' },
    { code: 'incomplete', label: '回答不完整' },
    { code: 'irrelevant', label: '答非所问' },
    { code: 'other', label: '其他原因' },
  ];

  let sessionId = param('session') || localStorage.getItem(LS_SESSION) || null;
  let busy = false;
  // 语音输入（任务 4.7）：录音 → /api/asr/transcribe → 文本填入输入框
  const voice = {
    recorder: null, stream: null, chunks: [], timer: null,
    startedAt: 0, maxMs: 60000, busy: false, enabled: false,
  };
  const INPUT_PLACEHOLDER = inputEl ? inputEl.placeholder : '';
  const VOICE_MIMES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
  // 渠道路由（任务 3.1）：?channel=<code> 决定默认助手、欢迎语与主题色
  const CHANNEL_CODE = param('channel');
  let channelInfo = null;
  let agentId = null;
  // 助手矩阵（任务 3.8）：有渠道时取渠道绑定助手，否则取公开助手清单
  let publicAgents = [];
  let agentSwitched = false;
  // 应用广场（任务 3.3）：按当前身份从 /api/apps 拉取，欢迎页展示
  let squareApps = [];
  // 登录态（任务 3.5）：token 存在时身份以 token 为准（与后端一致）
  let authState = null;
  const EMBED_MODE = param('embed') === '1';
  // 智能硬件/自助终端（任务 3.5）：?kiosk=1 放大触控目标并隐藏管理入口
  const KIOSK_MODE = param('kiosk') === '1';
  // 嵌入 SDK（任务 3.9）：父页通过 postMessage 下发 token / 用户信息
  const SDK_MODE = param('sdk') === '1' && window.parent !== window;
  let sdkToken = '';
  let sdkUser = null;
  let sdkInitialized = false;
  let sdkWaiting = false;
  let resolveSdkInit = null;
  const sdkInitPromise = new Promise((resolve) => { resolveSdkInit = resolve; });

  roleEl.value = localStorage.getItem(LS_ROLE) || '访客';
  if (EMBED_MODE) document.body.classList.add('embed-mode');
  if (KIOSK_MODE) document.body.classList.add('kiosk-mode');

  function param(name) {
    const m = new URLSearchParams(location.search).get(name);
    return m || null;
  }

  if (lmSso) {
    lmSso.href = '/api/auth/cas/login?redirect=' + encodeURIComponent(location.pathname + location.search);
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ---------------------------------------------------------------- 登录态（任务 3.5）
  function getToken() {
    // SDK 注入的令牌优先且只存内存：不污染宿主页的 localStorage
    if (sdkToken) return sdkToken;
    try { return localStorage.getItem(LS_TOKEN) || ''; } catch { return ''; }
  }
  function setToken(value) {
    try { localStorage.setItem(LS_TOKEN, value); } catch { /* 隐私模式忽略 */ }
  }
  function clearToken() {
    try { localStorage.removeItem(LS_TOKEN); } catch { /* ignore */ }
  }

  // 带 token 的请求：后端以 token 身份为准，未登录时行为不变
  function apiFetch(url, opts) {
    const options = Object.assign({}, opts || {});
    const headers = new Headers(options.headers || {});
    const t = getToken();
    if (t && !headers.has('Authorization')) headers.set('Authorization', 'Bearer ' + t);
    options.headers = headers;
    return fetch(url, options);
  }

  function effectiveRole() {
    if (authState && authState.authenticated) return authState.role || '访客';
    return roleEl.value || '访客';
  }

  function applyRoleFromAuth() {
    const role = authState && authState.authenticated ? authState.role : null;
    if (role) {
      const known = Array.prototype.some.call(roleEl.options, (o) => o.value === role);
      roleEl.value = known ? role : '访客';
      roleEl.disabled = true;
      roleEl.title = '身份由登录账号决定';
    } else {
      roleEl.disabled = false;
      roleEl.title = '选择身份，决定可见知识范围';
      roleEl.value = localStorage.getItem(LS_ROLE) || '访客';
    }
  }

  function renderAuth() {
    if (!authBox) return;
    authBox.innerHTML = '';
    if (authState && authState.authenticated) {
      const name = el('span', 'auth-name');
      name.appendChild(el('b', null, authState.userNo || '已登录'));
      name.appendChild(document.createTextNode(' · ' + (authState.role || '访客')));
      name.title = `${authState.userNo || ''} · ${authState.role || ''}`;
      const out = el('button', 'btn-ghost', '退出');
      out.type = 'button';
      out.onclick = logout;
      authBox.appendChild(name);
      authBox.appendChild(out);
    } else {
      const btn = el('button', 'btn-ghost', '登录');
      btn.type = 'button';
      btn.id = 'login-btn';
      btn.onclick = openLogin;
      authBox.appendChild(btn);
    }
  }

  function hashParam(name) {
    const m = new RegExp('[#&]' + name + '=([^&]*)').exec(location.hash || '');
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch { return m[1]; }
  }

  // CAS 回调（任务 4.1）：一次性交换码换正式 token；返回错误文案供登录框展示
  async function consumeSsoHash() {
    const code = hashParam('cas');
    const error = hashParam('cas_error');
    if (!code && !error) return '';
    try { history.replaceState(null, '', location.pathname + location.search); } catch { /* ignore */ }
    if (error) return error;
    try {
      const resp = await apiFetch('/api/auth/cas/exchange', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ code }),
      });
      const json = await resp.json().catch(() => ({}));
      if (resp.ok && json.ok && json.data && json.data.token) {
        setToken(json.data.token);
        location.reload();
        return '';
      }
      return json.error || ('统一身份认证失败（HTTP ' + resp.status + '）');
    } catch (err) {
      return '统一身份认证失败：' + (err.message || err);
    }
  }

  async function loadMe() {
    authState = null;
    if (!getToken()) { renderAuth(); return; }
    try {
      const resp = await apiFetch('/api/auth/me', { headers: { Accept: 'application/json' } });
      const json = await resp.json();
      if (resp.ok && json.ok && json.data && json.data.authenticated) authState = json.data;
      else clearToken();
    } catch {
      // 服务不可达时按未登录处理，不阻塞问答
    }
    renderAuth();
  }

  // ---------------------------------------------------------------- SDK 桥（任务 3.9）
  function postSdk(type, payload) {
    if (!SDK_MODE) return;
    try { window.parent.postMessage(Object.assign({ type }, payload || {}), '*'); } catch { /* ignore */ }
  }

  function sdkStatePayload() {
    const authed = !!(authState && authState.authenticated);
    return {
      authenticated: authed,
      role: authed ? authState.role || '访客' : null,
      userNo: authed ? authState.userNo || null : null,
      sdkUser: sdkUser,
    };
  }

  async function applySdkIdentity(data) {
    sdkToken = typeof data.token === 'string' ? data.token : '';
    sdkUser = data.user || null;
    await loadMe();
    applyRoleFromAuth();
    await Promise.all([loadChannel(), loadPublicAgents(), loadApps()]);
    ensureAgentVisible();
    renderAgentSelect();
    postSdk('nxz:sdk:state', sdkStatePayload());
  }

  function initSdkBridge() {
    if (!SDK_MODE) return;
    window.addEventListener('message', (event) => {
      if (event.source !== window.parent) return;  // 只接受宿主页消息
      const data = event.data || {};
      if (data.type !== 'nxz:sdk:init') return;
      sdkToken = typeof data.token === 'string' ? data.token : '';
      sdkUser = data.user || null;
      if (!sdkInitialized && sdkWaiting) {
        // 首次下发且在 boot 等待窗口内：只登记身份，由 boot() 统一加载
        sdkInitialized = true;
        if (resolveSdkInit) { resolveSdkInit(); resolveSdkInit = null; }
        return;
      }
      sdkInitialized = true;
      applySdkIdentity(data).catch(() => postSdk('nxz:sdk:state', sdkStatePayload()));
    });
    postSdk('nxz:sdk:ready', { version: '1.0.0' });
  }

  function waitSdkInit(ms) {
    if (!SDK_MODE) return Promise.resolve();
    return Promise.race([
      sdkInitPromise,
      new Promise((resolve) => setTimeout(resolve, ms)),
    ]);
  }

  function showLoginMsg(text) {
    if (!lmMsg) return;
    lmMsg.textContent = text;
    lmMsg.hidden = false;
  }

  function openLogin() {
    if (!loginModal) return;
    lmMsg.hidden = true;
    lmUser.value = (authState && authState.userNo) || '';
    lmPass.value = '';
    lmRole.value = roleEl.value || '本科生';
    loginModal.hidden = false;
    setTimeout(() => lmUser.focus(), 0);
  }

  function closeLogin() {
    if (loginModal) loginModal.hidden = true;
  }

  async function submitLogin(ev) {
    ev.preventDefault();
    const userNo = lmUser.value.trim();
    if (!userNo) { showLoginMsg('请输入学工号'); return; }
    lmSubmit.disabled = true;
    lmSubmit.textContent = '登录中…';
    try {
      const body = { userNo, role: lmRole.value };
      if (lmPass.value) body.password = lmPass.value;
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
      });
      const json = await resp.json().catch(() => null);
      if (!resp.ok || !json || !json.ok) {
        throw new Error((json && json.error) || ('登录失败 HTTP ' + resp.status));
      }
      setToken(json.data.token);
      await loadMe();
      applyRoleFromAuth();
      closeLogin();
      await refreshPortal();
    } catch (e) {
      showLoginMsg(e.message || '登录失败，请稍后重试');
    } finally {
      lmSubmit.disabled = false;
      lmSubmit.textContent = '登录';
    }
  }

  async function logout() {
    clearToken();
    authState = null;
    renderAuth();
    applyRoleFromAuth();
    await refreshPortal();
  }

  // ---------------------------------------------------------------- 历史会话（任务 3.5）
  function readHistory() {
    try {
      const raw = JSON.parse(localStorage.getItem(LS_HISTORY) || '[]');
      return Array.isArray(raw)
        ? raw.filter((e) => e && typeof e.id === 'string' && e.id)
        : [];
    } catch {
      return [];
    }
  }

  function saveHistory(list) {
    try {
      localStorage.setItem(LS_HISTORY, JSON.stringify(list.slice(0, HISTORY_MAX)));
    } catch { /* 隐私模式/配额不足时静默降级 */ }
  }

  function visibleHistory() {
    const channel = CHANNEL_CODE || '';
    return readHistory().filter((e) => (e.channel || '') === channel);
  }

  function relTime(ts) {
    const t = Number(ts) || 0;
    if (!t) return '';
    const diff = Date.now() - t;
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前';
    if (diff < 86400000) return Math.floor(diff / 3600000) + ' 小时前';
    const d = new Date(t);
    return `${d.getMonth() + 1}-${d.getDate()}`;
  }

  function rememberSession(sid, question) {
    if (!sid) return;
    const list = readHistory();
    const idx = list.findIndex((e) => e.id === sid);
    const title = String(question || '')
      .replace(/\s+/g, ' ').trim().slice(0, 36) || '未命名会话';
    if (idx >= 0) {
      list[idx].at = Date.now();
      if (!list[idx].title) list[idx].title = title;
    } else {
      list.unshift({ id: sid, title, at: Date.now(), channel: CHANNEL_CODE || '' });
    }
    list.sort((a, b) => (Number(b.at) || 0) - (Number(a.at) || 0));
    saveHistory(list);
    renderHistory();
  }

  function forgetSession(sid) {
    saveHistory(readHistory().filter((e) => e.id !== sid));
    renderHistory();
  }

  function renderHistory() {
    if (!historyList) return;
    historyList.innerHTML = '';
    const list = visibleHistory();
    if (!list.length) {
      const empty = el('div', 'history-empty',
        '暂无历史会话\n提一个问题后会自动记录在这里');
      historyList.appendChild(empty);
      return;
    }
    for (const entry of list) {
      const row = el('div', 'history-item');
      if (entry.id === sessionId) row.classList.add('active');
      const open = el('button', 'history-open');
      open.type = 'button';
      open.appendChild(el('span', 'history-title', entry.title || '未命名会话'));
      open.appendChild(el('span', 'history-time', relTime(entry.at)));
      open.onclick = () => openSession(entry.id);
      const del = el('button', 'history-del', '×');
      del.type = 'button';
      del.title = '从本机历史中移除（不删除服务端会话）';
      del.setAttribute('aria-label', '删除历史会话');
      del.onclick = (ev) => { ev.stopPropagation(); forgetSession(entry.id); };
      row.appendChild(open);
      row.appendChild(del);
      historyList.appendChild(row);
    }
  }

  async function openSession(sid) {
    if (!sid || busy) return;
    closeHistoryDrawer();
    await loadHistory(sid);
  }

  function openHistoryDrawer() {
    document.body.classList.add('history-open');
    if (historyOverlay) historyOverlay.hidden = false;
    if (historyToggle) historyToggle.setAttribute('aria-expanded', 'true');
  }

  function closeHistoryDrawer() {
    document.body.classList.remove('history-open');
    if (historyOverlay) historyOverlay.hidden = true;
    if (historyToggle) historyToggle.setAttribute('aria-expanded', 'false');
  }

  // ---------------------------------------------------------------- 富文本渲染（任务 1.9）
  // Markdown 子集：标题 / 列表 / 代码 / 引用 / 表格 / 链接 / 图片 / 加粗 / 斜体 / 删除线 / [n] 引用
  // 与后端 app/core/richtext.py 保持同一子集；**未识别语法一律转义**（防 XSS）。
  // 流式阶段由本渲染器增量渲染；done 时优先用服务端 answerHtml（同一份 blocks 渲染，结果一致）。
  const BLOCKED_URL = /^(?:javascript|vbscript|data|file|blob):/;
  const ALLOWED_URL = /^(?:https?:\/\/|mailto:|tel:)/;

  function safeUrl(raw) {
    const url = String(raw == null ? '' : raw).trim();
    if (!url) return '';
    // 先剥离控制字符与空白，防 `java\tscript:` 之类的混淆绕过
    const compact = url.replace(/[\u0000-\u0020\u007f]+/g, '').toLowerCase();
    if (BLOCKED_URL.test(compact)) return '';
    if (/^(?:\/|#|\.\/|\.\.\/)/.test(url)) return url; // 相对路径 / 页内锚点
    if (ALLOWED_URL.test(compact)) return url;
    return '';
  }

  const INLINE_RE = /!\[([^\]]*)\]\(([^)]*)\)|\[([^\]]*)\]\(([^)]*)\)|\[(\d{1,2})\]|`([^`\n]+)`|\*\*([^*\n]+)\*\*|~~([^~\n]+)~~|\*([^*\n]+)\*/g;

  function renderInlines(text) {
    let out = '';
    let pos = 0;
    let m;
    INLINE_RE.lastIndex = 0;
    while ((m = INLINE_RE.exec(text)) !== null) {
      if (m.index > pos) out += esc(text.slice(pos, m.index)).replace(/\n/g, '<br>');
      pos = INLINE_RE.lastIndex;
      if (m[1] !== undefined) {
        const src = safeUrl(m[2]);
        out += src
          ? `<img src="${esc(src)}" alt="${esc(m[1])}" loading="lazy">`
          : esc(m[1]);
      } else if (m[3] !== undefined) {
        const href = safeUrl(m[4]);
        out += href
          ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(m[3])}</a>`
          : esc(m[0]);
      } else if (m[5] !== undefined) {
        out += `<sup class="cite">[${m[5]}]</sup>`;
      } else if (m[6] !== undefined) {
        out += `<code>${esc(m[6])}</code>`;
      } else if (m[7] !== undefined) {
        out += `<strong>${esc(m[7])}</strong>`;
      } else if (m[8] !== undefined) {
        out += `<del>${esc(m[8])}</del>`;
      } else if (m[9] !== undefined) {
        out += `<em>${esc(m[9])}</em>`;
      }
    }
    if (pos < text.length) out += esc(text.slice(pos)).replace(/\n/g, '<br>');
    return out;
  }

  const FENCE_RE = /^\s*```\s*([A-Za-z0-9_+.-]*)\s*$/;
  const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  const HR_RE = /^\s{0,3}([-*_])\s*(?:\1\s*){2,}$/;
  const QUOTE_RE = /^\s{0,3}>\s?(.*)$/;
  const ULIST_RE = /^\s{0,3}[-*+]\s+(.+)$/;
  const OLIST_RE = /^\s{0,3}\d{1,3}[.)]\s+(.+)$/;
  const TABLE_SEP_RE = /^\s*\|?\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)*\|?\s*$/;
  const LONE_IMAGE_RE = /^!\[([^\]]*)\]\(([^)]*)\)$/;

  function splitRow(line) {
    let s = line.trim();
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
    return s.split('|').map((c) => c.trim());
  }

  function parseAlign(sep) {
    return splitRow(sep).map((cell) => {
      const left = cell.charAt(0) === ':';
      const right = cell.charAt(cell.length - 1) === ':';
      if (left && right) return 'center';
      if (right) return 'right';
      if (left) return 'left';
      return null;
    });
  }

  function isTableStart(lines, i) {
    if (i + 1 >= lines.length) return false;
    if (lines[i].indexOf('|') < 0) return false;
    if (lines[i + 1].indexOf('-') < 0) return false;
    return TABLE_SEP_RE.test(lines[i + 1]);
  }

  function isBlockStart(lines, i) {
    const line = lines[i];
    if (!line.trim()) return true;
    if (FENCE_RE.test(line) || HEADING_RE.test(line) || QUOTE_RE.test(line)) return true;
    if (ULIST_RE.test(line) || OLIST_RE.test(line)) return true;
    if (HR_RE.test(line)) return true;
    return isTableStart(lines, i);
  }

  function renderContent(text) {
    const src = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
    if (!src.trim()) return '';
    const lines = src.split('\n');
    let html = '';
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }

      let m;
      // 围栏代码块
      if ((m = FENCE_RE.exec(line)) !== null) {
        const lang = m[1];
        i++;
        const buf = [];
        while (i < lines.length && !FENCE_RE.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        html += `<pre class="rt-code"><code${lang ? ` class="language-${esc(lang)}"` : ''}>${esc(buf.join('\n'))}</code></pre>`;
        continue;
      }

      // 标题
      if ((m = HEADING_RE.exec(line)) !== null) {
        const level = Math.min(Math.max(m[1].length, 1), 6);
        html += `<h${level}>${renderInlines(m[2].trim())}</h${level}>`;
        i++;
        continue;
      }

      // 分隔线
      if (HR_RE.test(line)) { html += '<hr class="rt-hr">'; i++; continue; }

      // 表格
      if (isTableStart(lines, i)) {
        const header = splitRow(lines[i]);
        const aligns = parseAlign(lines[i + 1]);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') >= 0) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        const width = header.length;
        const cell = (tag, t, idx) => {
          const a = idx < aligns.length ? aligns[idx] : null;
          return `<${tag}${a ? ` style="text-align:${a}"` : ''}>${renderInlines(t)}</${tag}>`;
        };
        const head = header.map((h, k) => cell('th', h, k)).join('');
        const body = rows
          .map((r) => `<tr>${r.concat(new Array(width).fill('')).slice(0, width).map((c, k) => cell('td', c, k)).join('')}</tr>`)
          .join('');
        html += `<table class="rt-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
        continue;
      }

      // 引用块
      if (QUOTE_RE.test(line)) {
        const buf = [];
        while (i < lines.length && (m = QUOTE_RE.exec(lines[i])) !== null) { buf.push(m[1]); i++; }
        html += `<blockquote class="rt-quote">${renderInlines(buf.join('\n').trim())}</blockquote>`;
        continue;
      }

      // 列表
      if (ULIST_RE.test(line) || OLIST_RE.test(line)) {
        const ordered = OLIST_RE.test(line);
        const re = ordered ? OLIST_RE : ULIST_RE;
        const items = [];
        while (i < lines.length) {
          const im = re.exec(lines[i]);
          if (!im) break;
          items.push(im[1].trim());
          i++;
        }
        const tag = ordered ? 'ol' : 'ul';
        html += `<${tag} class="rt-list">${items.map((t) => `<li>${renderInlines(t)}</li>`).join('')}</${tag}>`;
        continue;
      }

      // 段落（保留内部换行 → <br>，与原型行为一致）
      const buf = [];
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines, i)) {
        buf.push(lines[i].trim());
        i++;
      }
      if (!buf.length) { buf.push(line.trim()); i++; }
      const body = buf.join('\n');

      // 整段只有一张图片 → 块级图（与后端 blocks 的 image 类型对齐）
      const lone = LONE_IMAGE_RE.exec(body);
      if (lone) {
        const src2 = safeUrl(lone[2]);
        const alt = esc(lone[1]);
        html += src2
          ? `<figure class="rt-figure"><img src="${esc(src2)}" alt="${alt}" loading="lazy">${alt ? `<figcaption>${alt}</figcaption>` : ''}</figure>`
          : `<p>${alt}</p>`;
        continue;
      }
      html += `<p>${renderInlines(body)}</p>`;
    }
    return html;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function msgWrap(role) {
    const row = el('div', 'msg ' + role);
    const bubble = el('div', 'bubble');
    row.appendChild(bubble);
    chatEl.appendChild(row);
    return bubble;
  }

  function scrollDown() {
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function addUser(text) {
    const b = msgWrap('user');
    b.innerHTML = renderContent(text);
    scrollDown();
  }

  async function loadChannel() {
    if (!CHANNEL_CODE) return;
    try {
      const role = encodeURIComponent(effectiveRole());
      const resp = await apiFetch(`/api/channels/${encodeURIComponent(CHANNEL_CODE)}?role=${role}`);
      const json = await resp.json();
      if (!json.ok || !json.data) return;
      channelInfo = json.data;
      agentId = channelInfo.defaultAgentId || null;
      agentSwitched = false;
      if (brandTitleEl) brandTitleEl.textContent = channelInfo.channelName || '能小智';
      if (brandSubtitleEl) brandSubtitleEl.textContent = '能小智 · 校园AI助手';
      document.title = `${channelInfo.channelName || '能小智'} · 校园AI助手`;
      const color = channelInfo.themeConfig && channelInfo.themeConfig.primaryColor;
      if (/^#[0-9a-f]{6}$/i.test(color || '')) {
        document.documentElement.style.setProperty('--brand-1', color);
        document.documentElement.style.setProperty(
          '--brand-grad', `linear-gradient(135deg, ${color} 0%, var(--brand-2) 100%)`);
      }
    } catch {
      // 渠道不存在/服务不可达时回落到通用对话，不阻塞问答
    }
  }

  // 任务 3.8：无渠道参数的根门户也能切换助手（按身份过滤上架助手）
  async function loadPublicAgents() {
    try {
      const role = encodeURIComponent(effectiveRole());
      const resp = await apiFetch(`/api/agents?role=${role}`);
      const json = await resp.json();
      const data = json && json.ok ? json.data : null;
      publicAgents = data && Array.isArray(data.agents) ? data.agents : [];
    } catch {
      publicAgents = [];
    }
  }

  // 有渠道时以渠道绑定集合为准（渠道治理），否则用公开上架清单
  function agentCards() {
    if (channelInfo && Array.isArray(channelInfo.agents) && channelInfo.agents.length) {
      return channelInfo.agents;
    }
    return publicAgents;
  }

  function findAgentCard(id) {
    if (id == null) return null;
    const card = agentCards().find((a) => Number(a.agentId) === Number(id));
    if (card) return card;
    const app = squareApps.find(
      (x) => x && x.sourceType === 'agent' && Number(x.agentId) === Number(id));
    if (!app) return null;
    return {
      agentId: app.agentId,
      agentName: app.agentName || app.appName || '',
      description: app.description || '',
      iconUrl: app.iconUrl || '',
      guideQuestions: Array.isArray(app.guideQuestions) ? app.guideQuestions : [],
    };
  }

  function defaultAgentId() {
    if (channelInfo && channelInfo.defaultAgentId != null) return channelInfo.defaultAgentId;
    const fallback = publicAgents.find((a) => a.isDefault) || publicAgents[0];
    return fallback ? fallback.agentId : null;
  }

  function selectedAgentId() {
    return agentId != null ? agentId : defaultAgentId();
  }

  // 顶栏常驻切换器：会话中途也能换助手（矩阵只在首屏出现）
  function renderAgentSelect() {
    if (!agentSelectEl) return;
    const cards = agentCards();
    if (cards.length < 2) {
      agentSelectEl.hidden = true;
      agentSelectEl.innerHTML = '';
      return;
    }
    agentSelectEl.innerHTML = '';
    for (const item of cards) {
      const opt = document.createElement('option');
      opt.value = String(item.agentId);
      opt.textContent = item.agentName || '助手';
      agentSelectEl.appendChild(opt);
    }
    const selected = selectedAgentId();
    agentSelectEl.value = String(selected != null ? selected : cards[0].agentId);
    agentSelectEl.hidden = false;
  }

  // 切换助手 = 新上下文：旧会话由上一个助手作答，不能继续混在同一会话里
  function switchAssistant(id) {
    if (id == null || busy) return;
    const target = Number(id);
    if (Number(selectedAgentId()) === target) return;
    agentId = target;
    agentSwitched = target !== Number(defaultAgentId());
    if (sessionId) {
      sessionId = null;
      try { localStorage.removeItem(LS_SESSION); } catch { /* ignore */ }
    }
    chatEl.innerHTML = '';
    renderHistory();
    renderAgentSelect();
    addWelcome();
  }

  // 切换身份后若当前助手不再可见（如仅教职工可用），回到默认助手
  function ensureAgentVisible() {
    if (agentId == null) return;
    const cards = agentCards();
    if (!cards.length) return;
    if (cards.some((a) => Number(a.agentId) === Number(agentId))) return;
    agentId = channelInfo ? channelInfo.defaultAgentId : null;
    agentSwitched = false;
  }

  // 任务 3.2：渠道门户个性化（M-C-02）。配置来自公开渠道接口的 themeConfig，
  // 所有文本一律走 textContent，链接先过 safeUrl 协议白名单。
  function portalTheme() {
    return (channelInfo && channelInfo.themeConfig) || {};
  }

  // 任务 3.4：渠道路由按身份覆写首屏助手/欢迎语/引导问题与功能模块
  function routeInfo() {
    return (channelInfo && channelInfo.route) || {};
  }

  function routeModules() {
    const mods = routeInfo().modules || {};
    const on = (key) => (mods[key] === undefined ? true : Number(mods[key]) !== 0);
    return {
      guideQuestions: on('guideQuestions'),
      shortcuts: on('shortcuts'),
      recommendedApps: on('recommendedApps'),
      appSquare: on('appSquare'),
      serviceEntry: on('serviceEntry'),
      feedback: on('feedback'),
    };
  }

  function safeOpen(url) {
    const href = safeUrl(url);
    if (!href) return;
    if (/^https?:\/\//i.test(href)) window.open(href, '_blank', 'noopener');
    else window.location.href = href;
  }

  function addPortalShortcuts(w, shortcuts) {
    const list = (shortcuts || []).filter((s) => s && s.label && s.content);
    if (!list.length) return;
    const row = el('div', 'shortcut-row');
    for (const item of list) {
      const btn = el('button', 'shortcut', `${item.icon || ''} ${item.label}`.trim());
      btn.onclick = () => {
        if (item.action === 'link') safeOpen(item.content);
        else ask(item.content);
      };
      row.appendChild(btn);
    }
    w.appendChild(row);
  }

  function addPortalApps(w, apps) {
    const grid = el('div', 'portal-app-grid');
    for (const item of apps || []) {
      const href = item && item.name ? safeUrl(item.url || '') : '';
      if (!href) continue;
      const card = document.createElement('a');
      card.className = 'portal-app';
      card.href = href;
      card.target = '_blank';
      card.rel = 'noopener noreferrer';
      card.appendChild(el('span', 'portal-app-icon', item.icon || '🔗'));
      const info = el('span', 'portal-app-info');
      info.appendChild(el('b', null, item.name));
      if (item.desc) info.appendChild(el('small', null, item.desc));
      card.appendChild(info);
      grid.appendChild(card);
    }
    if (!grid.children.length) return;
    const box = el('div', 'portal-apps');
    box.appendChild(el('div', 'portal-apps-title', '推荐应用'));
    box.appendChild(grid);
    w.appendChild(box);
  }

  function addPortalEntry(w, entry, fallbackLabel) {
    if (!entry || !entry.enabled) return;
    const href = safeUrl(entry.url || '');
    if (!href) return;
    let links = w.querySelector('.portal-links');
    if (!links) {
      links = el('div', 'portal-links');
      w.appendChild(links);
    }
    const a = document.createElement('a');
    a.className = 'portal-link';
    a.href = href;
    a.textContent = entry.label || fallbackLabel;
    if (/^https?:\/\//i.test(href)) {
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    }
    links.appendChild(a);
  }

  async function loadApps() {
    try {
      const role = encodeURIComponent(effectiveRole());
      const resp = await apiFetch(`/api/apps?role=${role}`);
      const json = await resp.json();
      const data = json && json.ok ? json.data : null;
      squareApps = data && Array.isArray(data.apps) ? data.apps : [];
    } catch {
      squareApps = [];
    }
  }

  // 登录/退出/切换身份后统一刷新：渠道路由 + 公开助手 + 应用广场 + 首屏欢迎语
  async function refreshPortal() {
    await Promise.all([loadChannel(), loadPublicAgents(), loadApps()]);
    ensureAgentVisible();
    renderAgentSelect();
    const welcome = chatEl.querySelector('.welcome');
    if (welcome) {
      welcome.remove();
      addWelcome();
    }
    renderHistory();
  }

  function addAppSquare(w) {
    const list = squareApps.filter((item) => item && item.appName);
    if (!list.length) return;
    const box = el('div', 'app-square');
    const head = el('div', 'app-square-head');
    head.appendChild(el('b', null, '应用广场'));
    head.appendChild(el('span', null, `当前身份 · ${effectiveRole()}`));
    box.appendChild(head);

    const grid = el('div', 'app-square-grid');
    for (const item of list) {
      const icon = item.icon || (item.sourceType === 'agent' ? '🤖' : '🔗');
      let card;
      if (item.sourceType === 'link') {
        const href = safeUrl(item.linkUrl || '');
        if (!href) continue;
        card = document.createElement('a');
        card.href = href;
        card.target = '_blank';
        card.rel = 'noopener noreferrer';
        card.title = item.description || item.appName;
      } else {
        card = document.createElement('button');
        card.type = 'button';
        card.title = item.description || item.appName;
        card.onclick = () => switchAssistant(item.agentId);
      }
      card.className = 'app-card';
      if (item.sourceType !== 'link' && agentId != null && Number(item.agentId) === Number(agentId)) {
        card.classList.add('active');
      }
      card.dataset.appId = String(item.appId);
      card.dataset.source = item.sourceType;
      card.appendChild(el('span', 'app-card-icon', icon));
      const body = el('span', 'app-card-body');
      body.appendChild(el('b', null, item.appName));
      body.appendChild(el('small', null, item.description || item.category || ''));
      card.appendChild(body);
      card.appendChild(el('span', 'app-card-kind', item.sourceType === 'agent' ? '助手' : '链接'));
      grid.appendChild(card);
    }
    if (!grid.children.length) return;
    box.appendChild(grid);
    w.appendChild(box);
  }

  // 任务 3.8：同页助手矩阵（渠道绑定集合 / 公开上架清单）
  function addAgentMatrix(w) {
    const cards = agentCards();
    if (cards.length < 2) return;
    const box = el('div', 'agent-matrix-box');
    const head = el('div', 'agent-matrix-head');
    head.appendChild(el('b', null, '切换助手'));
    head.appendChild(el('span', null, '切换后开启新会话'));
    box.appendChild(head);

    const grid = el('div', 'agent-matrix');
    for (const item of cards) {
      const btn = el('button', 'agent-chip');
      btn.type = 'button';
      btn.dataset.agentId = String(item.agentId);
      if (Number(item.agentId) === Number(selectedAgentId())) btn.classList.add('active');
      const icon = String(item.iconUrl || '🤖');
      const iconUrl = safeUrl(icon);
      if (iconUrl && /^(https?:\/\/|\/)/i.test(iconUrl)) {
        const img = document.createElement('img');
        img.className = 'agent-chip-icon';
        img.src = iconUrl;
        img.alt = '';
        btn.appendChild(img);
      } else {
        btn.appendChild(el('span', 'agent-chip-icon', icon));
      }
      const body = el('span', 'agent-chip-body');
      body.appendChild(el('b', null, item.agentName || '助手'));
      if (item.description) body.appendChild(el('small', null, item.description));
      btn.appendChild(body);
      btn.onclick = () => switchAssistant(item.agentId);
      grid.appendChild(btn);
    }
    box.appendChild(grid);
    w.appendChild(box);
  }

  function guideQuestionsFor(theme) {
    const route = routeInfo();
    const card = findAgentCard(selectedAgentId());
    const clean = (list) => (Array.isArray(list) ? list : [])
      .filter((q) => typeof q === 'string' && q.trim());
    const agentGuides = clean(card && card.guideQuestions);
    const routeGuides = clean(route.guideQuestions);
    const faqs = clean(theme.faq);
    // 显式切到某个助手时，场景问题优先；默认助手仍尊重 3.4 的角色路由问题
    if (agentSwitched && agentGuides.length) return agentGuides;
    if (routeGuides.length) return routeGuides;
    if (agentGuides.length) return agentGuides;
    if (faqs.length) return faqs;
    return SAMPLES;
  }

  function addWelcome() {
    const theme = portalTheme();
    const mods = routeModules();
    const meta = agentSwitched ? findAgentCard(selectedAgentId()) : null;
    const w = el('div', 'welcome');

    const icon = meta && meta.iconUrl ? String(meta.iconUrl) : '';
    const avatarUrl = safeUrl(icon || theme.avatarUrl || '');
    if (avatarUrl && /^(https?:\/\/|\/)/i.test(avatarUrl)) {
      const img = document.createElement('img');
      img.className = 'big-logo avatar-img';
      img.src = avatarUrl;
      img.alt = '';
      w.appendChild(img);
    } else {
      w.appendChild(el('div', 'big-logo', icon || theme.avatarText || '智'));
    }

    const heading = (meta && meta.agentName)
      || theme.assistantName
      || (channelInfo ? channelInfo.channelName : '')
      || '你好，我是能小智';
    const desc = (meta && meta.description)
      || (channelInfo && channelInfo.welcomeText
        ? channelInfo.welcomeText
        : '校园事务、办事流程、政策解读，随时问我。回答会附知识库来源，方便查证。');
    w.appendChild(el('h2', null, heading));
    w.appendChild(el('p', null, desc));

    addAgentMatrix(w);

    if (mods.guideQuestions) {
      const s = el('div', 'samples');
      for (const q of guideQuestionsFor(theme)) {
        const chip = el('button', 'sample', q);
        chip.onclick = () => ask(q);
        s.appendChild(chip);
      }
      w.appendChild(s);
    }

    if (mods.shortcuts) addPortalShortcuts(w, theme.shortcuts);
    if (mods.recommendedApps) addPortalApps(w, theme.recommendedApps);
    if (mods.appSquare) addAppSquare(w);
    if (mods.serviceEntry) addPortalEntry(w, theme.serviceEntry, '联系客服');
    if (mods.feedback) addPortalEntry(w, theme.feedback, '意见反馈');

    chatEl.appendChild(w);
  }

  function addMeta(bubble, meta) {
    const m = el('div', 'meta');
    if (meta.model) m.appendChild(el('span', 'tag', meta.model));
    if (meta.latencyMs != null) m.appendChild(el('span', '', `响应 ${(meta.latencyMs / 1000).toFixed(1)}s`));
    if (meta.confidence != null) m.appendChild(el('span', '', `置信度 ${meta.confidence.toFixed(2)}`));
    if (meta.refused) {
      m.appendChild(el('span', 'tag refused', '已拒答'));
      m.appendChild(el('span', 'tag waiting', '已转人工待处理队列'));
    }
    bubble.parentElement.appendChild(m);
  }

  // 任务 3.7：AI 生成内容显式标识（隐式元数据在接口 aiLabel / 响应头）
  function addAiLabel(bubble) {
    const row = bubble.parentElement;
    if (!row || row.querySelector('.ai-label')) return;
    const label = el('div', 'ai-label');
    label.appendChild(el('span', 'ai-badge', 'AI 生成'));
    label.appendChild(el('span', '', '内容由 AI 生成，仅供参考'));
    row.appendChild(label);
  }

  // ---------------------------------------------------------------- 分享 / 反馈（任务 4.9）
  let pendingFeedback = null;

  function toast(text) {
    const box = el('div', 'nxz-toast', text);
    document.body.appendChild(box);
    setTimeout(() => box.classList.add('show'), 10);
    setTimeout(() => {
      box.classList.remove('show');
      setTimeout(() => box.remove(), 220);
    }, 2600);
  }

  // ---------------------------------------------------------------- 语音输入（任务 4.7）
  function fmtClock(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }

  function stopVoiceTracks() {
    if (!voice.stream) return;
    voice.stream.getTracks().forEach((track) => { try { track.stop(); } catch { /* ignore */ } });
    voice.stream = null;
  }

  function setVoiceIdle() {
    if (!micBtn) return;
    micBtn.classList.remove('recording');
    micBtn.setAttribute('aria-pressed', 'false');
    micBtn.title = '语音输入（最长 ' + Math.round(voice.maxMs / 1000) + ' 秒）';
    micBtn.disabled = busy || voice.busy;
  }

  async function loadVoiceStatus() {
    if (!micBtn) return;
    try {
      const resp = await apiFetch('/api/asr/status', { headers: { Accept: 'application/json' } });
      const json = await resp.json();
      const data = (json && json.data) || {};
      voice.enabled = !!(json && json.ok && data.enabled);
      if (!voice.enabled) { micBtn.hidden = true; return; }
      voice.maxMs = Math.max(5, Number(data.maxSeconds) || 60) * 1000;
      micBtn.hidden = false;
      setVoiceIdle();
    } catch {
      micBtn.hidden = true;  // 状态未知时不展示一个可能失效的入口
    }
  }

  function pickVoiceMime() {
    if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
    return VOICE_MIMES.find((mime) => MediaRecorder.isTypeSupported(mime)) || '';
  }

  function tickVoice() {
    const seconds = (Date.now() - voice.startedAt) / 1000;
    inputEl.placeholder = '正在录音 ' + fmtClock(seconds) + '，点击麦克风结束';
    if (Date.now() - voice.startedAt >= voice.maxMs) stopVoice();
  }

  async function startVoice() {
    if (!micBtn || voice.busy || busy) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
      toast('当前浏览器不支持语音输入，请使用文字提问');
      return;
    }
    try {
      voice.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      toast('无法访问麦克风，请检查浏览器权限');
      return;
    }
    let recorder;
    try {
      const mime = pickVoiceMime();
      recorder = mime ? new MediaRecorder(voice.stream, { mimeType: mime }) : new MediaRecorder(voice.stream);
    } catch {
      stopVoiceTracks();
      toast('当前浏览器无法录音，请使用文字提问');
      return;
    }
    voice.recorder = recorder;
    voice.chunks = [];
    voice.startedAt = Date.now();
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size) voice.chunks.push(e.data);
    };
    recorder.onstop = () => { finishVoice(); };
    try {
      recorder.start();
    } catch {
      voice.recorder = null;
      stopVoiceTracks();
      setVoiceIdle();
      toast('录音启动失败，请重试');
      return;
    }
    micBtn.classList.add('recording');
    micBtn.setAttribute('aria-pressed', 'true');
    micBtn.title = '点击结束录音';
    inputEl.placeholder = '正在录音 00:00，点击麦克风结束';
    voice.timer = setInterval(tickVoice, 200);
  }

  function stopVoice() {
    if (voice.timer) { clearInterval(voice.timer); voice.timer = null; }
    if (voice.recorder && voice.recorder.state !== 'inactive') {
      try { voice.recorder.stop(); } catch { finishVoice(); }
    }
  }

  async function finishVoice() {
    if (!voice.recorder && !voice.chunks.length) return;  // onstop 与异常兜底可能重入
    const recorder = voice.recorder;
    const chunks = voice.chunks;
    const mime = (recorder && recorder.mimeType) || 'audio/webm';
    voice.recorder = null;
    voice.chunks = [];
    stopVoiceTracks();
    setVoiceIdle();
    inputEl.placeholder = INPUT_PLACEHOLDER;
    if (!chunks.length) {
      toast('没有录到声音，请重试');
      return;
    }
    const blob = new Blob(chunks, { type: mime.split(';')[0] || 'audio/webm' });
    if (!blob.size) {
      toast('没有录到声音，请重试');
      return;
    }

    voice.busy = true;
    micBtn.disabled = true;
    micBtn.classList.add('busy');
    inputEl.placeholder = '语音识别中…';
    try {
      const ext = /ogg/.test(mime) ? 'ogg' : /mp4|m4a/.test(mime) ? 'm4a' : 'webm';
      const form = new FormData();
      form.append('file', blob, 'voice.' + ext);
      const resp = await apiFetch('/api/asr/transcribe', {
        method: 'POST',
        headers: { Accept: 'application/json' },
        body: form,
      });
      const json = await resp.json().catch(() => null);
      if (!resp.ok || !json || !json.ok) {
        const detail = json && (json.detail || json.error);
        throw new Error(typeof detail === 'string' ? detail : 'HTTP ' + resp.status);
      }
      const text = (json.data && json.data.text) || '';
      if (!text) throw new Error('未识别到语音内容，请重试');
      inputEl.value = inputEl.value.trim() ? inputEl.value.trim() + ' ' + text : text;
      autosize();
      inputEl.focus();
      toast(json.data.truncated ? '已转写（超长部分已截断），可编辑后发送' : '已转写，可编辑后发送');
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      toast(/Failed to fetch|NetworkError/i.test(msg) ? '语音识别失败：服务不可用' : '语音识别失败：' + msg);
    } finally {
      voice.busy = false;
      micBtn.classList.remove('busy');
      inputEl.placeholder = INPUT_PLACEHOLDER;
      setVoiceIdle();
      setBusy(busy);
    }
  }

  if (micBtn) {
    micBtn.addEventListener('click', () => {
      if (voice.recorder && voice.recorder.state === 'recording') stopVoice();
      else startVoice();
    });
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch { /* ignore */ }
      ta.remove();
      return ok;
    }
  }

  function markFeedback(bar, rating) {
    if (!bar) return;
    bar.querySelectorAll('.msg-act[data-rating]').forEach((btn) => {
      btn.classList.toggle('active', Number(btn.dataset.rating) === Number(rating));
    });
  }

  async function sendMessageFeedback(meta, bar, rating, reasonCode, reason) {
    const sid = meta.sessionId || sessionId;
    if (!sid || meta.msgId == null) throw new Error('消息尚未保存，无法反馈');
    const resp = await apiFetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sessionId: sid,
        msgId: meta.msgId,
        rating,
        reasonCode: reasonCode || '',
        reason: reason || '',
        channelCode: (channelInfo && channelInfo.channelCode) || CHANNEL_CODE || '',
      }),
    });
    const json = await resp.json().catch(() => ({}));
    if (!resp.ok || !json.ok) {
      throw new Error(json.detail || json.error || ('HTTP ' + resp.status));
    }
    markFeedback(bar, rating);
  }

  function addFeedbackActions(bubble, meta) {
    const row = bubble.parentElement;
    const sid = meta && (meta.sessionId || sessionId);
    if (!row || !meta || meta.msgId == null || !sid || row.querySelector('.msg-actions')) return;

    const bar = el('div', 'msg-actions');
    const shareBtn = el('button', 'msg-act', '分享');
    shareBtn.type = 'button';
    shareBtn.dataset.act = 'share';
    shareBtn.title = '生成可公开访问的问答卡片';
    shareBtn.onclick = async () => {
      shareBtn.disabled = true;
      try {
        const resp = await apiFetch('/api/share', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sessionId: sid,
            msgId: meta.msgId,
            agentName: (findAgentCard(selectedAgentId()) || {}).agentName || '',
            channelCode: (channelInfo && channelInfo.channelCode) || CHANNEL_CODE || '',
          }),
        });
        const json = await resp.json().catch(() => ({}));
        if (!resp.ok || !json.ok || !json.data) {
          throw new Error(json.detail || json.error || ('HTTP ' + resp.status));
        }
        const url = json.data.url;
        let win = null;
        try { win = window.open(url, '_blank'); } catch { /* 弹窗被拦截 */ }
        if (win) {
          try { win.opener = null; } catch { /* ignore */ }
          toast('已生成分享卡片');
        } else {
          await copyText(url);
          toast('分享链接已复制');
        }
      } catch (e) {
        toast('分享失败：' + e.message);
      } finally {
        shareBtn.disabled = false;
      }
    };

    const likeBtn = el('button', 'msg-act', '有帮助');
    likeBtn.type = 'button';
    likeBtn.dataset.rating = '1';
    likeBtn.onclick = async () => {
      likeBtn.disabled = true;
      try {
        await sendMessageFeedback(meta, bar, 1);
        toast('感谢反馈');
      } catch (e) {
        toast('反馈失败：' + e.message);
      } finally {
        likeBtn.disabled = false;
      }
    };

    const dislikeBtn = el('button', 'msg-act', '没帮助');
    dislikeBtn.type = 'button';
    dislikeBtn.dataset.rating = '-1';
    dislikeBtn.onclick = () => openFeedbackModal(meta, bar);

    bar.appendChild(shareBtn);
    bar.appendChild(likeBtn);
    bar.appendChild(dislikeBtn);
    row.appendChild(bar);
  }

  function openFeedbackModal(meta, bar) {
    pendingFeedback = { meta, bar };
    if (fbMsg) {
      fbMsg.hidden = true;
      fbMsg.textContent = '';
    }
    if (fbNote) fbNote.value = '';
    if (fbReasons) {
      fbReasons.querySelectorAll('.fb-reason').forEach((btn) => btn.classList.remove('active'));
    }
    if (fbModal) fbModal.hidden = false;
  }

  function closeFeedbackModal() {
    if (fbModal) fbModal.hidden = true;
    pendingFeedback = null;
  }

  async function submitDislike() {
    if (!pendingFeedback) return;
    const picked = fbReasons && fbReasons.querySelector('.fb-reason.active');
    if (!picked) {
      if (fbMsg) {
        fbMsg.textContent = '请选择不满意的原因';
        fbMsg.hidden = false;
      }
      return;
    }
    if (fbSubmit) {
      fbSubmit.disabled = true;
      fbSubmit.textContent = '提交中…';
    }
    try {
      await sendMessageFeedback(
        pendingFeedback.meta,
        pendingFeedback.bar,
        -1,
        picked.dataset.code,
        (fbNote && fbNote.value.trim()) || '',
      );
      closeFeedbackModal();
      toast('已记录，我们会尽快改进');
    } catch (e) {
      if (fbMsg) {
        fbMsg.textContent = '提交失败：' + e.message;
        fbMsg.hidden = false;
      }
    } finally {
      if (fbSubmit) {
        fbSubmit.disabled = false;
        fbSubmit.textContent = '提交反馈';
      }
    }
  }

  if (fbReasons) {
    FB_REASONS.forEach((item) => {
      const btn = el('button', 'fb-reason', item.label);
      btn.type = 'button';
      btn.dataset.code = item.code;
      btn.onclick = () => {
        fbReasons.querySelectorAll('.fb-reason').forEach((x) => x.classList.remove('active'));
        btn.classList.add('active');
      };
      fbReasons.appendChild(btn);
    });
  }
  if (fbSubmit) fbSubmit.onclick = submitDislike;
  if (fbCancel) fbCancel.onclick = closeFeedbackModal;
  if (fbModal) {
    fbModal.addEventListener('click', (e) => {
      if (e.target === fbModal) closeFeedbackModal();
    });
  }

  function addCitations(bubble, citations, startN = 1) {
    if (!citations || !citations.length) return;
    const panel = el('div', 'citations collapsed');
    const toggle = el('span', 'citations-toggle', `引用来源 ${citations.length} 条`);
    toggle.onclick = () => panel.classList.toggle('collapsed');
    const list = el('div', 'citations-list');
    citations.forEach((c, i) => {
      const item = el('div', 'citation-item');
      const n = c.n || (startN + i);
      const dept = c.dept || '';
      const ktype = c.ktype || '';
      const path = c.titlePath || c.docTitle || '';
      const src = [dept, ktype].filter(Boolean).join('/');
      item.innerHTML = `<b>[${n}]</b> ${src ? esc(src) + ' · ' : ''}${esc(path)} · <code>${esc(c.chunkId)}</code>` +
        (c.similarity != null ? `<span class="sim">相似度 ${Number(c.similarity).toFixed(4)}</span>` : '');
      list.appendChild(item);
    });
    panel.appendChild(toggle);
    panel.appendChild(list);
    bubble.appendChild(panel);
  }

  // ---------------------------------------------------------------- 导办事项（任务 4.5）
  const guideBusy = new Set();

  function addGuideItems(bubble, items) {
    if (!Array.isArray(items) || !items.length) return;
    const box = el('div', 'guide-cards');
    const head = el('div', 'guide-head');
    head.appendChild(el('span', 'guide-head-title', '一网通办 · 可直接办理'));
    head.appendChild(el('span', 'guide-head-hint', '点击取票后跳转一网通办'));
    box.appendChild(head);

    items.forEach((item) => {
      if (!item || item.itemId == null) return;
      const card = el('div', 'guide-card');
      const info = el('div', 'guide-info');
      const title = el('div', 'guide-title');
      title.appendChild(el('b', '', item.itemName || '办事事项'));
      if (item.deptName) title.appendChild(el('span', 'guide-dept', item.deptName));
      info.appendChild(title);
      if (item.matchReason) info.appendChild(el('div', 'guide-reason', '匹配：' + item.matchReason));
      if (item.summary) info.appendChild(el('p', 'guide-summary', item.summary));
      const tags = Array.isArray(item.keywordTags) ? item.keywordTags.slice(0, 4) : [];
      if (tags.length) {
        const tagRow = el('div', 'guide-tags');
        tags.forEach((tag) => {
          if (tag) tagRow.appendChild(el('span', 'guide-tag', tag));
        });
        if (tagRow.childElementCount) info.appendChild(tagRow);
      }
      const btn = el('button', 'guide-go', item.needTicket ? '去办理' : '打开办理页');
      btn.type = 'button';
      btn.onclick = () => openGuide(item, btn);
      card.appendChild(info);
      card.appendChild(btn);
      box.appendChild(card);
    });
    if (box.querySelector('.guide-card')) bubble.appendChild(box);
  }

  async function openGuide(item, btn) {
    if (guideBusy.has(item.itemId)) return;
    guideBusy.add(item.itemId);
    const label = btn.textContent;
    btn.disabled = true;
    if (item.needTicket) btn.textContent = '正在取票…';
    // 先同步占位新窗口，避免 await 之后被浏览器判定为弹窗拦截
    let win = null;
    try { win = window.open('about:blank', '_blank'); } catch { win = null; }
    try {
      const resp = await apiFetch(`/api/gov/items/${encodeURIComponent(item.itemId)}/ticket`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ itemId: item.itemId, role: effectiveRole() }),
      });
      let json = null;
      try { json = await resp.json(); } catch { /* ignore */ }
      if (!resp.ok || !json || !json.ok) {
        if (resp.status === 401) {
          openLogin();
          showLoginMsg('登录后才能取票办理，请先登录');
        }
        const detail = (json && (json.error || json.detail)) || ('HTTP ' + resp.status);
        throw new Error(typeof detail === 'string' ? detail : '取票失败');
      }
      const url = safeUrl(json.data && json.data.jumpUrl);
      if (!url) throw new Error('办事地址无效，请联系管理员');
      if (win) {
        win.opener = null;
        win.location.replace(url);
      } else {
        location.href = url;
      }
      toast(json.data.needTicket ? '已签发一次性票据，请尽快完成办理' : '已打开办事页面');
    } catch (e) {
      if (win) { try { win.close(); } catch { /* ignore */ } }
      toast('办理入口打开失败：' + e.message);
    } finally {
      guideBusy.delete(item.itemId);
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  // ---------------------------------------------------------------- 问数卡片（任务 4.3）
  function addDataCards(bubble, cards) {
    if (!Array.isArray(cards) || !cards.length) return;
    const box = el('div', 'dq-cards');
    cards.forEach((card) => {
      if (!card || typeof card !== 'object') return;
      if (card.type === 'denied') {
        const node = el('div', 'dq-card dq-denied');
        const head = el('div', 'dq-head');
        head.appendChild(el('b', 'dq-title', card.title || '暂时无法查询'));
        if (card.status) head.appendChild(el('span', 'dq-status', 'HTTP ' + card.status));
        node.appendChild(head);
        node.appendChild(el('p', 'dq-message', card.message || '当前身份无权查询'));
        if (card.hint) node.appendChild(el('p', 'dq-hint', card.hint));
        if (card.footnote) node.appendChild(el('div', 'dq-footnote', card.footnote));
        box.appendChild(node);
        return;
      }

      const node = el('div', 'dq-card');
      const head = el('div', 'dq-head');
      head.appendChild(el('b', 'dq-title', card.title || '个人数据'));
      if (card.subtitle) head.appendChild(el('span', 'dq-sub', card.subtitle));
      const ident = card.identity || {};
      const idBits = [ident.userName, ident.userNo, ident.role].filter(Boolean);
      if (idBits.length) head.appendChild(el('span', 'dq-identity', idBits.join(' · ')));
      node.appendChild(head);

      const summary = Array.isArray(card.summary) ? card.summary : [];
      if (summary.length) {
        const metrics = el('div', 'dq-metrics');
        summary.forEach((m) => {
          if (!m) return;
          const cell = el('div', 'dq-metric' + (m.emphasis ? ' emphasis' : ''));
          cell.appendChild(el('span', 'dq-label', m.label || ''));
          cell.appendChild(el('span', 'dq-value', m.value == null ? '' : String(m.value)));
          metrics.appendChild(cell);
        });
        node.appendChild(metrics);
      }

      const cols = Array.isArray(card.columns) ? card.columns : [];
      const rows = Array.isArray(card.rows) ? card.rows : [];
      if (cols.length && rows.length) {
        const wrap = el('div', 'dq-table-wrap');
        const table = document.createElement('table');
        table.className = 'dq-table';
        const thead = document.createElement('thead');
        const hr = document.createElement('tr');
        cols.forEach((c) => hr.appendChild(el('th', '', c.label || c.key || '')));
        thead.appendChild(hr);
        table.appendChild(thead);
        const tbody = document.createElement('tbody');
        rows.forEach((row) => {
          const tr = document.createElement('tr');
          cols.forEach((c) => {
            const value = row ? row[c.key] : null;
            tr.appendChild(el('td', '', value == null ? '—' : String(value)));
          });
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
        node.appendChild(wrap);
      } else if (card.empty) {
        node.appendChild(el('div', 'dq-empty', '当前条件下暂无记录'));
      }

      const foot = [];
      if (card.source) foot.push(card.source === 'mock' ? '数据源：演示数据' : '数据源：实时接口');
      if (card.generatedAt) foot.push('查询于 ' + String(card.generatedAt).replace('T', ' '));
      if (card.footnote) foot.push(card.footnote);
      if (foot.length) node.appendChild(el('div', 'dq-footnote', foot.join(' · ')));
      box.appendChild(node);
    });
    if (box.childElementCount) bubble.appendChild(box);
  }

  function addAi(answer, meta, citations, answerHtml) {
    const b = msgWrap('ai');
    // 优先用服务端渲染好的安全 HTML（任务 1.9），缺失时回退到本地渲染
    b.innerHTML = answerHtml || renderContent(answer);
    if (citations) addCitations(b, citations);
    if (meta) {
      addMeta(b, meta);
      addFeedbackActions(b, meta);
    }
    addAiLabel(b);
    scrollDown();
    return b;
  }

  function addError(text) {
    const b = msgWrap('ai');
    b.classList.add('error');
    b.textContent = text;
    scrollDown();
  }

  function setBusy(v) {
    busy = v;
    sendBtn.disabled = v || !inputEl.value.trim();
    if (micBtn) micBtn.disabled = v || voice.busy;
  }

  async function ask(question) {
    question = (question || inputEl.value).trim();
    if (!question || busy) return;
    inputEl.value = '';
    autosize();
    setBusy(true);
    addUser(question);
    const role = effectiveRole();

    const b = msgWrap('ai');
    b.classList.add('thinking');
    b.textContent = '正在检索知识库并生成回答…';
    scrollDown();

    let acc = '';
    let started = false;

    const finalize = (d) => {
      if (d.sessionId) {
        sessionId = d.sessionId;
        localStorage.setItem(LS_SESSION, sessionId);
        rememberSession(sessionId, question);
      }
      // 任务 4.6：命中「找人工」表达时提示转人工入口（support-widget.js 监听）
      if (d.handoffSuggested) {
        document.dispatchEvent(new CustomEvent('nxz:handoff', {
          detail: { keywords: d.handoffKeywords || [] },
        }));
      }
      b.classList.remove('thinking');
      const msgMeta = {
        msgId: d.msgId,
        sessionId: d.sessionId || sessionId,
        model: d.model,
        latencyMs: d.latencyMs,
        confidence: d.confidence,
        refused: d.refused,
      };
      if (d.answer == null) {
        b.innerHTML = renderContent('抱歉，知识库中未找到相关信息，建议联系相关部门或转人工咨询。');
        addDataCards(b, d.dataCards);
        addGuideItems(b, d.guideItems);
        addMeta(b, Object.assign({}, msgMeta, { refused: true, confidence: 0.15 }));
        addFeedbackActions(b, msgMeta);
        addAiLabel(b);
        scrollDown();
        return;
      }
      b.innerHTML = d.answerHtml || renderContent(d.answer);
      addCitations(b, d.citations);
      addDataCards(b, d.dataCards);
      addGuideItems(b, d.guideItems);
      addMeta(b, msgMeta);
      addFeedbackActions(b, msgMeta);
      addAiLabel(b);
      scrollDown();
    };

    try {
      // 3.5：已登录时附带 token，后端以 token 身份为准（防前端伪造角色）
      const resp = await apiFetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question,
          role,
          sessionId,
          agentId: agentId == null ? null : String(agentId),
          // 任务 3.11：未识别问题按渠道归因（无渠道参数时为 null，后端回落到默认渠道）
          channelCode: (channelInfo && channelInfo.channelCode) || CHANNEL_CODE || null,
        }),
      });
      if (!resp.ok || !resp.body) {
        let msg = '服务错误 HTTP ' + resp.status;
        try { const j = await resp.json(); if (j.error) msg = j.error; } catch {}
        throw new Error(msg);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let finished = false;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line.startsWith('data:')) continue;
          let obj;
          try { obj = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (obj.type === 'delta') {
            if (!started) { b.textContent = ''; started = true; }
            acc += obj.text;
            b.innerHTML = renderContent(acc);
            scrollDown();
          } else if (obj.type === 'done') {
            finished = true;
            finalize(obj);
          } else if (obj.type === 'error') {
            finished = true;
            b.remove();
            addError(obj.error || '生成失败');
          }
        }
      }
      if (!finished && !started) { b.remove(); addError('回答中断，请重试'); }
    } catch (e) {
      if (b.parentElement) b.remove();
      addError('网络错误或服务未启动：' + e.message);
    } finally {
      setBusy(false);
    }
  }

  async function loadHistory(sid) {
    try {
      const resp = await apiFetch(`/api/sessions/${encodeURIComponent(sid)}/messages`);
      const json = await resp.json();
      if (!json.ok) {
        // 服务端会话不存在（过期/被清理）：移除本机条目，回到首屏
        forgetSession(sid);
        if (sid === sessionId) {
          sessionId = null;
          localStorage.removeItem(LS_SESSION);
        }
        chatEl.innerHTML = '';
        addWelcome();
        renderHistory();
        return;
      }
      chatEl.innerHTML = '';
      let firstQ = '';
      for (const m of json.data) {
        if (m.role === 'user') {
          if (!firstQ) firstQ = m.content;
          addUser(m.content);
        } else {
          addAi(m.content,
            {
              model: null,
              latencyMs: null,
              confidence: m.confidence,
              refused: m.refused,
              msgId: m.msgId,
              sessionId: sid,
            },
            m.citations, m.answerHtml);
        }
      }
      sessionId = sid;
      localStorage.setItem(LS_SESSION, sid);
      rememberSession(sid, firstQ || '历史会话');
      renderHistory();
      scrollDown();
    } catch {
      if (!chatEl.querySelector('.msg, .welcome')) addWelcome();
    }
  }

  function autosize() {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
  }

  sendBtn.onclick = () => ask();
  inputEl.addEventListener('input', () => { autosize(); setBusy(busy); });
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); }
  });
  roleEl.addEventListener('change', async () => {
    if (roleEl.disabled) return;
    localStorage.setItem(LS_ROLE, roleEl.value);
    await refreshPortal();
  });
  if (agentSelectEl) {
    agentSelectEl.addEventListener('change', () => {
      switchAssistant(agentSelectEl.value);
      // 忙碌或选中项已失效时 switchAssistant 会拒绝切换，下拉回显真实状态
      renderAgentSelect();
    });
  }
  newChatBtn.onclick = () => {
    sessionId = null;
    localStorage.removeItem(LS_SESSION);
    chatEl.innerHTML = '';
    closeHistoryDrawer();
    addWelcome();
    renderHistory();
  };

  // 历史面板（桌面常驻 / 移动抽屉）
  if (historyToggle) historyToggle.onclick = openHistoryDrawer;
  if (historyClose) historyClose.onclick = closeHistoryDrawer;
  if (historyOverlay) historyOverlay.onclick = closeHistoryDrawer;
  if (historyClear) {
    historyClear.onclick = () => {
      const channel = CHANNEL_CODE || '';
      saveHistory(readHistory().filter((e) => (e.channel || '') !== channel));
      // 清空后刷新不再恢复当前会话；屏上的消息保留，继续提问会重新记入历史
      try { localStorage.removeItem(LS_SESSION); } catch { /* ignore */ }
      renderHistory();
    };
  }

  // 登录弹窗
  if (loginForm) loginForm.addEventListener('submit', submitLogin);
  const loginCancel = document.getElementById('lm-cancel');
  if (loginCancel) loginCancel.onclick = closeLogin;
  if (loginModal) {
    loginModal.addEventListener('click', (e) => {
      if (e.target === loginModal) closeLogin();
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (fbModal && !fbModal.hidden) closeFeedbackModal();
    else if (loginModal && !loginModal.hidden) closeLogin();
    else if (document.body.classList.contains('history-open')) closeHistoryDrawer();
  });

  // 初始化（先确定登录身份，再拿渠道配置/历史，避免首屏身份与角色不一致）
  async function boot() {
    setBusy(false);
    const ssoError = SDK_MODE ? '' : await consumeSsoHash();
    initSdkBridge();
    if (SDK_MODE) {
      sdkWaiting = true;
      await waitSdkInit(800);
      sdkWaiting = false;
    }
    await loadMe();
    if (ssoError) { openLogin(); showLoginMsg(ssoError); }
    applyRoleFromAuth();
    await Promise.all([loadChannel(), loadPublicAgents(), loadApps(), loadVoiceStatus()]);
    ensureAgentVisible();
    renderAgentSelect();
    renderHistory();
    if (sessionId) loadHistory(sessionId);
    else addWelcome();
    postSdk('nxz:sdk:state', sdkStatePayload());
  }
  boot();

  // 测试钩子（任务 1.9）：Node 下把渲染器挂到 globalThis，便于与后端
  // app/core/richtext.py 做逐字符一致性比对。浏览器里 __NXZ_EXPORT__ 未定义，无副作用。
  if (typeof globalThis !== 'undefined' && globalThis.__NXZ_EXPORT__) {
    globalThis.__NXZ_EXPORT__.renderContent = renderContent;
    globalThis.__NXZ_EXPORT__.esc = esc;
    globalThis.__NXZ_EXPORT__.safeUrl = safeUrl;
  }
})();
