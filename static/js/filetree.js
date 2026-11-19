/* 文件树：渲染 + 新建/改名/移动/删除（走 API，WS 事件即时同步，P0-7）。
 * 行内悬浮按钮与右键菜单共用同一组动作函数。 */

import { api, reportError } from './api.js';
import { state, emit } from './state.js';
import { toast, confirmDialog, copyPath, $ } from './utils.js';
import { askText } from './dialog.js';
import { showContextMenu } from './contextmenu.js';

const expandedDirs = new Set();
let treeEl = null;

function icon(id) {
  return `<svg class="icon" aria-hidden="true"><use href="#${id}"/></svg>`;
}

export function initFiletree() {
  treeEl = $('#filetree');
  return refreshTree();
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
    treeEl.innerHTML = '';
    if (!tree.length) {
      treeEl.innerHTML = '<div class="panel-empty">（空目录）</div>';
      return;
    }
    renderNodes(tree, treeEl);
  } catch (err) { reportError(err); }
}

function renderNodes(nodes, container) {
  for (const node of nodes) {
    const wrap = document.createElement('div');
    wrap.className = 'tree-node';
    const row = document.createElement('div');
    row.className = `tree-row${node.path === state.activePath ? ' tree-row--active' : ''}`;
    row.dataset.path = node.path;
    row.dataset.type = node.type;

    const iconEl = document.createElement('span');
    iconEl.className = 'tree-row__icon';
    iconEl.innerHTML = icon(node.type === 'dir'
      ? (expandedDirs.has(node.path) ? 'i-folder-open' : 'i-folder')
      : (node.type === 'note' ? 'i-file' : 'i-download'));
    const name = document.createElement('span');
    name.className = 'tree-row__name';
    name.textContent = node.name;
    name.title = node.path;
    const actions = document.createElement('span');
    actions.className = 'tree-row__actions';
    buildActions(actions, node);
    row.appendChild(iconEl);
    row.appendChild(name);
    row.appendChild(actions);
    wrap.appendChild(row);

    row.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      showContextMenu(e.clientX, e.clientY, nodeMenuItems(node));
    });

    if (node.type === 'dir') {
      const children = document.createElement('div');
      children.className = `tree-children${expandedDirs.has(node.path) ? '' : ' collapsed'}`;
      if (node.children && node.children.length) renderNodes(node.children, children);
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
      row.addEventListener('click', (e) => {
        if (e.target.closest('.tree-row__actions')) return;
        if (node.type === 'note') emit('open-note', node.path);
      });
      row.draggable = true;
      row.addEventListener('dragstart', (e) => {
        e.dataTransfer.setData('text/kwey-path', node.path);
      });
    }
    container.appendChild(wrap);
  }
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
    { label: '打开', icon: 'i-file', run: () => emit('open-note', node.path) },
    { label: '作为对话上下文', icon: 'i-spark', run: () => emit('chat-context', { path: node.path, kind: 'note' }) },
  ];
  if (node.type === 'note') {
    items.push({ label: '在右屏打开', icon: 'i-panel-right', run: () => emit('open-in-pane', { pane: 'secondary', path: node.path }) });
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
