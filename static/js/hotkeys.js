/* 快捷键自定义：默认映射 + 设置页录制 + 全局绑定（P1-20）。
 * 映射持久化在 .kwey/settings.json 的 hotkeys 字段；冲突检测在录制时完成。 */

import { state } from './state.js';
import { saveSettings } from './settings/index.js';
import { toast, $, escapeHtml } from './utils.js';
import * as palette from './palette.js';

const LABELS = {
  search: '搜索笔记',
  switcher: '快速切换笔记',
  palette: '命令面板',
  'new-note': '新建笔记',
  capture: '快速捕获（收件箱）',
  save: '保存当前笔记',
  'close-tab': '关闭当前标签',
  daily: '打开/创建每日笔记',
  templates: '插入模板',
  graph: '打开全局图谱',
  'export-md': '导出为 Markdown',
  bookmark: '收藏/取消收藏',
  outline: '显示大纲面板',
  'split-edit': '分屏编辑（两篇笔记对照）',
};

const DEFAULTS = {
  search: 'Ctrl+Shift+F',
  switcher: 'Ctrl+O',
  palette: 'Ctrl+P',
  'new-note': 'Ctrl+N',
  capture: 'Ctrl+Shift+Space',
  save: 'Ctrl+S',
  'close-tab': 'Ctrl+W',
  daily: 'Ctrl+Shift+D',
  templates: 'Ctrl+Shift+T',
  graph: 'Ctrl+Shift+G',
  'export-md': 'Ctrl+Shift+E',
  bookmark: 'Ctrl+Shift+B',
  outline: 'Ctrl+Shift+O',
  'split-edit': 'Ctrl+Shift+S',
};

let actions = {};
let mapping = {};
let installed = false;
let recordingCleanup = null;

export function getMapping() { return { ...mapping }; }

/** 安装动作表并绑定全局 keydown；可重复调用（vault 切换后刷新映射）。 */
export function install(actionMap) {
  actions = actionMap;
  mapping = { ...DEFAULTS, ...(state.settings.hotkeys || {}) };
  syncPaletteLabels();
  renderHotkeys();
  if (!installed) {
    document.addEventListener('keydown', handleKey);
    installed = true;
  }
}

/** settings 更新后重新载入映射（不重绑事件）。 */
export function refresh() {
  mapping = { ...DEFAULTS, ...(state.settings.hotkeys || {}) };
  syncPaletteLabels();
  renderHotkeys();
}

function syncPaletteLabels() {
  for (const [id, key] of Object.entries(mapping)) palette.setHotkeyLabel(id, key);
}

function normalize(e) {
  const parts = [];
  if (e.ctrlKey || e.metaKey) parts.push('Ctrl');
  if (e.shiftKey) parts.push('Shift');
  if (e.altKey) parts.push('Alt');
  let key = e.key;
  if (key === ' ') key = 'Space';
  else if (key.length === 1) key = key.toUpperCase();
  parts.push(key);
  return parts.join('+');
}

function handleKey(e) {
  if (recordingCleanup) return;
  const combo = normalize(e);
  const id = Object.keys(mapping).find((k) => mapping[k] === combo);
  if (!id || !actions[id]) return;
  e.preventDefault();
  try { actions[id](); } catch (err) { console.error('[kwey] 快捷键执行失败', err); }
}

/* ------------------------------------------------------------------ 设置页 */
function renderHotkeys() {
  const box = $('#hotkeys-list');
  if (!box) return;
  box.innerHTML = Object.entries(mapping).map(([id, key]) => `
    <div class="hotkeys-row">
      <span class="hotkeys-row__name">${escapeHtml(LABELS[id] || id)}</span>
      <button type="button" class="hotkeys-row__key" data-id="${escapeHtml(id)}"
        title="点击后按下新的组合键">${escapeHtml(key)}</button>
    </div>`).join('');
  box.querySelectorAll('.hotkeys-row__key').forEach((btn) => {
    btn.addEventListener('click', () => startRecording(btn.dataset.id, btn));
  });
}

function startRecording(id, btn) {
  if (recordingCleanup) recordingCleanup();
  btn.classList.add('is-recording');
  btn.textContent = '按下组合键…';

  const cleanup = () => {
    document.removeEventListener('keydown', handler, true);
    btn.classList.remove('is-recording');
    recordingCleanup = null;
    renderHotkeys();
  };
  const handler = (e) => {
    // 捕获阶段拦截，避免触发全局快捷键或输入控件
    e.preventDefault();
    e.stopPropagation();
    if (e.key === 'Escape') { cleanup(); return; }
    if (['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) return;
    const combo = normalize(e);
    const allowed = combo.startsWith('Ctrl+') || combo.startsWith('Alt+') || /^F\d+$/.test(e.key);
    if (!allowed) {
      toast('请使用带 Ctrl / Alt 的组合键，避免影响正常输入', 'error');
      cleanup();
      return;
    }
    const conflict = Object.entries(mapping).find(([other, value]) => other !== id && value === combo);
    if (conflict) {
      toast(`与「${LABELS[conflict[0]] || conflict[0]}」冲突：${combo}`, 'error');
      cleanup();
      return;
    }
    mapping[id] = combo;
    state.settings.hotkeys = { ...mapping };
    saveSettings({ hotkeys: state.settings.hotkeys });
    syncPaletteLabels();
    cleanup();
    toast(`已设置「${LABELS[id] || id}」为 ${combo}`, 'success', 1500);
  };
  recordingCleanup = cleanup;
  document.addEventListener('keydown', handler, true);
}
