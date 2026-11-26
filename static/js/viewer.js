/* 只读预览：txt/csv/docx/xlsx/xls/pdf/图片 在编辑区上方以查看器呈现，不进入编辑态。 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, basename } from './utils.js';

let currentPath = null;
let currentReq = 0;

/** 可写回的纯文本格式：与服务端 PLAIN_TEXT_EXTS 保持一致。 */
const PLAIN_EXTS = new Set(['txt', 'text', 'log', 'csv', 'tsv']);

/** 附件索引未完整建立时的可见提示（预览/下载不受影响）。 */
const INDEX_HINTS = {
  too_large: '该文件超过 20MB，未被全文索引；仍可预览与下载。',
  truncated: '该文件较大，仅索引了前 20 万字；仍可预览与下载。',
  empty: '未能抽取文本，未建立全文索引；仍可预览与下载。',
  missing_converter: '缺少 LibreOffice，未建立全文索引；仍可预览与下载。',
  failed: '文本抽取失败，未建立全文索引；仍可预览与下载。',
};

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

/** 图片查看器：适应窗口 / 原始尺寸 / 缩放，并显示百分比。 */
function renderImage(body, src, name, data) {
  const bar = document.createElement('div');
  bar.className = 'file-viewer__imagebar';
  const label = document.createElement('span');
  label.className = 'file-viewer__zoom';

  const img = document.createElement('img');
  img.className = 'file-viewer__image is-fit';
  img.src = src;
  img.alt = name;
  const view = { mode: 'fit', scale: 1, nw: Number(data.width) || 0, nh: Number(data.height) || 0 };

  function currentScale() {
    if (view.mode === 'fit') {
      const natural = view.nw || img.naturalWidth;
      return natural && img.clientWidth ? img.clientWidth / natural : 1;
    }
    return view.scale;
  }

  function updateLabel() {
    const scale = currentScale();
    label.textContent = `${Math.round(scale * 100)}%`;
  }

  function fit() {
    view.mode = 'fit';
    img.classList.add('is-fit');
    img.classList.remove('is-free');
    img.style.width = '';
    img.style.height = '';
    window.requestAnimationFrame(updateLabel);
  }

  function original() {
    view.mode = 'original';
    view.scale = 1;
    img.classList.remove('is-fit');
    img.classList.add('is-free');
    img.style.width = `${view.nw || img.naturalWidth}px`;
    img.style.height = 'auto';
    updateLabel();
  }

  function zoom(delta) {
    const base = currentScale() || 1;
    view.scale = Math.min(8, Math.max(0.1, base + delta));
    view.mode = 'free';
    img.classList.remove('is-fit');
    img.classList.add('is-free');
    img.style.width = `${Math.round((view.nw || img.naturalWidth) * view.scale)}px`;
    img.style.height = 'auto';
    updateLabel();
  }

  const mkBtn = (text, handler) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--mini btn--ghost';
    btn.textContent = text;
    btn.addEventListener('click', handler);
    return btn;
  };
  bar.appendChild(mkBtn('适应窗口', fit));
  bar.appendChild(mkBtn('原始尺寸', original));
  bar.appendChild(mkBtn('−', () => zoom(-0.25)));
  bar.appendChild(label);
  bar.appendChild(mkBtn('＋', () => zoom(0.25)));

  img.addEventListener('load', () => {
    view.nw = img.naturalWidth || view.nw;
    view.nh = img.naturalHeight || view.nh;
    if (view.mode === 'fit') window.requestAnimationFrame(updateLabel);
  }, { once: true });
  img.addEventListener('dblclick', () => (view.mode === 'fit' ? original() : fit()));

  body.appendChild(bar);
  body.appendChild(img);
  window.requestAnimationFrame(updateLabel);
}

function imageMeta(data) {
  const parts = [(data.format || '').toUpperCase(), formatSize(data.size)];
  if (data.width && data.height) parts.push(`${data.width} × ${data.height}`);
  if (data.converted) parts.push('已转换');
  if (data.index_status && data.index_status.state === 'not_applicable') {
    parts.push('索引：不适用（图片）');
  }
  if (data.cached) parts.push('缓存');
  return parts.filter(Boolean).join(' · ');
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
    const metaText = data.kind === 'image'
      ? imageMeta(data)
      : `${(data.format || '').toUpperCase()} · ${formatSize(data.size)}${data.cached ? ' · 缓存' : ''}`;
    setMeta(metaText);
    const indexState = data.index_status && data.index_status.state;
    if (indexState && indexState !== 'indexed' && indexState !== 'not_applicable') {
      const notice = document.createElement('div');
      notice.className = 'file-viewer__notice';
      notice.textContent = INDEX_HINTS[data.index_status.state]
        || '该文件未被完整索引；仍可预览与下载。';
      if (data.index_status.detail) notice.title = data.index_status.detail;
      body.appendChild(notice);
    }
    if (data.kind === 'html' && PLAIN_EXTS.has(String(data.format || '').toLowerCase())) {
      $('#file-viewer-edit')?.classList.remove('hidden');
    }

    if (data.kind === 'html') {
      const holder = document.createElement('div');
      holder.className = 'file-viewer__doc';
      holder.innerHTML = data.html;
      body.appendChild(holder);
    } else if (data.kind === 'image') {
      renderImage(body, data.url || url, name, data);
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
