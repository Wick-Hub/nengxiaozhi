/* 能小智 · 一网通办大厅兑换页（任务 4.5 / P-DB-02）
 *
 * 流程：对话页取票 → /hall?ticket=… → POST /api/gov/tickets/verify
 * 票据在服务端原子消费（一次性）；页面拿到身份与事项后只做展示与模拟表单。
 * 为避免票据进入浏览器历史/ Referer，读取后立刻从地址栏移除。
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const ticket = (params.get('ticket') || '').trim();

  if (ticket) {
    params.delete('ticket');
    const rest = params.toString();
    try {
      history.replaceState(null, '', location.pathname + (rest ? '?' + rest : '') + location.hash);
    } catch (_) { /* ignore */ }
  }

  function fmt(raw) {
    if (!raw) return '-';
    const d = new Date(raw);
    if (isNaN(d.getTime())) return String(raw);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function showState(name) {
    $('state-loading').hidden = name !== 'loading';
    $('state-error').hidden = name !== 'error';
    $('state-ok').hidden = name !== 'ok';
  }

  function showError(title, desc, canRetry) {
    $('err-title').textContent = title;
    $('err-desc').textContent = desc;
    $('retry').hidden = !canRetry;
    showState('error');
  }

  function failText(status, detail) {
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (status === 410) return '票据已过期，请返回能小智重新发起办理。';
    if (status === 409) return '票据已被使用或事项已停用，请返回能小智重新取票。';
    if (status === 404) return '票据无效或不存在，请返回能小智重新办理。';
    if (status === 403) return '当前票据身份无权办理该事项。';
    if (status === 400) return '票据格式不正确，请从能小智对话页重新进入。';
    return '票据核验失败（HTTP ' + status + '），请稍后重试。';
  }

  function fill(data) {
    const item = data.item || {};
    const identity = data.identity || {};
    $('ok-item').textContent = item.itemName || '办事事项';
    $('ok-name').textContent = identity.userName || '-';
    $('ok-no').textContent = identity.userNo || '-';
    $('ok-role').textContent = identity.role || '-';
    $('ok-dept').textContent = item.deptName || '-';
    $('ok-conditions').textContent = item.conditions || '无特殊条件，按事项要求在线办理。';
    $('ok-materials').textContent = item.materials || '无，按页面提示填写即可。';
    $('ok-meta').textContent =
      `票据签发：${fmt(data.issuedAt)} · 核验时间：${fmt(data.usedAt)} · 原有效期至：${fmt(data.expiresAt)}`;
    showState('ok');
  }

  async function verify() {
    if (!ticket) {
      showError('缺少办理票据', '请从能小智对话页点击事项卡片上的「去办理」按钮进入本页。', false);
      return;
    }
    showState('loading');
    try {
      const resp = await fetch('/api/gov/tickets/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ ticket }),
        credentials: 'same-origin',
      });
      let json = null;
      try { json = await resp.json(); } catch (_) { /* ignore */ }
      if (!resp.ok || !json || !json.ok) {
        const detail = json && (json.error || json.detail);
        const title = resp.status === 410 ? '票据已过期'
          : resp.status === 409 ? '票据不可用'
            : resp.status === 404 ? '票据无效'
              : '票据核验失败';
        showError(title, failText(resp.status, detail), resp.status >= 500);
        return;
      }
      fill(json.data || {});
    } catch (_) {
      showError('网络异常', '无法连接办事服务，请检查网络后重试。', true);
    }
  }

  $('retry').onclick = verify;

  $('apply-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const phone = $('apply-phone').value.trim();
    if (phone && !/^[0-9+\-\s]{6,20}$/.test(phone)) {
      $('apply-phone').focus();
      return;
    }
    const stamp = Date.now().toString(36).toUpperCase();
    const result = $('apply-result');
    result.textContent = `申请已受理（演示）：受理编号 NXZ-${stamp}。真实环境将由一网通办返回受理结果。`;
    result.hidden = false;
    $('apply-submit').disabled = true;
    $('apply-submit').textContent = '已提交';
  });

  verify();
})();
