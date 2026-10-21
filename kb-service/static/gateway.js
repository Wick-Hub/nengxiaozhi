/* 模型网关页（C-01 LLM 网关接入 · 2.4 延伸）
 *
 * 对接接口（/api/admin 前缀，AUTH_ENABLED=true 时需管理员）：
 *   GET /api/admin/gateway/status   网关可达性 + 控制台入口 + 错误码对照
 *
 * 三条设计约定：
 *
 * 1. **只做外链，不做内嵌**。网关控制台带
 *    `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`，
 *    这是它保护「内存中访问令牌」的有意设计，不能绕过。
 *    因此这里只提供 `<a target="_blank">` 入口。
 * 2. **不持有网关管理员凭据**。后端只探无需鉴权的 `/healthz`，
 *    所以本页拿不到配额/用量类指标 —— 页面上要**如实说明**，
 *    而不是显示一堆「—」让人以为功能坏了。
 * 3. **探测失败不是错误**。网关没起时依然正常渲染，用状态徽章说明情况；
 *    否则管理页会因为一个暂时不通的地址而整屏报错。
 *
 * 由 admin.js 的页签切换调用 window.initGatewayPage()，只初始化一次（懒加载）。
 */
(() => {
  'use strict';

  let booted = false;
  let hintsOpen = false;

  const el = id => document.getElementById(id);

  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function authFetch(url, opts) {
    return (window.NXZAuth ? window.NXZAuth.fetch : fetch)(url, opts);
  }

  async function api(url, opts) {
    const resp = await authFetch(url, { credentials: 'same-origin', ...(opts || {}) });
    if (resp.status === 401) throw new Error('未登录或登录已失效，请先登录管理员账号');
    if (resp.status === 403) throw new Error('当前账号无管理端权限');
    let json;
    try {
      json = await resp.json();
    } catch (e) {
      throw new Error(`服务返回异常（HTTP ${resp.status}）`);
    }
    if (!resp.ok) throw new Error(json.detail || json.error || `请求失败（HTTP ${resp.status}）`);
    if (json.ok === false) throw new Error(json.error || '操作失败');
    return json.data;
  }

  function msg(text, cls) {
    const node = el('gw-note');
    if (!node) return;
    node.textContent = text || '';
    node.className = `cl-note${cls ? ' ' + cls : ''}`;
  }

  /** 状态徽章。**三态刻意不合并**：有响应但非 200 ≠ 离线。 */
  const STATUS_META = {
    online: { cls: 'gw-on', text: '网关在线' },
    unknown: { cls: 'gw-unk', text: '有服务·非网关' },
    offline: { cls: 'gw-off', text: '连不上' },
    'vendor-direct': { cls: 'gw-skip', text: '厂商直连' },
  };

  function statusBadge(status) {
    const meta = STATUS_META[status] || { cls: 'gw-skip', text: esc(status) };
    return `<span class="gw-badge ${meta.cls}"><i class="gw-dot"></i>${meta.text}</span>`;
  }

  /** 单个端点卡片。 */
  function endpointCard(ep) {
    const probe = ep.probe || {};
    const parts = [];

    parts.push('<div class="gw-item">');
    parts.push('<div class="gw-head">');
    parts.push(statusBadge(ep.status));
    parts.push(`<code class="gw-origin">${esc(ep.origin)}</code>`);
    parts.push(`<span class="gw-src">${esc(ep.label)}</span>`);
    if (ep.status === 'online' && probe.latencyMs !== null && probe.latencyMs !== undefined) {
      parts.push(`<span class="gw-lat">${esc(probe.latencyMs)} ms</span>`);
    }
    if (ep.status === 'vendor-direct') {
      parts.push(`<span class="gw-lat">${esc(ep.vendorLabel || '未走网关')}</span>`);
    }
    parts.push('</div>');

    // 关联模型 + remoteModel（这正是最容易配错的地方：白名单要逐字一致）
    if (ep.models && ep.models.length) {
      parts.push('<div class="gw-models">');
      for (const m of ep.models) {
        const rm = m.hasRemoteModel
          ? `remoteModel=<code>${esc(m.remoteModel)}</code>`
          : '<span class="gw-warn">未填 remoteModel</span>';
        parts.push(`<span class="gw-model"><b>${esc(m.modelCode)}</b> · ${rm}</span>`);
      }
      parts.push('</div>');
    }

    // offline 是真故障（红）；unknown 只是"有服务但没网关健康端点"（中性提示）
    if (probe.error) {
      const cls = ep.status === 'offline' ? 'gw-err' : 'gw-unk-note';
      const prefix = ep.status === 'offline' ? '探测失败：' : '';
      parts.push(`<div class="${cls}">${prefix}${esc(probe.error)}</div>`);
    }

    parts.push('</div>');
    return parts.join('');
  }

  /** 接入配置指引（按当前实际状态给，而不是一段死文案）。 */
  /** 网关下发的模型清单（**本平台模型的唯一来源**）。 */
  function renderModels(data) {
    const box = el('gw-models');
    if (!box) return;
    const count = el('gw-model-count');

    // 清单可能来自 /gateway/status（随总览一起下发）。单独刷新时另走 /models。
    let items = data.models || [];
    let error = data.modelsError || null;
    let stale = data.modelsStale;

    if (error) {
      if (count) count.textContent = '拉取失败';
      box.innerHTML = `<div class="gw-unk-note">${esc(error)}</div>`;
      const empty = el('gw-models-empty');
      if (empty) empty.hidden = true;
      return;
    }

    if (count) {
      count.textContent = stale
        ? `${items.length} 个（缓存）`
        : `${items.length} 个`;
    }

    const empty = el('gw-models-empty');
    if (empty) empty.hidden = items.length > 0;

    box.innerHTML = items.map(m => `
      <div class="gw-model-row">
        <span class="gw-model-name"><code>${esc(m.id)}</code></span>
        <span class="gw-model-owner">${esc(m.ownedBy || '网关')}</span>
        <button class="btn-ghost" data-probe="${esc(m.id)}">自测</button>
      </div>`).join('');

    // 逐个自测：点按钮 → 起一次最小调用，结果落在页面常驻区
    box.querySelectorAll('[data-probe]').forEach(btn => {
      btn.onclick = () => probeModel(btn.dataset.probe, btn);
    });
  }

  /** 单个模型连通性自测。**失败也是结果**，要把网关错误码与 requestId 显示出来。 */
  async function probeModel(code, btn) {
    const out = el('gw-probe-page');
    const label = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '自测中…'; }
    if (out) { out.hidden = false; out.innerHTML = `<div class="tr-answer">正在用「${esc(code)}」发起一次最小调用…</div>`; }

    let d;
    try {
      d = await api(`/api/admin/gateway/models/${encodeURIComponent(code)}/probe`, { method: 'POST' });
    } catch (e) {
      if (out) out.innerHTML = `<div class="tr-answer err">${esc(e.message)}</div>`;
      if (btn) { btn.disabled = false; btn.textContent = label; }
      return;
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = label; }
    }

    if (out) {
      out.innerHTML = `
        <div class="tr-head">连通性自测 · ${esc(d.modelCode)} <span class="tag">${esc(d.sourceLabel || '')}</span></div>
        <div class="tr-answer">${d.ok ? '✅ 调用成功' : '❌ 调用失败'}${d.mocked ? '（模拟回答，未真实调用）' : ''}</div>
        <div class="tr-meta">
          <span>接口地址：${esc(d.baseUrl || '-')}</span>
          ${d.ok ? `<span>耗时：${esc(d.latencyMs ?? '-')} ms</span>` : ''}
        </div>
        ${d.sample ? `<div class="tr-answer mono">${esc(d.sample)}</div>` : ''}
        ${d.error ? `<div class="tr-answer err">${esc(d.error)}</div>` : ''}
        ${d.hint ? `<div class="tr-meta"><span>处置建议：${esc(d.hint)}</span></div>` : ''}
        ${d.requestId ? `<div class="tr-meta"><span>requestId：${esc(d.requestId)}（可在网关请求日志中检索）</span></div>` : ''}`;
    }
  }

  /** 单独刷新模型清单（绕过后端 30 秒缓存）。 */
  async function refreshModels() {
    const btn = el('gw-model-refresh');
    if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
    try {
      const d = await api('/api/admin/models?refresh=1');
      renderModels({ models: d.items || [], modelsError: d.error, modelsStale: d.stale });
    } catch (e) {
      const box = el('gw-models');
      if (box) box.innerHTML = `<div class="gw-unk-note">${esc(e.message)}</div>`;
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '刷新清单'; }
    }
  }

  function renderGuide(data) {
    const node = el('gw-guide');
    if (!node) return;

    const gateways = (data.endpoints || []).filter(e => e.isGateway);
    const sample = gateways.length ? gateways[0] : null;
    const apiBase = sample ? `${sample.origin}/v1` : 'http://<网关主机>:<端口>/v1';

    const rows = [
      ['apiBase', apiBase, '网关地址，以 /v1 结尾'],
      ['remoteModel', '<网关的虚拟模型别名>', '必须与网关业务 Key 的 AllowedModels 逐字一致'],
      ['apiKey', 'sk-campus-xxxxx', '网关签发的业务 Key（厂商 Key 无需交给能小智）'],
    ];

    node.innerHTML = `
      <table class="gw-table">
        <thead><tr><th>字段</th><th>填什么</th><th>说明</th></tr></thead>
        <tbody>
          ${rows.map(([k, v, d]) => `
            <tr>
              <td><code>${esc(k)}</code></td>
              <td><code class="gw-val">${esc(v)}</code></td>
              <td class="gw-desc">${esc(d)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p class="gw-fineprint">
        填好后在<b>本页模型清单</b>里点该行的<b>自测</b>验证连通；失败时会显示网关错误码与 requestId，
        可直接到网关请求日志里检索。
      </p>`;
  }

  function renderHints(data) {
    const node = el('gw-hints');
    if (!node) return;
    const rows = data.errorHints || [];
    node.innerHTML = `
      <table class="gw-table">
        <thead><tr><th>errorDetail.code</th><th>含义与处置</th></tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td><code class="gw-code">${esc(r.code)}</code></td>
              <td class="gw-desc">${esc(r.hint)}</td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  }

  async function load() {
    msg('探测中…');
    let data;
    try {
      data = await api('/api/admin/gateway/status');
    } catch (e) {
      msg(e.message, 'err');
      const list = el('gw-list');
      if (list) list.innerHTML = '';
      const empty = el('gw-empty');
      if (empty) empty.hidden = false;
      return;
    }

    const endpoints = data.endpoints || [];
    const count = el('gw-count');
    if (count) {
      count.textContent = data.gatewayCount
        ? `${data.onlineCount}/${data.gatewayCount} 个网关在线`
        : `${endpoints.length} 个接口地址`;
    }

    const list = el('gw-list');
    if (list) list.innerHTML = endpoints.map(endpointCard).join('');
    const empty = el('gw-empty');
    if (empty) empty.hidden = endpoints.length > 0;

    const btn = el('gw-console');
    if (btn) {
      if (data.consoleUrl) {
        btn.disabled = false;
        btn.dataset.url = data.consoleUrl;
      } else {
        btn.disabled = true;
        delete btn.dataset.url;
      }
    }

    msg(data.hint || '', data.onlineCount ? '' : 'err');
    renderModels(data);
    renderGuide(data);
    renderHints(data);
  }

  /** 页签入口：由 admin.js 的 BOOTERS 调用（懒加载，只初始化一次）。
   *
   *  返回 Promise —— admin.js 的页签切换据此显示/收起「加载中」提示。
   */
  function initGatewayPage() {
    if (booted) return Promise.resolve();
    booted = true;

    const refresh = el('gw-refresh');
    if (refresh) refresh.onclick = () => { load(); };

    const modelRefresh = el('gw-model-refresh');
    if (modelRefresh) modelRefresh.onclick = () => { refreshModels(); };

    const consoleBtn = el('gw-console');
    if (consoleBtn) {
      consoleBtn.onclick = () => {
        const url = consoleBtn.dataset.url;
        if (!url) return;
        // 新标签页打开；noopener 防止新页面通过 window.opener 反向操作本页
        window.open(url, '_blank', 'noopener,noreferrer');
      };
    }

    const toggle = el('gw-hints-toggle');
    if (toggle) {
      toggle.onclick = () => {
        hintsOpen = !hintsOpen;
        const box = el('gw-hints');
        if (box) box.hidden = !hintsOpen;
        toggle.textContent = hintsOpen ? '收起' : '展开';
      };
    }

    return load();
  }

  window.initGatewayPage = initGatewayPage;
})();
