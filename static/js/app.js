/* 应用入口：初始化各模块、绑定快捷键、WS 订阅分发、自动保存（T10 编排）。 */

import { api, connectWS, reportError } from './api.js';
import { state, on, emit } from './state.js';
import { debounce, escapeHtml, toast, wordCount, $, $all } from './utils.js';
import * as tabs from './tabs.js';
import * as editor from './editor.js';
import * as preview from './preview.js';
import * as properties from './properties.js';
import * as attachments from './attachments.js';
import * as filetree from './filetree.js';
import * as tagpanel from './tagpanel.js';
import * as backlinks from './backlinks.js';
import * as search from './search.js';
import * as switcher from './switcher.js';
import * as palette from './palette.js';
import * as settings from './settings.js';

let autosaveRunner = null;
let wsClosed = null;

/* ------------------------------------------------------------------ 启动 */
async function bootstrap() {
  bindStaticButtons();
  registerCommands();
  bindGlobalKeys();

  try {
    const health = await api.get('/api/health');
    if (health.vault) {
      state.settings = await api.get('/api/settings');
      applyTheme(state.settings.theme || 'light');
      state.editorMode = state.settings.editor_mode || 'split';
      await startVaultUI();
    } else {
      $('#welcome-hint').textContent = '尚未打开 vault：请点击下方按钮，或在设置中打开一个文件夹。';
      // 未打开 vault 时先初始化编辑器与设置（打开 vault 后无需重载页面）
      await editor.initEditor($('#editor-source'), handleEditorInput);
      settings.initSettings();
      palette.initPalette();
    }
  } catch (err) {
    reportError(err);
  }
  updateIndexStatus(null);
}

/** vault 就绪后的 UI 初始化（可重复调用：切换 vault 后）。 */
async function startVaultUI() {
  const info = await api.get('/api/vault/info');
  $('#vault-name').textContent = `· ${info.name}`;
  $('#welcome-screen').classList.add('hidden');

  if (!editor.isReady()) {
    await editor.initEditor($('#editor-source'), handleEditorInput);
    attachments.bindAttachments($('#editor-container'));
    settings.initSettings();
    palette.initPalette();
  }

  // 事件订阅先于标签恢复（restore 期间会 emit tab-activated）
  on('tab-activated', (path) => { if (path) loadNote(path); else showNoNote(); });
  on('open-note', (path) => tabs.openTab(path).catch(reportError));
  on('tree-refresh', () => filetree.refreshTree());
  on('note-moved-locally', ({ old: oldPath, newPath }) => tabs.renameTab(oldPath, newPath));
  on('note-deleted', ({ path }) => tabs.closeTab(path));
  on('note-updated', ({ path }) => {
    if (path !== state.activePath) return;
    preview.renderPreview(path);
    backlinks.refreshBacklinks(path);
  });
  // 标签过滤由 tagpanel.js 自行展示笔记列表，无需额外处理

  await tabs.initTabs();
  filetree.initFiletree();
  tagpanel.initTagpanel();

  autosaveRunner = debounce(() => saveNote(false), state.settings.autosave_delay_ms || 800);
  setEditorMode(state.editorMode);

  if (wsClosed) wsClosed();
  wsClosed = connectWS(handleWsEvent);
}

/* ------------------------------------------------------------------ 笔记装载 */
async function loadNote(path) {
  try {
    const doc = await api.get('/api/note', { path });
    state.doc = doc;
    $('#editor-area').classList.remove('hidden');
    $('#note-title').textContent = doc.title;
    editor.setValue(doc.content);
    properties.renderProperties(doc.frontmatter, saveFrontmatter);
    renderPreviewAndPanels();
    tabs.refreshTabTitle(path, doc.title);
    setSaveState('');
    updateWordCount();
    editor.focus();
  } catch (err) {
    if (err.code === 404) {
      toast(`笔记不存在: ${path}（已从标签页移除）`, 'error');
      tabs.closeTab(path);
      emit('tree-refresh');
    } else {
      reportError(err);
    }
  }
}

function showNoNote() {
  state.doc = null;
  $('#editor-area').classList.add('hidden');
  preview.clearPreview();
  properties.clearProperties();
  backlinks.refreshBacklinks(null);
  updateWordCount();
}

