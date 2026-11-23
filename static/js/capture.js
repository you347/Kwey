/* 快速捕获：一句话丢进收件箱。
 *
 * 入口：Ctrl+Shift+Space / 命令面板 / 悬浮按钮。
 * 不做「选目录、想标题、建 frontmatter」——这些交给 AI 收件箱整理。
 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, toast } from './utils.js';
import { registerCommand } from './palette.js';

export const COMMAND_ID = 'capture';
const STYLE_ID = 'capture-style';

let dom = null;
let saving = false;

export function initCapture() {
  registerCommand({
    id: COMMAND_ID,
    title: '快速捕获（存到收件箱）',
    hotkey: 'Ctrl+Shift+Space',
    run: () => openCapture(),
  });
  $('#btn-capture')?.addEventListener('click', () => openCapture());
}

export function openCapture(prefill = '') {
  ensureDom();
  dom.overlay.classList.remove('hidden');
  dom.text.value = prefill || '';
  dom.title.value = '';
  dom.tags.value = '';
  dom.status.textContent = '';
  setTimeout(() => dom.text.focus(), 30);
}

function close() {
  if (dom) dom.overlay.classList.add('hidden');
}

async function save() {
  if (saving) return;
  const text = dom.text.value.trim();
  if (!text) {
    dom.text.focus();
    return;
  }
  saving = true;
  dom.status.textContent = '保存中…';
  try {
    const data = await api.post('/api/capture', {
      text,
      title: dom.title.value.trim() || null,
      tags: dom.tags.value.split(/[,，\s]+/).map((t) => t.trim().replace(/^#/, '')).filter(Boolean),
    });
    close();
    toast(`已存入收件箱：${data.title}`, 'success');
    emit('tree-refresh');
    if (data.path) setTimeout(() => emit('open-note', data.path), 60);
  } catch (err) {
    dom.status.textContent = `保存失败：${err.message}`;
    reportError(err);
  } finally {
    saving = false;
  }
}

function ensureDom() {
  if (dom) return;
  injectStyle();
  const overlay = document.createElement('div');
  overlay.id = 'overlay-capture';
  overlay.className = 'overlay hidden';
  overlay.innerHTML = `
    <div class="cap-dialog" role="dialog" aria-modal="true" aria-labelledby="cap-title">
      <div class="cap-head">
        <span id="cap-title">快速捕获</span>
        <span class="cap-hint">Ctrl+Enter 保存 · Esc 取消</span>
      </div>
      <textarea id="cap-text" class="cap-text" placeholder="想到什么就写什么：一句想法、待读链接、会议结论…"></textarea>
      <div class="cap-row">
        <input id="cap-title-input" class="cap-input" type="text" placeholder="标题（留空自动取第一行）">
        <input id="cap-tags" class="cap-input" type="text" placeholder="标签（逗号分隔，可空）">
      </div>
      <div class="cap-foot">
        <span id="cap-status" class="cap-status"></span>
        <span style="flex:1"></span>
        <button class="btn btn--mini" id="btn-cap-cancel" type="button">取消</button>
        <button class="btn btn--primary btn--mini" id="btn-cap-save" type="button">存入收件箱</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  dom = {
    overlay,
    text: $('#cap-text', overlay),
    title: $('#cap-title-input', overlay),
    tags: $('#cap-tags', overlay),
    status: $('#cap-status', overlay),
  };
  $('#btn-cap-cancel', overlay).addEventListener('click', close);
  $('#btn-cap-save', overlay).addEventListener('click', () => { save().catch(reportError); });
  dom.text.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      save().catch(reportError);
    }
  });
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { close(); e.stopPropagation(); }
  });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) close();
  });
}

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
#overlay-capture { align-items: flex-start; padding-top: 12vh; }
.cap-dialog { width: min(620px, 92vw); background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: var(--radius-md); box-shadow: var(--shadow-soft);
  padding: 14px; display: flex; flex-direction: column; gap: 10px; }
.cap-head { display: flex; align-items: baseline; gap: 10px; font-weight: 600; }
.cap-hint { margin-left: auto; font-size: var(--fs-xs); color: var(--text-dim); font-weight: 400; }
.cap-text { width: 100%; box-sizing: border-box; min-height: 148px; max-height: 46vh; resize: vertical;
  font-size: var(--fs-md); line-height: 1.6; padding: 8px 10px; }
.cap-row { display: flex; gap: 8px; }
.cap-input { flex: 1; min-width: 0; }
.cap-foot { display: flex; align-items: center; gap: 6px; }
.cap-status { font-size: var(--fs-xs); color: var(--text-dim); }
`;
  document.head.appendChild(style);
}
