/* F4 网页剪藏：浮层 #overlay-clip（URL + 目标目录 + 标签 + 抓取预览 + 落盘）。
 * 命令 id: clip.open。
 *
 * 浮层 DOM 由本模块运行时挂载（若 index.html 已提供 #overlay-clip 则直接复用），
 * 不依赖共享文件改动；抓取与正文抽取全部在后端完成（含 SSRF 拦截）。 */

import { api, reportError } from './api.js';
import { state, emit } from './state.js';
import { $, escapeHtml, toast } from './utils.js';
import { registerCommand } from './palette.js';

export const CLIP_COMMAND_ID = 'clip.open';

const PREVIEW_CHARS = 4000;

let initialized = false;
let overlayEl = null;
let urlEl = null;
let dirEl = null;
let tagsEl = null;
let statusEl = null;
let previewEl = null;
let previewBtn = null;
let saveBtn = null;
let lastPreview = null;
let busy = false;

/* ------------------------------------------------------------------ 初始化 */
export function initClipper() {
  if (initialized) return;
  initialized = true;

  registerCommand({
    id: CLIP_COMMAND_ID,
    title: '剪藏网页到知识库',
    run: () => openClipper(),
  });

  mountOverlay();
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlayEl && !overlayEl.classList.contains('hidden')) closeClipper();
  });
  // 设置的剪藏目录变化后同步输入框默认值
  document.addEventListener('change', (e) => {
    if (e.target && e.target.id === 'settings-clip-dir' && dirEl) {
      dirEl.value = e.target.value || 'clips';
    }
  });
}

/** 挂载浮层（复用已有 #overlay-clip，避免第二次插入）。 */
function mountOverlay() {
  overlayEl = $('#overlay-clip');
  if (!overlayEl) {
    overlayEl = document.createElement('div');
    overlayEl.id = 'overlay-clip';
    overlayEl.className = 'overlay hidden';
    overlayEl.innerHTML = `
      <div class="overlay__panel overlay__panel--wide">
        <div class="graph-head">
          <span>网页剪藏</span>
          <span id="clip-status" class="graph-head__stats">先预览，确认后再写入</span>
          <button id="btn-clip-close" class="btn btn--ghost" type="button">关闭</button>
        </div>
        <div class="organize-form">
          <input id="clip-url" type="text" autocomplete="off"
            placeholder="https://example.com/article（仅 http/https 公网地址）"/>
          <div class="settings-row settings-row--inline">
            <label>目标目录 <input id="clip-dir" type="text" placeholder="clips"/></label>
            <label>标签 <input id="clip-tags" type="text" placeholder="剪藏, 阅读"/></label>
          </div>
          <div class="settings-row settings-row--inline">
            <button id="btn-clip-preview" class="btn btn--primary" type="button">预览抓取结果</button>
            <button id="btn-clip-save" class="btn" type="button" disabled>保存剪藏</button>
          </div>
        </div>
        <div id="clip-preview" class="overlay__results">
          <div class="panel-empty">粘贴链接后点「预览抓取结果」，正文会转成 Markdown 显示在这里。</div>
        </div>
        <div class="overlay__hint">正文优先取 article/main；同一 URL 重复剪藏会原地更新 · <kbd>Esc</kbd> 关闭</div>
      </div>`;
    document.body.appendChild(overlayEl);

    overlayEl.addEventListener('mousedown', (e) => {
      if (e.target === overlayEl) closeClipper();
    });
    const closeBtn = $('#btn-clip-close', overlayEl);
    if (closeBtn) closeBtn.addEventListener('click', closeClipper);
  }

  urlEl = $('#clip-url', overlayEl);
  dirEl = $('#clip-dir', overlayEl);
  tagsEl = $('#clip-tags', overlayEl);
  statusEl = $('#clip-status', overlayEl);
  previewEl = $('#clip-preview', overlayEl);
  previewBtn = $('#btn-clip-preview', overlayEl);
  saveBtn = $('#btn-clip-save', overlayEl);

  if (previewBtn) previewBtn.addEventListener('click', () => preview().catch(reportError));
  if (saveBtn) saveBtn.addEventListener('click', () => save().catch(reportError));
  if (urlEl) {
    urlEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); preview().catch(reportError); }
    });
    urlEl.addEventListener('input', () => {
      lastPreview = null;
      if (saveBtn) saveBtn.disabled = true;
    });
  }
}

