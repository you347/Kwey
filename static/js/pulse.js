/* 知识脉冲：空态驾驶舱。今日字数 / 连续天数 / 孤儿 / 断链 四统计块 + Agent 最近活动。
   数据来自 GET /api/pulse；孤儿/断链明细点击时懒加载 /api/graph/full 前端推导。 */

import { api, reportError } from './api.js';
import { on, emit } from './state.js';
import { $, escapeHtml, toast, debounce, basename, stripMd } from './utils.js';
import * as templates from './templates.js';

const ACTION_LABEL = {
  create: '新建', update: '更新', delete: '删除', link: '加链', tags: '改标签',
  append: '追加', move: '移动', rename: '重命名', mkdir: '建目录', rmdir: '删目录',
  restore: '恢复', rollback: '回滚', organize: '整理', purge: '清除',
};

let initialized = false;
let rootEl = null;
let overlayEl = null;
let listEl = null;
let listTitleEl = null;
let loading = false;
let lastData = null;

export function initPulse() {
  if (initialized) return;
  initialized = true;
  rootEl = $('#pulse');
  overlayEl = $('#overlay-pulse');
  listEl = $('#pulse-list');
  listTitleEl = $('#pulse-list-title');

  if (rootEl) rootEl.addEventListener('click', onRootClick);

  if (overlayEl) {
    overlayEl.addEventListener('mousedown', (e) => {
      if (e.target === overlayEl) closeList();
    });
    const closeBtn = $('#btn-pulse-list-close');
    if (closeBtn) closeBtn.addEventListener('click', closeList);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !overlayEl.classList.contains('hidden')) closeList();
    });
  }

  const wsRefresh = debounce(() => refreshPulse(), 800);
  for (const ev of ['ws:created', 'ws:updated', 'ws:deleted', 'ws:moved', 'ws:links_changed']) {
    on(ev, wsRefresh);
  }
}

/** 空态可见时拉取并渲染脉冲数据；失败不阻塞空态其余部分。 */
export async function refreshPulse() {
  if (!rootEl) return;
  const empty = $('#empty-state');
  if (empty && empty.classList.contains('hidden')) return;
  if (loading) return;
  loading = true;
  if (!lastData) renderSkeleton();
  try {
    const data = await api.get('/api/pulse');
    lastData = data;
    render(data);
  } catch (err) {
    if (!lastData) renderError(err);
    // 已有旧数据时静默保留，避免 WS 抖动清空驾驶舱
  } finally {
    loading = false;
  }
}

/* ------------------------------------------------------------------ 渲染 */

function renderSkeleton() {
  rootEl.innerHTML = `
    <div class="pulse__tiles">
      ${'<div class="pulse-tile pulse-tile--skeleton"><span class="pulse-tile__num"></span><span class="pulse-tile__label"></span></div>'.repeat(4)}
    </div>`;
}

function renderError(err) {
  rootEl.innerHTML = `
    <div class="pulse__error">
      <span>知识脉冲加载失败：${escapeHtml(err.message || String(err))}</span>
      <button type="button" class="btn btn--mini" data-act="retry">重试</button>
    </div>`;
}

