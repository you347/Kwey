/* 链接悬浮预览卡（P1-20）：预览区 wikilink 停留 350ms 出卡，
 * 显示标题/首段摘要/反链数；断链变体虚线提示。事件委托 survives 预览重渲染。
 * 浮层纪律：z-index 140、阴影仅 var(--shadow)、reduced-motion 降级。 */

import { api } from './api.js';
import { emit, on } from './state.js';
import { escapeHtml, stripMd, basename } from './utils.js';

const SHOW_DELAY = 350;
const HIDE_GRACE = 120;
const EXCERPT_LEN = 120;

let root = null;
let cardEl = null;
let showTimer = null;
let hideTimer = null;
let currentLink = null;
let fetchSeq = 0;

const resolveCache = new Map();   // target -> {exists, path}
const excerptCache = new Map();   // path -> {mtime, title, excerpt, backlinks}

export function initLinkPreview() {
  root = document.querySelector('#editor-preview');
  if (!root) return;
  // 委托：预览每次按键重渲染 innerHTML，逐次 bind 会漏
  root.addEventListener('mouseover', onOver);
  root.addEventListener('mouseout', onOut);
  root.addEventListener('focusin', onFocusIn);
  root.addEventListener('click', onClickElsewhere);
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('scroll', onScroll, true);
  const clear = () => { resolveCache.clear(); excerptCache.clear(); };
  on('ws:created', clear);
  on('ws:moved', clear);
  on('ws:deleted', clear);
  on('ws:updated', clear);
  on('ws:links_changed', clear);
}

function linkOf(e) {
  const el = e.target.closest ? e.target.closest('.kwey-wikilink') : null;
  return el && root.contains(el) ? el : null;
}

function onOver(e) {
  const link = linkOf(e);
  if (!link) return;
  clearTimeout(hideTimer);
  if (currentLink === link && cardEl) return;  // 已出卡，避免重复 show 闪烁
  clearTimeout(showTimer);
  currentLink = link;
  showTimer = setTimeout(() => show(link), SHOW_DELAY);
}

function onOut(e) {
  const link = linkOf(e);
  if (!link) return;
  // 指针移入卡内不隐藏
  if (cardEl && e.relatedTarget && cardEl.contains(e.relatedTarget)) return;
  clearTimeout(showTimer);
  scheduleHide();
}

function onFocusIn(e) {
  const link = linkOf(e);
  if (!link) return;
  clearTimeout(showTimer);
  currentLink = link;
  show(link);   // 键盘焦点立即出卡（a11y）
}

function onClickElsewhere(e) {
  if (cardEl && !cardEl.contains(e.target) && !linkOf(e)) hide();
}

function onKey(e) {
  if (e.key === 'Escape' && cardEl) { e.stopPropagation(); hide(); }
}

function onScroll() { if (cardEl) hide(); }

function scheduleHide() {
  clearTimeout(hideTimer);
  hideTimer = setTimeout(hide, HIDE_GRACE);
}

function removeCard() {
  if (cardEl) { cardEl.remove(); cardEl = null; }
}

function hide() {
  clearTimeout(showTimer);
  clearTimeout(hideTimer);
  fetchSeq++;
  removeCard();
  currentLink = null;
}

async function resolve(target) {
  if (resolveCache.has(target)) return resolveCache.get(target);
  const r = await api.get('/api/note/resolve', { target });
  resolveCache.set(target, r);
  return r;
}

function firstParagraph(content) {
  const para = String(content || '').split(/\n\s*\n/).find((b) => b.trim()) || '';
  return para
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/!?\[\[([^\]|]*)(\|[^\]]*)?\]\]/g, '$1')
    .replace(/[*_`>#]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, EXCERPT_LEN);
}

async function excerpt(path) {
  const hit = excerptCache.get(path);
  if (hit) return hit;
  const [note, back] = await Promise.all([
    api.get('/api/note', { path }),
    api.get('/api/graph/backlinks', { path }),
  ]);
  const entry = {
    title: note.title || stripMd(basename(path)),
    excerpt: firstParagraph(note.content),
    backlinks: back.count || 0,
  };
  excerptCache.set(path, entry);
  return entry;
}

async function show(link) {
  const target = link.dataset.target;
  if (!target) return;
  const seq = ++fetchSeq;
  let resolved;
  try { resolved = await resolve(target); } catch { return; }
  if (seq !== fetchSeq || currentLink !== link) return;

  removeCard();     // 单实例：先清旧卡（不动 currentLink / fetchSeq）
  cardEl = document.createElement('div');
  cardEl.className = 'link-card';
  cardEl.setAttribute('role', 'tooltip');

  if (!resolved.exists) {
    cardEl.classList.add('link-card--broken');
    cardEl.innerHTML = `
      <div class="link-card__title">${escapeHtml(stripMd(target.split('|')[0].split('#')[0]))}</div>
      <div class="link-card__missing">目标不存在</div>`;
  } else {
    let info = null;
    try { info = await excerpt(resolved.path); } catch { /* 读失败降级为仅标题 */ }
    if (seq !== fetchSeq) return;
    cardEl.innerHTML = `
      <div class="link-card__title">${escapeHtml(info ? info.title : stripMd(basename(resolved.path)))}</div>
      ${info && info.excerpt ? `<div class="link-card__excerpt">${escapeHtml(info.excerpt)}</div>` : ''}
      <div class="link-card__meta">${info ? info.backlinks : 0} 条反链</div>`;
    cardEl.addEventListener('click', () => { emit('open-note', resolved.path); hide(); });
  }

  cardEl.addEventListener('mouseenter', () => clearTimeout(hideTimer));
  cardEl.addEventListener('mouseleave', scheduleHide);
  document.body.appendChild(cardEl);
  place(link);
}

function place(link) {
  const r = link.getBoundingClientRect();
  const rect = cardEl.getBoundingClientRect();
  const x = Math.max(8, Math.min(r.left, window.innerWidth - rect.width - 8));
  let y = r.bottom + 6;
  if (y + rect.height > window.innerHeight - 8) y = Math.max(8, r.top - rect.height - 6);
  cardEl.style.left = `${x}px`;
  cardEl.style.top = `${y}px`;
}