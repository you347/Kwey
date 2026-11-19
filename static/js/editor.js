/* 编辑器：CodeMirror(vendored CM5) 集成；加载失败自动降级增强 textarea。
 * 另提供光标状态、行跳转与 [[双链]] 行内补全（Obsidian 式轻提示）。
 * P1-19 起支持多实例：createEditor(container, onInput) 每容器一个独立实例；
 * 模块级同名导出为「主编辑区」兼容层，app.js 等现有调用零改动。 */

import { api } from './api.js';
import { state } from './state.js';
import { basename, stripMd } from './utils.js';

const VENDOR = '/static/vendor/codemirror/';
const MODE_SCRIPTS = [
  'mode/xml/xml.min.js',
  'mode/javascript/javascript.min.js',
  'mode/css/css.min.js',
  'mode/clike/clike.min.js',
  'mode/htmlmixed/htmlmixed.min.js',
  'mode/markdown/markdown.min.js',
  'addon/edit/closebrackets.min.js',
  'addon/selection/active-line.min.js',
];

/* ------------------------------------------------------------------ 加载 */
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`加载失败: ${src}`));
    document.head.appendChild(s);
  });
}

let vendorPromise = null;
/** vendored CM 全应用只加载一次，多实例共享。 */
function loadVendor() {
  if (!vendorPromise) {
    vendorPromise = (async () => {
      try {
        await loadScript(VENDOR + 'codemirror.min.js');
        for (const rel of MODE_SCRIPTS) await loadScript(VENDOR + rel);
        window.KweyVendorOK = true;
      } catch {
        window.KweyVendorOK = false;
      }
      return Boolean(window.KweyVendorOK && window.CodeMirror);
    })();
  }
  return vendorPromise;
}

/* ------------------------------------------------------------------ 实例工厂 */
/**
 * 创建一个独立编辑器实例。container 内须含 textarea（可选 .editor-gutter /
 * .fallback-editor 降级行号）。vendor 加载完成后 resolve 出操作句柄。
 * @param {HTMLElement} container 编辑源码窗格容器
 * @param {(content: string) => void} onInput 内容变化回调（调用方做防抖保存）
 * @returns {Promise<{getValue,setValue,focus,onCursor,scrollToLine,insertText,enableLinkHints,refresh,destroy,getCursor,preserveCursor,isCM}>}
 */
