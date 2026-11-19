/* F4 复习流水线：右栏「复习」面板。
 * 到期列表（review_due <= 今天）+ again/good/easy 三档标记 + 今日进度条。
 * 命令 id: review.open（打开面板）/ review.schedule（把当前笔记加入复习）。
 *
 * 面板 DOM 由本模块运行时挂到 #rightbar（shell.js 的面板白名单是共享文件，
 * 本卡不改；这里自行注入 tab 与 .panel-view，与 shell 的互斥切换兼容：
 * shell.activatePanel 会隐藏所有非当前 data-panel 的 .panel-view）。 */

import { api, reportError } from './api.js';
import { state, on, emit } from './state.js';
import { $, escapeHtml, toast, debounce, basename, stripMd } from './utils.js';
import { registerCommand } from './palette.js';

export const REVIEW_COMMAND_ID = 'review.open';
export const REVIEW_SCHEDULE_COMMAND_ID = 'review.schedule';

const DEFAULT_SCHEDULE_DAYS = 3;
const RESULT_LABEL = { again: '重来', good: '良好', easy: '简单' };

let initialized = false;
let panelEl = null;
let tabBtn = null;
let listEl = null;
let summaryEl = null;
let countEl = null;
let progressEl = null;
let scheduleBtn = null;
let loading = false;
let marking = false;

/* ------------------------------------------------------------------ 初始化 */
export function initReview() {
  if (initialized) return;
  initialized = true;

  registerCommand({
    id: REVIEW_COMMAND_ID,
    title: '打开复习面板（到期笔记）',
    run: () => openReview(),
  });
  registerCommand({
    id: REVIEW_SCHEDULE_COMMAND_ID,
    title: '把当前笔记加入复习',
    run: () => scheduleCurrent().catch(reportError),
  });

  mountPanel();

  const refresh = debounce(() => { if (isVisible()) refreshReview(); }, 700);
  for (const ev of ['ws:created', 'ws:updated', 'ws:deleted', 'ws:moved']) on(ev, refresh);
  on('panel-activated', ({ side, name }) => {
    if (side === 'right' && name !== 'review') {
      // shell 面板接管：保持各自高亮状态一致
      if (tabBtn) tabBtn.classList.remove('panel-tab--active');
      return;
    }
    if (side === 'right' && name === 'review') refreshReview();
  });
}

/** 注入 tab 与面板骨架；若 index.html 已提供同 id 节点则直接复用。 */
function mountPanel() {
  const bar = $('#rightbar');
  const tabs = $('#right-tabs');
  const holder = bar && bar.querySelector('.sidebar__panels');
  if (!bar || !tabs || !holder) return;

  panelEl = $('#panel-review');
  if (!panelEl) {
    panelEl = document.createElement('section');
    panelEl.id = 'panel-review';
    panelEl.className = 'panel-view hidden';
    panelEl.dataset.panel = 'review';
    panelEl.innerHTML = `
      <div class="sidebar__section" style="flex:1;min-height:0">
        <div class="sidebar__header">
          <span>复习</span>
          <span id="review-count" class="sidebar__header-actions" style="font-weight:400;color:var(--text-dim)">—</span>
        </div>
        <div id="review-summary" class="panel-empty" style="padding:0 10px 6px"></div>
        <div style="height:6px;margin:0 10px 8px;border-radius:3px;background:var(--bg-hover);overflow:hidden">
          <div id="review-progress" style="height:100%;width:0%;background:var(--success);transition:width .25s"></div>
        </div>
        <div id="review-list" style="flex:1;min-height:0;overflow-y:auto"></div>
      </div>
      <div class="sidebar__section" style="flex:none;border-top:1px solid var(--border-faint);padding:6px 10px">
        <button id="btn-review-schedule" class="btn btn--mini"
          title="把当前打开的笔记纳入复习（默认 3 天后）">+ 加入复习</button>
      </div>`;
    holder.appendChild(panelEl);
  }

  tabBtn = tabs.querySelector('[data-panel="review"]');
  if (!tabBtn) {
    tabBtn = document.createElement('button');
    tabBtn.dataset.panel = 'review';
    tabBtn.setAttribute('role', 'tab');
    tabBtn.textContent = '复习';
    tabBtn.addEventListener('click', () => openReview());
    tabs.appendChild(tabBtn);
  } else {
    tabBtn.addEventListener('click', () => openReview());
  }

  listEl = $('#review-list', panelEl);
  summaryEl = $('#review-summary', panelEl);
  countEl = $('#review-count', panelEl);
  progressEl = $('#review-progress', panelEl);
  scheduleBtn = $('#btn-review-schedule', panelEl);
  if (scheduleBtn) {
    scheduleBtn.addEventListener('click', () => scheduleCurrent().catch(reportError));
  }
  if (listEl) listEl.addEventListener('click', onListClick);
}

