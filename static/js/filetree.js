/* 文件树：渲染 + 新建/改名/移动/删除（走 API，WS 事件即时同步，P0-7）。
 * 行内悬浮按钮与右键菜单共用同一组动作函数。 */

import { api, reportError } from './api.js';
import { state, emit, on } from './state.js';
import { toast, confirmDialog, copyPath, $ } from './utils.js';
import { askText } from './dialog.js';
import { showContextMenu } from './contextmenu.js';
import { scanUnreviewed, invalidate } from './provenance.js';

const expandedDirs = new Set();
let treeEl = null;
let lastTree = [];
let onlyUnreviewed = false;
let unreviewedPaths = new Set();
let scanning = false;
let scanMeta = null;
let scanSeq = 0;

function icon(id) {
  return `<svg class="icon" aria-hidden="true"><use href="#${id}"/></svg>`;
}

/** 图片缩略图地址（服务端会做格式转换/消毒）。 */
function thumbUrl(path) {
  return '/api/preview/thumb?path=' + encodeURIComponent(path);
}

export function initFiletree() {
  treeEl = $('#filetree');
  const filterBtn = $('#btn-unreviewed-filter');
  if (filterBtn) filterBtn.addEventListener('click', () => toggleUnreviewedFilter());
  on('provenance-updated', ({ path } = {}) => {
    if (!onlyUnreviewed) return;
    invalidate(path);
    refreshTree();
  });
  on('ws:updated', ({ path } = {}) => {
    if (!onlyUnreviewed) return;
    invalidate(path);
    refreshTree();
  });
  return refreshTree();
}

async function toggleUnreviewedFilter() {
  onlyUnreviewed = !onlyUnreviewed;
  if (!onlyUnreviewed) {
    scanSeq += 1;  // 丢弃仍在途的扫描结果
    scanning = false;
    unreviewedPaths = new Set();
    scanMeta = null;
    syncFilterButton();
    renderCurrentTree();
    return;
  }
  syncFilterButton();
  await applyUnreviewedScan(true);
  renderCurrentTree();
}

function collectNotePaths(nodes, out = []) {
  for (const node of nodes || []) {
    if (node.type === 'note') out.push(node.path);
    else if (node.children) collectNotePaths(node.children, out);
  }
  return out;
}

async function applyUnreviewedScan(force = false) {
  const seq = ++scanSeq;
  scanning = true;
  syncFilterButton();
  try {
    const result = await scanUnreviewed(collectNotePaths(lastTree), { force });
    if (seq !== scanSeq || !onlyUnreviewed) return;
    unreviewedPaths = result.paths;
    scanMeta = result;
    expandDirsFor(lastTree);
  } catch (err) {
    if (seq !== scanSeq) return;
    unreviewedPaths = new Set();
    scanMeta = null;
    reportError(err);
  } finally {
    if (seq === scanSeq) {
      scanning = false;
      syncFilterButton();
    }
  }
}

function syncFilterButton() {
  const btn = $('#btn-unreviewed-filter');
  if (!btn) return;
  btn.classList.toggle('btn--active', onlyUnreviewed);
  btn.setAttribute('aria-pressed', String(onlyUnreviewed));
  if (scanning) {
    btn.textContent = '筛选中…';
    btn.title = '正在读取各笔记的溯源状态';
    return;
  }
  btn.textContent = onlyUnreviewed ? `未审 ${unreviewedPaths.size}` : '未审';
  const parts = ['只看 Agent 改写且尚未审阅的笔记'];
  if (onlyUnreviewed && scanMeta) {
    parts.push(`已检查 ${scanMeta.scanned}/${scanMeta.total} 篇`);
    if (scanMeta.truncated) parts.push('超过上限，仅检查前 500 篇');
  }
  btn.title = parts.join('；');
}

/**
 * 展开祖先目录并滚动定位到某路径（智能体「打开文件夹 / 定位附件」用）。
 * 目录用 open-folder 事件触发，路径为 vault 相对路径。
 */
export async function revealPath(path) {
  const rel = String(path || '').replace(/\\/g, '/').replace(/\/+$/, '');
  if (!rel || !treeEl) return;
  const parts = rel.split('/');
  for (let i = 1; i <= parts.length; i += 1) expandedDirs.add(parts.slice(0, i).join('/'));
  await refreshTree();
  const row = treeEl.querySelector(`.tree-row[data-path="${CSS.escape(rel)}"]`);
  if (!row) return;
  row.scrollIntoView({ block: 'center' });
  row.classList.add('tree-row--reveal');
  window.setTimeout(() => row.classList.remove('tree-row--reveal'), 2400);
}

export async function refreshTree() {
  if (!treeEl) return;
  try {
    const tree = await api.get('/api/tree');
    lastTree = Array.isArray(tree) ? tree : [];
    if (onlyUnreviewed) await applyUnreviewedScan();
    renderCurrentTree();
  } catch (err) { reportError(err); }
}

