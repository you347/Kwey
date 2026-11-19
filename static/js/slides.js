/* P2 幻灯片演示：把当前笔记按 --- / ## / # 切页后全屏逐页演示。
 * DOM 由本模块自建（不依赖 index.html 容器），每页正文交给后端渲染，
 * 单页渲染失败降级为 <pre> 纯文本，不阻断整场演示。
 * 分页纯函数在 slides-paging.js（零依赖，便于 node 自检）。
 */

import { api, authHeaders, renewToken } from './api.js';
import { state } from './state.js';
import { $, escapeHtml, toast } from './utils.js';
import { registerCommand } from './palette.js';
import { splitSlides } from './slides-paging.js';

const OVERLAY_ID = 'slides-overlay';

let initialized = false;
let pages = [];          // 每页 Markdown 原文
let index = 0;           // 当前页序号（0 起）
let opened = false;      // 是否处于演示中
let savedOverflow = '';  // 打开前的 body 滚动值，退出时还原
let renderTicket = 0;    // 渲染令牌：翻页/退出后丢弃迟到的渲染结果

/* ------------------------------------------------------------------ DOM */

/** 自建（或复用）演示层；只在首次创建时绑定一次点击。 */
function ensureOverlay() {
  const exist = document.getElementById(OVERLAY_ID);
  if (exist) return exist;

  const overlay = document.createElement('div');
  overlay.id = OVERLAY_ID;
  overlay.className = 'slides hidden';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', '幻灯片演示');
  overlay.innerHTML = `
    <div class="slides__stage">
      <article class="slide">
        <div class="slide__body markdown-body"></div>
      </article>
    </div>
    <div class="slides__bar">
      <button class="slides__btn" type="button" data-act="prev" title="上一页（←）">‹</button>
      <span class="slides__page"><span class="slides__cur">1</span> / <span class="slides__total">1</span></span>
      <button class="slides__btn" type="button" data-act="next" title="下一页（→）">›</button>
      <button class="slides__btn slides__btn--close" type="button" data-act="close" title="退出演示（Esc）">×</button>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', onOverlayClick);
  return overlay;
}

/** 点击遮罩空白处不退出（防误触）；只有工具条按钮生效。 */
function onOverlayClick(e) {
  const btn = e.target.closest('[data-act]');
  if (!btn || btn.disabled) return;
  const act = btn.dataset.act;
  if (act === 'prev') go(index - 1);
  else if (act === 'next') go(index + 1);
  else if (act === 'close') closeSlides();
  btn.blur();   // 鼠标点过按钮后释放焦点，避免随后 Space 既翻页又激活按钮（双跳页）
}

/** 同步页码与首末页按钮的 disabled 状态。 */
function updateBar() {
  const overlay = document.getElementById(OVERLAY_ID);
  if (!overlay) return;
  const cur = overlay.querySelector('.slides__cur');
  const total = overlay.querySelector('.slides__total');
  const prev = overlay.querySelector('[data-act="prev"]');
  const next = overlay.querySelector('[data-act="next"]');
  if (cur) cur.textContent = String(index + 1);
  if (total) total.textContent = String(pages.length);
  if (prev) prev.disabled = index <= 0;
  if (next) next.disabled = index >= pages.length - 1;
}

/* -------------------------------------------------------------- 渲染 */

/** 调后端渲染一页正文；401 时续签 token 重试一次。 */
async function renderMarkdown(markdown) {
  // 后端契约要求 path（用于解析 wikilink 相对路径），content 为待渲染正文
  const body = JSON.stringify({ path: state.activePath || '', content: markdown });
  const post = () => fetch('/api/note/render', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body,
  });

  let res = await post();
  if (res.status === 401 && await renewToken()) res = await post();

  const payload = await res.json().catch(() => null);
  if (!res.ok || !payload || payload.code !== 0) {
    const message = (payload && (payload.message || payload.detail)) || `HTTP ${res.status}`;
    throw new Error(message);
  }
  return (payload.data && payload.data.html) || '';
}

/** 渲染当前页：失败降级为 <pre>，绝不阻断演示。 */
async function renderPage() {
  const overlay = document.getElementById(OVERLAY_ID);
  const bodyEl = overlay && overlay.querySelector('.slide__body');
  if (!bodyEl) return;

  const markdown = pages[index] || '';
  const mine = ++renderTicket;
  if (!markdown.trim()) {
    bodyEl.innerHTML = '<div class="slide__empty">（本页无内容）</div>';
    return;
  }

  bodyEl.innerHTML = '<div class="slide__empty">渲染中…</div>';
  let html;
  try {
    html = await renderMarkdown(markdown);
  } catch {
    html = `<pre class="slide__fallback">${escapeHtml(markdown)}</pre>`;
  }
  if (mine !== renderTicket || !opened) return;   // 已翻页或已退出：丢弃旧结果
  bodyEl.innerHTML = html || `<pre class="slide__fallback">${escapeHtml(markdown)}</pre>`;
  bodyEl.scrollTop = 0;
}

/* -------------------------------------------------------------- 翻页 */

function go(next) {
  if (!opened || !pages.length) return;
  const target = Math.min(Math.max(next, 0), pages.length - 1);
  if (target === index) { updateBar(); return; }
  index = target;
  updateBar();
  renderPage();
}

/** 键盘：捕获阶段拦截，preventDefault + stopPropagation 防止落到 CodeMirror 与全局快捷键。 */
function onKeyDown(e) {
  if (!opened) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;   // 不劫持带修饰键的组合
  let handled = true;
  switch (e.key) {
    case 'ArrowLeft':
    case 'PageUp': go(index - 1); break;
    case 'ArrowRight':
    case 'PageDown':
    case ' ': go(index + 1); break;
    case 'Home': go(0); break;
    case 'End': go(pages.length - 1); break;
    case 'Escape': closeSlides(); break;
    default: handled = false;
  }
  if (!handled) return;
  e.preventDefault();
  e.stopPropagation();
}

/* -------------------------------------------------------------- 入口 */

/** 幂等初始化：注册命令，并保证演示层 DOM 就绪。 */
export function initSlides() {
  if (initialized) return;
  initialized = true;
  ensureOverlay();
  registerCommand({
    id: 'slides.present',
    title: '演示当前笔记（幻灯片）',
    // 命令面板同步调用，异步失败在此兜住，避免未捕获 rejection
    run: () => { openSlides().catch((err) => toast(`演示启动失败：${err.message || err}`, 'error')); },
  });
}

/** 打开演示：没有激活笔记时提示并返回。 */
export async function openSlides() {
  const path = state.activePath;
  if (!path) {
    toast('先打开一篇笔记', 'error');
    return;
  }
  if (opened) return;

  // 优先用已载入的文档缓存（与激活路径一致），否则回读磁盘
  let content = '';
  const doc = state.doc;
  if (doc && doc.path === path && typeof doc.content === 'string') {
    content = doc.content;
  } else {
    try {
      const loaded = await api.get('/api/note', { path });
      content = (loaded && loaded.content) || '';
    } catch (err) {
      toast(`读取笔记失败：${err.message || err}`, 'error');
      return;
    }
  }

  pages = splitSlides(content);
  if (!pages.length) {
    toast('这篇笔记是空的', 'error');
    return;
  }

  const overlay = ensureOverlay();
  index = 0;
  opened = true;
  savedOverflow = document.body.style.overflow || '';
  document.body.style.overflow = 'hidden';   // 演示期间锁背景滚动，退出时还原
  overlay.classList.remove('hidden');
  updateBar();
  renderPage();
  document.addEventListener('keydown', onKeyDown, true);
}

/** 退出演示：关层、还原滚动、丢弃迟到渲染、焦点还给编辑器。 */
export function closeSlides() {
  opened = false;
  renderTicket += 1;
  document.removeEventListener('keydown', onKeyDown, true);
  const overlay = document.getElementById(OVERLAY_ID);
  if (overlay) overlay.classList.add('hidden');
  document.body.style.overflow = savedOverflow;
  savedOverflow = '';
  pages = [];
  index = 0;

  // CodeMirror 接管时 #editor-textarea 不可见，优先聚焦它的隐藏输入域
  const editorInput = $('.CodeMirror textarea') || $('#editor-textarea');
  if (editorInput) editorInput.focus();
}

// 与 splash.js 一致的自举方式
initSlides();
