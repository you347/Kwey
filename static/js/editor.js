/* 编辑器：CodeMirror(vendored CM5) 集成；下载/加载失败自动降级增强 textarea
 * （行号 + 基础样式保留，保存/预览/双链功能不缩水）。防抖自动保存由 app.js 编排。 */

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

let cm = null;            // CodeMirror 实例（可用时）
let ta = null;            // 降级 textarea
let gutter = null;        // 降级行号
let fallbackWrap = null;
let ready = false;
let inputCallback = null;
let suppressInput = false;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`加载失败: ${src}`));
    document.head.appendChild(s);
  });
}

/**
 * 初始化编辑器。container 内须含 #editor-textarea / #editor-gutter。
 * @param {HTMLElement} container 编辑源码窗格容器
 * @param {(content: string) => void} onInput 内容变化回调（app.js 做防抖保存）
 */
export async function initEditor(container, onInput) {
  inputCallback = onInput;
  ta = container.querySelector('#editor-textarea');
  gutter = container.querySelector('#editor-gutter');
  fallbackWrap = container.querySelector('.fallback-editor');

  ta.addEventListener('input', () => {
    updateGutter();
    if (!suppressInput && inputCallback) inputCallback(ta.value);
  });

  try {
    await loadScript(VENDOR + 'codemirror.min.js');
    for (const rel of MODE_SCRIPTS) await loadScript(VENDOR + rel);
    window.KweyVendorOK = true;
  } catch {
    window.KweyVendorOK = false;
  }

  if (window.KweyVendorOK && window.CodeMirror) {
    cm = window.CodeMirror.fromTextArea(ta, {
      mode: 'markdown',
      theme: 'kwey',
      lineNumbers: true,
      lineWrapping: true,
      styleActiveLine: true,
      autoCloseBrackets: true,
      viewportMargin: Infinity,
      extraKeys: {
        'Ctrl-S': () => { /* 保存由 app.js 全局快捷键处理 */ },
      },
    });
    cm.on('change', () => {
      if (!suppressInput && inputCallback) inputCallback(cm.getValue());
    });
    if (fallbackWrap) fallbackWrap.classList.add('hidden');
  } else {
    // 降级：增强 textarea（行号 + 等宽 + 保存/预览功能完整）
    window.KweyVendorOK = false;
    updateGutter();
  }
  ready = true;
}

export function isReady() { return ready; }
export function isCM() { return cm !== null; }

export function getValue() {
  return cm ? cm.getValue() : (ta ? ta.value : '');
}

export function setValue(text) {
  suppressInput = true;
  try {
    if (cm) cm.setValue(text || '');
    else if (ta) { ta.value = text || ''; updateGutter(); }
  } finally {
    suppressInput = false;
  }
}

/** 光标处插入文本（附件粘贴/拖入用）。 */
export function insertText(text) {
  if (cm) {
    cm.replaceSelection(text);
    cm.focus();
  } else if (ta) {
    const start = ta.selectionStart ?? ta.value.length;
    const end = ta.selectionEnd ?? start;
    ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
    ta.selectionStart = ta.selectionEnd = start + text.length;
    ta.focus();
  }
  if (inputCallback) inputCallback(getValue());
}

export function focus() {
  if (cm) cm.focus();
  else if (ta) ta.focus();
}

/** 模式切换时保持光标位置合理（源码模式回到编辑焦点）。 */
export function preserveCursor() {
  if (cm) { cm.refresh(); cm.focus(); }
}

function updateGutter() {
  if (!ta || !gutter) return;
  const lines = Math.max(ta.value.split('\n').length, 1);
  const nums = [];
  for (let i = 1; i <= lines; i++) nums.push(i);
  gutter.textContent = nums.join('\n');
}
