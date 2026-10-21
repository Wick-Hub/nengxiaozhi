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
  // 任务 2.7：执行中的类型 / 分类筛选（空 = 不过滤）；选择集合（批量操作）
  let fKtype = '';
  let fCategory = '';
  const selected = new Set();

  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function renderStats() {
    const counts = { '2': 'stat-pub', '1': 'stat-pending', '0': 'stat-draft', '3': 'stat-offline' };
    const el = Object.fromEntries(Object.entries(counts).map(([k, id]) => [k, document.getElementById(id)]));
    for (const [status, node] of Object.entries(el)) {
      if (!node) continue;
      const n = docs.filter(d => String(d.status) === status).length;
      node.textContent = n;
    }
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

  function authFetch(url, opts) {
    return (window.NXZAuth ? window.NXZAuth.fetch : fetch)(url, opts);
  }

  async function api(url, opts) {
    const resp = await authFetch(url, opts);
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
      b.onclick = () => { filter = v; renderFilters(); load(); };
      filtersEl.appendChild(b);
    }
  }

  function render() {
    const list = filter === 'all' ? docs : docs.filter(d => String(d.status) === filter);
    countEl.textContent = `共 ${docs.length} 篇`;
    emptyEl.hidden = list.length > 0;
    // 统计概览（视觉增强，任务 1.9）：upstream 不变，仅同步计数
    renderStats();
    tbody.innerHTML = '';
    for (const d of list) {
      const tr = document.createElement('tr');
      const ops = [];
      // 预览入口（任务 1.11）：跳转 /preview 对比原文与切片
      ops.push(`<a class="op-btn" data-perm="docs.view" href="/preview?doc=${encodeURIComponent(d.docId)}">预览</a>`);
      if (d.status !== 2) ops.push(`<button class="op-btn" data-perm="docs.audit" data-op="publish">发布</button>`);
      if (d.status === 2) ops.push(`<button class="op-btn" data-perm="docs.audit" data-op="offline">下线</button>`);
      ops.push(`<button class="op-btn danger" data-perm="docs.delete" data-op="delete">删除</button>`);

      tr.innerHTML = `
        <td class="chk"><input type="checkbox" data-chk=${d.docId} ${selected.has(String(d.docId)) ? 'checked' : ''}></td>
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
      const chk = tr.querySelector('[data-chk]');
      if (chk) chk.onchange = () => {
        const id = String(d.docId);
        if (chk.checked) selected.add(id); else selected.delete(id);
        syncBatchBar();
      };
      tbody.appendChild(tr);
    }
    syncBatchBar();
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
      // 任务 2.7：类型/分类筛选下推到服务端（MySQL 模式）；JSON 模式内存过滤
      const qs = new URLSearchParams();
      if (filter !== 'all') qs.set('status', filter);
      if (fKtype) qs.set('ktype', fKtype);
      if (fCategory) qs.set('categoryId', fCategory);
      const path = '/api/docs' + (qs.toString() ? `?${qs}` : '');
      docs = await api(path);
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

  // ---- 文档筛选与批量操作（任务 2.7 / M-K-05·09）----
  // 类型/分类下拉 + 批量审核（通过/驳回）/删除/移动。
  // 批量操作依赖 MySQL：JSON 模式点击时由服务端返回 400 + 指引。
  const fKtypeEl = document.getElementById('f-ktype');
  const fCategoryEl = document.getElementById('f-category');
  const batchBar = document.getElementById('batch-bar');
  const batchCount = document.getElementById('batch-count');
  const batchTargetCat = document.getElementById('batch-target-cat');
  const batchMsg = document.getElementById('batch-msg');
  const chkAll = document.getElementById('chk-all');

  function syncBatchBar() {
    batchCount.textContent = selected.size;
    batchBar.hidden = selected.size === 0;
    chkAll.checked = docs.length > 0 && docs.every(d => selected.has(String(d.docId)));
  }

  function selectedDocIds() {
    return [...selected];
  }

  async function loadFilters() {
    try {
      // 类型下拉来自 /api/meta；分类下拉来自 /api/admin/kb-categories（含全部部门）
      const meta = await api('/api/meta');
      fKtypeEl.innerHTML =
        `<option value="">全部类型</option>` +
        meta.ktypes.map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
      let cats = [];
      try {
        if (!window.NXZAuth || window.NXZAuth.can('categories.view')) {
          const c = await api('/api/admin/kb-categories');
          cats = c.items || [];
        }
      } catch (e) { /* 分类面板不可用时忽略 */ }
      fCategoryEl.innerHTML =
        `<option value="">全部分类</option>` +
        cats.map(x => `<option value="${x.categoryId}">${esc(x.categoryName)}（${esc(x.dept)}）</option>`).join('');
      batchTargetCat.innerHTML =
        `<option value="">选择目标分类…</option>` +
        cats.map(x => `<option value="${x.categoryId}">${esc(x.categoryName)}（${esc(x.dept)}）</option>`).join('');
    } catch (e) {
      fKtypeEl.innerHTML = `<option value="">全部类型</option>`;
    }
  }

  fKtypeEl.onchange = () => { fKtype = fKtypeEl.value; load(); };
  fCategoryEl.onchange = () => { fCategory = fCategoryEl.value; load(); };

  chkAll.onchange = () => {
    if (chkAll.checked) {
      for (const d of docs) selected.add(String(d.docId));
    } else {
      for (const d of docs) selected.delete(String(d.docId));
    }
    render(); syncBatchBar();
  };

  async function runBatch(action) {
    const ids = selectedDocIds();
    if (!ids.length) return;
    if (action === 'delete' && !confirm(`确认删除选中的 ${ids.length} 篇文档？其切片/向量将一并删除，不可恢复。`)) return;
    let body = { docIds: ids, action };
    if (action === 'move') {
      const cat = batchTargetCat.value;
      if (!cat) { batchMsg.textContent = '请先选择目标分类'; batchMsg.className = 'msg-line err'; return; }
      body = { docIds: ids, action, categoryId: +cat };
    }
    batchMsg.textContent = '提交中…';
    batchMsg.className = 'msg-line';
    try {
      const r = await api('/api/docs/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const n = r.updated != null ? r.updated : (r.deleted != null ? r.deleted : ids.length);
      batchMsg.textContent = `✅ 已完成 ${n} 篇（${action}）`;
      batchMsg.className = 'msg-line ok';
      selected.clear();
      await load(); syncBatchBar();
    } catch (e) {
      batchMsg.textContent = '批量操作失败：' + e.message;
      batchMsg.className = 'msg-line err';
    }
  }

  const batchButtons = [
    ['batch-publish', 'publish'],
    ['batch-reject', 'reject'],
    ['batch-move', 'move'],
    ['batch-delete', 'delete'],
    ['batch-clear', null],
  ];
  for (const [id, action] of batchButtons) {
    const b = document.getElementById(id);
    if (!b) continue;
    b.onclick = () => {
      if (action === null) { selected.clear(); render(); syncBatchBar(); return; }
      runBatch(action);
    };
  }

  // ---- 知识分类管理（任务 2.5 / M-K-01·02）----
  // 分类 = 「部门 × 知识类型」。MySQL 模式为真实表记录（可增删改），
  // JSON 模式为索引只读推导（editable=false），此分类面板据此决定是否显示编辑.
  const catNote = document.getElementById('cat-note');
  const catForm = document.getElementById('cat-form');
  const catDept = document.getElementById('cat-dept');
  const catKtype = document.getElementById('cat-ktype');
  const catName = document.getElementById('cat-name');
  const catSort = document.getElementById('cat-sort');
  const catNewBtn = document.getElementById('cat-new-btn');
  const catMsg = document.getElementById('cat-msg');
  const catTree = document.getElementById('cat-tree');
  const catEmpty = document.getElementById('cat-empty');

  function renderCats(items, editable) {
    catEmpty.hidden = items.length > 0;
    catTree.innerHTML = '';
    // 按部门分组渲染（每部门一个折叠区）
    const byDept = {};
    for (const c of items) (byDept[c.dept] = byDept[c.dept] || []).push(c);
    const depts = Object.keys(byDept);
    for (const dept of depts) {
      const grp = document.createElement('div');
      grp.className = 'cat-group';
      const head = document.createElement('div');
      head.className = 'cat-dept';
      head.textContent = dept;
      grp.appendChild(head);
      const list = document.createElement('div');
      list.className = 'cat-list';
      for (const c of byDept[dept]) {
        const row = document.createElement('div');
        row.className = 'cat-row';
        const info = document.createElement('span');
        info.className = 'cat-info';
        info.innerHTML =
          `<span class="cat-name" title="${esc(c.categoryName)}">${esc(c.categoryName)}</span>` +
          ` <span class="cat-count">${c.ktype} · ${c.docCount} 篇</span>`;
        row.appendChild(info);
        const ops = document.createElement('span');
        ops.className = 'cat-ops';
        if (editable) {
          ops.appendChild(catBtnBtn('重命名', () => renameCat(c.categoryId, c.categoryName)));
          ops.appendChild(catBtnBtn('删除', () => deleteCat(c.categoryId, c.categoryName), true));
        }
        row.appendChild(ops);
        list.appendChild(row);
      }
      grp.appendChild(list);
      catTree.appendChild(grp);
    }
  }

  function catBtnBtn(label, fn, danger) {
    const b = document.createElement('button');
    b.className = 'op-btn' + (danger ? ' danger' : '');
    b.dataset.perm = 'categories.manage';
    b.textContent = label;
    b.onclick = fn;
    return b;
  }

  async function loadCats() {
    try {
      const data = await api('/api/admin/kb-categories');
      renderCats(data.items || [], !!data.editable);
      const editable = !!data.editable;
      catNote.textContent = editable
        ? ''
        : '当前为只读模式（JSON 存储），分类由本地索引自动推导，仅可查看，不可增删改。';
      catForm.hidden = !editable;
      // 回填下拉（分类管理表单用「部门 × 知识类型」）
      catDept.innerHTML = (data.depts || []).map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
      catKtype.innerHTML = (data.ktypes || []).map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
    } catch (e) {
      catEmpty.hidden = false;
      catEmpty.textContent = '分类加载失败：' + e.message;
    }
  }

  async function createCat() {
    const dept = catDept.value;
    const ktype = catKtype.value;
    if (!dept) { catMsg.textContent = '请先选择部门'; catMsg.className = 'msg-line err'; return; }
    if (!ktype) { catMsg.textContent = '请先选择知识类型'; catMsg.className = 'msg-line err'; return; }
    catNewBtn.disabled = true;
    catMsg.textContent = '创建中…';
    catMsg.className = 'msg-line';
    try {
      const r = await api('/api/admin/kb-categories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          dept, ktype,
          categoryName: catName.value.trim() || undefined,
          sortNo: +catSort.value || 0,
        }),
      });
      catMsg.textContent = `✅ 已创建分类：${r.categoryName}`;
      catMsg.className = 'msg-line ok';
      catName.value = '';
      await loadCats();
    } catch (e) {
      catMsg.textContent = '创建失败：' + e.message;
      catMsg.className = 'msg-line err';
    } finally {
      catNewBtn.disabled = false;
    }
  }

  async function renameCat(id, current) {
    const name = window.prompt(`重命名分类（当前：「${current}」）`, current);
    if (name == null || !name.trim()) return;
    try {
      await api(`/api/admin/kb-categories/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ categoryName: name.trim(), sortNo: 0 }),
      });
      await loadCats();
    } catch (e) {
      alert('重命名失败：' + e.message);
    }
  }

  async function deleteCat(id, current) {
    if (!window.confirm(`确认删除分类「${current}」？该分类下有文档时将无法删除。`)) return;
    try {
      await api(`/api/admin/kb-categories/${id}`, { method: 'DELETE' });
      await loadCats();
    } catch (e) {
      alert('删除失败：' + e.message);
    }
  }

  if (catNewBtn) catNewBtn.onclick = createCat;

  // ---- 知识库部门授权（任务 2.6 / M-K-03）----
  // 超管为部门联络员按「用户 × 部门 × 知识类型」勾选 5 个操作位；
  // JSON 模式为只读（editable=false），仅展示授权矩阵，隐藏授予表单。
  const permNote = document.getElementById('perm-note');
  const permForm = document.getElementById('perm-form');
  const permUser = document.getElementById('perm-user');
  const permDept = document.getElementById('perm-dept');
  const permKtype = document.getElementById('perm-ktype');
  const permGrantBtn = document.getElementById('perm-grant-btn');
  const permMsg = document.getElementById('perm-msg');
  const permList = document.getElementById('perm-list');
  const permEmpty = document.getElementById('perm-empty');
  const PERM_FLAGS = [
    ['perm-can-upload', 'canUpload', '上传'],
    ['perm-can-view', 'canView', '查看'],
    ['perm-can-download', 'canDownload', '下载'],
    ['perm-can-delete', 'canDelete', '删除'],
    ['perm-can-create-sub', 'canCreateSub', '建子类'],
  ];

  function permChecked() {
    const out = {};
    for (const [id, key] of PERM_FLAGS) out[key] = document.getElementById(id).checked ? 1 : 0;
    return out;
  }

  function renderPerm(items, editable) {
    permEmpty.hidden = items.length > 0;
    permList.innerHTML = '';
    for (const it of items) {
      const row = document.createElement('div');
      row.className = 'perm-row';
      const info = document.createElement('div');
      info.className = 'perm-info';
      info.innerHTML =
        `<b>${esc(it.realName || it.userNo)}</b>` +
        `<span class="muted">（${esc(it.userNo)}）</span>` +
        `<span class="perm-dept">${esc(it.deptId ? it.dept : '全部部门')} · ${esc(it.ktype === '*' ? '全部类型' : it.ktype)}</span>`;
      row.appendChild(info);
      const badges = document.createElement('div');
      badges.className = 'perm-flags';
      for (const [id, key, label] of PERM_FLAGS) {
        const b = document.createElement('span');
        b.className = 'perm-flag' + (it[key] ? ' on' : '');
        b.textContent = label;
        badges.appendChild(b);
      }
      row.appendChild(badges);
      const ops = document.createElement('span');
      ops.className = 'cat-ops';
      if (editable) {
        const del = document.createElement('button');
        del.className = 'op-btn danger';
        del.dataset.perm = 'deptperm.manage';
        del.textContent = '撤销';
        del.onclick = () => revokePerm(it.permId, it.realName || it.userNo);
        ops.appendChild(del);
      }
      row.appendChild(ops);
      permList.appendChild(row);
    }
  }

  async function loadPerms() {
    try {
      const data = await api('/api/admin/kb-permissions');
      renderPerm(data.items || [], !!data.editable);
      const editable = !!data.editable;
      permNote.textContent = editable
        ? ''
        : '当前为只读模式（JSON 存储），授权由 MySQL 表承载，此处仅作展示。';
      permForm.hidden = !editable;
      // 回填下拉
      permUser.innerHTML = (data.liaisons || []).map(
        u => `<option value="${u.userId}">${esc(u.realName || u.userNo)}（${esc(u.userNo)}）</option>`
      ).join('');
      permDept.innerHTML = `<option value="0">全部部门</option>` +
        (data.depts || []).map(d => `<option value="${d.deptId}">${esc(d.dept)}</option>`).join('');
      permKtype.innerHTML = `<option value="*">全部类型</option>` +
        (data.ktypes || []).map(k => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
    } catch (e) {
      permEmpty.hidden = false;
      permEmpty.textContent = '授权加载失败：' + e.message;
    }
  }

  async function grantPerm() {
    const userId = permUser.value;
    if (!userId) { permMsg.textContent = '请先选择被授权用户'; permMsg.className = 'msg-line err'; return; }
    const flags = permChecked();
    if (!Object.values(flags).some(Boolean)) {
      permMsg.textContent = '请至少勾选一个操作位';
      permMsg.className = 'msg-line err';
      return;
    }
    permGrantBtn.disabled = true;
    permMsg.textContent = '授权中…';
    permMsg.className = 'msg-line';
    try {
      await api('/api/admin/kb-permissions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          userId: +userId,
          deptId: +permDept.value,
          ktype: permKtype.value,
          ...flags,
        }),
      });
      permMsg.textContent = '✅ 已授予';
      permMsg.className = 'msg-line ok';
      await loadPerms();
    } catch (e) {
      permMsg.textContent = '授予失败：' + e.message;
      permMsg.className = 'msg-line err';
    } finally {
      permGrantBtn.disabled = false;
    }
  }

  async function revokePerm(permId, label) {
    if (!window.confirm(`确认撤销「${label}」的这条知识授权？`)) return;
    try {
      await api(`/api/admin/kb-permissions/${permId}`, { method: 'DELETE' });
      await loadPerms();
    } catch (e) {
      alert('撤销失败：' + e.message);
    }
  }

  if (permGrantBtn) permGrantBtn.onclick = grantPerm;

  // ---- 系统管理：敏感词/关键词库 + 操作日志（任务 2.10 / M-SYS-07·08）----
  // 词库改动即时生效：服务端 CRUD 成功后失效词表缓存，下一问按新词表检测。
  // JSON 模式没有词库表，页面展示实际生效的内置/文件词表（只读）。
  const SW_PAGE_SIZE = 10;
  const OP_PAGE_SIZE = 20;
  const swState = { page: 1, kind: '', status: '', q: '' };
  const opState = { page: 1, userId: '', action: '', from: '', to: '' };
  const swKindEl = document.getElementById('sw-kind');
  const swStatusEl = document.getElementById('sw-status');
  const swQEl = document.getElementById('sw-q');
  const swCountEl = document.getElementById('sw-count');
  const swNoteEl = document.getElementById('sw-note');
  const swFormEl = document.getElementById('sw-form');
  const swWordEl = document.getElementById('sw-word');
  const swFormKindEl = document.getElementById('sw-form-kind');
  const swCategoryEl = document.getElementById('sw-category');
  const swRemarkEl = document.getElementById('sw-remark');
  const swAddBtn = document.getElementById('sw-add');
  const swMsgEl = document.getElementById('sw-msg');
  const swTbody = document.getElementById('sw-tbody');
  const swEmpty = document.getElementById('sw-empty');
  const swPager = document.getElementById('sw-pager');

  const opUserEl = document.getElementById('op-user');
  const opActionEl = document.getElementById('op-action');
  const opFromEl = document.getElementById('op-from');
  const opToEl = document.getElementById('op-to');
  const opCountEl = document.getElementById('op-count');
  const opNoteEl = document.getElementById('op-note');
  const opTbody = document.getElementById('op-tbody');
  const opEmpty = document.getElementById('op-empty');
  const opPager = document.getElementById('op-pager');

  function renderSimplePager(box, page, total, pageSize, go) {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    box.innerHTML =
      `<button class="btn-ghost" data-go="prev" ${page <= 1 ? 'disabled' : ''}>← 上一页</button>` +
      `<span class="cl-page-info">第 ${page} / ${pages} 页</span>` +
      `<button class="btn-ghost" data-go="next" ${page >= pages ? 'disabled' : ''}>下一页 →</button>`;
    box.querySelectorAll('[data-go]').forEach(b => {
      b.onclick = () => go(b.dataset.go === 'prev' ? Math.max(1, page - 1) : Math.min(pages, page + 1));
    });
  }

  function renderWords(data) {
    const editable = !!data.editable;
    const stats = data.stats || {};
    const sensitive = stats.sensitive || {};
    const keyword = stats.keyword || {};
    swCountEl.textContent = `共 ${data.total} 条 · 敏感词启用 ${sensitive.enabled || 0} / 停用 ${sensitive.disabled || 0}` +
      ` · 关键词启用 ${keyword.enabled || 0}`;
    swNoteEl.textContent = editable
      ? '词条改动即时生效：新增/启用后下一问即命中，停用/删除后立即失效。关键词暂不参与风控检测。'
      : `当前为只读模式（JSON 存储），展示实际生效的词表（来源：${data.source || '内置'}）；维护词库需 MySQL 存储。`;
    swFormEl.hidden = !editable;

    const items = data.items || [];
    swEmpty.hidden = items.length > 0;
    swEmpty.textContent = '暂无词条';
    swTbody.innerHTML = '';
    for (const it of items) {
      const tr = document.createElement('tr');
      const kindLabel = it.kind === 'keyword' ? '关键词' : '敏感词';
      const statusLabel = it.status === 1 ? '启用' : '停用';
      const ops = [];
      if (editable) {
        ops.push(`<button class="op-btn" data-perm="system.words" data-op="toggle">${it.status === 1 ? '停用' : '启用'}</button>`);
        ops.push(`<button class="op-btn" data-perm="system.words" data-op="rename">编辑</button>`);
        ops.push(`<button class="op-btn danger" data-perm="system.words" data-op="delete">删除</button>`);
      }
      tr.innerHTML = `
        <td><b>${esc(it.word)}</b>${it.builtin ? ' <span class="muted">内置</span>' : ''}</td>
        <td>${kindLabel}</td>
        <td>${esc(it.category)}</td>
        <td>${statusLabel}</td>
        <td class="cl-q" title="${esc(it.remark)}">${esc(it.remark || '—')}</td>
        <td>${fmtTime(it.updatedAt || it.createdAt)}</td>
        <td>${ops.join('')}</td>`;
      tr.querySelectorAll('[data-op]').forEach(btn => {
        btn.onclick = () => handleWordOp(btn.dataset.op, it);
      });
      swTbody.appendChild(tr);
    }
    renderSimplePager(swPager, data.page || 1, data.total || 0, data.pageSize || SW_PAGE_SIZE, p => {
      swState.page = p;
      loadWords();
    });
  }

  async function loadWords() {
    try {
      const qs = new URLSearchParams({ page: swState.page, pageSize: SW_PAGE_SIZE });
      if (swState.kind) qs.set('kind', swState.kind);
      if (swState.status !== '') qs.set('status', swState.status);
      if (swState.q) qs.set('q', swState.q);
      const data = await api(`/api/admin/sensitive-words?${qs}`);
      renderWords(data);
    } catch (e) {
      swEmpty.hidden = false;
      swEmpty.textContent = '词库加载失败：' + e.message;
    }
  }

  async function handleWordOp(op, item) {
    if (op === 'delete' && !window.confirm(`确认删除词条「${item.word}」？删除后立即失效。`)) return;
    try {
      if (op === 'toggle') {
        await api(`/api/admin/sensitive-words/${item.wordId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: item.status === 1 ? 0 : 1 }),
        });
      } else if (op === 'rename') {
        const word = window.prompt(`编辑词条（当前：「${item.word}」）`, item.word);
        if (word == null || !word.trim() || word.trim() === item.word) return;
        await api(`/api/admin/sensitive-words/${item.wordId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ word: word.trim() }),
        });
      } else {
        await api(`/api/admin/sensitive-words/${item.wordId}`, { method: 'DELETE' });
      }
      await loadWords();
    } catch (e) {
      alert('操作失败：' + e.message);
    }
  }

  async function addWord() {
    const word = swWordEl.value.trim();
    if (!word) { swMsgEl.textContent = '请先填写词条'; swMsgEl.className = 'msg-line err'; return; }
    swAddBtn.disabled = true;
    swMsgEl.textContent = '提交中…';
    swMsgEl.className = 'msg-line';
    try {
      await api('/api/admin/sensitive-words', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          word,
          kind: swFormKindEl.value,
          category: swCategoryEl.value.trim() || undefined,
          remark: swRemarkEl.value.trim() || undefined,
        }),
      });
      swMsgEl.textContent = '✅ 已新增，下一问立即生效';
      swMsgEl.className = 'msg-line ok';
      swWordEl.value = '';
      swRemarkEl.value = '';
      await loadWords();
    } catch (e) {
      swMsgEl.textContent = '新增失败：' + e.message;
      swMsgEl.className = 'msg-line err';
    } finally {
      swAddBtn.disabled = false;
    }
  }

  function renderOpLog(data) {
    opCountEl.textContent = `共 ${data.total || 0} 条`;
    opNoteEl.textContent = data.items && data.items.length
      ? ''
      : '当前筛选条件下暂无记录。JSON 模式不落审计表，切换到 MySQL 后可见。';

    opUserEl.innerHTML = '<option value="">全部操作人</option>' +
      (data.operators || []).map(u =>
        `<option value="${u.userId}">${esc(u.realName || u.userNo)}（${esc(u.userNo)}）</option>`
      ).join('');
    opActionEl.innerHTML = '<option value="">全部动作</option>' +
      (data.actions || []).map(a => `<option value="${esc(a.action)}">${esc(a.label)}（${esc(a.action)}）</option>`).join('');
    opUserEl.value = opState.userId;
    opActionEl.value = opState.action;

    const items = data.items || [];
    opEmpty.hidden = items.length > 0;
    opTbody.innerHTML = '';
    const labels = Object.fromEntries((data.actions || []).map(a => [a.action, a.label]));
    for (const it of items) {
      const tr = document.createElement('tr');
      const detail = it.detail ? JSON.stringify(it.detail) : '—';
      tr.innerHTML = `
        <td>${fmtTime(it.createdAt)}</td>
        <td>${esc(it.operatorName || it.userId || '系统')}</td>
        <td>${esc(labels[it.action] || it.action)}</td>
        <td>${esc(it.targetType)}</td>
        <td>${esc(it.targetId)}</td>
        <td>${esc(it.ipAddr || '—')}</td>
        <td class="cl-q" title="${esc(detail)}">${esc(detail)}</td>`;
      opTbody.appendChild(tr);
    }
    renderSimplePager(opPager, data.page || 1, data.total || 0, data.pageSize || OP_PAGE_SIZE, p => {
      opState.page = p;
      loadOperations();
    });
  }

  async function loadOperations() {
    try {
      const qs = new URLSearchParams({ page: opState.page, pageSize: OP_PAGE_SIZE });
      if (opState.userId) qs.set('userId', opState.userId);
      if (opState.action) qs.set('action', opState.action);
      if (opState.from) qs.set('dateFrom', opState.from);
      if (opState.to) qs.set('dateTo', opState.to);
      const data = await api(`/api/admin/operation-logs?${qs}`);
      renderOpLog(data);
    } catch (e) {
      opEmpty.hidden = false;
      opEmpty.textContent = '操作日志加载失败：' + e.message;
    }
  }

  swAddBtn.onclick = addWord;
  swKindEl.onchange = () => { swState.kind = swKindEl.value; swState.page = 1; loadWords(); };
  swStatusEl.onchange = () => { swState.status = swStatusEl.value; swState.page = 1; loadWords(); };
  swQEl.onkeydown = e => {
    if (e.key !== 'Enter') return;
    swState.q = swQEl.value.trim();
    swState.page = 1;
    loadWords();
  };
  document.getElementById('op-search').onclick = () => {
    opState.userId = opUserEl.value;
    opState.action = opActionEl.value;
    opState.from = opFromEl.value;
    opState.to = opToEl.value;
    opState.page = 1;
    loadOperations();
  };
  document.getElementById('op-reset').onclick = () => {
    opState.userId = '';
    opState.action = '';
    opState.from = '';
    opState.to = '';
    opState.page = 1;
    opFromEl.value = '';
    opToEl.value = '';
    loadOperations();
  };
  document.getElementById('op-refresh').onclick = () => loadOperations();

  window.initSystemPage = () => Promise.all([
    (!window.NXZAuth || window.NXZAuth.can('system.words')) ? loadWords() : Promise.resolve(),
    (!window.NXZAuth || window.NXZAuth.can('system.logs')) ? loadOperations() : Promise.resolve(),
  ]);

  // ---- 页签切换：知识库 / 智能体管理 / 模型网关 / 会话日志 / 统计分析 / 系统管理 ----
  // 后四页懒加载：首次切过去才拉数据，避免无谓的后台查询。
  // ⚠ 原「模型管理」页已下线 —— 模型的唯一来源是网关（见 gateway.js）。
  const tabsEl = document.getElementById('tabs');
  const PAGES = {
    docs: document.getElementById('page-docs'),
    agents: document.getElementById('page-agents'),
    gateway: document.getElementById('page-gateway'),
    logs: document.getElementById('page-logs'),
    stats: document.getElementById('page-stats'),
    system: document.getElementById('page-system'),
  };
  // 懒加载页 → 初始化钩子名（由 agent.js / chatlog.js / stats.js / gateway.js / 本文件挂到 window 上）
  const BOOTERS = {
    agents: 'initAgentPage',
    gateway: 'initGatewayPage',
    logs: 'initChatlogPage',
    stats: 'initStatsPage',
    system: 'initSystemPage',
  };
  const bootedPages = new Set();

  /** 页签懒加载的「加载中」提示。
   *
   *  延迟 250ms 才显示：本地库通常几十毫秒就返回，立刻显示会让提示**闪一下**，
   *  比不显示更糟。只有真正慢的时候（慢网络 / 大表）才需要给用户反馈。
   *
   *  返回收尾函数；无论成功失败都要调用，否则提示会一直挂在那儿。
   */
  function beginLoading(node) {
    let shown = false;
    const timer = setTimeout(() => {
      shown = true;
      if (node) node.setAttribute('data-loading', '1');
    }, 250);
    return () => {
      clearTimeout(timer);
      if (shown && node) node.removeAttribute('data-loading');
    };
  }

  if (tabsEl) {
    tabsEl.querySelectorAll('.tab').forEach(btn => {
      btn.onclick = () => {
        const name = btn.dataset.tab;
        tabsEl.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
        let target = null;
        for (const [key, node] of Object.entries(PAGES)) {
          if (node) node.hidden = key !== name;
          if (key === name) target = node;
        }
        const hook = BOOTERS[name];
        if (hook && !bootedPages.has(name) && typeof window[hook] === 'function') {
          bootedPages.add(name);
          const done = beginLoading(target);
          // 钩子返回 Promise（各页 init 已约定），失败也要收尾，否则提示卡住
          Promise.resolve(window[hook]()).catch(() => {}).finally(done);
        }
      };
    });
  }

  // 首屏数据等鉴权就绪后再拉：无权限的模块不发请求（避免无谓的 403 与审计噪声）。
  function can(code) {
    return !window.NXZAuth || window.NXZAuth.can(code);
  }
  function bootData() {
    renderFilters();
    const tasks = [loadMeta()];
    if (can('docs.view')) tasks.push(load(), loadFilters());
    if (can('categories.view')) tasks.push(loadCats());
    if (can('deptperm.view')) tasks.push(loadPerms());
    return Promise.all(tasks);
  }
  const ready = window.NXZAuth ? window.NXZAuth.ready() : Promise.resolve();
  ready.then(bootData).catch(() => {});
})();
