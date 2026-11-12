/* 通用工具：防抖、模糊匹配、路径处理、HTML 转义、Toast。 */

/** 防抖：返回带 flush/cancel 的新函数。 */
export function debounce(fn, wait = 250) {
  let timer = null;
  function wrapped(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...args); }, wait);
  }
  wrapped.cancel = () => { clearTimeout(timer); timer = null; };
  wrapped.flush = (...args) => { wrapped.cancel(); fn(...args); };
  return wrapped;
}

/** HTML 转义。 */
export function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** vault 相对路径取文件名。 */
export function basename(p) {
  const parts = String(p || '').replace(/\\/g, '/').split('/');
  return parts[parts.length - 1] || '';
}

/** vault 相对路径取目录部分（无目录返回 ''）。 */
export function dirname(p) {
  const norm = String(p || '').replace(/\\/g, '/');
  const idx = norm.lastIndexOf('/');
  return idx === -1 ? '' : norm.slice(0, idx);
}

/** 去掉 .md 扩展名。 */
export function stripMd(name) {
  return String(name || '').replace(/\.(md|markdown)$/i, '');
}

/** 模糊匹配评分：query 为 text 的子序列则返回分数（越大越好），否则 -1。 */
export function fuzzyScore(query, text) {
  const q = String(query || '').toLowerCase();
  const t = String(text || '').toLowerCase();
  if (!q) return 0;
  if (!t.includes(q[0]) && t.indexOf(q[0]) === -1) return -1;
  let score = 0;
  let ti = 0;
  let streak = 0;
  for (const ch of q) {
    const found = t.indexOf(ch, ti);
    if (found === -1) return -1;
    streak = found === ti ? streak + 1 : 0;
    score += 1 + streak * 2;
    if (found === 0) score += 4;
    if (found > 0 && /[\s/\\-]/.test(t[found - 1])) score += 2;
    ti = found + 1;
  }
  return score - Math.floor(t.length / 20);
}

/** Toast 提示。 */
export function toast(message, type = 'info', duration = 3200) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const el = document.createElement('div');
  el.className = `toast${type === 'error' ? ' toast--error' : ''}${type === 'success' ? ' toast--success' : ''}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

/** 确认浮层（同步 confirm 简化版，M1 够用）。 */
export function confirmDialog(message) {
  return window.confirm(message);
}

/** 字数统计（中文按字、英文按词）。 */
export function wordCount(text) {
  if (!text) return 0;
  const cjk = (text.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g) || []).length;
  const words = (text.replace(/[\u4e00-\u9fff\u3400-\u4dbf]/g, ' ').match(/[A-Za-z0-9_]+/g) || []).length;
  return cjk + words;
}

export function $(sel, root = document) { return root.querySelector(sel); }
export function $all(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }
