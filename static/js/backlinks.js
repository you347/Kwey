/* 反向链接 + 未链接提及面板（含一键转双链，P0-4）。 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { escapeHtml, toast, $ } from './utils.js';

/** 刷新当前笔记的反链/未链接提及。path 为 null 时清空。 */
export async function refreshBacklinks(path) {
  const blBox = $('#backlinks');
  const ulBox = $('#unlinked');
  if (!blBox || !ulBox) return;
  if (!path) {
    blBox.innerHTML = '<div class="panel-empty">未打开笔记</div>';
    ulBox.innerHTML = '<div class="panel-empty">未打开笔记</div>';
    return;
  }
  // 反向链接
  try {
    const data = await api.get('/api/graph/backlinks', { path });
    blBox.innerHTML = '';
    if (!data.backlinks.length) {
      blBox.innerHTML = '<div class="panel-empty">暂无反向链接</div>';
    }
    for (const item of data.backlinks) {
      const el = document.createElement('div');
      el.className = 'backlink-item';
      el.innerHTML = `
        <div class="backlink-item__title">${escapeHtml(item.title)}</div>
        ${item.snippet ? `<div class="backlink-item__snippet">${escapeHtml(item.snippet)}</div>` : ''}`;
      el.addEventListener('click', () => emit('open-note', item.path));
      blBox.appendChild(el);
    }
  } catch (err) {
    blBox.innerHTML = `<div class="panel-empty">${escapeHtml(err.message)}</div>`;
  }

  // 未链接提及
  try {
    const data = await api.get('/api/graph/unlinked', { path });
    ulBox.innerHTML = '';
    if (!data.mentions.length) {
      ulBox.innerHTML = '<div class="panel-empty">暂无未链接提及</div>';
    }
    for (const m of data.mentions) {
      const el = document.createElement('div');
      el.className = 'backlink-item';
      el.innerHTML = `
        <div class="backlink-item__title">${escapeHtml(m.title)}</div>
        ${m.snippet ? `<div class="backlink-item__snippet">${escapeHtml(m.snippet)}</div>` : ''}
        <button class="btn btn--mini linkify-btn" title="在该处创建 [[双链]]"><svg class="icon" aria-hidden="true"><use href="#i-link"/></svg> 转为双链</button>`;
      el.querySelector('.linkify-btn').addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          await api.post('/api/graph/linkify', { path: m.path, mention_path: path, line: m.line || null });
          toast(`已在《${m.title}》中创建双链`, 'success');
          emit('note-updated', { path: m.path });
          refreshBacklinks(path);
        } catch (err) { reportError(err); }
      });
      el.addEventListener('click', () => emit('open-note', m.path));
      ulBox.appendChild(el);
    }
  } catch (err) {
    ulBox.innerHTML = `<div class="panel-empty">${escapeHtml(err.message)}</div>`;
  }
}
