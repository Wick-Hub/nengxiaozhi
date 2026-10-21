(() => {
  const tbody = document.getElementById('tbody');
  const emptyEl = document.getElementById('empty');
  const countEl = document.getElementById('count');
  const filtersEl = document.getElementById('filters');
  const fileEl = document.getElementById('file');
  const titleEl = document.getElementById('title');
  const deptEl = document.getElementById('dept');
  const deptListEl = document.getElementById('dept-list');
  const ktypeEl = document.getElementById('ktype');
  const statusEl = document.getElementById('status');
  const rolesEl = document.getElementById('roles');
  const uploadBtn = document.getElementById('upload');
  const uploadMsg = document.getElementById('upload-msg');

  const STATUS_CLS = ['s0', 's1', 's2', 's3'];
  let docs = [];
  let filter = 'all';

  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function fmtSize(n) {
    if (n == null) return '-';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }
  function fmtTime(s) {
    if (!s) return '-';
    const d = new Date(s);
    if (isNaN(d)) return s;
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  async function api(url, opts) {
    const resp = await fetch(url, opts);
    const json = await resp.json();
    if (!json.ok) throw new Error(json.error || '请求失败');
    return json.data;
  }

  function renderFilters() {
    const tabs = [
      ['all', '全部'], ['2', '已发布'], ['1', '待审核'], ['0', '草稿'], ['3', '已下线'],
    ];
    filtersEl.innerHTML = '';
    for (const [v, label] of tabs) {
      const b = document.createElement('button');
      b.className = 'f' + (filter === v ? ' active' : '');
      b.textContent = label;
      b.onclick = () => { filter = v; renderFilters(); render(); };
      filtersEl.appendChild(b);
    }
  }

  function render() {
    const list = filter === 'all' ? docs : docs.filter(d => String(d.status) === filter);
    countEl.textContent = `共 ${docs.length} 篇`;
    emptyEl.hidden = list.length > 0;
    tbody.innerHTML = '';
    for (const d of list) {
      const tr = document.createElement('tr');
      const ops = [];
      if (d.status !== 2) ops.push(`<button class="op-btn" data-op="publish">发布</button>`);
      if (d.status === 2) ops.push(`<button class="op-btn" data-op="offline">下线</button>`);
      ops.push(`<button class="op-btn danger" data-op="delete">删除</button>`);

      tr.innerHTML = `
        <td>${d.docId}</td>
        <td>
          <div class="doc-title" title="${esc(d.title)}">${esc(d.title)}</div>
          <div class="doc-meta">${esc(d.fileKey)} · ${esc(d.fileFormat || '-')}</div>
        </td>
        <td>${esc(d.dept)}</td>
        <td>${esc(d.ktype)}</td>
        <td><span class="badge ${STATUS_CLS[d.status] || 's0'}">${esc(d.statusName)}</span></td>
        <td>${d.chunkCount}</td>
        <td>${fmtSize(d.fileSize)}</td>
        <td>${fmtTime(d.updatedAt || d.publishedAt)}</td>
        <td>${ops.join('')}</td>`;
      tr.querySelectorAll('[data-op]').forEach(btn => {
        btn.onclick = () => handleOp(btn.dataset.op, d);
      });
      tbody.appendChild(tr);
    }
  }

  async function handleOp(op, d) {
    if (op === 'delete' && !confirm(`确认删除「${d.title}」？其切片与向量将一并删除，且不可恢复。`)) return;
    const status = op === 'publish' ? 2 : op === 'offline' ? 3 : null;
    try {
      if (op === 'delete') {
        await api(`/api/docs/${d.docId}`, { method: 'DELETE' });
      } else {
        await api(`/api/docs/${d.docId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status }),
        });
      }
      await load();
    } catch (e) {
      alert(e.message);
    }
  }

  async function load() {
    try {
      docs = await api('/api/docs');
      render();
    } catch (e) {
      emptyEl.hidden = false;
      emptyEl.textContent = '加载失败：' + e.message;
    }
  }

  async function loadMeta() {
    try {
      const meta = await api('/api/meta');
      deptListEl.innerHTML = meta.depts.map(d => `<option value="${esc(d)}">`).join('');
      ktypeEl.innerHTML = meta.ktypes.map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
      rolesEl.innerHTML = meta.roles.map((r, i) =>
        `<label><input type="checkbox" value="${esc(r)}" ${i === 0 ? 'checked' : ''}> ${esc(r)}</label>`
      ).join('');
    } catch (e) {
      uploadMsg.textContent = '元数据加载失败：' + e.message;
      uploadMsg.className = 'msg-line err';
    }
  }

  async function upload() {
    const file = fileEl.files[0];
    if (!file) { uploadMsg.textContent = '请先选择文件'; uploadMsg.className = 'msg-line err'; return; }
    const dept = deptEl.value.trim();
    const ktype = ktypeEl.value;
    const roles = [...rolesEl.querySelectorAll('input:checked')].map(c => c.value);
    if (!dept) { uploadMsg.textContent = '请填写部门'; uploadMsg.className = 'msg-line err'; return; }

    uploadBtn.disabled = true;
    uploadMsg.textContent = '解析中…（大文件需下载解析依赖，请稍候）';
    uploadMsg.className = 'msg-line';
    try {
      const data = await file.arrayBuffer();
      const base64 = btoa(String.fromCharCode(...new Uint8Array(data)));
      const r = await api('/api/docs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: file.name,
          data: base64,
          dept, ktype,
          status: +statusEl.value,
          visibleRoles: roles,
          title: titleEl.value.trim(),
        }),
      });
      uploadMsg.textContent = `✅ 已入库：${r.title}（${r.chunkCount} 个切片，doc_id=${r.docId}）`;
      uploadMsg.className = 'msg-line ok';
      fileEl.value = '';
      titleEl.value = '';
      await load();
    } catch (e) {
      uploadMsg.textContent = '上传失败：' + e.message;
      uploadMsg.className = 'msg-line err';
    } finally {
      uploadBtn.disabled = false;
    }
  }

  uploadBtn.onclick = upload;

  const syncBtn = document.getElementById('sync');
  async function sync() {
    syncBtn.disabled = true;
    syncBtn.textContent = '同步中…';
    try {
      const r = await api('/api/sync', { method: 'POST' });
      const line = `新增 ${r.added} · 更新 ${r.updated} · 删除 ${r.deleted} · 跳过 ${r.skipped}`;
      if (r.errors && r.errors.length) {
        alert('同步完成：' + line + '\n错误：' + r.errors.join('\n'));
      } else {
        alert('同步完成：' + line);
      }
      await load();
    } catch (e) {
      alert('同步失败：' + e.message);
    } finally {
      syncBtn.disabled = false;
      syncBtn.textContent = '增量同步';
    }
  }
  syncBtn.onclick = sync;

  renderFilters();
  load();
  loadMeta();
})();
