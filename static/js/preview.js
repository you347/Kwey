/* 实时预览：调后端 /api/note/render（服务端 markdown-it 兜底），
 * wikilink/嵌入/标签渲染与点击跳转（P0-3）。 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { escapeHtml, confirmDialog, $ } from './utils.js';

function pane() { return $('#editor-preview'); }

/** 渲染当前 path 的预览。 */
export async function renderPreview(path) {
  const el = pane();
  if (!el) return;
  try {
    const data = await api.get('/api/note/render', { path });
    el.innerHTML = data.html;
    bindInteractions(el);
  } catch (err) {
    el.innerHTML = `<div class="preview-error">${escapeHtml(err.message)}</div>`;
  }
}

export function clearPreview() {
  const el = pane();
  if (el) el.innerHTML = '';
}

function bindInteractions(root) {
  // wikilink 点击跳转；断链提示创建（P0-3）
  root.querySelectorAll('.kwey-wikilink').forEach((a) => {
    a.addEventListener('click', async (ev) => {
      ev.preventDefault();
      const target = a.dataset.target;
      if (!target) return;
      try {
        const resolved = await api.get('/api/note/resolve', { target });
        if (resolved.exists) {
          emit('open-note', resolved.path);
        } else if (confirmDialog(`笔记「${target}」不存在，现在创建吗？`)) {
          await api.post('/api/note', { path: `${target}.md` });
          emit('tree-refresh');
          emit('open-note', `${target}.md`);
        }
      } catch (err) { reportError(err); }
    });
  });

  // 嵌入 ![[..]] → 回填内容（异步，逐个）
  root.querySelectorAll('.kwey-embed').forEach(async (el) => {
    const target = el.dataset.target;
    if (!target) return;
    try {
      const resolved = await api.get('/api/note/resolve', { target });
      const res = await api.get('/api/note/embed', { path: resolved.path || target });
      el.innerHTML = res.html;
      el.classList.toggle('kwey-embed--missing', !res.exists);
      bindInteractions(el);
    } catch (err) {
      el.innerHTML = `<span class="preview-error">${escapeHtml(err.message)}</span>`;
    }
  });

  // 标签点击 → 标签面板过滤
  root.querySelectorAll('.kwey-tag').forEach((el) => {
    el.addEventListener('click', () => emit('tag-filter', el.dataset.tag));
  });
}