function renderPreviewAndPanels() {
  if (!state.doc) return;
  if (state.editorMode !== 'source') preview.renderPreview(state.doc.path);
  backlinks.refreshBacklinks(state.doc.path);
}

/* ------------------------------------------------------------------ 保存 */
function handleEditorInput(content) {
  if (!state.doc) return;
  state.doc.content = content;
  tabs.setDirty(true);
  setSaveState('编辑中…');
  updateWordCount();
  if (autosaveRunner) autosaveRunner();
  // 分屏模式下节流刷新预览
  if (state.editorMode !== 'source' && previewDebounced) previewDebounced();
}

const previewDebounced = debounce(() => {
  if (state.doc && state.editorMode !== 'source') preview.renderPreview(state.doc.path);
}, 600);

async function saveNote(manual) {
  if (!state.doc) return;
  const content = editor.getValue();
  try {
    const result = await api.put('/api/note', {
      path: state.doc.path,
      content,
      frontmatter: state.doc.frontmatter,
      base_mtime: state.doc.mtime,
    });
    state.doc.mtime = result.mtime;
    state.doc.content = content;
    tabs.setDirty(false);
    setSaveState(result.links_changed ? '已保存（链接已更新）' : '已保存');
    if (result.links_changed) backlinks.refreshBacklinks(state.doc.path);
    if (manual) toast('已保存', 'success', 1500);
  } catch (err) {
    if (err.code === 409) {
      toast('笔记已被外部修改（409），正在加载最新版本…', 'error');
      await loadNote(state.doc.path);
    } else {
      reportError(err);
      setSaveState('保存失败');
    }
  }
}

async function saveFrontmatter(newFm) {
  if (!state.doc) return;
  try {
    const result = await api.put('/api/note', {
      path: state.doc.path,
      content: editor.getValue(),
      frontmatter: newFm,
      base_mtime: state.doc.mtime,
    });
    state.doc.frontmatter = newFm;
    state.doc.mtime = result.mtime;
    const title = newFm.title || state.doc.path.split('/').pop().replace(/\.(md|markdown)$/i, '');
    $('#note-title').textContent = title;
    tabs.refreshTabTitle(state.doc.path, title);
    emit('tree-refresh');
  } catch (err) { reportError(err); }
}

/* ------------------------------------------------------------------ 模式/主题 */
function setEditorMode(mode) {
  state.editorMode = mode;
  const area = $('#editor-area');
  if (area) area.dataset.mode = mode;
  $all('#mode-switch .btn--mini').forEach((b) => b.classList.toggle('btn--active', b.dataset.mode === mode));
  if (mode !== 'source') { if (state.doc) preview.renderPreview(state.doc.path); }
  else editor.preserveCursor();
  settings.saveSettings({ editor_mode: mode });
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';
  const btn = $('#btn-theme');
  if (btn) btn.textContent = theme === 'dark' ? '☀ 主题' : '🌗 主题';
}

async function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  await settings.saveSettings({ theme: next });
  toast(`已切换为${next === 'dark' ? '暗色' : '亮色'}主题`, 'success', 1500);
}

/* ------------------------------------------------------------------ WS 事件分发 */
function handleWsEvent(msg) {
  const { event, path, old_path: oldPath } = msg;
  switch (event) {
    case 'created':
    case 'deleted':
      filetree.refreshTree();
      tagpanel.refreshTags();
      if (event === 'deleted' && path === state.activePath) {
        toast(`当前笔记已在别处被删除: ${path}`, 'error');
        tabs.closeTab(path);
      }
      break;
    case 'moved':
      filetree.refreshTree();
      if (oldPath) tabs.renameTab(oldPath, path);
      break;
    case 'updated':
      filetree.refreshTree();
      if (state.doc && path === state.doc.path && !state.dirty) {
        // 外部/Agent 修改 → 重新装载与刷新面板
        loadNote(path);
      }
      break;
    case 'links_changed':
      if (state.doc) backlinks.refreshBacklinks(state.doc.path);
      tagpanel.refreshTags();
      break;
    case 'index_ready':
      updateIndexStatus(Date.now());
      filetree.refreshTree();
      tagpanel.refreshTags();
      break;
    default:
      break;
  }
}

