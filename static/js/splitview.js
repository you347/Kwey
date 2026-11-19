/* P1-19 两篇独立笔记分屏编辑：副屏（secondary）编排。
 * 左右各持独立 CodeMirror 实例（editor.createEditor）与独立标签栈（tabs pane API），
 * 可拖拽分栏比例；同一笔记两屏打开时靠 base_mtime 乐观锁不互相覆盖（409 → 重载最新）。
 * DOM 契约（接线人提供，见卡1报告）：#editor-main 包裹主源码+预览；
 * #split-resizer 拖拽条；#editor-secondary > .secondary-head(#tabs-secondary /
 * #secondary-save-state / #btn-split-close) + #editor-secondary-body(textarea)。 */

import { api, reportError } from './api.js';
import { state, on, emit } from './state.js';
import { debounce, toast, wordCount, saveStateKind, $ } from './utils.js';
import * as tabs from './tabs.js';
import { createEditor, getMainEditor } from './editor.js';
import { getMapping } from './hotkeys.js';
import * as shell from './shell.js';

const LS_SPLIT = 'kwey.split.v1';
const LOCAL_WRITE_WINDOW_MS = 2500;   // 与 app.js isRecentLocalWrite 同窗

let initialized = false;
let secondaryEditor = null;           // createEditor 句柄（关闭分屏时 destroy）
let autosave = null;
let savePromise = null;               // 防抖保存与 Ctrl+S 并发去重
let lastSecondaryWrite = null;
const els = {};

/* ------------------------------------------------------------------ 初始化 */
export function initSplitView() {
  if (initialized) return;
  initialized = true;
  els.container = $('#editor-container');
  els.main = $('#editor-main');
  els.secondary = $('#editor-secondary');
  els.body = $('#editor-secondary-body');
  els.resizer = $('#split-resizer');

  // 焦点追踪：焦点落在哪个 pane，Ctrl+S / Ctrl+W 就作用于哪个 pane
  document.addEventListener('focusin', (e) => {
    if (!isSplitOpen()) return;
    if (els.secondary && els.secondary.contains(e.target)) setFocused('secondary');
    else if (els.main && els.main.contains(e.target)) setFocused('main');
  });

  bindResizer();
  bindSecondaryAttachments();
  const closeBtn = $('#btn-split-close');
  if (closeBtn) closeBtn.addEventListener('click', () => closeSplit().catch(reportError));

  on('secondary-tab-activated', (path) => { loadSecondaryNote(path).catch(reportError); });
  // 文件树/标签右键「在右屏打开」：未开分屏则先开，再在副屏打开目标笔记
  on('open-in-pane', ({ pane, path } = {}) => {
    if (pane !== 'secondary' || !path) return;
    openInSecondary(path).catch(reportError);
  });
  // 关闭副屏脏标签前先落盘（saveSecondary 同步捕获文档状态，先于 closeTabIn 清空）
  on('tab-closing', ({ pane, path } = {}) => {
    if (pane !== 'secondary') return;
    const sec = tabs.paneState('secondary');
    if (sec.dirty && sec.doc && sec.doc.path === path) saveSecondary(false).catch(reportError);
  });
  on('ws:updated', (msg = {}) => {
    if (!isSplitOpen()) return;
    const sec = tabs.paneState('secondary');
    if (!sec.doc || msg.path !== sec.doc.path) return;
    if (sec.dirty || isRecentSecondaryWrite(msg.path)) return;
    loadSecondaryNote(msg.path, { silentIfSame: true }).catch(reportError);
  });
  on('ws:deleted', (msg = {}) => {
    if (!isSplitOpen()) return;
    const sec = tabs.paneState('secondary');
    if (!sec.tabs.some((t) => t.path === msg.path)) return;
    if (sec.activePath === msg.path) toast(`右屏笔记已在别处被删除: ${msg.path}`, 'error');
    tabs.closeTabIn('secondary', msg.path).catch(reportError);
  });
  on('note-deleted', ({ path } = {}) => {
    if (path) tabs.closeTabIn('secondary', path).catch(reportError);
  });

  document.addEventListener('keydown', handlePaneKeys, true);
  window.addEventListener('resize', () => { if (isSplitOpen()) refreshEditors(); });
  window.addEventListener('beforeunload', (e) => {
    if (!isSplitOpen()) return;
    if (tabs.paneState('secondary').dirty) {
      saveSecondary(false);
      e.preventDefault();
      e.returnValue = '';
    }
  });
  // 主屏模式切换（源码/分屏/预览）改变主区宽度 → 重排两屏 CM
  const area = $('#editor-area');
  if (area) {
    new MutationObserver(() => { if (isSplitOpen()) refreshEditors(); })
      .observe(area, { attributes: true, attributeFilter: ['data-mode'] });
  }
}