export async function createEditor(container, onInput) {
  const ta = container.querySelector('textarea');
  const gutter = container.querySelector('.editor-gutter');
  if (!ta) throw new Error('createEditor: container 内缺少 textarea');

  let cm = null;            // 本实例的 CodeMirror（可用时）
  let suppressInput = false;
  let destroyed = false;
  const taCursorHandlers = [];

  // [[ 补全提示：每实例独立，避免两屏互相抢提示框
  let hintEl = null;
  let hintItems = [];
  let hintIndex = 0;
  let hintStart = null;
  let hintTimer = null;

  const emitInput = () => {
    if (!suppressInput && !destroyed && onInput) onInput(handle.getValue());
  };
  const onTaInput = () => { updateGutter(); emitInput(); };
  ta.addEventListener('input', onTaInput);

  function updateGutter() {
    if (!gutter) return;
    const lines = Math.max(ta.value.split('\n').length, 1);
    const nums = [];
    for (let i = 1; i <= lines; i++) nums.push(i);
    gutter.textContent = nums.join('\n');
  }

  function getCursor() {
    if (cm) {
      const c = cm.getCursor();
      return { line: c.line + 1, ch: c.ch + 1 };
    }
    const before = ta.value.slice(0, ta.selectionStart ?? 0).split('\n');
    return { line: before.length, ch: before[before.length - 1].length + 1 };
  }

  function getSelectionLength() {
    if (cm) return cm.getSelection().length;
    return Math.abs((ta.selectionEnd ?? 0) - (ta.selectionStart ?? 0));
  }

  /* ---------------- [[ 双链补全（仅 CodeMirror 模式） ---------------- */
  function detectHint() {
    const cur = cm.getCursor();
    const before = cm.getLine(cur.line).slice(0, cur.ch);
    const m = /\[\[([^\]\n|]*)$/.exec(before);
    if (!m) { hideHint(); return; }
    hintStart = { line: cur.line, ch: cur.ch - m[0].length };
    const query = m[1];
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => fetchHints(query), 140);
  }

  async function fetchHints(query) {
    try {
      const data = await api.get('/api/search', { q: query ? `file:${query}` : '', limit: 8 });
      showHint(data.hits || []);
    } catch { hideHint(); }
  }

  function showHint(hits) {
    hideHint();
    if (!hits.length || !cm || destroyed) return;
    hintItems = hits;
    hintIndex = 0;
    const coords = cm.cursorCoords(true, 'page');
    hintEl = document.createElement('div');
    hintEl.className = 'link-hint';
    hintEl.style.left = `${Math.min(coords.left, window.innerWidth - 290)}px`;
    hintEl.style.top = `${coords.bottom + 4}px`;
    hintEl.innerHTML = hintItems.map((hit, i) => `
      <div class="link-hint__item${i === 0 ? ' link-hint__item--active' : ''}" data-i="${i}">
        ${escapeText(stripMd(basename(hit.path)))} <span class="panel-list__meta">${escapeText(hit.path)}</span>
      </div>`).join('');
    hintEl.querySelectorAll('.link-hint__item').forEach((el) => {
      el.addEventListener('mousedown', (e) => { e.preventDefault(); hintIndex = Number(el.dataset.i); applyHint(); });
    });
    document.body.appendChild(hintEl);
  }

  function moveHint(delta) {
    if (!hintEl) return;
    hintIndex = (hintIndex + delta + hintItems.length) % hintItems.length;
    hintEl.querySelectorAll('.link-hint__item').forEach((el, i) => {
      el.classList.toggle('link-hint__item--active', i === hintIndex);
    });
  }

  function applyHint() {
    if (!hintEl || !cm || !hintStart) { hideHint(); return; }
    const hit = hintItems[hintIndex];
    const cur = cm.getCursor();
    if (!hit || cur.line !== hintStart.line || cur.ch < hintStart.ch) { hideHint(); return; }
    const label = stripMd(basename(hit.path));
    cm.replaceRange(`[[${label}]]`, hintStart, cur);
    hideHint();
    cm.focus();
  }

  function hideHint() {
    clearTimeout(hintTimer);
    if (hintEl) { hintEl.remove(); hintEl = null; }
    hintItems = [];
    hintStart = null;
  }

  const handle = {
    getValue() {
      return cm ? cm.getValue() : ta.value;
    },

    setValue(text) {
      suppressInput = true;
      try {
        if (cm) cm.setValue(text || '');
        else { ta.value = text || ''; updateGutter(); }
      } finally {
        suppressInput = false;
      }
      hideHint();
    },

    /** 光标处插入文本（附件粘贴/拖入、模板插入用）。 */
    insertText(text) {
      if (cm) {
        cm.replaceSelection(text);
        cm.focus();
      } else {
        const start = ta.selectionStart ?? ta.value.length;
        const end = ta.selectionEnd ?? start;
        ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
        ta.selectionStart = ta.selectionEnd = start + text.length;
        ta.focus();
      }
      if (onInput) onInput(handle.getValue());
    },

    focus() {
      if (cm) cm.focus();
      else ta.focus();
    },

    /** 模式切换时保持光标位置合理（源码模式回到编辑焦点）。 */
    preserveCursor() {
      if (cm) { cm.refresh(); cm.focus(); }
      else ta.focus();
    },

    getCursor,

    /** 订阅光标移动（状态栏行列 + 选区字数显示）。 */
    onCursor(cb) {
      const emitCursor = () => cb({ ...getCursor(), selLen: getSelectionLength() });
      if (cm) cm.on('cursorActivity', emitCursor);
      ta.addEventListener('keyup', emitCursor);
      ta.addEventListener('click', emitCursor);
      ta.addEventListener('select', emitCursor);
      taCursorHandlers.push(emitCursor);
      emitCursor();
    },

    /** 跳到指定 1-based 行（大纲面板点击）。 */
    scrollToLine(line) {
      const n = Math.max(1, Number(line) || 1);
      if (cm) {
        cm.setCursor({ line: n - 1, ch: 0 });
        cm.scrollIntoView({ line: n - 1, ch: 0 }, 80);
        cm.focus();
      } else {
        const lines = ta.value.split('\n');
        const pos = lines.slice(0, n - 1).reduce((sum, s) => sum + s.length + 1, 0);
        ta.focus();
        ta.setSelectionRange(pos, pos);
      }
    },

    /** 启用 [[ 触发的笔记名补全（仅 CodeMirror 模式；降级 textarea 功能不受影响）。 */
    enableLinkHints() {
      if (!cm) return;
      cm.on('change', detectHint);
      cm.on('cursorActivity', detectHint);
      cm.on('keydown', (_instance, ev) => {
        if (!hintEl) return;
        if (ev.key === 'ArrowDown') { ev.preventDefault(); moveHint(1); }
        else if (ev.key === 'ArrowUp') { ev.preventDefault(); moveHint(-1); }
        else if (ev.key === 'Enter') { ev.preventDefault(); applyHint(); }
        else if (ev.key === 'Escape') { hideHint(); }
      });
    },

    /** 容器尺寸/可见性变化后重排（分屏拖拽、模式切换）。 */
    refresh() {
      if (cm) cm.refresh();
    },

    isCM() { return cm !== null; },

    destroy() {
      if (destroyed) return;
      destroyed = true;
      hideHint();
      ta.removeEventListener('input', onTaInput);
      for (const h of taCursorHandlers) {
        ta.removeEventListener('keyup', h);
        ta.removeEventListener('click', h);
        ta.removeEventListener('select', h);
      }
      if (cm) { cm.toTextArea(); cm = null; }
      ta.classList.remove('hidden');
      if (gutter) gutter.classList.remove('hidden');
    },
  };

  if (await loadVendor()) {
    cm = window.CodeMirror.fromTextArea(ta, {
      mode: 'markdown',
      theme: 'kwey',
      lineNumbers: true,
      lineWrapping: true,
      styleActiveLine: true,
      autoCloseBrackets: true,
      viewportMargin: Infinity,
      extraKeys: {
        // 交还给 document 上的全局保存快捷键（CodeMirror.Pass 不吞事件）
        'Ctrl-S': () => window.CodeMirror.Pass,
      },
    });
    cm.on('change', emitInput);
    // CM 包装器插在 .fallback-editor 内部：只能隐藏 textarea/行号，不能隐藏整个降级容器
    ta.classList.add('hidden');
    if (gutter) gutter.classList.add('hidden');
  } else {
    // 降级：增强 textarea（行号 + 等宽 + 保存/预览功能完整）
    window.KweyVendorOK = false;
    updateGutter();
  }

  return handle;
}

