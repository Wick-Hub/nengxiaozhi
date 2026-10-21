(() => {
  const chatEl = document.getElementById('chat');
  const inputEl = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const roleEl = document.getElementById('role');
  const newChatBtn = document.getElementById('new-chat');

  const LS_SESSION = 'nxz.sessionId';
  const LS_ROLE = 'nxz.role';
  const SAMPLES = [
    '校园网怎么开通？',
    '一卡通丢了怎么办？',
    '研究生怎么申请奖学金？',
    '图书馆借书流程是什么？',
  ];

  let sessionId = param('session') || localStorage.getItem(LS_SESSION) || null;
  let busy = false;

  roleEl.value = localStorage.getItem(LS_ROLE) || '访客';

  function param(name) {
    const m = new URLSearchParams(location.search).get(name);
    return m || null;
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // 极简渲染：**加粗** + [n] 引用上标 + 换行
  function renderContent(text) {
    let html = esc(text);
    html = html.replace(/\[(\d{1,2})\]/g, '<sup class="cite">[$1]</sup>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\n/g, '<br>');
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

  function addWelcome() {
    const w = el('div', 'welcome');
    w.innerHTML = `
      <div class="big-logo">智</div>
      <h2>你好，我是能小智</h2>
      <p>校园事务、办事流程、政策解读，随时问我。回答会附知识库来源，方便查证。</p>`;
    const s = el('div', 'samples');
    for (const q of SAMPLES) {
      const chip = el('button', 'sample', q);
      chip.onclick = () => ask(q);
      s.appendChild(chip);
    }
    w.appendChild(s);
    chatEl.appendChild(w);
    scrollDown();
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

  function addAi(answer, meta, citations) {
    const b = msgWrap('ai');
    b.innerHTML = renderContent(answer);
    if (citations) addCitations(b, citations);
    if (meta) addMeta(b, meta);
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
  }

  async function ask(question) {
    question = (question || inputEl.value).trim();
    if (!question || busy) return;
    inputEl.value = '';
    autosize();
    setBusy(true);
    addUser(question);
    const role = roleEl.value;

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
      }
      b.classList.remove('thinking');
      if (d.answer == null) {
        b.innerHTML = renderContent('抱歉，知识库中未找到相关信息，建议联系相关部门或转人工咨询。');
        addMeta(b, { refused: true, confidence: 0.15 });
        scrollDown();
        return;
      }
      b.innerHTML = renderContent(d.answer);
      addCitations(b, d.citations);
      addMeta(b, { model: d.model, latencyMs: d.latencyMs, confidence: d.confidence, refused: d.refused });
      scrollDown();
    };

    try {
      const resp = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, role, sessionId }),
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
      const resp = await fetch(`/api/sessions/${encodeURIComponent(sid)}/messages`);
      const json = await resp.json();
      if (!json.ok) { addWelcome(); return; }
      chatEl.innerHTML = '';
      for (const m of json.data) {
        if (m.role === 'user') addUser(m.content);
        else addAi(m.content,
          { model: null, latencyMs: null, confidence: m.confidence, refused: false },
          m.citations);
      }
    } catch {
      addWelcome();
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
  roleEl.addEventListener('change', () => localStorage.setItem(LS_ROLE, roleEl.value));
  newChatBtn.onclick = () => {
    sessionId = null;
    localStorage.removeItem(LS_SESSION);
    chatEl.innerHTML = '';
    addWelcome();
  };

  // 初始化
  setBusy(false);
  if (sessionId) loadHistory(sessionId);
  else addWelcome();
})();
