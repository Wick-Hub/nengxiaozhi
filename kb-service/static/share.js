(() => {
  const shareId = decodeURIComponent(
    (location.pathname.match(/^\/share\/([^/]+)\/?$/) || [])[1] || ''
  );
  const cardEl = document.getElementById('share-card');
  const errorEl = document.getElementById('share-error');
  const errorText = document.getElementById('share-error-text');
  const questionEl = document.getElementById('share-question');
  const answerEl = document.getElementById('share-answer');
  const metaEl = document.getElementById('share-meta');
  const viewsEl = document.getElementById('share-views');
  const citationsEl = document.getElementById('share-citations');
  const citationCountEl = document.getElementById('share-citation-count');
  const citationListEl = document.getElementById('share-citation-list');
  const likeBtn = document.getElementById('share-like');
  const dislikeBtn = document.getElementById('share-dislike');
  const likesEl = document.getElementById('share-likes');
  const dislikesEl = document.getElementById('share-dislikes');
  const toastEl = document.getElementById('share-toast');
  const copyBtn = document.getElementById('share-copy');
  const saveBtn = document.getElementById('share-save');
  const nativeBtn = document.getElementById('share-native');
  const modalEl = document.getElementById('share-fb-modal');
  const reasonsEl = document.getElementById('share-fb-reasons');
  const noteEl = document.getElementById('share-fb-note');
  const fbMsgEl = document.getElementById('share-fb-msg');
  const fbSubmit = document.getElementById('share-fb-submit');
  const fbCancel = document.getElementById('share-fb-cancel');

  const LS_RATING = 'nxz.share.rating.' + shareId;
  const FALLBACK_REASONS = [
    { code: 'inaccurate', label: '内容不准确' },
    { code: 'incomplete', label: '回答不完整' },
    { code: 'irrelevant', label: '答非所问' },
    { code: 'other', label: '其他原因' },
  ];
  const KTYPES = { faq: 'FAQ', doc: '文档', knowledge: '知识点', qa: '问答' };

  const state = {
    data: null,
    reasons: FALLBACK_REASONS.slice(),
    likes: 0,
    dislikes: 0,
    chosen: null,
    pending: false,
    reasonCode: '',
  };

  let toastTimer = null;
  function toast(text) {
    if (!toastEl) return;
    toastEl.textContent = text;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2400);
  }

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function showError(text) {
    cardEl.hidden = true;
    errorEl.hidden = false;
    errorText.textContent = text || '分享不存在或已失效。';
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch { /* 回退到 execCommand */ }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }

  function formatSimilarity(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    return '相似度 ' + Math.round(num <= 1 ? num * 100 : num) + '%';
  }

  function citationText(item) {
    const parts = [];
    if (item.title) parts.push(item.title);
    if (item.dept) parts.push(item.dept);
    const sim = formatSimilarity(item.similarity);
    if (sim) parts.push(sim);
    return parts.join(' · ');
  }

  function buildCopyText() {
    const d = state.data;
    const lines = ['【能小智】' + d.question, '', d.answer.trim()];
    if (d.citations && d.citations.length) {
      lines.push('', '引用来源：');
      d.citations.forEach((c, i) => {
        const text = citationText(c);
        if (text) lines.push(`${i + 1}. ${text}`);
      });
    }
    lines.push('', '本内容由 AI 生成，仅供参考，请以学校最新通知为准。', location.href);
    return lines.join('\n');
  }

  function buildMarkdown() {
    const d = state.data;
    const lines = ['# ' + d.question, ''];
    const source = d.agentName ? '能小智 · ' + d.agentName : '能小智';
    lines.push('> ' + source + ' 生成 · 内容由 AI 生成，仅供参考。', '');
    lines.push(d.answer.trim(), '');
    if (d.citations && d.citations.length) {
      lines.push('## 引用来源', '');
      d.citations.forEach((c, i) => {
        const text = citationText(c);
        if (text) lines.push(`${i + 1}. ${text}`);
      });
      lines.push('');
    }
    lines.push('---', '[查看原分享](' + location.href + ')');
    return lines.join('\n');
  }

  function saveMarkdown() {
    const blob = new Blob([buildMarkdown()], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '能小智问答-' + (state.data.shareId || shareId) + '.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast('已保存 Markdown 文件');
  }

  function renderCitations(items) {
    citationListEl.innerHTML = '';
    if (!items || !items.length) {
      citationsEl.hidden = true;
      return;
    }
    for (const item of items) {
      const row = document.createElement('div');
      row.className = 'citation-item';
      const type = KTYPES[item.ktype] || item.ktype || '';
      const sim = formatSimilarity(item.similarity);
      row.innerHTML =
        '<b>' + esc(item.n) + '.</b>' +
        '<span>' + esc(item.title || '未命名来源') + '</span>' +
        (item.dept ? '<span>' + esc(item.dept) + '</span>' : '') +
        (type ? '<span class="sim">' + esc(type) + '</span>' : '') +
        (sim ? '<span class="sim">' + esc(sim) + '</span>' : '');
      citationListEl.appendChild(row);
    }
    citationCountEl.textContent = '(' + items.length + ')';
    citationsEl.hidden = false;
  }

  function renderMeta(d) {
    metaEl.innerHTML = '';
    const chips = [];
    if (d.agentName) chips.push('助手 · ' + d.agentName);
    if (d.role) chips.push('知识范围 · ' + d.role);
    if (d.createdAt) chips.push('生成于 ' + String(d.createdAt).replace('T', ' '));
    for (const text of chips) {
      const span = document.createElement('span');
      span.textContent = text;
      metaEl.appendChild(span);
    }
    viewsEl.textContent = d.views ? '浏览 ' + d.views + ' 次' : '';
  }

  function paintCounts() {
    likesEl.textContent = String(state.likes);
    dislikesEl.textContent = String(state.dislikes);
    likeBtn.classList.toggle('active', state.chosen === 1);
    dislikeBtn.classList.toggle('active', state.chosen === -1);
    likeBtn.setAttribute('aria-pressed', state.chosen === 1 ? 'true' : 'false');
    dislikeBtn.setAttribute('aria-pressed', state.chosen === -1 ? 'true' : 'false');
  }

  function restoreRating() {
    try {
      const raw = Number(localStorage.getItem(LS_RATING));
      if (raw === 1 || raw === -1) state.chosen = raw;
    } catch { /* ignore */ }
  }

  function storeRating(rating) {
    try { localStorage.setItem(LS_RATING, String(rating)); } catch { /* ignore */ }
  }

  function applyLocalRating(rating) {
    const prev = state.chosen;
    if (prev === rating) return;
    if (prev === 1) state.likes = Math.max(0, state.likes - 1);
    if (prev === -1) state.dislikes = Math.max(0, state.dislikes - 1);
    if (rating === 1) state.likes += 1;
    if (rating === -1) state.dislikes += 1;
    state.chosen = rating;
    storeRating(rating);
    paintCounts();
  }

  function setPending(value) {
    state.pending = value;
    likeBtn.disabled = value;
    dislikeBtn.disabled = value;
    fbSubmit.disabled = value;
  }

  async function sendFeedback(rating, extra) {
    const payload = Object.assign(
      { rating: rating, shareId: shareId },
      extra || {}
    );
    const resp = await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    let body = null;
    try { body = await resp.json(); } catch { /* 非 JSON 响应 */ }
    if (!resp.ok || !body || body.ok === false) {
      const detail = body && (body.detail || body.message);
      throw new Error(detail || '提交失败，请稍后重试');
    }
    return body.data;
  }

  async function handleLike() {
    if (state.pending || !state.data) return;
    setPending(true);
    try {
      await sendFeedback(1);
      applyLocalRating(1);
      toast(state.chosen === 1 ? '感谢反馈！' : '已记录你的反馈');
    } catch (err) {
      toast(err.message || '提交失败，请稍后重试');
    } finally {
      setPending(false);
    }
  }

  function openModal() {
    if (state.pending || !state.data) return;
    state.reasonCode = '';
    noteEl.value = '';
    fbMsgEl.hidden = true;
    for (const chip of reasonsEl.querySelectorAll('.fb-reason')) {
      chip.classList.remove('active');
      chip.setAttribute('aria-pressed', 'false');
    }
    modalEl.hidden = false;
    const first = reasonsEl.querySelector('.fb-reason');
    if (first) first.focus();
  }

  function closeModal() {
    modalEl.hidden = true;
    state.reasonCode = '';
  }

  function buildReasons(items) {
    reasonsEl.innerHTML = '';
    for (const item of items) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'fb-reason';
      btn.dataset.code = item.code;
      btn.textContent = item.label;
      btn.setAttribute('aria-pressed', 'false');
      btn.addEventListener('click', () => {
        state.reasonCode = item.code;
        fbMsgEl.hidden = true;
        for (const chip of reasonsEl.querySelectorAll('.fb-reason')) {
          const active = chip === btn;
          chip.classList.toggle('active', active);
          chip.setAttribute('aria-pressed', active ? 'true' : 'false');
        }
      });
      reasonsEl.appendChild(btn);
    }
  }

  async function submitDislike() {
    if (state.pending) return;
    if (!state.reasonCode) {
      fbMsgEl.textContent = '请选择不满意原因';
      fbMsgEl.hidden = false;
      return;
    }
    fbMsgEl.hidden = true;
    setPending(true);
    try {
      await sendFeedback(-1, {
        reasonCode: state.reasonCode,
        reason: noteEl.value.trim(),
      });
      applyLocalRating(-1);
      closeModal();
      toast('已记录，我们会尽快改进');
    } catch (err) {
      fbMsgEl.textContent = err.message || '提交失败，请稍后重试';
      fbMsgEl.hidden = false;
    } finally {
      setPending(false);
    }
  }

  async function shareLink() {
    const d = state.data;
    if (!d) return;
    const title = '能小智问答：' + d.question;
    if (navigator.share) {
      try {
        await navigator.share({ title: title, text: d.question, url: location.href });
        return;
      } catch (err) {
        if (err && err.name === 'AbortError') return;
      }
    }
    const ok = await copyText(location.href);
    toast(ok ? '分享链接已复制' : '复制失败，请手动复制地址栏');
  }

  async function loadReasons() {
    try {
      const resp = await fetch('/api/feedback/reasons');
      const body = await resp.json();
      if (resp.ok && body && body.ok && Array.isArray(body.data) && body.data.length) {
        state.reasons = body.data;
      }
    } catch { /* 使用内置字典 */ }
    buildReasons(state.reasons);
  }

  async function load() {
    if (!shareId) {
      showError('分享地址不完整。');
      return;
    }
    restoreRating();
    await loadReasons();
    try {
      const resp = await fetch('/api/share/' + encodeURIComponent(shareId));
      let body = null;
      try { body = await resp.json(); } catch { /* ignore */ }
      if (resp.status === 404) {
        showError('分享不存在或已失效。');
        return;
      }
      if (!resp.ok || !body || body.ok === false || !body.data) {
        const detail = body && (body.detail || body.message);
        throw new Error(detail || '分享加载失败');
      }
      const d = body.data;
      state.data = d;
      state.likes = Number((d.feedback && d.feedback.likes) || 0);
      state.dislikes = Number((d.feedback && d.feedback.dislikes) || 0);
      document.title = d.question ? d.question + ' · 能小智问答分享' : '问答分享 · 能小智';
      questionEl.textContent = d.question || '';
      answerEl.innerHTML = d.answerHtml || '';
      renderMeta(d);
      renderCitations(d.citations);
      paintCounts();
      cardEl.hidden = false;
    } catch (err) {
      showError(err.message || '分享加载失败，请稍后重试。');
    }
  }

  likeBtn.addEventListener('click', handleLike);
  dislikeBtn.addEventListener('click', openModal);
  copyBtn.addEventListener('click', async () => {
    if (!state.data) return;
    const ok = await copyText(buildCopyText());
    toast(ok ? '内容已复制' : '复制失败，请手动选择文本');
  });
  saveBtn.addEventListener('click', () => { if (state.data) saveMarkdown(); });
  nativeBtn.addEventListener('click', shareLink);
  fbSubmit.addEventListener('click', submitDislike);
  fbCancel.addEventListener('click', closeModal);
  modalEl.addEventListener('click', (ev) => {
    if (ev.target === modalEl) closeModal();
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !modalEl.hidden) closeModal();
  });

  load();
})();
