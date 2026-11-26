/* 只读预览：txt/csv/docx/xlsx/xls/pdf/图片 在编辑区上方以查看器呈现，不进入编辑态。 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, basename } from './utils.js';

let currentPath = null;
let currentReq = 0;

/** 可写回的纯文本格式：与服务端 PLAIN_TEXT_EXTS 保持一致。 */
const PLAIN_EXTS = new Set(['txt', 'text', 'log', 'csv', 'tsv']);

/** 是否纯文本/CSV（走 /api/plain-file 编辑，不注入 frontmatter）。 */
export function isPlainPath(path) {
  const name = String(path || '');
  const dot = name.lastIndexOf('.');
  if (dot < 0) return false;
  return PLAIN_EXTS.has(name.slice(dot + 1).toLowerCase());
}

/** 逐段 encodeURIComponent，避免中文/空格路径拼坏。 */
function fileUrl(path) {
  return '/file/' + String(path).split('/').map(encodeURIComponent).join('/');
}

function formatSize(bytes) {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function setMeta(text) {
  const meta = $('#file-viewer-meta');
  if (meta) meta.textContent = text || '';
}

function reset() {
  const body = $('#file-viewer-body');
  if (body) body.innerHTML = '';
}

export function initViewer() {
  $('#file-viewer-close')?.addEventListener('click', () => close(true));
  $('#file-viewer-edit')?.addEventListener('click', () => {
    if (currentPath) emit('edit-plain', currentPath);
  });
  $('#file-viewer-reload')?.addEventListener('click', () => {
    if (currentPath) open(currentPath, { refresh: true }).catch(reportError);
  });
}

export function isOpen() {
  const box = $('#file-viewer');
  return !!box && !box.classList.contains('hidden');
}

export function close(notify = false) {
  currentPath = null;
  currentReq += 1;
  const box = $('#file-viewer');
  if (box) box.classList.add('hidden');
  reset();
  const meta = $('#file-viewer-meta');
  if (meta) meta.textContent = '';
  $('#file-viewer-edit')?.classList.add('hidden');
  if (notify) emit('viewer-closed');
}

/**
 * 打开只读预览。图片/PDF 走 /file/ 直出，其余由后端转成已消毒 HTML。
 * @param {string} path vault 相对路径
 */
export async function open(path, { refresh = false } = {}) {
  const req = ++currentReq;
  currentPath = path;
  $('#editor-area')?.classList.add('hidden');
  $('#empty-state')?.classList.add('hidden');
  const box = $('#file-viewer');
  if (!box) return;
  box.classList.remove('hidden');

  const name = basename(path);
  $('#file-viewer-name').textContent = name;
  $('#file-viewer-name').title = path;
  const url = fileUrl(path);
  const dl = $('#file-viewer-download');
  const raw = $('#file-viewer-raw');
  if (dl) {
    dl.href = url;
    dl.setAttribute('download', name);
  }
  if (raw) raw.href = url;
  setMeta('加载中…');
  $('#file-viewer-edit')?.classList.add('hidden');

  const body = $('#file-viewer-body');
  body.innerHTML = '';
  const loading = document.createElement('div');
  loading.className = 'file-viewer__hint';
  loading.textContent = '正在转换…';
  body.appendChild(loading);

  try {
    const query = { path };
    if (refresh) query.refresh = 1;
    const data = await api.get('/api/preview', query);
    if (req !== currentReq) return;
    body.innerHTML = '';
    setMeta(`${(data.format || '').toUpperCase()} · ${formatSize(data.size)}${data.cached ? ' · 缓存' : ''}`);
    if (data.kind === 'html' && PLAIN_EXTS.has(String(data.format || '').toLowerCase())) {
      $('#file-viewer-edit')?.classList.remove('hidden');
    }

    if (data.kind === 'html') {
      const holder = document.createElement('div');
      holder.className = 'file-viewer__doc';
      holder.innerHTML = data.html;
      body.appendChild(holder);
    } else if (data.kind === 'image') {
      const img = document.createElement('img');
      img.className = 'file-viewer__image';
      img.src = url;
      img.alt = name;
      body.appendChild(img);
    } else if (data.kind === 'pdf') {
      const frame = document.createElement('iframe');
      frame.className = 'file-viewer__pdf';
      frame.src = url;
      frame.title = name;
      body.appendChild(frame);
    } else if (data.kind === 'unsupported') {
      const tip = document.createElement('div');
      tip.className = 'file-viewer__hint';
      tip.textContent = data.reason || '暂不支持该格式预览，可下载后用本地应用打开。';
      body.appendChild(tip);
      setMeta('无法预览');
    }
  } catch (err) {
    if (req !== currentReq) return;
    body.innerHTML = '';
    const box2 = document.createElement('div');
    box2.className = 'file-viewer__hint file-viewer__hint--error';
    box2.textContent = err && err.message ? err.message : '预览失败';
    const tip = document.createElement('p');
    tip.className = 'file-viewer__hint';
    tip.textContent = '可点击右上角「下载」用本地应用查看。';
    body.appendChild(box2);
    body.appendChild(tip);
    setMeta('无法预览');
  }
}