/* ------------------------------------------------------------------ 开/关分屏 */
export async function activateSplit() {
  if (isSplitOpen()) {
    setFocused('secondary');
    if (secondaryEditor) secondaryEditor.focus();
    return true;
  }
  if (!els.container || !els.main || !els.secondary || !els.body || !els.resizer) {
    toast('分屏编辑未接线：index.html 缺少 #editor-main / #editor-secondary / #split-resizer', 'error');
    return false;
  }
  const split = tabs.ensureSplitState();
  if (!secondaryEditor) {
    secondaryEditor = await createEditor(els.body, handleSecondaryInput);
    secondaryEditor.onCursor(shell.setStatusCursor);
    secondaryEditor.enableLinkHints();
  }
  if (autosave) autosave.cancel();
  autosave = debounce(() => saveSecondary(false).catch(reportError), state.settings.autosave_delay_ms || 800);

  split.open = true;
  els.container.classList.add('editor-container--split');
  els.secondary.classList.remove('hidden');
  els.resizer.classList.remove('hidden');
  applyRatio(readSavedRatio());
  setFocused('secondary');

  // 恢复上次的副屏标签；没有则默认对照主屏当前笔记
  const restored = await tabs.restoreSecondaryTabs();
  if (!restored && state.activePath) await tabs.openTabIn('secondary', state.activePath);
  refreshEditors();
  persistSplit();
  emit('split-changed', { open: true });
  return true;
}

export async function closeSplit() {
  if (!isSplitOpen()) return;
  const sec = tabs.paneState('secondary');
  if (sec.dirty) await saveSecondary(false);   // 先落盘，避免丢编辑
  if (autosave) autosave.cancel();
  if (secondaryEditor) { secondaryEditor.destroy(); secondaryEditor = null; }
  tabs.clearPane('secondary');
  tabs.ensureSplitState().open = false;
  setFocused('main');
  els.container.classList.remove('editor-container--split');
  els.secondary.classList.add('hidden');
  els.resizer.classList.add('hidden');
  els.main.style.flexGrow = '';
  els.secondary.style.flexGrow = '';
  persistSplit();
  emit('split-changed', { open: false });
  const main = getMainEditor();
  if (main) main.focus();
}

export function isSplitOpen() { return !!(state.split && state.split.open); }

/** 副屏当前激活笔记路径（未开分屏/无标签为 null）。 */
export function getSecondaryPath() { return tabs.paneState('secondary').activePath; }

export function getFocusedPane() { return isSplitOpen() ? tabs.ensureSplitState().focused : 'main'; }

/** 在副屏打开指定笔记（必要时先开分屏），并把焦点交给副屏。 */
export async function openInSecondary(path) {
  if (!isSplitOpen()) await activateSplit();
  await tabs.openTabIn('secondary', path);
  setFocused('secondary');
  if (secondaryEditor) secondaryEditor.focus();
}

/* ------------------------------------------------------------------ 副屏装载 */
async function loadSecondaryNote(path, { silentIfSame = false } = {}) {
  if (!secondaryEditor) return;
  const sec = tabs.paneState('secondary');
  if (!path) {
    sec.doc = null;
    secondaryEditor.setValue('');
    setSecondarySaveState('');
    return;
  }
  // 切笔记前把旧笔记未落盘的改动先保存（防抖未触发时防丢）
  if (!silentIfSame && sec.dirty && sec.doc && sec.doc.path !== path) {
    await saveSecondary(false);
  }
  try {
    const doc = await api.get('/api/note', { path });
    // 静默刷新期间用户又开始输入 → 放弃装载，保留编辑（保存时走 409 校验）
    if (silentIfSame && sec.dirty) return;
    const same = sec.doc && sec.doc.path === path && sec.doc.content === doc.content;
    sec.doc = doc;
    tabs.setDirtyIn('secondary', false);
    // 内容一致（如同笔记另一屏刚保存）→ 只续 mtime，不动光标/撤销栈
    if (!(silentIfSame && same)) {
      secondaryEditor.setValue(doc.content);
      if (getFocusedPane() === 'secondary') updateWordCount(doc.content);
    }
    setSecondarySaveState('');
    if (getFocusedPane() === 'secondary') {
      shell.setStatusPath(path);
      secondaryEditor.focus();
    }
  } catch (err) {
    if (err.code === 404) {
      toast(`笔记不存在: ${path}（已从右屏标签移除）`, 'error');
      tabs.closeTabIn('secondary', path).catch(reportError);
    } else {
      reportError(err);
    }
  }
}

