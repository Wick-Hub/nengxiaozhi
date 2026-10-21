/* 管理端登录与菜单/按钮级权限（任务 2.9）。
 *
 * 页面引入顺序：auth.js 必须在 admin.js / points.js / preview.js 之前。
 * 各页的 api() 包装统一调用 NXZAuth.fetch()，它会等待登录完成再发请求；
 * AUTH_ENABLED=false 时立即放行，保持开发/演示体验不变。
 */
(() => {
  const TOKEN_KEY = 'nxz_token';
  const ROLE_OPTIONS = ['本科生', '研究生', '教职工', '离退休人员', '访客'];
  const state = {
    authEnabled: false,
    authenticated: false,
    userNo: '',
    role: '访客',
    isAdmin: false,
    isManager: false,
    permissions: [],
    ssoEnabled: true,
  };
  let resolveReady;
  const readyPromise = new Promise((resolve) => { resolveReady = resolve; });
  let readyResolved = false;

  function token() {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (_) { return ''; }
  }
  function setToken(value) {
    try { localStorage.setItem(TOKEN_KEY, value); } catch (_) { /* 隐私模式忽略 */ }
  }
  function clearToken() {
    try { localStorage.removeItem(TOKEN_KEY); } catch (_) { /* ignore */ }
  }

  function rawFetch(url, opts) {
    const options = Object.assign({}, opts || {});
    const headers = new Headers(options.headers || {});
    const t = token();
    if (t && !headers.has('Authorization')) headers.set('Authorization', 'Bearer ' + t);
    if (!headers.has('Accept') && !(options.body instanceof FormData)) {
      headers.set('Accept', 'application/json');
    }
    options.headers = headers;
    return fetch(url, options);
  }

  async function fetchAuthed(url, opts) {
    await readyPromise;
    const resp = await rawFetch(url, opts);
    if (resp.status === 401) {
      clearToken();
      showLogin('登录已失效，请重新登录');
      const err = new Error('需要登录');
      err.status = 401;
      throw err;
    }
    return resp;
  }

  function can(code) {
    if (!state.authEnabled) return true;
    if (!state.authenticated) return false;
    if (state.isAdmin) return true;
    if (!code) return true;
    const codes = String(code).split(',').map((s) => s.trim()).filter(Boolean);
    if (!codes.length) return true;
    return codes.some((c) => state.permissions.indexOf(c) >= 0);
  }

  function applyPermissions() {
    document.querySelectorAll('[data-perm]').forEach((el) => {
      el.classList.toggle('perm-denied', !can(el.dataset.perm));
    });
    const active = document.querySelector('#tabs .tab.active');
    if (active && active.classList.contains('perm-denied')) {
      const first = document.querySelector('#tabs .tab:not(.perm-denied)');
      if (first) first.click();
    }
    if (state.authEnabled && state.authenticated && location.pathname.startsWith('/admin')) {
      const visibleTabs = document.querySelectorAll('#tabs .tab:not(.perm-denied)').length;
      if (!visibleTabs) showDenied('当前角色未分配管理端任何模块权限，请联系管理员。');
    }
    const path = location.pathname.replace(/\/+$/, '');
    const pageNeed = path === '/points' ? 'points.view'
      : path === '/channels' ? 'channels.view'
        : path === '/apps' ? 'apps.view'
          : path === '/unrecognized' ? 'unrecognized.view'
            : path === '/feedback' ? 'feedback.view'
            : path === '/gov' ? 'gov.view'
              : path === '/access' ? 'access.view'
              : path === '/support' ? 'support.view'
              : path === '/preview' ? 'docs.view' : null;
    if (pageNeed && !can(pageNeed)) showDenied('当前角色无权访问该页面。');
  }

  function showDenied(message) {
    let box = document.getElementById('nxz-denied');
    if (!box) {
      box = document.createElement('div');
      box.id = 'nxz-denied';
      box.className = 'nxz-denied';
      document.body.appendChild(box);
    }
    box.innerHTML =
      '<div class="nxz-denied-box">' +
      '<h3>403 · 无权访问</h3>' +
      '<p></p>' +
      '<div class="nxz-denied-ops">' +
      '<button type="button" data-auth-logout>切换账号</button>' +
      '<a href="/">返回对话</a>' +
      '</div></div>';
    box.querySelector('p').textContent = message;
    box.hidden = false;
    const logoutBtn = box.querySelector('[data-auth-logout]');
    if (logoutBtn) logoutBtn.onclick = logout;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function hashParam(name) {
    const m = new RegExp('[#&]' + name + '=([^&]*)').exec(location.hash || '');
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (_) { return m[1]; }
  }

  // CAS 回调（任务 4.1）：#cas=<一次性交换码> → 换正式 token；失败信息暂存后由登录框展示
  async function consumeSsoHash() {
    const code = hashParam('cas');
    const error = hashParam('cas_error');
    if (!code && !error) return;
    try { history.replaceState(null, '', location.pathname + location.search); } catch (_) { /* ignore */ }
    if (error) {
      try { sessionStorage.setItem('nxz_sso_error', error); } catch (_) { /* ignore */ }
      return;
    }
    try {
      const resp = await rawFetch('/api/auth/cas/exchange', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      });
      const json = await resp.json().catch(() => ({}));
      if (resp.ok && json.ok && json.data && json.data.token) {
        setToken(json.data.token);
        try { sessionStorage.removeItem('nxz_sso_error'); } catch (_) { /* ignore */ }
        location.reload();
        return;
      }
      try {
        sessionStorage.setItem('nxz_sso_error', json.error || ('统一身份认证失败（HTTP ' + resp.status + '）'));
      } catch (_) { /* ignore */ }
    } catch (err) {
      try { sessionStorage.setItem('nxz_sso_error', '统一身份认证失败：' + (err.message || err)); } catch (_) { /* ignore */ }
    }
  }

  function ssoLoginUrl() {
    return '/api/auth/cas/login?redirect=' + encodeURIComponent(location.pathname + location.search);
  }

  function showLogin(message) {
    let modal = document.getElementById('nxz-auth-modal');
    if (modal) {
      modal.hidden = false;
      const msg = document.getElementById('nxz-auth-msg');
      if (msg && message) msg.textContent = message;
      return;
    }

    modal = el('div', 'nxz-auth-modal');
    modal.id = 'nxz-auth-modal';
    const mask = el('div', 'nxz-auth-mask');
    const form = el('form', 'nxz-auth-box');
    form.innerHTML =
      '<h3>登录管理后台</h3>' +
      '<p class="nxz-auth-sub">能小智 · 校园 AI 助手平台</p>' +
      '<div class="nxz-auth-msg" id="nxz-auth-msg"></div>' +
      '<label>学工号<input id="nxz-auth-user" autocomplete="username" maxlength="64" required></label>' +
      '<label>密码（管理员账号）<input id="nxz-auth-pass" type="password" autocomplete="current-password" maxlength="128" placeholder="演示账号可留空"></label>' +
      '<label>身份（未登记账号时使用）<select id="nxz-auth-role">' +
      ROLE_OPTIONS.map((r) => `<option value="${r}">${r}</option>`).join('') +
      '</select></label>' +
      '<div class="nxz-auth-actions"><button type="submit" class="btn-primary">登录</button></div>' +
      '<a class="nxz-auth-sso" id="nxz-auth-sso" href="#">使用统一身份认证登录</a>' +
      '<p class="nxz-auth-hint">管理员账号由系统管理员在「系统管理 · 角色权限」中创建。</p>';
    modal.appendChild(mask);
    modal.appendChild(form);
    document.body.appendChild(modal);

    const ssoLink = form.querySelector('#nxz-auth-sso');
    if (ssoLink) {
      ssoLink.href = ssoLoginUrl();
      ssoLink.hidden = !state.ssoEnabled;
    }

    const msg = form.querySelector('#nxz-auth-msg');
    let text = message;
    if (!text) {
      try { text = sessionStorage.getItem('nxz_sso_error') || ''; } catch (_) { text = ''; }
      if (text) { try { sessionStorage.removeItem('nxz_sso_error'); } catch (_) { /* ignore */ } }
    }
    if (text) msg.textContent = text;

    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const userNo = form.querySelector('#nxz-auth-user').value.trim();
      const password = form.querySelector('#nxz-auth-pass').value;
      const role = form.querySelector('#nxz-auth-role').value;
      if (!userNo) { msg.textContent = '请输入学工号'; return; }
      const btn = form.querySelector('button[type="submit"]');
      btn.disabled = true;
      msg.textContent = '登录中…';
      try {
        const body = { userNo };
        if (password) body.password = password;
        if (role) body.role = role;
        const resp = await rawFetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const json = await resp.json().catch(() => ({}));
        if (!resp.ok || !json.ok) {
          msg.textContent = json.error || ('登录失败（HTTP ' + resp.status + '）');
          return;
        }
        setToken(json.data.token);
        window.location.reload();
      } catch (err) {
        msg.textContent = '登录失败：' + (err.message || err);
      } finally {
        btn.disabled = false;
      }
    };
  }

  function logout() {
    clearToken();
    window.location.reload();
  }

  function renderBadge() {
    const host = document.querySelector('header .controls') || document.querySelector('header .topnav');
    if (!host) return;
    let badge = document.getElementById('nxz-auth-badge');
    if (!badge) {
      badge = el('span', 'nxz-auth-badge');
      badge.id = 'nxz-auth-badge';
      host.insertBefore(badge, host.firstChild);
    }
    badge.innerHTML = '';
    if (!state.authEnabled) {
      badge.appendChild(el('span', 'nxz-auth-chip muted', '演示模式 · 鉴权未开启'));
      return;
    }
    if (!state.authenticated) {
      const btn = el('button', 'btn-ghost', '登录');
      btn.type = 'button';
      btn.onclick = () => showLogin('');
      badge.appendChild(btn);
      return;
    }
    badge.appendChild(el('span', 'nxz-auth-chip', `${state.userNo} · ${state.role}`));
    const out = el('button', 'btn-ghost', '退出');
    out.type = 'button';
    out.onclick = logout;
    badge.appendChild(out);
  }

  function authorizeImages(root) {
    const scope = root || document;
    scope.querySelectorAll('img[data-auth-src]').forEach(async (img) => {
      if (img.dataset.authLoaded) return;
      img.dataset.authLoaded = '1';
      try {
        const resp = await fetchAuthed(img.dataset.authSrc);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const url = URL.createObjectURL(await resp.blob());
        img.src = url;
      } catch (_) {
        img.alt = (img.alt || '') + '（图片加载失败）';
      }
    });
  }

  async function download(url, filename) {
    const resp = await fetchAuthed(url);
    if (!resp.ok) {
      let detail = 'HTTP ' + resp.status;
      try { const j = await resp.json(); detail = j.detail || j.error || detail; } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    const blob = await resp.blob();
    let name = filename || '';
    if (!name) {
      const disp = resp.headers.get('Content-Disposition') || '';
      // 后端中文文件名走 RFC 5987（filename*=UTF-8''...），优先取它
      const star = /filename\*=UTF-8''([^;]+)/i.exec(disp);
      if (star) {
        try { name = decodeURIComponent(star[1]); } catch (_) { name = star[1]; }
      } else {
        const plain = /filename="?([^";]+)"?/i.exec(disp);
        if (plain) name = plain[1];
      }
    }
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = name || 'download';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objectUrl);
  }

  function applyMe(me) {
    state.authEnabled = !!me.authEnabled;
    state.authenticated = !!me.authenticated;
    state.userNo = me.userNo || '';
    state.role = me.role || '访客';
    state.isAdmin = !!me.isAdmin;
    state.isManager = !!me.isManager;
    state.permissions = Array.isArray(me.permissions) ? me.permissions : [];
    state.ssoEnabled = me.ssoEnabled !== false;
  }

  async function loadMe() {
    try {
      const resp = await rawFetch('/api/auth/me');
      if (!resp.ok) return false;
      const json = await resp.json();
      if (json && json.ok && json.data) {
        applyMe(json.data);
        return true;
      }
    } catch (_) { /* 后端不可达时按演示模式处理，页面自己报错 */ }
    return false;
  }

  // 角色矩阵变更后刷新当前身份与菜单/按钮可见性（无需重新登录）
  async function refresh() {
    if (!(await loadMe())) return;
    renderBadge();
    applyPermissions();
  }

  async function boot() {
    await consumeSsoHash();
    await loadMe();
    renderBadge();

    if (!state.authEnabled) {
      resolveOnce();
      applyPermissions();
      return;
    }
    if (!state.authenticated) {
      showLogin('');
      return;
    }
    resolveOnce();
    applyPermissions();
  }

  function resolveOnce() {
    if (readyResolved) return;
    readyResolved = true;
    resolveReady();
  }

  const observer = new MutationObserver((mutations) => {
    if (mutations.some((m) => m.addedNodes.length)) {
      window.requestAnimationFrame(applyPermissions);
    }
  });

  window.NXZAuth = {
    fetch: fetchAuthed,
    rawFetch,
    ready: () => readyPromise,
    can,
    apply: applyPermissions,
    refresh,
    authorizeImages,
    download,
    logout,
    get me() { return Object.assign({}, state); },
    get token() { return token(); },
  };

  document.addEventListener('DOMContentLoaded', () => {
    observer.observe(document.body, { childList: true, subtree: true });
  });
  boot();
})();
