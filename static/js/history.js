/* 快照时间线（M2）：git 提交列表、查看历史正文、一键回滚（回滚前自动留存当前版本）。 */

import { api, reportError } from './api.js';
import { state, emit } from './state.js';
import { $, escapeHtml, toast, confirmDialog } from './utils.js';

let initialized = false;

export function initHistory() {
  if (initialized) return;
  initialized = true;
  $('#btn-history-close').addEventListener('click', closeHistory);
  $('#overlay-history').addEventListener('mousedown', (e) => {
    if (e.target === $('#overlay-history')) closeHistory();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#overlay-history').classList.contains('hidden')) closeHistory();
  });
}

export function openHistory() {
  if (!state.activePath) { toast('请先打开一篇笔记', 'error'); return; }
  $('#history-path').textContent = state.activePath;
  $('#overlay-history').classList.remove('hidden');
  refresh().catch(reportError);
}

export function closeHistory() {
  $('#overlay-history').classList.add('hidden');
}

function fmt(ts) {
  if (!ts) return '—';
  const d = new Date(Number(ts) * 1000);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('zh-CN', { hour12: false });
}

async function refresh() {
  const box = $('#history-list');
  const path = state.activePath;
  box.innerHTML = '<div class="panel-empty">正在读取快照…</div>';
  const status = await api.get('/api/history/status');
  if (!status.available) {
    box.innerHTML = '<div class="panel-empty">系统未安装 git，快照功能不可用。</div>';
    return;
  }
  const log = await api.get('/api/history/log', { path, limit: 30 });
  if (!log.length) {
    box.innerHTML = '<div class="panel-empty">这篇笔记暂无快照。请在「设置 → Agent Link」开启 git 快照，之后每次写盘会自动提交。</div>';
    return;
  }
  box.innerHTML = log.map((item) => `
    <div class="history-item" data-hash="${escapeHtml(item.hash)}">
      <div class="history-item__head">
        <code>${escapeHtml(item.short)}</code>
        <span class="history-item__time">${escapeHtml(fmt(item.ts))}</span>
        <span class="history-item__msg">${escapeHtml(item.message)}</span>
        <button class="btn btn--mini history-item__show">查看</button>
        <button class="btn btn--mini history-item__rollback">回滚</button>
      </div>
      <pre class="history-item__body hidden"></pre>
    </div>`).join('');

  box.querySelectorAll('.history-item').forEach((el) => {
    const hash = el.dataset.hash;
    el.querySelector('.history-item__show').addEventListener('click', async () => {
      const pre = el.querySelector('.history-item__body');
      if (!pre.classList.contains('hidden')) { pre.classList.add('hidden'); return; }
      try {
        const text = await api.get('/api/history/show', { hash, path });
        pre.textContent = text;
        pre.classList.remove('hidden');
      } catch (err) { reportError(err); }
    });
    el.querySelector('.history-item__rollback').addEventListener('click', async () => {
      if (!await confirmDialog(`确认把「${path}」回滚到快照 ${hash.slice(0, 7)}？当前版本会先自动提交留存。`, {
        title: '回滚快照', confirmText: '回滚',
      })) return;
      try {
        await api.post('/api/history/rollback', { hash, path, confirm: true });
        toast('已回滚，当前版本已留存为一条快照', 'success');
        emit('open-note', path);
        await refresh();
      } catch (err) { reportError(err); }
    });
  });
}
