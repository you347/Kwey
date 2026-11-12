/* 文件树：渲染 + 新建/改名/移动/删除（走 API，WS 事件即时同步，P0-7）。 */

import { api, reportError } from './api.js';
import { state, emit } from './state.js';
import { toast, confirmDialog, $ } from './utils.js';

const expandedDirs = new Set();
let treeEl = null;

export function initFiletree() {
  treeEl = $('#filetree');
  onTreeEvents();
  return refreshTree();
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

    const icon = document.createElement('span');
    icon.className = 'tree-row__icon';
    icon.textContent = node.type === 'dir' ? (expandedDirs.has(node.path) ? '📂' : '📁')
      : (node.type === 'note' ? '📄' : '📎');
    const name = document.createElement('span');
    name.className = 'tree-row__name';
    name.textContent = node.name;
    name.title = node.path;
    const actions = document.createElement('span');
    actions.className = 'tree-row__actions';
    buildActions(actions, node);
    row.appendChild(icon);
    row.appendChild(name);
    row.appendChild(actions);
    wrap.appendChild(row);

    if (node.type === 'dir') {
      const children = document.createElement('div');
      children.className = `tree-children${expandedDirs.has(node.path) ? '' : ' collapsed'}`;
      if (node.children && node.children.length) renderNodes(node.children, children);
      wrap.appendChild(children);
      row.addEventListener('click', (e) => {
        if (e.target.closest('.tree-row__actions')) return;
        toggleDir(node.path, children, icon);
      });
      // 拖放目标：目录（移动）
      row.addEventListener('dragover', (e) => { e.preventDefault(); row.style.outline = '1px dashed var(--accent)'; });
      row.addEventListener('dragleave', () => { row.style.outline = ''; });
      row.addEventListener('drop', async (e) => {
        e.preventDefault();
        row.style.outline = '';
        const src = e.dataTransfer.getData('text/kwey-path');
        if (!src || src === node.path || !src.includes('/')) return;
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
    iconEl.textContent = '📁';
  } else {
    expandedDirs.add(path);
    childrenEl.classList.remove('collapsed');
    iconEl.textContent = '📂';
  }
}

function buildActions(box, node) {
  const mk = (label, title, fn) => {
    const b = document.createElement('button');
    b.className = 'btn btn--mini';
    b.textContent = label;
    b.title = title;
    b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
    box.appendChild(b);
  };
  if (node.type === 'dir') {
    mk('＋', '新建笔记', async () => {
      const name = window.prompt('新笔记名称：');
      if (!name) return;
      try {
        const res = await api.post('/api/files/create-note', { dir: node.path, name });
        expandedDirs.add(node.path);
        emit('tree-refresh');
        emit('open-note', res.path);
      } catch (err) { reportError(err); }
    });
    mk('＋Dir', '新建子目录', async () => {
      const name = window.prompt('新目录名称：');
      if (!name) return;
      try {
        await api.post('/api/files/create-dir', { dir: node.path, name });
        expandedDirs.add(node.path);
        emit('tree-refresh');
      } catch (err) { reportError(err); }
    });
  }
  mk('✎', '改名', async () => {
    const newName = window.prompt(`改名「${node.name}」为：`, node.name);
    if (!newName || newName === node.name) return;
    try {
      const res = await api.post('/api/files/rename', { path: node.path, new_name: newName });
      toast(`已改名（重写链接 ${res.rewritten_links} 处）`, 'success');
      emit('tree-refresh');
      if (node.type === 'note') emit('note-moved-locally', { old: node.path, newPath: res.new_path });
    } catch (err) { reportError(err); }
  });
  mk('🗑', '删除', async () => {
    if (!confirmDialog(`确认删除「${node.name}」？（进入 .trash/ 回收站）`)) return;
    try {
      if (node.type === 'note') await api.del('/api/note', { path: node.path });
      else await api.del('/api/files/dir', { path: node.path, confirm: 1 });
      toast('已移入回收站 .trash/', 'success');
      emit('tree-refresh');
      if (node.type === 'note') emit('note-deleted', { path: node.path });
    } catch (err) { reportError(err); }
  });
}

function onTreeEvents() {
  emit; // noop 引用避免 lint
}
