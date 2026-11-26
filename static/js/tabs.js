/* 多标签页：开/关/切/恢复（localStorage 持久化，P0-11）。
 * P1-19：按 pane('main'|'secondary') 双标签栈。主屏沿用 state 顶层字段
 * （tabs/activePath/doc/dirty，兼容全部现有调用）；副屏挂 state.split.secondary
 * （同构字段，懒创建，state.js 无需改动）。副屏激活发 secondary-tab-activated，
 * 避免触发 app.js 的主屏 loadNote。 */

import { api, reportError } from './api.js';
import { state, emit } from './state.js';
import { basename, stripMd, copyPath, $ } from './utils.js';
import { showContextMenu } from './contextmenu.js';
import { isPlainPath } from './viewer.js';

const LS_KEY = 'kwey.tabs.v1';
let tabsEl = null;

export function initTabs() {
  tabsEl = $('#tabs');
  render('main');
  return restore();
}

/* ------------------------------------------------------------------ pane 状态 */
/** state.split 懒创建（字段契约见卡1报告；state.js 形式化补充由接线人完成）。 */
export function ensureSplitState() {
  if (!state.split) {
    state.split = {
      open: false,
      ratio: 0.5,
      focused: 'main',
      secondary: { tabs: [], activePath: null, doc: null, dirty: false },
    };
  }
  return state.split;
}

/** main 与 secondary 的 pane 对象同构：{tabs, activePath, doc, dirty}。 */
export function paneState(pane) {
  return pane === 'secondary' ? ensureSplitState().secondary : state;
}

const activatedEvent = (pane) => (pane === 'secondary' ? 'secondary-tab-activated' : 'tab-activated');

/* ------------------------------------------------------------------ 开/关/切（按 pane） */
/** 在指定 pane 打开（或激活）标签。 */
export async function openTabIn(pane, path, { activate = true } = {}) {
  const p = paneState(pane);
  let tab = p.tabs.find((t) => t.path === path);
  if (!tab) {
    tab = { path, title: stripMd(basename(path)) };
    p.tabs.push(tab);
  }
  if (activate) await activateTabIn(pane, path);
  render(pane);
  persist();
}

/** 激活标签 → 主屏广播 tab-activated（app.js 装载），副屏广播 secondary-tab-activated（splitview 装载）。 */
export async function activateTabIn(pane, path) {
  paneState(pane).activePath = path;
  render(pane);
  persist();
  emit(activatedEvent(pane), path);
}

/** 关闭标签；若关闭的是激活页则激活相邻页。关闭前广播 tab-closing（splitview 借此落盘副屏脏页）。 */
export async function closeTabIn(pane, path) {
  const p = paneState(pane);
  const idx = p.tabs.findIndex((t) => t.path === path);
  if (idx === -1) return;
  emit('tab-closing', { pane, path });
  const wasActive = p.activePath === path;
  p.tabs.splice(idx, 1);
  if (wasActive) {
    p.activePath = null;
    p.doc = null;
    const next = p.tabs[idx] || p.tabs[idx - 1] || null;
    if (next) await activateTabIn(pane, next.path);
    else emit(activatedEvent(pane), null);
  }
  render(pane);
  persist();
}

/** 清空指定 pane 的标签栈（关闭分屏时用）。 */
export function clearPane(pane) {
  const p = paneState(pane);
  p.tabs = [];
  p.activePath = null;
  p.doc = null;
  p.dirty = false;
  render(pane);
  persist();
}

/* ------------------------------------------------------------------ 主屏兼容层（原签名不变） */
/** 兼容入口：分屏开启时跟随焦点屏（文件树/搜索/切换器在右屏聚焦时开右屏），否则主屏。 */
export async function openTab(path, opts) {
  const pane = state.split && state.split.open && state.split.focused === 'secondary' ? 'secondary' : 'main';
  return openTabIn(pane, path, opts);
}
export async function activateTab(path) { return activateTabIn('main', path); }
export async function closeTab(path) { return closeTabIn('main', path); }
export function setDirty(dirty) { setDirtyIn('main', dirty); }

/* ------------------------------------------------------------------ 双屏联动 */
/** 重命名标签（WS moved / 本地改名联动）：两屏同步。 */
export function renameTab(oldPath, newPath) {
  for (const pane of ['main', 'secondary']) {
    const p = paneState(pane);
    const tab = p.tabs.find((t) => t.path === oldPath);
    if (!tab) continue;
    tab.path = newPath;
    tab.title = stripMd(basename(newPath));
    if (p.activePath === oldPath) p.activePath = newPath;
    if (p.doc && p.doc.path === oldPath) p.doc.path = newPath;
    render(pane);
  }
  persist();
}

/** 刷新标签标题（笔记改名后）：两屏同步。 */
export function refreshTabTitle(path, title) {
  for (const pane of ['main', 'secondary']) {
    const p = paneState(pane);
    const tab = p.tabs.find((t) => t.path === path);
    if (!tab) continue;
    tab.title = title || stripMd(basename(path));
    render(pane);
  }
}