function render(d) {
  const tiles = [
    { act: 'today', num: d.today_words, label: '今日字数', cls: ' pulse-tile--accent', tip: d.today_notes && d.today_notes.length ? '打开今日最新笔记' : '今天还没有笔记' },
    { act: 'daily', num: d.streak_days, label: '连续天数', cls: '', tip: '打开/创建每日笔记' },
    { act: 'orphan', num: d.orphan_count, label: '孤儿笔记', cls: '', tip: '无任何双链的笔记，点击查看明细' },
    { act: 'broken', num: d.broken_count, label: '断链', cls: d.broken_count > 0 ? ' pulse-tile--broken' : '', tip: '指向不存在目标的链接，点击查看明细' },
  ];
  const tilesHtml = tiles.map((t) => `
    <button type="button" class="pulse-tile${t.cls}" data-act="${t.act}" title="${escapeHtml(t.tip)}">
      <span class="pulse-tile__num">${Number(t.num) || 0}</span>
      <span class="pulse-tile__label">${t.label}</span>
    </button>`).join('');

  const agents = (Array.isArray(d.agent_recent) ? d.agent_recent : []).slice(0, 3);
  const agentHtml = agents.length ? `
    <div class="pulse__agent">
      <div class="pulse__agent-title">Agent 最近活动</div>
      ${agents.map((a) => `
        <div class="pulse__agent-item" data-path="${escapeHtml(a.path || '')}" title="${escapeHtml(a.path || '')}">
          <span class="pulse__agent-name">${escapeHtml(a.name || a.role || 'agent')}</span>
          <span class="pulse__agent-action">${escapeHtml(ACTION_LABEL[a.action] || a.action || '')}</span>
          <span class="pulse__agent-path">${escapeHtml(stripMd(basename(a.path || '')))}</span>
          <span class="pulse__agent-time">${relTime(a.ts)}</span>
        </div>`).join('')}
    </div>` : '';

  rootEl.innerHTML = `
    <div class="pulse__head">
      <span class="pulse__head-title">知识脉冲</span>
      <span class="pulse__head-count">共 ${Number(d.note_count) || 0} 篇</span>
    </div>
    <div class="pulse__tiles">${tilesHtml}</div>
    ${agentHtml}`;
}

function relTime(ts) {
  const t = Number(ts);
  if (!Number.isFinite(t) || t <= 0) return '';
  const diff = Math.max(0, Date.now() / 1000 - t);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

/* ------------------------------------------------------------------ 交互 */

function onRootClick(e) {
  const agentItem = e.target.closest('.pulse__agent-item');
  if (agentItem && rootEl.contains(agentItem)) {
    const p = agentItem.dataset.path;
    if (p) emit('open-note', p);
    return;
  }
  const btn = e.target.closest('[data-act]');
  if (!btn || !rootEl.contains(btn)) return;
  const act = btn.dataset.act;
  if (act === 'retry') { refreshPulse(); return; }
  if (act === 'today') {
    const path = lastData && lastData.today_notes && lastData.today_notes[0];
    if (path) emit('open-note', path);
    else toast('今天还没有笔记，先写一篇吧');
    return;
  }
  if (act === 'daily') {
    templates.openDailyNote().catch(reportError);
    return;
  }
  if (act === 'orphan' || act === 'broken') openList(act);
}

/** 孤儿/断链明细浮层：懒加载全库图谱，前端推导名单（与 /api/pulse 口径一致）。 */
async function openList(kind) {
  if (!overlayEl || !listEl) return;
  overlayEl.classList.remove('hidden');
  if (listTitleEl) listTitleEl.textContent = kind === 'orphan' ? '孤儿笔记' : '断链明细';
  listEl.innerHTML = '<div class="panel-empty">加载中…</div>';
  try {
    const data = await api.get('/api/graph/full');
    const items = kind === 'orphan' ? orphanItems(data) : brokenItems(data);
    renderList(kind, items);
  } catch (err) {
    listEl.innerHTML = '';
    const box = document.createElement('div');
    box.className = 'panel-empty';
    box.textContent = `加载失败：${err.message || err}`;
    listEl.appendChild(box);
  }
}

function orphanItems(data) {
  const linked = new Set();
  for (const edge of (data.edges || [])) {
    linked.add(edge.source);
    linked.add(edge.target);
  }
  return (data.nodes || [])
    .filter((n) => !linked.has(n.path))
    .map((n) => ({ path: n.path, title: n.title || n.path, sub: n.path }));
}

function brokenItems(data) {
  return (data.unresolved || []).map((u) => ({
    path: u.source,
    title: `${stripMd(basename(u.source))} → ${u.target}`,
    sub: u.source,
  }));
}

function renderList(kind, items) {
  listEl.innerHTML = '';
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'panel-empty';
    empty.textContent = kind === 'orphan' ? '没有孤儿笔记，双链覆盖良好' : '没有断链';
    listEl.appendChild(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'result-item';
    row.title = item.sub;
    row.innerHTML = `
      <span class="result-item__title">${escapeHtml(item.title)}</span>
      <span class="result-item__path">${escapeHtml(item.sub)}</span>`;
    row.addEventListener('click', () => {
      closeList();
      emit('open-note', item.path);
    });
    listEl.appendChild(row);
  }
}

function closeList() {
  if (overlayEl) overlayEl.classList.add('hidden');
}