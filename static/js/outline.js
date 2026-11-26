/* 大纲面板：读取当前笔记的 Markdown 标题，点击跳转到编辑器对应行（P1）。
 * 说明：大纲取自服务端已落盘的正文（/api/note/outline），编辑中内容尚未保存时
 * 大纲可能滞后，属可接受限制；保存或外部更新触发刷新后即可同步。 */

import { api } from './api.js';
import { state, on } from './state.js';
import { $, escapeHtml } from './utils.js';
import * as editor from './editor.js';
import { isPlainPath } from './viewer.js';

let initialized = false;
let currentPath = null;   // 最近一次 tab-activated 的笔记路径

function icon(id) {
  return `<svg class="icon" aria-hidden="true"><use href="#${id}"/></svg>`;
}

/** 当前应展示的笔记路径：优先事件记录，兜底 state.activePath（避免错过初始化前的事件）。 */
function trackedPath() {
  return currentPath || state.activePath || null;
}

export function initOutline() {
  if (initialized) return;
  initialized = true;
  currentPath = state.activePath || null;

  on('tab-activated', (path) => {
    currentPath = path || null;
    refreshOutline(currentPath);
  });
  on('note-updated', (data = {}) => {
    const path = trackedPath();
    if (path && (!data.path || data.path === path)) refreshOutline(path);
  });
  on('ws:updated', (msg = {}) => {
    const path = trackedPath();
    if (path && msg.path === path) refreshOutline(path);
  });
  on('panel-activated', ({ side, name } = {}) => {
    // 仅当左侧「大纲」面板被激活时补一次刷新
    if (side === 'left' && name === 'outline') refreshOutline(trackedPath());
  });

  // 首次渲染（无激活笔记时为空态）
  refreshOutline(trackedPath());
}

/**
 * 拉取并渲染指定笔记的大纲。path 为 null 时清空面板。
 * 无笔记 / 无标题 / 请求失败均在面板内给出空态提示。
 */
export async function refreshOutline(path) {
  const box = $('#outline-list');
  if (!box) return;
  if (!path) {
    box.innerHTML = '<div class="panel-empty">未打开笔记</div>';
    return;
  }
  if (isPlainPath(path)) {
    // 纯文本/CSV 不是 Markdown，服务端不会解析标题
    box.innerHTML = '<div class="panel-empty">纯文本文件无大纲</div>';
    return;
  }
  try {
    const data = await api.get('/api/note/outline', { path });
    const headings = data && Array.isArray(data.headings) ? data.headings : [];
    box.innerHTML = '';
    if (!headings.length) {
      box.innerHTML = '<div class="panel-empty">（无标题）</div>';
      return;
    }
    for (const h of headings) {
      const item = document.createElement('div');
      item.className = 'panel-list__item';
      item.dataset.level = String(Number(h.level) || 1);
      item.title = `第 ${h.line} 行`;
      item.innerHTML = `${icon('i-list')}<span class="panel-list__name">${escapeHtml(h.text)}</span>`;
      item.addEventListener('click', () => editor.scrollToLine(h.line));
      box.appendChild(item);
    }
  } catch (err) {
    // 面板内展示错误，避免静默吞掉（隐藏面板时不影响用户）
    box.innerHTML = `<div class="panel-empty">${escapeHtml(err.message)}</div>`;
  }
}
