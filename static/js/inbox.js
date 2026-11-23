/* AI 收件箱：把 capture 扔进来的东西批量归置。
 *
 * 流程：打开 → 看到收件箱原文 → 点「AI 整理」拿建议（目录/标题/标签）→
 * 人只勾选与微调 → 应用（移动 + 改名 + 写标签，全部走 VaultService）。
 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, $all, escapeHtml, toast } from './utils.js';
import { registerCommand } from './palette.js';
import { openCapture } from './capture.js';

export const COMMAND_ID = 'inbox.open';
const STYLE_ID = 'inbox-style';

const ib = {
  open: false,
  items: [],
  dirs: [],
  tags: [],
  ai: false,
  busy: false,
  selected: new Set(),
  prevFocus: null,
};
let dom = null;

export function initInbox() {
  registerCommand({
    id: COMMAND_ID,
    title: '打开 AI 收件箱',
    run: () => { openInbox().catch(reportError); },
  });
  $('#btn-inbox')?.addEventListener('click', () => { openInbox().catch(reportError); });
}

export async function openInbox() {
  ensureDom();
  ib.open = true;
  ib.prevFocus = document.activeElement;
  dom.overlay.classList.remove('hidden');
  await refresh();
}

function close() {
  ib.open = false;
  dom.overlay.classList.add('hidden');
  try { ib.prevFocus && ib.prevFocus.focus({ preventScroll: true }); } catch { /* 已卸载 */ }
}

async function refresh() {
  dom.body.innerHTML = '<div class="ib-empty">读取收件箱…</div>';
  try {
    const data = await api.get('/api/inbox');
    ib.items = (data.items || []).map((item) => ({ ...item, suggestion: null, checked: true }));
    ib.selected = new Set(ib.items.map((item) => item.path));
    render();
  } catch (err) {
    dom.body.innerHTML = `<div class="ib-empty">读取失败：${escapeHtml(err.message)}</div>`;
  }
}

async function suggest() {
  if (ib.busy || !ib.items.length) return;
  ib.busy = true;
  dom.headStatus.textContent = 'AI 正在读收件箱…';
  try {
    const data = await api.post('/api/inbox/suggest', {});
    const byPath = new Map((data.items || []).map((item) => [item.path, item]));
    ib.dirs = data.dirs || [];
    ib.tags = data.tags || [];
    ib.ai = !!data.ai;
    for (const item of ib.items) {
      const hit = byPath.get(item.path);
      if (hit) item.suggestion = hit;
    }
    dom.headStatus.textContent = data.ai ? 'AI 建议已就绪（可逐条修改）' : (data.message || '已生成规则建议');
    render();
    if (!data.ai) toast(data.message || 'AI 未配置，已给出规则建议', 'info', 4000);
  } catch (err) {
    dom.headStatus.textContent = '';
    toast(`整理建议失败：${err.message}`, 'error');
  } finally {
    ib.busy = false;
  }
}

/* ------------------------------------------------------------------ 渲染 */
function render() {
  if (!ib.items.length) {
    dom.body.innerHTML = `<div class="ib-empty">
      <h3>收件箱是空的</h3>
      <p>用「快速捕获」（Ctrl+Shift+Space）把想法、链接、会议结论先扔进来，攒够了再一起整理。</p>
      <button class="btn btn--primary btn--mini" id="ib-capture" type="button">快速捕获</button>
    </div>`;
    dom.foot.innerHTML = '';
    dom.headStatus.textContent = '';
    $('#ib-capture', dom.body)?.addEventListener('click', () => { close(); openCapture(); });
    return;
  }

  const rows = ib.items.map((item, index) => {
    const s = item.suggestion;
    const suggestion = s ? s.suggestion : null;
    const dirOptions = ['<option value="">（留在原目录）</option>']
      .concat(ib.dirs.map((dir) => {
        const selected = suggestion && suggestion.folder === dir ? ' selected' : '';
        return `<option value="${escapeHtml(dir)}"${selected}>${escapeHtml(dir)}</option>`;
      })).join('');
    const tags = (suggestion && suggestion.tags ? suggestion.tags : item.tags || []).join(', ');
    return `<div class="ib-row${item.checked ? ' is-checked' : ''}" data-index="${index}">
      <input type="checkbox" class="ib-check" ${item.checked ? 'checked' : ''} data-check="${index}">
      <div class="ib-main">
        <div class="ib-line">
          <input class="ib-title" type="text" data-title="${index}"
            value="${escapeHtml((suggestion && suggestion.title) || item.title || '')}"
            placeholder="标题">
          <select class="ib-folder" data-folder="${index}">${dirOptions}</select>
        </div>
        <div class="ib-line">
          <input class="ib-tags" type="text" data-tags="${index}" value="${escapeHtml(tags)}"
            placeholder="标签（逗号分隔）">
          <span class="ib-path" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</span>
        </div>
        ${s ? `<div class="ib-summary">${escapeHtml(s.summary || '')}
          ${s.reason ? `<span class="ib-reason">· ${escapeHtml(s.reason)}</span>` : ''}</div>` : ''}
      </div>
    </div>`;
  }).join('');

  dom.body.innerHTML = `<div class="ib-list">${rows}</div>`;
  dom.foot.innerHTML = `
    <span class="ib-count">已选 ${ib.selected.size} / ${ib.items.length}</span>
    <span style="flex:1"></span>
    <button class="btn btn--mini" id="ib-all" type="button">全选/全不选</button>
    <button class="btn btn--primary btn--mini" id="ib-apply" type="button">应用整理</button>`;

  for (const box of $all('[data-check]', dom.body)) {
    box.addEventListener('change', () => {
      const index = Number(box.dataset.check);
      ib.items[index].checked = box.checked;
      if (box.checked) ib.selected.add(ib.items[index].path);
      else ib.selected.delete(ib.items[index].path);
      box.closest('.ib-row').classList.toggle('is-checked', box.checked);
      dom.foot.querySelector('.ib-count').textContent = `已选 ${ib.selected.size} / ${ib.items.length}`;
    });
  }
  $('#ib-all', dom.foot)?.addEventListener('click', () => {
    const all = ib.selected.size < ib.items.length;
    ib.items.forEach((item) => { item.checked = all; });
    ib.selected = new Set(all ? ib.items.map((i) => i.path) : []);
    render();
  });
  $('#ib-apply', dom.foot)?.addEventListener('click', () => { apply().catch(reportError); });
}

