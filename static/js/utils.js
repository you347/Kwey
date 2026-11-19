/* 通用工具：防抖、模糊匹配、路径处理、HTML 转义、Toast。 */

import { askConfirm } from './dialog.js';

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

/** Toast 提示（退场带淡出动画）。 */
export function toast(message, type = 'info', duration = 3200) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const el = document.createElement('div');
  el.className = `toast${type === 'error' ? ' toast--error' : ''}${type === 'success' ? ' toast--success' : ''}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add('toast--out');
    setTimeout(() => el.remove(), 200);
  }, duration);
}

/** 确认浮层（应用内样式化对话框，异步）。 */
export function confirmDialog(message, opts = {}) {
  return askConfirm({ title: opts.title || '请确认', message, ...opts });
}

/** 复制到剪贴板（Clipboard API 不可用时降级 execCommand）。 */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

/** 复制 vault 相对路径并提示结果（右键菜单/状态栏共用）。 */
export async function copyPath(path) {
  const ok = await copyText(path);
  toast(ok ? `路径已复制：${path}` : '复制失败：剪贴板不可用', ok ? 'success' : 'error', 1800);
}

/** 保存状态文案 → 语义 kind（状态圆点着色用）。 */
export function saveStateKind(text) {
  if (!text) return '';
  if (text.startsWith('已保存')) return 'saved';
  if (text === '保存失败') return 'error';
  return 'editing';
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

/**
 * 绑定"选择文件夹"按钮（仅 pywebview 桥可用时显示；纯浏览器拿不到绝对路径，保持隐藏）。
 * 选定后写入 inputId 对应输入框。
 */
export function bindFolderPicker(btnId, inputId) {
  let tries = 0;
  const tryBind = () => {
    const api = window.pywebview && window.pywebview.api;
    if (api && typeof api.pick_folder === 'function') {
      const btn = document.getElementById(btnId);
      const input = document.getElementById(inputId);
      if (!btn || !input) return;
      btn.classList.remove('hidden');
      btn.addEventListener('click', async () => {
        try {
          const path = await api.pick_folder();
          if (path) {
            input.value = path;
            input.dispatchEvent(new Event('change'));
            input.focus();
          }
        } catch { /* 用户取消或桥异常：忽略 */ }
      });
    } else if (tries++ < 12) {
      setTimeout(tryBind, 300);  // pywebview 桥异步注入，最多等 ~3.6s
    }
  };
  tryBind();
}