function renderCurrentTree() {
  if (!treeEl) return;
  treeEl.innerHTML = '';
  if (!lastTree.length) {
    treeEl.innerHTML = '<div class="panel-empty">（空目录）</div>';
    return;
  }
  const rendered = renderNodes(lastTree, treeEl, onlyUnreviewed);
  if (onlyUnreviewed && rendered === 0) {
    treeEl.innerHTML = '<div class="panel-empty">没有 Agent 改写未审的笔记</div>';
  }
}

/** 过滤模式下自动展开含未审笔记的目录，避免结果藏在折叠目录里。 */
function expandDirsFor(nodes) {
  let has = false;
  for (const node of nodes || []) {
    if (node.type === 'note') {
      if (unreviewedPaths.has(node.path)) has = true;
    } else if (node.children && expandDirsFor(node.children)) {
      expandedDirs.add(node.path);
      has = true;
    }
  }
  return has;
}

function renderNodes(nodes, container, filtered = false) {
  let rendered = 0;
  for (const node of nodes) {
    if (filtered && node.type !== 'dir' && !unreviewedPaths.has(node.path)) continue;

    const wrap = document.createElement('div');
    wrap.className = 'tree-node';
    const row = document.createElement('div');
    row.className = `tree-row${node.path === state.activePath ? ' tree-row--active' : ''}`;
    row.dataset.path = node.path;
    row.dataset.type = node.type;

    const iconEl = document.createElement('span');
    iconEl.className = 'tree-row__icon';
    if (node.type === 'image') {
      // 图片行用缩略图代替通用文件图标；加载失败（无解码器）时回退文件图标
      iconEl.classList.add('tree-row__icon--thumb');
      const thumb = document.createElement('img');
      thumb.className = 'tree-row__thumb';
      thumb.loading = 'lazy';
      thumb.alt = '';
      thumb.src = thumbUrl(node.path);
      thumb.addEventListener('error', () => {
        iconEl.classList.remove('tree-row__icon--thumb');
        iconEl.innerHTML = icon('i-file');
      }, { once: true });
      iconEl.appendChild(thumb);
    } else {
      iconEl.innerHTML = icon(node.type === 'dir'
        ? (expandedDirs.has(node.path) ? 'i-folder-open' : 'i-folder')
        : (node.type === 'note' ? 'i-file' : 'i-download'));
    }
    const name = document.createElement('span');
    name.className = 'tree-row__name';
    name.textContent = node.name;
    name.title = node.path;
    const actions = document.createElement('span');
    actions.className = 'tree-row__actions';
    buildActions(actions, node);
    row.appendChild(iconEl);
    row.appendChild(name);
    if (node.type !== 'note' && node.type !== 'image' && node.index_state) {
      const badge = document.createElement('span');
      badge.className = 'tree-row__badge';
      badge.textContent = '未全索引';
      badge.title = node.index_detail || '该文件未被完整索引；仍可预览与下载';
      row.appendChild(badge);
    }
    row.appendChild(actions);
    wrap.appendChild(row);

    row.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      showContextMenu(e.clientX, e.clientY, nodeMenuItems(node));
    });

    if (node.type === 'dir') {
      const children = document.createElement('div');
      children.className = `tree-children${expandedDirs.has(node.path) ? '' : ' collapsed'}`;
      const childCount = node.children && node.children.length
        ? renderNodes(node.children, children, filtered)
        : 0;
      if (filtered && childCount === 0) continue;  // 无未审后代：整目录隐藏
      rendered += childCount;
      wrap.appendChild(children);
      row.addEventListener('click', (e) => {
        if (e.target.closest('.tree-row__actions')) return;
        toggleDir(node.path, children, iconEl);
      });
      // 拖放目标：目录（移动）
      row.addEventListener('dragover', (e) => { e.preventDefault(); row.style.outline = '1px dashed var(--accent)'; });
      row.addEventListener('dragleave', () => { row.style.outline = ''; });
      row.addEventListener('drop', async (e) => {
        e.preventDefault();
        row.style.outline = '';
        const src = e.dataTransfer.getData('text/kwey-path');
        if (!src || src === node.path) return;  // 根级笔记同样可拖入目录
        try {
          const res = await api.post('/api/files/move', { path: src, new_dir: node.path });
          toast(`已移动到 ${res.new_path}（重写链接 ${res.rewritten_links} 处）`, 'success');
          emit('tree-refresh');
        } catch (err) { reportError(err); }
      });
    } else {
      rendered += 1;
      row.addEventListener('click', (e) => {
        if (e.target.closest('.tree-row__actions')) return;
        if (node.type === 'note') emit('open-note', node.path);
        else emit('open-file', node.path);
      });
      row.draggable = true;
      row.addEventListener('dragstart', (e) => {
        e.dataTransfer.setData('text/kwey-path', node.path);
      });
    }
    container.appendChild(wrap);
  }
  return rendered;
}

