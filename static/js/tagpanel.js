/* 标签面板：嵌套标签层级树 + 点击过滤对应笔记（P0-5）。 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, $all } from './utils.js';

let activeTag = null;
let panelEl = null;

export function initTagpanel() {
  panelEl = $('#tagpanel');
  return refreshTags();
}

export async function refreshTags() {
  if (!panelEl) return;
  try {
    const tree = await api.get('/api/graph/tags');
    panelEl.innerHTML = '';
    if (!tree.length) {
      panelEl.innerHTML = '<div class="panel-empty">（暂无标签）</div>';
      return;
    }
    renderTree(tree, panelEl);
  } catch (err) { reportError(err); }
}

function renderTree(nodes, container) {
  for (const node of nodes) {
    const wrap = document.createElement('div');
    wrap.className = 'tag-tree__node';
    const row = document.createElement('div');
    row.className = `tag-row${node.name === activeTag ? ' tag-row--active' : ''}`;
    row.dataset.tag = node.name;
    row.innerHTML = `<span class="tag-row__name">#${escapeName(node.name)}</span>` +
      `<span class="tag-row__count">${node.count}</span>`;
    row.addEventListener('click', () => selectTag(node.name, wrap));
    wrap.appendChild(row);
    if (node.children && node.children.length) {
      const children = document.createElement('div');
      children.className = 'tag-children';
      renderTree(node.children, children);
      wrap.appendChild(children);
    }
    container.appendChild(wrap);
  }
}

/** 预览内点击 #标签 → 高亮标签面板对应行并展开笔记列表。 */
export function filterByTag(tag) {
  if (!panelEl || !tag) return;
  const row = [...panelEl.querySelectorAll('.tag-row')].find((el) => el.dataset.tag === tag);
  if (row) row.click();
}

/** 选中标签 → 拉取笔记列表展示（frontmatter 与行内标签合并）。 */
async function selectTag(tag, wrapEl) {
  activeTag = tag;
  $all('.tag-row', panelEl).forEach((el) => el.classList.remove('tag-row--active'));
  const row = wrapEl.querySelector('.tag-row');
  if (row) row.classList.add('tag-row--active');
  emit('tag-filter', tag);

  // 移除旧列表
  $all('.tag-note-list', panelEl).forEach((el) => el.remove());
  const list = document.createElement('div');
  list.className = 'tag-note-list';
  try {
    const notes = await api.get('/api/graph/tag-notes', { tag });
    if (!notes.length) {
      list.innerHTML = '<div class="panel-empty">（无笔记）</div>';
    }
    for (const note of notes) {
      const item = document.createElement('div');
      item.className = 'tag-note-item';
      item.textContent = note.title || note.path;
      item.title = note.path;
      item.addEventListener('click', () => emit('open-note', note.path));
      list.appendChild(item);
    }
  } catch (err) {
    list.innerHTML = `<div class="panel-empty">${escapeName(err.message)}</div>`;
  }
  wrapEl.appendChild(list);
}

function escapeName(text) {
  const div = document.createElement('div');
  div.textContent = String(text == null ? '' : text);
  return div.innerHTML;
}