function handleSecondaryInput(content) {
  const sec = tabs.paneState('secondary');
  if (!sec.doc) return;
  sec.doc.content = content;
  tabs.setDirtyIn('secondary', true);
  setSecondarySaveState('编辑中…');
  updateWordCount(content);
  if (autosave) autosave();
}

/* ------------------------------------------------------------------ 副屏保存（base_mtime 乐观锁） */
export function saveSecondary(manual = false) {
  if (savePromise) return savePromise;
  savePromise = doSaveSecondary(manual).finally(() => { savePromise = null; });
  return savePromise;
}

async function doSaveSecondary(manual) {
  const sec = tabs.paneState('secondary');
  if (!secondaryEditor || !sec.doc) return;
  if (autosave) autosave.cancel();
  const path = sec.doc.path;
  const content = secondaryEditor.getValue();
  try {
    const result = await api.put('/api/note', {
      path,
      content,
      frontmatter: sec.doc.frontmatter,
      base_mtime: sec.doc.mtime,
    });
    lastSecondaryWrite = { path, ts: Date.now() };
    // 保存期间可能已切换标签/继续输入：仅当文档与缓冲区仍一致才清脏标记
    if (sec.doc && sec.doc.path === path) {
      sec.doc.mtime = result.mtime;
      if (secondaryEditor && secondaryEditor.getValue() === content) {
        sec.doc.content = content;
        tabs.setDirtyIn('secondary', false);
        setSecondarySaveState(result.links_changed ? '已保存（链接已更新）' : '已保存');
      }
    }
    syncMainAfterSecondarySave(path, content, result.mtime);
    if (manual) toast('已保存（右屏）', 'success', 1500);
  } catch (err) {
    if (err.code === 409) {
      toast('右屏笔记已被外部修改（409），正在加载最新版本…', 'error');
      if (sec.doc && sec.doc.path === path) await loadSecondaryNote(path);
    } else {
      reportError(err);
      setSecondarySaveState('保存失败');
    }
  }
}

/** 副屏落盘后同步主屏（仅同笔记）：
 *  - 缓冲内容一致 → 只续 mtime 并清脏；
 *  - 主屏无未保存编辑但内容已旧 → 代为静默装载（app.js 的 isRecentLocalWrite
 *    窗口会吞掉这次 WS reload，不补同步会让主屏下次保存误 409）；
 *  - 主屏有未保存编辑 → 不动，保存时 409 重载，乐观锁兜底。 */
function syncMainAfterSecondarySave(path, content, mtime) {
  if (!state.doc || state.doc.path !== path) return;
  if (state.doc.content === content) {
    state.doc.mtime = mtime;
    tabs.setDirty(false);
    return;
  }
  if (!state.dirty) {
    state.doc.content = content;
    state.doc.mtime = mtime;
    const main = getMainEditor();
    if (main) main.setValue(content);
  }
}

function isRecentSecondaryWrite(path) {
  return !!lastSecondaryWrite && lastSecondaryWrite.path === path
    && Date.now() - lastSecondaryWrite.ts < LOCAL_WRITE_WINDOW_MS;
}

/* ------------------------------------------------------------------ 快捷键路由（捕获阶段） */
/** 焦点在副屏时接管 save / close-tab（跟随 settings 自定义映射），主屏行为不变。 */
function handlePaneKeys(e) {
  if (!isSplitOpen() || getFocusedPane() !== 'secondary') return;
  if (document.querySelector('.hotkeys-row__key.is-recording')) return;  // 录制快捷键时不拦截
  const combo = normalizeCombo(e);
  const mapping = getMapping();
  if (combo === mapping.save) {
    e.preventDefault();
    e.stopPropagation();
    saveSecondary(true).catch(reportError);
  } else if (combo === mapping['close-tab']) {
    e.preventDefault();
    e.stopPropagation();
    const path = getSecondaryPath();
    if (path) tabs.closeTabIn('secondary', path).catch(reportError);
  }
}