function toggleDir(path, childrenEl, iconEl) {
  if (expandedDirs.has(path)) {
    expandedDirs.delete(path);
    childrenEl.classList.add('collapsed');
    iconEl.innerHTML = icon('i-folder');
  } else {
    expandedDirs.add(path);
    childrenEl.classList.remove('collapsed');
    iconEl.innerHTML = icon('i-folder-open');
  }
}

/* ------------------------------------------------------------------ 共享动作（悬浮按钮 + 右键菜单） */
async function createNoteIn(dir) {
  const name = await askText({
    title: '新建笔记',
    label: dir ? `在「${dir}」下创建（名称可含子目录）` : '笔记名称（可含子目录，如 notes/读书/随笔）',
    placeholder: '未命名',
    confirmText: '创建',
  });
  if (!name) return;
  try {
    const res = await api.post('/api/files/create-note', { dir, name });
    if (dir) expandedDirs.add(dir);
    emit('tree-refresh');
    emit('open-note', res.path);
  } catch (err) { reportError(err); }
}

async function createDirIn(dir) {
  const name = await askText({
    title: '新建目录',
    label: dir ? `在「${dir}」下创建子目录` : '目录名称（可含父目录）',
    confirmText: '创建',
  });
  if (!name) return;
  try {
    await api.post('/api/files/create-dir', { dir, name });
    if (dir) expandedDirs.add(dir);
    emit('tree-refresh');
  } catch (err) { reportError(err); }
}

async function renameNode(node) {
  const newName = await askText({
    title: '改名',
    label: `将「${node.name}」改名为`,
    value: node.name,
    confirmText: '改名',
  });
  if (!newName || newName === node.name) return;
  try {
    const res = await api.post('/api/files/rename', { path: node.path, new_name: newName });
    toast(`已改名（重写链接 ${res.rewritten_links} 处）`, 'success');
    emit('tree-refresh');
    if (node.type === 'note') emit('note-moved-locally', { old: node.path, newPath: res.new_path });
  } catch (err) { reportError(err); }
}

async function deleteNode(node) {
  const ok = await confirmDialog(`「${node.name}」将移入 .trash/ 回收站，可随时恢复。`, {
    title: '删除', danger: true, confirmText: '移入回收站',
  });
  if (!ok) return;
  try {
    if (node.type === 'note') await api.del('/api/note', { path: node.path });
    else await api.del('/api/files/dir', { path: node.path, confirm: 1 });
    toast('已移入回收站 .trash/', 'success');
    emit('tree-refresh');
    if (node.type === 'note') emit('note-deleted', { path: node.path });
  } catch (err) { reportError(err); }
}

function buildActions(box, node) {
  const mk = (iconId, title, fn) => {
    const b = document.createElement('button');
    b.className = 'btn btn--mini';
    b.innerHTML = icon(iconId);
    b.title = title;
    b.setAttribute('aria-label', title);
    b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
    box.appendChild(b);
  };
  if (node.type === 'dir') {
    mk('i-plus', '新建笔记', () => createNoteIn(node.path));
    mk('i-folder-plus', '新建子目录', () => createDirIn(node.path));
  }
  mk('i-edit', '改名', () => renameNode(node));
  mk('i-trash', '删除', () => deleteNode(node));
}

function nodeMenuItems(node) {
  if (node.type === 'dir') {
    return [
      { label: '新建笔记', icon: 'i-plus', run: () => createNoteIn(node.path) },
      { label: '新建子目录', icon: 'i-folder-plus', run: () => createDirIn(node.path) },
      { sep: true },
      { label: '作为对话上下文', icon: 'i-spark', run: () => emit('chat-context', { path: node.path, kind: 'dir' }) },
      { sep: true },
      { label: '改名', icon: 'i-edit', run: () => renameNode(node) },
      { label: '删除', icon: 'i-trash', danger: true, run: () => deleteNode(node) },
    ];
  }
  const items = [
    node.type === 'note'
      ? { label: '打开', icon: 'i-file', run: () => emit('open-note', node.path) }
      : { label: '预览', icon: 'i-file', run: () => emit('open-file', node.path) },
    { label: '作为对话上下文', icon: 'i-spark', run: () => emit('chat-context', { path: node.path, kind: 'note' }) },
  ];
  if (node.type === 'note') {
    items.push({ label: '在右屏打开', icon: 'i-panel-right', run: () => emit('open-in-pane', { pane: 'secondary', path: node.path }) });
  } else {
    items.push({
      label: '下载',
      icon: 'i-download',
      run: () => {
        const url = '/file/' + node.path.split('/').map(encodeURIComponent).join('/');
        const a = document.createElement('a');
        a.href = url;
        a.download = node.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
      },
    });
  }
  items.push(
    { sep: true },
    { label: '改名', icon: 'i-edit', run: () => renameNode(node) },
    { label: '复制路径', icon: 'i-copy', run: () => copyPath(node.path) },
    { sep: true },
    { label: '删除', icon: 'i-trash', danger: true, run: () => deleteNode(node) },
  );
  return items;
}
