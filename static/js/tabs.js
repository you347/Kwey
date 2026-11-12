/* 多标签页：开/关/切/恢复（localStorage 持久化，P0-11）。 */

import { api, reportError } from './api.js';
import { state, emit } from './state.js';
import { basename, stripMd, $, $all, confirmDialog } from './utils.js';

const LS_KEY = 'kwey.tabs.v1';
let tabsEl = null;

export function initTabs() {
  tabsEl = $('#tabs');
  render();
  return restore();
}

/** 打开（或激活）标签。 */
export async function openTab(path, { activate = true } = {}) {
  let tab = state.tabs.find((t) => t.path === path);
  if (!tab) {
    tab = { path, title: stripMd(basename(path)) };
    state.tabs.push(tab);
  }
  if (activate) await activateTab(path);
  render();
  persist();
}

/** 激活标签 → 广播 tab-activated（app.js 负责装载文档）。 */
export async function activateTab(path) {
  state.activePath = path;
  render();
  persist();
  emit('tab-activated', path);
}

/** 关闭标签；若关闭的是激活页则激活相邻页。 */
export async function closeTab(path) {
  const idx = state.tabs.findIndex((t) => t.path === path);
  if (idx === -1) return;
  const wasActive = state.activePath === path;
  state.tabs.splice(idx, 1);
  if (wasActive) {
    state.activePath = null;
    state.doc = null;
    const next = state.tabs[idx] || state.tabs[idx - 1] || null;
    if (next) await activateTab(next.path);
    else emit('tab-activated', null);
  }
  render();
  persist();
}

/** 重命名标签（WS moved 事件联动）。 */
export function renameTab(oldPath, newPath) {
  const tab = state.tabs.find((t) => t.path === oldPath);
  if (!tab) return;
  tab.path = newPath;
  tab.title = stripMd(basename(newPath));
  if (state.activePath === oldPath) state.activePath = newPath;
  render();
  persist();
}

/** 刷新标签标题（笔记改名后）。 */
export function refreshTabTitle(path, title) {
  const tab = state.tabs.find((t) => t.path === path);
  if (!tab) return;
  tab.title = title || stripMd(basename(path));
  render();
}

export function setDirty(dirty) {
  const tab = state.tabs.find((t) => t.path === state.activePath);
  if (tab && tab.dirty !== dirty) {
    tab.dirty = dirty;
    render();
  }
  state.dirty = dirty;
}

function render() {
  if (!tabsEl) return;
  tabsEl.innerHTML = '';
  for (const tab of state.tabs) {
    const el = document.createElement('div');
    el.className = `tab${tab.path === state.activePath ? ' tab--active' : ''}${tab.dirty ? ' tab--dirty' : ''}`;
    el.title = tab.path;
    const title = document.createElement('span');
    title.className = 'tab__title';
    title.textContent = tab.title;
    const close = document.createElement('button');
    close.className = 'tab__close';
    close.textContent = '✕';
    close.title = '关闭 (Ctrl+W)';
    close.addEventListener('click', (e) => { e.stopPropagation(); closeTab(tab.path).catch(reportError); });
    el.appendChild(title);
    el.appendChild(close);
    el.addEventListener('click', () => activateTab(tab.path).catch(reportError));
    el.addEventListener('auxclick', (e) => { if (e.button === 1) closeTab(tab.path).catch(reportError); });
    tabsEl.appendChild(el);
  }
}

function persist() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      tabs: state.tabs.map((t) => t.path),
      active: state.activePath,
    }));
  } catch { /* localStorage 不可用时静默 */ }
}

/** 重启恢复：校验存在性后逐个恢复（P0-11）。 */
async function restore() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
  } catch { saved = null; }
  if (!saved || !Array.isArray(saved.tabs)) return;
  for (const path of saved.tabs) {
    try {
      await api.get('/api/note', { path });
      await openTab(path, { activate: false });
    } catch { /* 笔记已不存在则跳过 */ }
  }
  if (saved.active && state.tabs.some((t) => t.path === saved.active)) {
    await activateTab(saved.active);
  } else if (state.tabs.length) {
    await activateTab(state.tabs[0].path);
  }
}

export { confirmDialog };
