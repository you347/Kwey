/* 书签 / 最近文件面板：读写 state.settings 中的 bookmarks / recent（P1）。
 * 说明：后端 /api/settings 的 recent 为路径字符串数组，不含时间戳；这里用内存
 * Map 记录最近打开时间，仅用于列表项的 title 提示，不落盘。 */

import { state, on, emit } from './state.js';
import { saveSettings } from './settings/index.js';
import { $, escapeHtml, toast, basename, stripMd } from './utils.js';

const MAX_RECENT = 30;
let initialized = false;
const recentTimes = new Map();   // path -> 最近打开时间戳（仅内存）

function icon(id) {
  return `<svg class="icon" aria-hidden="true"><use href="#${id}"/></svg>`;
}

function getBookmarks() {
  return Array.isArray(state.settings.bookmarks) ? state.settings.bookmarks.slice() : [];
}

/** 兼容两种数据形态：路径字符串，或 {path, ts}（时间用于 title 提示）。 */
function recentEntries() {
  const raw = Array.isArray(state.settings.recent) ? state.settings.recent : [];
  const out = [];
  for (const entry of raw) {
    if (typeof entry === 'string') {
      out.push({ path: entry, ts: recentTimes.get(entry) || null });
    } else if (entry && typeof entry.path === 'string') {
      // 兼容秒级 / 毫秒级时间戳，统一成毫秒
      let ts = Number(entry.ts);
      if (ts && ts < 1e12) ts *= 1000;
      if (!ts) ts = recentTimes.get(entry.path) || null;
      out.push({ path: entry.path, ts });
      if (ts) recentTimes.set(entry.path, ts);
    }
  }
  return out;
}

function dedupe(list) {
  return Array.from(new Set(list));
}

function sameArray(a, b) {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

function formatTime(ts) {
  const d = new Date(ts);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 构造一个列表行内的动作按钮。 */
function makeAction(iconId, title, onClick) {
  const btn = document.createElement('button');
  btn.className = 'panel-list__action';
  btn.title = title;
  btn.setAttribute('aria-label', title);
  btn.innerHTML = icon(iconId);
  btn.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
  return btn;
}

export function initBookmarks() {
  if (initialized) return;
  initialized = true;

  const addBtn = $('#btn-bookmark-add');
  if (addBtn) addBtn.addEventListener('click', toggleBookmark);

  on('tab-activated', (path) => addRecent(path));
  on('panel-activated', ({ side, name } = {}) => {
    if (side !== 'left') return;
    if (name === 'bookmarks') refreshBookmarks();
    else if (name === 'recent') refreshRecent();
  });

  // 文件删除后清理失效路径；移动时把旧路径跟随重命名为新路径
  on('ws:deleted', (msg = {}) => {
    const path = msg.path;
    if (!path) return;
    recentTimes.delete(path);
    rewriteLists((list) => list.filter((p) => p !== path));
  });
  on('ws:moved', (msg = {}) => {
    const from = msg.old_path || msg.oldPath;
    const to = msg.path;
    if (!from || !to) return;
    if (recentTimes.has(from)) {
      recentTimes.set(to, recentTimes.get(from));
      recentTimes.delete(from);
    }
    rewriteLists((list) => list.map((p) => (p === from ? to : p)));
  });

  // 首次渲染两个列表
  refreshBookmarks();
  refreshRecent();
}

/** 把两个列表按 transform 变换后写回 state；有变化才保存设置，随后刷新。 */
function rewriteLists(transform) {
  const oldBookmarks = getBookmarks();
  const oldRecent = recentEntries().map((e) => e.path);
  const bookmarks = dedupe(transform(oldBookmarks));
  const recent = dedupe(transform(oldRecent)).slice(0, MAX_RECENT);
  const changed = !sameArray(oldBookmarks, bookmarks) || !sameArray(oldRecent, recent);
  state.settings.bookmarks = bookmarks;
  state.settings.recent = recent;
  if (changed) saveSettings({ bookmarks, recent });
  refreshBookmarks();
  refreshRecent();
}

/** 渲染书签列表：点击打开，右侧按钮移除。 */
export function refreshBookmarks() {
  const box = $('#bookmark-list');
  if (!box) return;
  const list = getBookmarks();
  box.innerHTML = '';
  if (!list.length) {
    box.innerHTML = '<div class="panel-empty">（暂无书签）</div>';
    return;
  }
  for (const path of list) {
    const item = document.createElement('div');
    item.className = 'panel-list__item';
    item.title = path;
    item.innerHTML = `${icon('i-bookmark')}<span class="panel-list__name">${escapeHtml(stripMd(basename(path)))}</span>`;
    item.appendChild(makeAction('i-close', '移除书签', () => {
      const next = getBookmarks().filter((p) => p !== path);
      state.settings.bookmarks = next;
      saveSettings({ bookmarks: next });
      refreshBookmarks();
    }));
    item.addEventListener('click', () => emit('open-note', path));
    box.appendChild(item);
  }
}

/** 渲染最近文件列表（最多 30 条，最新在前），时间放在 title 提示中。 */
export function refreshRecent() {
  const box = $('#recent-list');
  if (!box) return;
  const list = recentEntries().slice(0, MAX_RECENT);
  box.innerHTML = '';
  if (!list.length) {
    box.innerHTML = '<div class="panel-empty">（暂无最近文件）</div>';
    return;
  }
  for (const entry of list) {
    const item = document.createElement('div');
    item.className = 'panel-list__item';
    item.title = entry.ts
      ? `${entry.path} · 打开于 ${formatTime(entry.ts)}`
      : entry.path;
    item.innerHTML = `${icon('i-clock')}<span class="panel-list__name">${escapeHtml(stripMd(basename(entry.path)))}</span>`;
    item.addEventListener('click', () => emit('open-note', entry.path));
    box.appendChild(item);
  }
}

/** 对当前激活笔记增删书签，toast 提示后保存并刷新。 */
export function toggleBookmark() {
  const path = state.activePath;
  if (!path) {
    toast('当前没有打开的笔记', 'error');
    return;
  }
  const bookmarks = getBookmarks();
  const idx = bookmarks.indexOf(path);
  const added = idx === -1;
  if (added) bookmarks.push(path);
  else bookmarks.splice(idx, 1);
  state.settings.bookmarks = bookmarks;
  saveSettings({ bookmarks });
  toast(added
    ? `已收藏「${stripMd(basename(path))}」`
    : `已取消收藏「${stripMd(basename(path))}」`, 'success');
  refreshBookmarks();
}

/**
 * 记录一次打开：path 插到最前、去重、截断 30，保存并刷新。
 * 若 path 已在首位则直接跳过，避免每次 tab-activated 都写盘。
 */
export function addRecent(path) {
  if (!path) return;
  const recent = recentEntries().map((e) => e.path);
  if (recent[0] === path) return;
  const next = dedupe([path, ...recent]).slice(0, MAX_RECENT);
  state.settings.recent = next;
  recentTimes.set(path, Date.now());
  // 写盘统一交给 saveSettings（内部已 try/catch 并上报）；此处失败静默，不额外报错
  saveSettings({ recent: next });
  refreshRecent();
}