export function setDirtyIn(pane, dirty) {
  const p = paneState(pane);
  const tab = p.tabs.find((t) => t.path === p.activePath);
  if (tab && tab.dirty !== dirty) {
    tab.dirty = dirty;
    render(pane);
  }
  p.dirty = dirty;
}

/* ------------------------------------------------------------------ 渲染 / 持久化 */
function render(pane) {
  const el = pane === 'secondary' ? $('#tabs-secondary') : tabsEl;
  if (!el) return;
  const p = paneState(pane);
  el.innerHTML = '';
  for (const tab of p.tabs) {
    const elTab = document.createElement('div');
    elTab.className = `tab${tab.path === p.activePath ? ' tab--active' : ''}${tab.dirty ? ' tab--dirty' : ''}`;
    elTab.title = tab.path;
    const title = document.createElement('span');
    title.className = 'tab__title';
    title.textContent = tab.title;
    const close = document.createElement('button');
    close.className = 'tab__close';
    close.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-close"/></svg>';
    close.title = '关闭 (Ctrl+W)';
    close.addEventListener('click', (e) => { e.stopPropagation(); closeTabIn(pane, tab.path).catch(reportError); });
    elTab.appendChild(title);
    elTab.appendChild(close);
    elTab.addEventListener('click', () => activateTabIn(pane, tab.path).catch(reportError));
    elTab.addEventListener('auxclick', (e) => { if (e.button === 1) closeTabIn(pane, tab.path).catch(reportError); });
    elTab.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      showContextMenu(e.clientX, e.clientY, tabMenuItems(pane, tab.path));
    });
    el.appendChild(elTab);
  }
}

function persist() {
  try {
    const payload = {
      tabs: state.tabs.map((t) => t.path),
      active: state.activePath,
    };
    if (state.split) {
      payload.secondary = {
        tabs: state.split.secondary.tabs.map((t) => t.path),
        active: state.split.secondary.activePath,
      };
    } else {
      // 主屏恢复先于分屏初始化：保留已存的副屏载荷，避免被覆盖清空
      try {
        const prev = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
        if (prev && prev.secondary) payload.secondary = prev.secondary;
      } catch { /* 旧载荷损坏则忽略 */ }
    }
    localStorage.setItem(LS_KEY, JSON.stringify(payload));
  } catch { /* localStorage 不可用时静默 */ }
}

/** 重启恢复：校验存在性后逐个恢复（P0-11，仅主屏）。 */
async function restore() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
  } catch { saved = null; }
  if (!saved || !Array.isArray(saved.tabs)) return;
  for (const path of saved.tabs) {
    try {
      await api.get(isPlainPath(path) ? '/api/plain-file' : '/api/note', { path });
      await openTab(path, { activate: false });
    } catch { /* 笔记已不存在则跳过 */ }
  }
  if (saved.active && state.tabs.some((t) => t.path === saved.active)) {
    await activateTab(saved.active);
  } else if (state.tabs.length) {
    await activateTab(state.tabs[0].path);
  }
}

/** 恢复副屏标签栈（splitview 打开分屏时调用）；返回是否恢复出标签。 */
export async function restoreSecondaryTabs() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
  } catch { saved = null; }
  const sec = saved && saved.secondary;
  if (!sec || !Array.isArray(sec.tabs) || !sec.tabs.length) return false;
  for (const path of sec.tabs) {
    try {
      await api.get(isPlainPath(path) ? '/api/plain-file' : '/api/note', { path });
      await openTabIn('secondary', path, { activate: false });
    } catch { /* 笔记已不存在则跳过 */ }
  }
  const p = paneState('secondary');
  if (sec.active && p.tabs.some((t) => t.path === sec.active)) {
    await activateTabIn('secondary', sec.active);
  } else if (p.tabs.length) {
    await activateTabIn('secondary', p.tabs[0].path);
  }
  return p.tabs.length > 0;
}

/** 标签右键菜单：关闭/关闭其他/跨屏打开/复制路径。 */
function tabMenuItems(pane, path) {
  const items = [
    { label: '关闭标签', icon: 'i-close', hint: 'Ctrl+W', run: () => closeTabIn(pane, path).catch(reportError) },
    { label: '关闭其他标签', icon: 'i-close', run: () => closeOthers(pane, path).catch(reportError) },
  ];
  if (pane === 'main') {
    items.push({ label: '在右屏打开', icon: 'i-panel-right', run: () => emit('open-in-pane', { pane: 'secondary', path }) });
  } else {
    items.push({ label: '在左屏打开', icon: 'i-panel-left', run: () => openTabIn('main', path).catch(reportError) });
  }
  items.push({ sep: true }, { label: '复制路径', icon: 'i-copy', run: () => copyPath(path) });
  return items;
}

async function closeOthers(pane, path) {
  const p = paneState(pane);
  for (const t of [...p.tabs]) {
    if (t.path !== path) await closeTabIn(pane, t.path);
  }
}