/* ------------------------------------------------------------------ 面板切换 */
export function openReview() {
  if (!panelEl) mountPanel();
  if (!panelEl) { toast('复习面板不可用（缺少右栏容器）', 'error'); return; }
  const bar = $('#rightbar');
  if (bar) bar.classList.remove('collapsed');
  if (bar) {
    bar.querySelectorAll('.panel-view').forEach((view) => {
      view.classList.toggle('hidden', view !== panelEl);
    });
  }
  const tabs = $('#right-tabs');
  if (tabs) {
    tabs.querySelectorAll('button').forEach((btn) => {
      btn.classList.toggle('panel-tab--active', btn.dataset.panel === 'review');
    });
  }
  refreshReview();
}

function isVisible() {
  return !!panelEl && !panelEl.classList.contains('hidden');
}

/* ------------------------------------------------------------------ 数据 */
export async function refreshReview() {
  if (!panelEl || loading) return;
  loading = true;
  try {
    const [due, stats] = await Promise.all([
      api.get('/api/review/due', { limit: 100 }),
      api.get('/api/review/stats'),
    ]);
    render(due || [], stats || {});
  } catch (err) {
    if (listEl) {
      listEl.innerHTML = `<div class="panel-empty" style="padding:6px 10px">复习数据加载失败：${escapeHtml(err.message || String(err))}</div>`;
    }
  } finally {
    loading = false;
  }
}

function render(items, stats) {
  const dueCount = Number(stats.due ?? items.length) || 0;
  const doneToday = Number(stats.done_today) || 0;
  const scheduled = Number(stats.scheduled) || 0;

  if (countEl) countEl.textContent = `待复习 ${dueCount}`;
  if (summaryEl) {
    const parts = [`今日已完成 ${doneToday}`];
    if (scheduled) parts.push(`已排期 ${scheduled}`);
    if (Number(stats.max_overdue_days) > 0) parts.push(`最长逾期 ${stats.max_overdue_days} 天`);
    summaryEl.textContent = parts.join(' · ');
  }

  const total = dueCount + doneToday;
  const pct = total > 0 ? Math.round((doneToday / total) * 100) : 0;
  if (progressEl) {
    progressEl.style.width = `${pct}%`;
    progressEl.style.background = dueCount > 0 ? 'var(--accent)' : 'var(--success)';
    progressEl.title = `今日进度 ${pct}%（已完成 ${doneToday} / 共 ${total}）`;
  }

  if (!listEl) return;
  if (!items.length) {
    listEl.innerHTML = `
      <div class="panel-empty" style="padding:6px 10px">
        没有到期笔记。${scheduled ? '下一次到期会自动出现在这里。' : '点下方按钮把当前笔记加入复习。'}
      </div>`;
    return;
  }

  listEl.innerHTML = items.map((item) => {
    const overdue = Number(item.overdue_days) || 0;
    const dueText = overdue > 0 ? `逾期 ${overdue} 天` : '今天到期';
    const overdueStyle = overdue > 0 ? 'color:var(--broken)' : 'color:var(--text-dim)';
    const title = item.title || stripMd(basename(item.path || ''));
    return `
      <div class="result-item" data-path="${escapeHtml(item.path)}" title="${escapeHtml(item.path)}">
        <span class="result-item__title">${escapeHtml(title)}</span>
        <span class="result-item__path">
          <span style="${overdueStyle}">${escapeHtml(dueText)}</span>
          · 间隔 ${Number(item.interval) || 1} 天${item.due ? ` · ${escapeHtml(item.due)}` : ''}
        </span>
        <span style="display:flex;gap:4px;margin-top:6px">
          ${Object.entries(RESULT_LABEL).map(([key, label]) => `
            <button type="button" class="btn btn--mini" data-result="${key}"
              data-path="${escapeHtml(item.path)}">${label}</button>`).join('')}
        </span>
      </div>`;
  }).join('');
}

/* ------------------------------------------------------------------ 交互 */
function onListClick(e) {
  const btn = e.target.closest('[data-result]');
  if (btn) {
    e.stopPropagation();
    const path = btn.dataset.path;
    const result = btn.dataset.result;
    if (path && result) markNote(path, result).catch(reportError);
    return;
  }
  const row = e.target.closest('.result-item');
  if (row && row.dataset.path) emit('open-note', row.dataset.path);
}

/** 标记复习结果：again→1 天 / good→×2 / easy→×3（后端算间隔）。 */
export async function markNote(path, result) {
  if (marking) return;
  marking = true;
  try {
    const data = await api.post('/api/review/mark', { path, result });
    toast(`已标记「${RESULT_LABEL[result] || result}」，${data.interval} 天后（${data.due}）再复习`, 'success');
    await refreshReview();
  } finally {
    marking = false;
  }
}

/** 把当前打开的笔记纳入复习。 */
export async function scheduleCurrent() {
  const path = state.activePath;
  if (!path) { toast('先打开一篇笔记再加入复习', 'info'); return; }
  const data = await api.post('/api/review/schedule', {
    path,
    interval: DEFAULT_SCHEDULE_DAYS,
  });
  toast(`已加入复习：${DEFAULT_SCHEDULE_DAYS} 天后（${data.due}）`, 'success');
  if (isVisible()) await refreshReview();
}
