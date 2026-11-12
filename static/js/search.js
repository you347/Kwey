/* 搜索面板：操作符输入 + 高亮结果 + 点击跳转（P0-6）。 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $ } from './utils.js';

let overlayEl = null;
let inputEl = null;
let resultsEl = null;
let debouncedTimer = null;

export function initSearch() {
  overlayEl = $('#overlay-search');
  inputEl = $('#search-input');
  resultsEl = $('#search-results');

  $('#btn-search').addEventListener('click', openSearch);
  overlayEl.addEventListener('mousedown', (e) => { if (e.target === overlayEl) closeSearch(); });
  inputEl.addEventListener('input', () => {
    clearTimeout(debouncedTimer);
    debouncedTimer = setTimeout(() => runSearch(inputEl.value), 180);
  });
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeSearch();
    if (e.key === 'Enter') {
      const first = resultsEl.querySelector('.result-item');
      if (first && first.dataset.path) { emit('open-note', first.dataset.path); closeSearch(); }
    }
  });
}

export function openSearch() {
  overlayEl.classList.remove('hidden');
  inputEl.value = '';
  resultsEl.innerHTML = '<div class="panel-empty">输入关键词；可用 path:目录 tag:标签 file:文件名 缩小范围</div>';
  inputEl.focus();
}

export function closeSearch() {
  overlayEl.classList.add('hidden');
}

async function runSearch(q) {
  if (!q.trim()) {
    resultsEl.innerHTML = '<div class="panel-empty">输入关键词开始搜索</div>';
    return;
  }
  try {
    const data = await api.get('/api/search', { q, limit: 50 });
    resultsEl.innerHTML = '';
    if (!data.hits.length) {
      resultsEl.innerHTML = `<div class="panel-empty">无结果（${data.total} 条 / ${data.elapsed_ms}ms）</div>`;
      return;
    }
    const head = document.createElement('div');
    head.className = 'panel-empty';
    head.textContent = `共 ${data.total} 条 · ${data.elapsed_ms}ms`;
    resultsEl.appendChild(head);
    for (const hit of data.hits) {
      const el = document.createElement('div');
      el.className = 'result-item';
      el.dataset.path = hit.path;
      // snippet 由服务端转义 + <mark> 高亮，此处可直接插入
      el.innerHTML = `
        <span class="result-item__title">${escapeText(hit.title)}${hit.tags.map((t) => ` <small>#${escapeText(t)}</small>`).join('')}</span>
        <span class="result-item__path">${escapeText(hit.path)}</span>
        ${hit.snippet ? `<span class="result-item__snippet">${hit.snippet}</span>` : ''}`;
      el.addEventListener('click', () => { emit('open-note', hit.path); closeSearch(); });
      resultsEl.appendChild(el);
    }
  } catch (err) { reportError(err); }
}

function escapeText(text) {
  const div = document.createElement('div');
  div.textContent = String(text == null ? '' : text);
  return div.innerHTML;
}