async function apply() {
  const items = [];
  for (const [index, item] of ib.items.entries()) {
    if (!item.checked) continue;
    const title = dom.body.querySelector(`[data-title="${index}"]`)?.value.trim() || null;
    const folder = dom.body.querySelector(`[data-folder="${index}"]`)?.value ?? '';
    const tags = (dom.body.querySelector(`[data-tags="${index}"]`)?.value || '')
      .split(/[,，]/).map((t) => t.trim()).filter(Boolean);
    items.push({ path: item.path, title, folder: folder || null, tags });
  }
  if (!items.length) { toast('先勾选要整理的条目', 'error'); return; }
  dom.foot.querySelector('#ib-apply').disabled = true;
  try {
    const res = await api.post('/api/inbox/apply', { items });
    toast(`已整理 ${res.applied} 条${res.errors.length ? `，${res.errors.length} 条失败` : ''}`, 'success');
    emit('tree-refresh');
    await refresh();
  } catch (err) {
    toast(`应用失败：${err.message}`, 'error');
  }
}

/* ------------------------------------------------------------------ DOM */
function ensureDom() {
  if (dom) return;
  injectStyle();
  const overlay = document.createElement('div');
  overlay.id = 'overlay-inbox';
  overlay.className = 'overlay hidden';
  overlay.innerHTML = `
    <div id="inbox-panel" class="overlay__panel" style="width:min(860px,94vw);height:min(80vh,800px);max-height:none;display:flex;flex-direction:column;padding:0;overflow:hidden">
      <div class="ib-head">
        <svg class="icon" aria-hidden="true"><use href="#i-folder-open"/></svg>
        <span class="ib-title">AI 收件箱</span>
        <span id="ib-head-status" class="ib-head__status"></span>
        <span class="ib-head__actions">
          <button id="btn-ib-suggest" class="btn btn--mini" type="button">AI 整理</button>
          <button id="btn-ib-capture" class="btn btn--mini" type="button">捕获</button>
          <button id="btn-ib-refresh" class="btn btn--mini" type="button">刷新</button>
          <button id="btn-ib-close" class="btn btn--ghost btn--mini" type="button">关闭</button>
        </span>
      </div>
      <div id="ib-body" class="ib-body"></div>
      <div id="ib-foot" class="ib-foot"></div>
    </div>`;
  document.body.appendChild(overlay);
  dom = { overlay, body: $('#ib-body', overlay), foot: $('#ib-foot', overlay), headStatus: $('#ib-head-status', overlay) };
  $('#btn-ib-close', overlay).addEventListener('click', close);
  $('#btn-ib-refresh', overlay).addEventListener('click', () => { refresh().catch(reportError); });
  $('#btn-ib-suggest', overlay).addEventListener('click', () => { suggest().catch(reportError); });
  $('#btn-ib-capture', overlay).addEventListener('click', () => { close(); openCapture(); });
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { close(); e.stopPropagation(); }
  });
}

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
.ib-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  border-bottom: 1px solid var(--border); flex: none; }
.ib-title { font-weight: 600; font-size: var(--fs-md); }
.ib-head__status { font-size: var(--fs-xs); color: var(--text-dim); }
.ib-head__actions { margin-left: auto; display: flex; gap: 6px; }
.ib-body { flex: 1; overflow: auto; }
.ib-foot { flex: none; padding: 8px 12px; border-top: 1px solid var(--border-faint);
  display: flex; gap: 8px; align-items: center; font-size: var(--fs-xs); color: var(--text-dim); }
.ib-list { display: flex; flex-direction: column; }
.ib-row { display: flex; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--border-faint); }
.ib-row.is-checked { background: var(--accent-faint); }
.ib-check { margin-top: 8px; flex: none; }
.ib-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.ib-line { display: flex; gap: 8px; align-items: center; }
.ib-title { flex: 1; min-width: 0; }
.ib-folder { width: 160px; flex: none; }
.ib-tags { flex: 1; min-width: 0; }
.ib-path { font-size: var(--fs-xs); color: var(--text-dim); max-width: 42%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ib-summary { font-size: var(--fs-xs); color: var(--text-dim); line-height: 1.6; }
.ib-reason { opacity: .75; }
.ib-empty { padding: 56px 24px; text-align: center; color: var(--text-dim); font-size: var(--fs-sm); }
.ib-empty h3 { color: var(--text); margin: 0 0 8px; font-size: var(--fs-md); }
.ib-empty p { max-width: 420px; margin: 0 auto 14px; line-height: 1.7; }
`;
  document.head.appendChild(style);
}
