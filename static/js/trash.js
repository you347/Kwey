/* 回收站面板：列出 / 恢复 / 彻底删除 / 清空（P1）。 */

import { api, reportError } from './api.js';
import { on, emit } from './state.js';
import { $, escapeHtml, toast, confirmDialog } from './utils.js';

let initialized = false;

function icon(id) {
  return `<svg class="icon" aria-hidden="true"><use href="#${id}"/></svg>`;
}

/** 构造列表行内的动作按钮。 */
function makeAction(iconId, title, onClick) {
  const btn = document.createElement('button');
  btn.className = 'panel-list__action';
  btn.title = title;
  btn.setAttribute('aria-label', title);
  btn.innerHTML = icon(iconId);
  btn.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
  return btn;
}

function formatMtime(ts) {
  const n = Number(ts);
  if (!n) return '';
  const d = new Date(n * 1000);   // 后端 st_mtime 为秒级时间戳
  const pad = (v) => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function initTrash() {
  if (initialized) return;
  initialized = true;

  const purgeBtn = $('#btn-trash-purge');
  if (purgeBtn) purgeBtn.addEventListener('click', purgeAll);

  on('panel-activated', ({ side, name } = {}) => {
    if (side === 'left' && name === 'trash') refreshTrash();
  });
  // 文件删除/新建都会改变回收站内容（删除进站、恢复出站）
  on('ws:deleted', () => refreshTrash());
  on('ws:created', () => refreshTrash());

  // 首次渲染（失败仅在面板内提示，隐藏面板时无感）
  refreshTrash();
}

/** 清空回收站：二次确认后永久删除全部条目。 */
async function purgeAll() {
  if (!await confirmDialog('确认清空回收站？此操作将永久删除其中全部文件与目录，不可恢复、不可撤销。', {
    title: '清空回收站', danger: true, confirmText: '永久清空',
  })) return;
  try {
    const data = await api.del('/api/trash', { confirm: 1 });
    toast(`已清空回收站（删除 ${data && data.deleted != null ? data.deleted : 0} 项）`, 'success');
    emit('tree-refresh');
    refreshTrash();
  } catch (err) { reportError(err); }
}

/** 恢复单条：成功后同步文件树并刷新，单文件恢复顺手打开。 */
async function restoreItem(item) {
  try {
    const data = await api.post('/api/trash/restore', { trash_path: item.trash_path });
    toast(`已恢复「${item.name}」`, 'success');
    emit('tree-refresh');
    refreshTrash();
    // 目录恢复没有对应笔记，仅对单文件打开；用后端返回的实际路径（可能已去重改名）
    if (!item.is_dir && data && data.path) emit('open-note', data.path);
  } catch (err) { reportError(err); }
}

/** 彻底删除单条：二次确认后永久删除。 */
async function purgeItem(item) {
  if (!await confirmDialog(`确认彻底删除「${item.name}」？此操作将永久删除该文件/目录，不可恢复、不可撤销。`, {
    title: '彻底删除', danger: true, confirmText: '永久删除',
  })) return;
  try {
    await api.del('/api/trash', { path: item.trash_path });
    toast(`已彻底删除「${item.name}」`, 'success');
    emit('tree-refresh');
    refreshTrash();
  } catch (err) { reportError(err); }
}

/** 拉取并渲染回收站列表；空态或失败均在面板内提示。 */
export async function refreshTrash() {
  const box = $('#trash-list');
  if (!box) return;
  try {
    const data = await api.get('/api/trash');
    const items = data && Array.isArray(data.items) ? data.items : [];
    box.innerHTML = '';
    if (!items.length) {
      box.innerHTML = '<div class="panel-empty">回收站为空</div>';
      return;
    }
    for (const item of items) {
      const row = document.createElement('div');
      row.className = 'panel-list__item';
      const mtime = formatMtime(item.mtime);
      row.title = `原路径：${item.original_path}${mtime ? ` · 删除于 ${mtime}` : ''}`;
      row.innerHTML = `
        ${icon(item.is_dir ? 'i-folder' : 'i-file')}
        <span class="panel-list__name">${escapeHtml(item.name)}</span>
        <span class="panel-list__meta">${escapeHtml(item.original_path)}</span>`;
      row.appendChild(makeAction('i-restore', '恢复', () => restoreItem(item)));
      row.appendChild(makeAction('i-close', '彻底删除', () => purgeItem(item)));
      box.appendChild(row);
    }
  } catch (err) {
    box.innerHTML = `<div class="panel-empty">${escapeHtml(err.message)}</div>`;
  }
}
