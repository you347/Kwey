/* 实时预览：把编辑器当前正文（含未落盘内容）交给后端渲染，
 * wikilink/嵌入/标签渲染与点击跳转（P0-3）。 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { escapeHtml, confirmDialog, $ } from './utils.js';

function pane() { return $('#editor-preview'); }

/**
 * 渲染预览。
 * @param {string} path 笔记路径
 * @param {string} [content] 编辑器当前正文；传入则用 POST 实时渲染，缺省回退读盘
 */
export async function renderPreview(path, content) {
  const el = pane();
  if (!el) return;
  try {
    const data = content === undefined
      ? await api.get('/api/note/render', { path })
      : await api.post('/api/note/render', { path, content });
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
        } else if (await confirmDialog(`笔记「${target}」不存在，现在创建吗？`, { title: '创建笔记', confirmText: '创建' })) {
          // 目标可能携带路径/扩展名/别名/锚点，统一清洗后再创建
          const clean = String(target).split('|')[0].split('#')[0].trim();
          const newPath = /\.(md|markdown)$/i.test(clean) ? clean : `${clean}.md`;
          await api.post('/api/note', { path: newPath });
          emit('tree-refresh');
          emit('open-note', newPath);
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