/* ------------------------------------------------------------------ 主编辑区兼容层 */
let main = null;

/**
 * 初始化主编辑区。container 内须含 #editor-textarea / #editor-gutter。
 * @param {HTMLElement} container 编辑源码窗格容器
 * @param {(content: string) => void} onInput 内容变化回调（app.js 做防抖保存）
 */
export async function initEditor(container, onInput) {
  main = await createEditor(container, onInput);
}

/** 主编辑区句柄（splitview 拖拽后 refresh 用）；未初始化返回 null。 */
export function getMainEditor() { return main; }

export function isReady() { return main !== null; }

/** 字号/主题变化后重排 CodeMirror（行高与光标位置依赖字体度量）。 */
export function refreshLayout() {
  if (main && typeof main.refresh === 'function') main.refresh();
}
export function isCM() { return main ? main.isCM() : false; }

export function getValue() {
  return main ? main.getValue() : '';
}

export function setValue(text) {
  if (main) main.setValue(text);
}

export function insertText(text) {
  if (main) main.insertText(text);
}

/** 分屏时焦点在副屏则不抢焦点（WS 重载/409 重载不打断另一屏输入）。 */
function secondaryHasFocus() {
  if (!state.split || !state.split.open) return false;
  const sec = document.getElementById('editor-secondary');
  return !!sec && sec.contains(document.activeElement);
}

export function focus() {
  if (main && !secondaryHasFocus()) main.focus();
}

export function preserveCursor() {
  if (!main) return;
  if (secondaryHasFocus()) { main.refresh(); return; }
  main.preserveCursor();
}

export function getCursor() {
  return main ? main.getCursor() : { line: 1, ch: 1 };
}

export function onCursor(cb) {
  if (main) main.onCursor(cb);
}

export function scrollToLine(line) {
  if (main) main.scrollToLine(line);
}

export function enableLinkHints() {
  if (main) main.enableLinkHints();
}

function escapeText(text) {
  const div = document.createElement('div');
  div.textContent = String(text == null ? '' : text);
  return div.innerHTML;
}