/* ------------------------------------------------------------------ 快捷键 & 命令 */
function bindGlobalKeys() {
  document.addEventListener('keydown', (e) => {
    const ctrl = e.ctrlKey || e.metaKey;
    if (!ctrl) return;
    const key = e.key.toLowerCase();
    if (key === 'o') { e.preventDefault(); switcher.openSwitcher(); }
    else if (key === 'p') { e.preventDefault(); palette.openPalette(); }
    else if (key === 'f' && e.shiftKey) { e.preventDefault(); search.openSearch(); }
    else if (key === 's') { e.preventDefault(); saveNote(true); }
    else if (key === 'w') {
      if (state.activePath) { e.preventDefault(); tabs.closeTab(state.activePath); }
    }
    else if (key === 'n') { e.preventDefault(); newNotePrompt(''); }
  });
}

function registerCommands() {
  palette.registerCommand({ id: 'search', title: '搜索笔记', hotkey: 'Ctrl+Shift+F', run: () => search.openSearch() });
  palette.registerCommand({ id: 'switcher', title: '快速切换笔记', hotkey: 'Ctrl+O', run: () => switcher.openSwitcher() });
  palette.registerCommand({ id: 'new-note', title: '新建笔记', hotkey: 'Ctrl+N', run: () => newNotePrompt('') });
  palette.registerCommand({ id: 'new-dir', title: '新建目录', run: newDirPrompt });
  palette.registerCommand({ id: 'theme', title: '切换亮色/暗色主题', run: toggleTheme });
  palette.registerCommand({ id: 'mode-source', title: '编辑模式：源码', run: () => setEditorMode('source') });
  palette.registerCommand({ id: 'mode-split', title: '编辑模式：分屏（源码+预览）', run: () => setEditorMode('split') });
  palette.registerCommand({ id: 'mode-preview', title: '编辑模式：预览', run: () => setEditorMode('preview') });
  palette.registerCommand({ id: 'save', title: '保存当前笔记', hotkey: 'Ctrl+S', run: () => saveNote(true) });
  palette.registerCommand({ id: 'settings', title: '打开设置', run: () => settings.openSettings() });
  palette.registerCommand({ id: 'refresh-tree', title: '刷新文件树与索引面板', run: () => { filetree.refreshTree(); tagpanel.refreshTags(); } });
}

async function newNotePrompt(dir) {
  const name = window.prompt('新笔记名称（可含子目录，如 notes/读书/随笔）：');
  if (!name) return;
  try {
    const res = await api.post('/api/files/create-note', { dir, name });
    emit('tree-refresh');
    emit('open-note', res.path);
  } catch (err) { reportError(err); }
}

async function newDirPrompt() {
  const name = window.prompt('新目录名称（可含父目录）：');
  if (!name) return;
  try {
    await api.post('/api/files/create-dir', { dir: '', name });
    emit('tree-refresh');
  } catch (err) { reportError(err); }
}

/* ------------------------------------------------------------------ 静态按钮/状态栏 */
function bindStaticButtons() {
  $('#btn-theme').addEventListener('click', toggleTheme);
  $('#mode-switch').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-mode]');
    if (btn) setEditorMode(btn.dataset.mode);
  });
  $('#btn-new-note').addEventListener('click', () => newNotePrompt(''));
  $('#btn-new-dir').addEventListener('click', newDirPrompt);
  $('#sidebar-collapse').addEventListener('click', () => {
    const sb = $('#sidebar');
    sb.classList.toggle('collapsed');
    $('#sidebar-collapse').textContent = sb.classList.contains('collapsed') ? '»' : '«';
  });
  // 附件目录保存（设置页内 Enter/失焦）
  $('#settings-attachment-dir').addEventListener('change', () => settings.saveAttachmentDir());
  window.addEventListener('beforeunload', (e) => {
    if (state.dirty) { saveNote(false); e.preventDefault(); }
  });
}

function setSaveState(text) {
  $('#save-state').textContent = text;
  $('#status-save').textContent = text || '—';
}

function updateWordCount() {
  const n = wordCount(editor.getValue());
  $('#status-words').textContent = `字数 ${n}`;
}

function updateIndexStatus(ts) {
  api.get('/api/health').then((h) => {
    $('#status-index').textContent = `索引：${h.indexed} 篇` + (ts ? '（已就绪）' : '');
  }).catch(() => {});
}

/* ------------------------------------------------------------------ go */
bootstrap().catch((err) => {
  console.error('[kwey] 启动失败', err);
  toast(`启动失败: ${err.message}`, 'error');
});