function normalizeCombo(e) {
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

/* ------------------------------------------------------------------ 焦点 / 状态栏 */
function setFocused(pane) {
  const split = tabs.ensureSplitState();
  if (split.focused === pane) return;
  split.focused = pane;
  if (els.secondary) els.secondary.classList.toggle('editor-pane--focused', pane === 'secondary');
  if (els.main) els.main.classList.toggle('editor-pane--focused', pane === 'main');
  emit('pane-focused', { pane });
}

function setSecondarySaveState(text) {
  const el = $('#secondary-save-state');
  if (!el) return;
  el.textContent = text;
  el.dataset.kind = saveStateKind(text);
}

function updateWordCount(text) {
  const el = $('#status-words');
  if (el) el.textContent = `字数 ${wordCount(text)}`;
}

/* ------------------------------------------------------------------ 拖拽分栏 */
function applyRatio(r) {
  const ratio = Math.min(0.8, Math.max(0.2, Number(r) || 0.5));
  tabs.ensureSplitState().ratio = ratio;
  els.main.style.flexGrow = String(ratio);
  els.secondary.style.flexGrow = String(1 - ratio);
}

function readSavedRatio() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_SPLIT) || 'null');
    if (saved && Number(saved.ratio)) return Number(saved.ratio);
  } catch { /* 忽略损坏值 */ }
  return 0.5;
}

function persistSplit() {
  try {
    localStorage.setItem(LS_SPLIT, JSON.stringify({
      ratio: tabs.ensureSplitState().ratio,
      open: isSplitOpen(),
    }));
  } catch { /* localStorage 不可用时静默 */ }
}

function bindResizer() {
  const el = els.resizer;
  if (!el) return;
  el.addEventListener('pointerdown', (e) => {
    if (!isSplitOpen()) return;
    e.preventDefault();
    const rect = els.container.getBoundingClientRect();
    el.classList.add('is-dragging');
    document.body.classList.add('is-resizing');
    try { el.setPointerCapture(e.pointerId); } catch { /* 某些 WebView 不支持 */ }
    const onMove = (ev) => {
      applyRatio((ev.clientX - rect.left) / Math.max(rect.width, 1));
    };
    const onUp = () => {
      el.removeEventListener('pointermove', onMove);
      el.classList.remove('is-dragging');
      document.body.classList.remove('is-resizing');
      persistSplit();
      refreshEditors();
    };
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp, { once: true });
    el.addEventListener('pointercancel', onUp, { once: true });
  });
}

function refreshEditors() {
  const main = getMainEditor();
  if (main) main.refresh();
  if (secondaryEditor) secondaryEditor.refresh();
}

/* ------------------------------------------------------------------ 副屏附件（拦截冒泡，防插入主屏） */
function bindSecondaryAttachments() {
  if (!els.secondary) return;
  els.secondary.addEventListener('paste', (e) => {
    const items = Array.from((e.clipboardData && e.clipboardData.items) || []);
    const imageItem = items.find((i) => i.type && i.type.startsWith('image/'));
    if (!imageItem) return;
    e.preventDefault();
    e.stopPropagation();
    const file = imageItem.getAsFile();
    if (!file) return;
    const name = file.name && file.name !== 'image.png' ? file.name : `粘贴图片_${Date.now()}.png`;
    uploadAndInsertSecondary(file, name);
  }, true);
  els.secondary.addEventListener('dragover', (e) => { e.preventDefault(); e.stopPropagation(); }, true);
  els.secondary.addEventListener('drop', (e) => {
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || [])
      .filter((f) => f.type && f.type.startsWith('image/'));
    if (!files.length) return;
    e.preventDefault();
    e.stopPropagation();
    for (const f of files) uploadAndInsertSecondary(f, f.name);
  }, true);
}

async function uploadAndInsertSecondary(file, displayName) {
  try {
    const renamed = new File([file], displayName, { type: file.type || 'image/png' });
    const data = await api.upload(renamed);
    if (secondaryEditor) secondaryEditor.insertText(`![](${data.path})\n`);
    toast(`附件已保存: ${data.path}`, 'success');
  } catch (err) {
    reportError(err);
  }
}