/* ------------------------------------------------------------------ 开关 */
export function openClipper() {
  if (!overlayEl) mountOverlay();
  if (!overlayEl) { toast('剪藏浮层不可用', 'error'); return; }
  if (dirEl) dirEl.value = dirEl.value || state.settings.clip_dir || 'clips';
  if (statusEl) statusEl.textContent = '先预览，确认后再写入';
  overlayEl.classList.remove('hidden');
  if (urlEl) { urlEl.focus(); urlEl.select(); }
}

export function closeClipper() {
  if (overlayEl) overlayEl.classList.add('hidden');
}

/* ------------------------------------------------------------------ 预览 */
function buildBody() {
  return {
    url: (urlEl && urlEl.value.trim()) || '',
    path: (dirEl && dirEl.value.trim()) || undefined,
    tags: (tagsEl && tagsEl.value.trim()) || undefined,
  };
}

function localValidate(body) {
  if (!body.url) { toast('请先粘贴网页链接', 'error'); return false; }
  if (!/^https?:\/\//i.test(body.url)) { toast('仅支持 http/https 链接', 'error'); return false; }
  return true;
}

async function preview() {
  if (busy) return;
  const body = buildBody();
  if (!localValidate(body)) return;
  busy = true;
  setBusy(true, '正在抓取并抽取正文…');
  try {
    const data = await api.post('/api/clip/preview', body);
    lastPreview = data;
    renderPreview(data);
    if (saveBtn) saveBtn.disabled = false;
    setBusy(false, `预览完成：${data.chars} 字${data.container ? ` · 容器 ${data.container}` : ''}`);
  } catch (err) {
    lastPreview = null;
    if (saveBtn) saveBtn.disabled = true;
    renderError(err);
    setBusy(false, '预览失败');
    throw err;
  } finally {
    busy = false;
  }
}

function renderPreview(data) {
  if (!previewEl) return;
  const md = String(data.markdown || '');
  const shown = md.length > PREVIEW_CHARS ? `${md.slice(0, PREVIEW_CHARS)}\n\n…（预览截断，完整 ${md.length} 字将写入笔记）` : md;
  const exists = data.existing_path
    ? `<span style="color:var(--accent)">已剪藏过该链接，保存将原地更新 ${escapeHtml(data.existing_path)}</span>`
    : `将写入 <code>${escapeHtml(data.suggested_path || '')}</code>`;
  previewEl.innerHTML = `
    <div style="padding:8px 10px 4px">
      <div style="font-weight:600;font-size:var(--fs-sm)">${escapeHtml(data.title || '（无标题）')}</div>
      <div class="result-item__path" style="margin:2px 0 6px">${escapeHtml(data.source || '')}</div>
      <div class="result-item__path">${exists}</div>
      ${(data.tags && data.tags.length) ? `<div class="result-item__path">标签：${data.tags.map((t) => escapeHtml(t)).join(' · ')}</div>` : ''}
    </div>
    <pre style="white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Consolas,monospace;font-size:12px;line-height:1.6;background:var(--code-bg);padding:10px;border-radius:6px;margin:0 10px 10px;max-height:44vh;overflow:auto">${escapeHtml(shown)}</pre>`;
}

function renderError(err) {
  if (!previewEl) return;
  previewEl.innerHTML = `<div class="panel-empty" style="padding:8px 10px">抓取失败：${escapeHtml(err.message || String(err))}</div>`;
}

function setBusy(flag, status) {
  if (statusEl && status) statusEl.textContent = status;
  if (previewBtn) previewBtn.disabled = flag;
  if (saveBtn) saveBtn.disabled = flag || !lastPreview;
}

/* ------------------------------------------------------------------ 落盘 */
async function save() {
  if (busy) return;
  const body = buildBody();
  if (!localValidate(body)) return;
  busy = true;
  setBusy(true, '正在写入剪藏笔记…');
  try {
    const data = await api.post('/api/clip/url', body);
    toast(
      data.deduped ? `已更新剪藏：${data.path}` : `剪藏已保存：${data.path}`,
      'success',
    );
    closeClipper();
    emit('tree-refresh');
    emit('open-note', data.path);
  } catch (err) {
    setBusy(false, `保存失败：${err.message || err}`);
    throw err;
  } finally {
    busy = false;
  }
}
