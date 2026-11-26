/* 应用入口：初始化各模块、绑定快捷键、WS 订阅分发、自动保存（T10 编排）。 */

import { api, connectWS, reportError } from './api.js';
import { state, on, emit } from './state.js';
import { debounce, escapeHtml, toast, wordCount, saveStateKind, copyPath, basename, stripMd, $, $all } from './utils.js';
import { askText } from './dialog.js';
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
import * as settings from './settings/index.js';
import * as shell from './shell.js';
import * as outline from './outline.js';
import * as bookmarks from './bookmarks.js';
import * as trash from './trash.js';
import * as templates from './templates.js';
import * as exporter from './export.js';
import * as graph from './graph.js';
import * as hotkeys from './hotkeys.js';
import * as agentlink from './agentlink.js';
import * as organize from './organize.js';
import * as chat from './chat.js';
import * as history from './history.js';
import * as splitview from './splitview.js';
import { initLinkPreview } from './linkpreview.js';
import { initPulse, refreshPulse } from './pulse.js';
import { initCompile } from './compile.js';
import { initMemory } from './memory.js';
import { initVectorSettings } from './vectorsettings.js';
import { initAutopush, renderAutopushSettings } from './autopush.js';
// ---- Wave A 第二批（C1-C6）----
import { initCanvas } from './canvas.js';
import './periodic.js';              // 模块加载即自注册命令（无 init）
import { initPlugins } from './plugins.js';
import { initThemeEditor } from './themeeditor.js';
import { initRag } from './rag.js';
import { initReview } from './review.js';
import { initClipper } from './clipper.js';
import { initImporter } from './importer.js';
import { initBases } from './bases.js';
import { initUpdate } from './update.js';
import { initDiagnose } from './diagnose.js';
import { initCapture, openCapture } from './capture.js';
import { initInbox } from './inbox.js';
import { initDuty } from './duty.js';
import * as provenance from './provenance.js';
import * as freshness from './freshness.js';
import * as viewer from './viewer.js';

let autosaveRunner = null;
let wsClosed = null;
let vaultUIBound = false;
let lastLocalWrite = null;
// 外部/Agent 写入冲突：仅针对当前激活笔记；conflictBusy 防止两个出口被重复触发
let conflictPath = null;
let conflictBusy = false;

/* ------------------------------------------------------------------ 启动 */
async function bootstrap() {
  shell.initShell();
  provenance.initProvenance();
  freshness.initFreshness();
  viewer.initViewer();
  bindStaticButtons();
  registerCommands();
  bindGlobalKeys();
  // P0-6 / P0-8：浮层初始化与 vault 是否打开无关，必须在这里完成一次
  search.initSearch();
  switcher.initSwitcher();

  on('theme-changed', (theme) => applyTheme(theme));
  on('fonts-changed', (fonts) => applyFonts(fonts));
  on('tag-filter', (tag) => {
    shell.activatePanel('left', 'tags');
    tagpanel.filterByTag(tag);
  });

  // 登录层 / 设置页打开 vault 后初始化主界面（splash.js 也会触发此事件）
  on('vault-opened', () => {
    applyFonts(state.settings.fonts);
    startVaultUI().catch(reportError);
  });

  try {
    const health = await api.get('/api/health');
    if (health.vault) {
      state.settings = await api.get('/api/settings');
      applyTheme(state.settings.theme || 'dark');
      applyFonts(state.settings.fonts);
      state.editorMode = state.settings.editor_mode || 'split';
      await startVaultUI();
    } else {
      $('#welcome-hint').textContent = '尚未打开 vault：请点击下方按钮，或在设置中打开一个文件夹。';
      // 未打开 vault 时先初始化编辑器与设置（打开 vault 后无需重载页面）
      await setupEditor();
      settings.initSettings();
      palette.initPalette();
    }
  } catch (err) {
    reportError(err);
  }
  updateIndexStatus(null);
}

/** 编辑器只初始化一次；光标状态与 [[ 补全随之挂载。 */
async function setupEditor() {
  if (editor.isReady()) return;
  await editor.initEditor($('#editor-source'), handleEditorInput);
  editor.onCursor(shell.setStatusCursor);
  editor.enableLinkHints();
  splitview.initSplitView();
}

/** vault 就绪后的 UI 初始化（可重复调用：切换 vault 后）。 */
async function startVaultUI() {
  // A4 后 pulse/chat/organize 等端点需 token；本地 UI 首次使用时自动引导签发，
  // 之后 api.js 会给所有请求统一带上（409=库里已有 token 但本机未保存，交给设置页处理）。
  try {
    if (!localStorage.getItem('kwey.agent.token')) {
      try {
        const boot = await api.post('/api/agent/bootstrap', {});
        if (boot && boot.token) localStorage.setItem('kwey.agent.token', boot.token);
      } catch { /* 409=库里已有 token 但本机未保存；在设置页重新生成即可 */ }
    }
  } catch { /* localStorage 不可用时忽略 */ }
  const info = await api.get('/api/vault/info');
  $('#vault-name').textContent = `· ${info.name}`;
  $('#welcome-screen').classList.add('hidden');

  if (!editor.isReady()) {
    await setupEditor();
    attachments.bindAttachments($('#editor-container'));
    settings.initSettings();
    palette.initPalette();
  }

  // 事件订阅只注册一次（vault 切换会重复进入本函数）
  if (!vaultUIBound) {
    vaultUIBound = true;
    // 事件订阅先于标签恢复（restore 期间会 emit tab-activated）
    on('tab-activated', (path) => { if (path) loadNote(path); else showNoNote(); });
    on('open-note', (path) => tabs.openTab(path).catch(reportError));
    // 非 md 文件：只读预览，不进入编辑态
    on('open-file', (path) => { if (path) viewer.open(path).catch(reportError); });
    on('viewer-closed', () => showNoNote());
    // 智能体「打开文件夹 / 定位附件」：切到文件树并展开定位
    on('open-folder', (payload) => {
      const path = typeof payload === 'string' ? payload : payload && payload.path;
      if (!path) return;
      shell.activatePanel('left', 'files');
      filetree.revealPath(path).catch(reportError);
    });
    on('tree-refresh', () => filetree.refreshTree());
    on('note-moved-locally', ({ old: oldPath, newPath }) => tabs.renameTab(oldPath, newPath));
    on('note-deleted', ({ path }) => tabs.closeTab(path));
    on('note-updated', ({ path }) => {
      if (path !== state.activePath) return;
      refreshPreview();
      backlinks.refreshBacklinks(path);
      freshness.refresh(path).catch(() => {});
    });
    // 审阅只改管理字段，但确实写盘；吞掉紧随其后的 updated 事件，避免重载编辑器丢光标。
    on('provenance-reviewed', ({ path }) => {
      if (path) lastLocalWrite = { path, ts: Date.now() };
    });
  }
  // 标签过滤由 tagpanel.js 自行展示笔记列表，无需额外处理

  // P1 面板模块：先 init（注册内部事件订阅），再恢复面板状态触发首屏加载
  outline.initOutline();
  bookmarks.initBookmarks();
  trash.initTrash();
  templates.initTemplates();
  graph.initGraph();
  initLinkPreview();
  agentlink.initAgentLink();
  organize.initOrganize();
  chat.initChat();
  history.initHistory();
  initCompile();
  initMemory();
  initVectorSettings();
  initAutopush();
  initCanvas();
  initPlugins();
  initThemeEditor();
  initRag();
  initReview();
  initClipper();
  initImporter();
  initBases();
  initUpdate();
  initDiagnose();
  initCapture();
  initInbox();
  initDuty();
  initPulse();
  hotkeys.refresh();
  shell.onVaultReady();

  await tabs.initTabs();
  if (!state.activePath) showNoNote();
  filetree.initFiletree();
  tagpanel.initTagpanel();

  autosaveRunner = debounce(() => saveNote(false), state.settings.autosave_delay_ms || 800);
  setEditorMode(state.editorMode);

  if (wsClosed) wsClosed();
  wsClosed = connectWS(handleWsEvent);
}

/* ------------------------------------------------------------------ 写入冲突 */
/** 显示冲突条；只有当前激活笔记才能触发（切走/无文档时忽略）。 */
function showConflict(path) {
  if (!path || !state.doc || path !== state.doc.path) return;
  conflictPath = path;
  const text = $('#conflict-text');
  if (text) {
    text.textContent = `磁盘上的「${stripMd(basename(path))}」已被外部修改（可能来自智能体）。`;
  }
  const bar = $('#conflict-bar');
  if (bar) bar.classList.remove('hidden');
}

/** 隐藏冲突条并复位；保存副本进行中不复位（避免唯一出口被提前关掉）。 */
function hideConflict() {
  if (conflictBusy) return;
  const bar = $('#conflict-bar');
  if (bar) bar.classList.add('hidden');
  conflictPath = null;
}

/** 把当前缓冲另存为同目录「原名 (冲突副本).md」；重名追加序号，最多 20 次。 */
async function saveConflictCopy() {
  if (conflictBusy || !state.doc || !editor.isReady()) return;
  const path = conflictPath || state.doc.path;
  const content = editor.getValue();
  const frontmatter = state.doc.frontmatter;
  const reloadBtn = $('#btn-conflict-reload');
  const keepBtn = $('#btn-conflict-keep');
  conflictBusy = true;
  if (reloadBtn) reloadBtn.disabled = true;
  if (keepBtn) keepBtn.disabled = true;
  let savedTarget = null;
  try {
    savedTarget = await createConflictCopy(path, content, frontmatter);
  } catch (err) {
    reportError(err);
  } finally {
    conflictBusy = false;
    if (reloadBtn) reloadBtn.disabled = false;
    if (keepBtn) keepBtn.disabled = false;
  }
  if (!savedTarget) return;   // 失败保留冲突条，不能把唯一出口关掉

  hideConflict();
  toast(`已另存为 ${savedTarget}`, 'success');
  emit('tree-refresh');
  await tabs.openTab(savedTarget);
  // 副本在副屏打开时，原笔记仍是主屏激活态 → 同步一次磁盘最新内容
  if (state.activePath === path) await loadNote(path);
}

/** 逐候选 POST 创建副本：409 换下一个序号，其它错误直接抛出。 */
async function createConflictCopy(path, content, frontmatter) {
  const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/') + 1) : '';
  const file = path.slice(dir.length);
  const dot = file.lastIndexOf('.');
  const stem = dot > 0 ? file.slice(0, dot) : file;
  const ext = dot > 0 ? file.slice(dot) : '.md';
  let lastError = null;
  for (let i = 1; i <= 20; i += 1) {
    const suffix = i === 1 ? ' (冲突副本)' : ` (冲突副本 ${i})`;
    const target = `${dir}${stem}${suffix}${ext}`;
    try {
      // 标题与文件名保持一致：标签页显示的是 frontmatter.title，若继承原名
      // 会和原笔记同名，用户无法分辨哪一个保住了本地内容。
      await api.post('/api/note', {
        path: target, content, frontmatter: { ...(frontmatter || {}), title: `${stem}${suffix}` },
      });
      return target;
    } catch (err) {
      if (err.code !== 409) throw err;
      lastError = err;
    }
  }
  throw lastError || new Error('同名冲突副本过多（已尝试 20 个）');
}

/* ------------------------------------------------------------------ 笔记装载 */
async function loadNote(path) {
  hideConflict();
  viewer.close();
  try {
    const doc = await api.get('/api/note', { path });
    state.doc = doc;
    $('#editor-area').classList.remove('hidden');
    $('#empty-state').classList.add('hidden');
    $('#note-title').textContent = doc.title;
    shell.setStatusPath(path);
    editor.setValue(doc.content);
    properties.renderProperties(doc.frontmatter, saveFrontmatter);
    renderPreviewAndPanels();
    tabs.refreshTabTitle(path, doc.title);
    tabs.setDirty(false);   // 编辑器内容与磁盘一致；切标签后不残留上一页的脏标记
    setSaveState('');
    updateWordCount();
    editor.focus();
    hideConflict();
    provenance.refresh(path).catch(() => {});
    freshness.refresh(path).catch(() => {});
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
  hideConflict();
  viewer.close();
  state.doc = null;
  shell.setStatusPath('');
  $('#editor-area').classList.add('hidden');
  $('#empty-state').classList.remove('hidden');
  renderEmptyRecent();
  refreshPulse();
  preview.clearPreview();
  properties.clearProperties();
  backlinks.refreshBacklinks(null);
  provenance.clear();
  freshness.clear();
  updateWordCount();
}

/** 空态页「最近打开」列表（数据来自 settings.recent，bookmarks.js 维护）。 */
function renderEmptyRecent() {
  const box = $('#empty-recent');
  if (!box) return;
  const recent = (Array.isArray(state.settings.recent) ? state.settings.recent : [])
    .map((e) => (typeof e === 'string' ? e : e && e.path))
    .filter(Boolean)
    .slice(0, 6);
  box.innerHTML = '';
  if (!recent.length) return;
  const title = document.createElement('div');
  title.className = 'empty-state__recent-title';
  title.textContent = '最近打开';
  box.appendChild(title);
  for (const path of recent) {
    const item = document.createElement('div');
    item.className = 'empty-state__recent-item';
    item.title = path;
    item.innerHTML = `
      <svg class="icon" aria-hidden="true"><use href="#i-clock"/></svg>
      <span class="empty-state__recent-name">${escapeHtml(stripMd(basename(path)))}</span>
      <span class="empty-state__recent-path">${escapeHtml(path)}</span>`;
    item.addEventListener('click', () => emit('open-note', path));
    box.appendChild(item);
  }
}

/** 用 state.doc 当前正文（可能尚未落盘）渲染预览。 */
function refreshPreview() {
  if (state.doc && state.editorMode !== 'source') {
    preview.renderPreview(state.doc.path, state.doc.content);
  }
}

function renderPreviewAndPanels() {
  if (!state.doc) return;
  refreshPreview();
  backlinks.refreshBacklinks(state.doc.path);
}

/* ------------------------------------------------------------------ 保存 */
function handleEditorInput(content) {
  if (!state.doc) return;
  state.doc.content = content;
  tabs.setDirty(true);
  setSaveState('编辑中…');
  updateWordCount();
  // 已进入冲突态：不再自动保存（避免反复 409 / 反复弹提示），等用户选出口
  if (autosaveRunner && conflictPath !== state.doc.path) autosaveRunner();
  // 分屏模式下节流刷新预览
  if (state.editorMode !== 'source' && previewDebounced) previewDebounced();
}

const previewDebounced = debounce(() => { refreshPreview(); }, 600);

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
    const path = state.doc.path;
    lastLocalWrite = { path, ts: Date.now() };
    // 窗口只用于吞掉自己这次保存引发的 updated 事件：过期即清，避免与后续外部写入叠加
    setTimeout(() => {
      if (lastLocalWrite && lastLocalWrite.path === path) lastLocalWrite = null;
    }, 800);
    tabs.setDirty(false);
    hideConflict();
    provenance.invalidate(state.doc.path);
    provenance.refresh(state.doc.path).catch(() => {});
    freshness.refresh(state.doc.path).catch(() => {});
    setSaveState(result.links_changed ? '已保存（链接已更新）' : '已保存');
    if (result.links_changed) backlinks.refreshBacklinks(state.doc.path);
    if (manual) toast('已保存', 'success', 1500);
  } catch (err) {
    if (err.code === 409) {
      // 磁盘已被外部/Agent 改过：保留本地缓冲，让用户选「重新载入」或「另存副本」
      showConflict(state.doc.path);
      setSaveState('保存冲突');
      toast('磁盘内容已更新，请选择重新载入或另存副本', 'error');
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
    lastLocalWrite = { path: state.doc.path, ts: Date.now() };
    const title = newFm.title || state.doc.path.split('/').pop().replace(/\.(md|markdown)$/i, '');
    $('#note-title').textContent = title;
    tabs.refreshTabTitle(state.doc.path, title);
    provenance.invalidate(state.doc.path);
    provenance.refresh(state.doc.path).catch(() => {});
    freshness.refresh(state.doc.path).catch(() => {});
    emit('tree-refresh');
  } catch (err) { reportError(err); }
}

/* ------------------------------------------------------------------ 模式/主题 */
function setEditorMode(mode) {
  state.editorMode = mode;
  const area = $('#editor-area');
  if (area) area.dataset.mode = mode;
  $all('#mode-switch .btn--mini').forEach((b) => b.classList.toggle('btn--active', b.dataset.mode === mode));
  if (mode !== 'source') refreshPreview();
  else editor.preserveCursor();
  settings.saveSettings({ editor_mode: mode });
}

/* ------------------------------------------------------------------ 字号 */
/** 设置页可调的四档字号：写入 <html> 内联 CSS 变量，全局即时生效。 */
const FONT_VARS = {
  ui: '--font-ui',
  sidebar: '--font-sidebar',
  editor: '--font-editor',
  chat: '--font-chat',
};
const FONT_DEFAULTS = { ui: 13, sidebar: 13, editor: 15, chat: 13 };

function applyFonts(fonts) {
  const conf = { ...FONT_DEFAULTS, ...(fonts || {}) };
  const root = document.documentElement;
  for (const [key, cssVar] of Object.entries(FONT_VARS)) {
    const n = Number(conf[key]);
    const size = Number.isFinite(n) ? Math.min(32, Math.max(8, Math.round(n))) : FONT_DEFAULTS[key];
    root.style.setProperty(cssVar, `${size}px`);
  }
  // CodeMirror 的行高与光标位置依赖字体度量，字号变化后需要重排
  if (editor.isReady()) editor.refreshLayout();
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';
  if (editor.isReady()) editor.refreshLayout();
  // 按钮内是 SVG 图标 + 文本，只切换图标引用，避免整钮重写
  const use = document.querySelector('#btn-theme use');
  if (use) use.setAttribute('href', theme === 'dark' ? '#i-sun' : '#i-moon');
}

async function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  await settings.saveSettings({ theme: next });
  toast(`已切换为${next === 'dark' ? '暗色' : '亮色'}主题`, 'success', 1500);
}

/* ------------------------------------------------------------------ WS 事件分发 */
/** 本进程刚保存产生的 updated 事件不应触发 loadNote（会丢光标/撤销栈）。 */
function isRecentLocalWrite(path) {
  return !!lastLocalWrite && lastLocalWrite.path === path && Date.now() - lastLocalWrite.ts < 700;
}

function handleWsEvent(msg) {
  const { event, path, old_path: oldPath } = msg;
  // 新面板通过 on('ws:<event>') 自订阅，避免此处 switch 随功能线性膨胀
  emit(`ws:${event}`, msg);
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
      if (path === state.activePath) {
        provenance.invalidate(path);
        provenance.refresh(path).catch(() => {});
      }
      filetree.refreshTree();
      if (!state.doc || path !== state.doc.path || isRecentLocalWrite(path)) break;
      // 外部/Agent 修改：干净则静默重载；脏则只提示冲突，绝不动编辑器缓冲
      if (state.dirty) showConflict(path);
      else loadNote(path);
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
  // 快捷键表由 hotkeys.js 统一持有（设置页可改，冲突检测在录制时完成）
  hotkeys.install({
    search: () => search.openSearch(),
    switcher: () => switcher.openSwitcher(),
    palette: () => palette.openPalette(),
    'new-note': () => newNotePrompt(''),
    capture: () => openCapture(),
    save: () => saveNote(true),
    'close-tab': () => { if (state.activePath) tabs.closeTab(state.activePath); },
    daily: () => templates.openDailyNote(),
    templates: () => templates.openTemplates(),
    graph: () => graph.openGlobalGraph(),
    'export-md': () => exporter.exportCurrent('md'),
    'export-pdf': () => exporter.exportCurrent('pdf'),
    'export-print': () => exporter.exportPrint(),
    bookmark: () => bookmarks.toggleBookmark(),
    outline: () => shell.activatePanel('left', 'outline'),
    'split-edit': () => {
      (splitview.isSplitOpen() ? splitview.closeSplit() : splitview.activateSplit()).catch(reportError);
    },
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

  // P1 功能命令（快捷键展示由 hotkeys.js 同步）
  palette.registerCommand({ id: 'focus-files', title: '显示文件树', run: () => shell.activatePanel('left', 'files') });
  palette.registerCommand({ id: 'outline', title: '显示大纲面板', run: () => shell.activatePanel('left', 'outline') });
  palette.registerCommand({ id: 'bookmark', title: '收藏/取消收藏当前笔记', run: () => bookmarks.toggleBookmark() });
  palette.registerCommand({ id: 'trash', title: '打开回收站', run: () => shell.activatePanel('left', 'trash') });
  palette.registerCommand({ id: 'daily', title: '打开/创建每日笔记', run: () => templates.openDailyNote() });
  palette.registerCommand({ id: 'templates', title: '插入模板（新建笔记）', run: () => templates.openTemplates() });
  palette.registerCommand({ id: 'export-md', title: '导出当前笔记为 Markdown', run: () => exporter.exportCurrent('md') });
  palette.registerCommand({ id: 'export-html', title: '导出当前笔记为 HTML', run: () => exporter.exportCurrent('html') });
  palette.registerCommand({ id: 'export-pdf', title: '导出当前笔记为 PDF', run: () => exporter.exportCurrent('pdf') });
  palette.registerCommand({ id: 'export-print', title: '打印 / 另存为 PDF（浏览器）', run: () => exporter.exportPrint() });
  palette.registerCommand({ id: 'graph', title: '打开全局图谱', run: () => graph.openGlobalGraph() });
  palette.registerCommand({ id: 'graph-local', title: '显示本地图谱', run: () => shell.activatePanel('right', 'graph') });
  palette.registerCommand({ id: 'hotkeys', title: '快捷键设置', run: () => settings.openSettings() });
  palette.registerCommand({ id: 'focus-mode', title: '专注模式（隐藏/恢复两侧面板）', run: () => shell.toggleFocusMode() });
  // M2 Agent Link
  palette.registerCommand({ id: 'agent-settings', title: '打开 Agent Link 设置', run: () => settings.openSettings() });
  palette.registerCommand({ id: 'organize', title: '整理当前笔记（diff 确认）', run: () => organize.openOrganize() });
  palette.registerCommand({ id: 'chat', title: '打开内置智能体聊天', run: () => shell.activatePanel('right', 'chat') });
  palette.registerCommand({ id: 'history', title: '查看当前笔记快照时间线', run: () => history.openHistory() });
  palette.registerCommand({
    id: 'split-edit', title: '分屏编辑（两篇笔记对照）', hotkey: 'Ctrl+Shift+S',
    run: () => {
      (splitview.isSplitOpen() ? splitview.closeSplit() : splitview.activateSplit()).catch(reportError);
    },
  });
}

async function newNotePrompt(dir) {
  const name = await askText({
    title: '新建笔记',
    label: '笔记名称（可含子目录，如 notes/读书/随笔）',
    placeholder: '未命名',
    confirmText: '创建',
  });
  if (!name) return;
  try {
    const res = await api.post('/api/files/create-note', { dir, name });
    emit('tree-refresh');
    emit('open-note', res.path);
  } catch (err) { reportError(err); }
}

async function newDirPrompt() {
  const name = await askText({ title: '新建目录', label: '目录名称（可含父目录）', confirmText: '创建' });
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
  // 空态页快捷操作
  $('#btn-empty-new').addEventListener('click', () => newNotePrompt(''));
  $('#btn-empty-daily').addEventListener('click', () => templates.openDailyNote().catch(reportError));
  $('#btn-empty-switch').addEventListener('click', () => switcher.openSwitcher());
  // 双屏开关（与 Ctrl+Shift+S 同一入口语义）
  const splitBtn = $('#btn-split-toggle');
  splitBtn.addEventListener('click', () => {
    (splitview.isSplitOpen() ? splitview.closeSplit() : splitview.activateSplit()).catch(reportError);
  });
  on('split-changed', ({ open }) => {
    splitBtn.classList.toggle('btn--active', open);
    splitBtn.setAttribute('aria-pressed', String(open));
    splitBtn.title = open ? '关闭双屏 (Ctrl+Shift+S)' : '两篇笔记对照编辑 (Ctrl+Shift+S)';
  });
  // 写入冲突条：两个安全出口（重新载入 / 保留我的并另存）
  const conflictReload = $('#btn-conflict-reload');
  if (conflictReload) {
    conflictReload.addEventListener('click', () => {
      const path = conflictPath || (state.doc && state.doc.path);
      hideConflict();
      if (path) loadNote(path).catch(reportError);
    });
  }
  const conflictKeep = $('#btn-conflict-keep');
  if (conflictKeep) {
    conflictKeep.addEventListener('click', () => { saveConflictCopy().catch(reportError); });
  }
  // 状态栏路径点击复制
  $('#status-path').addEventListener('click', () => {
    if (state.activePath) copyPath(state.activePath);
  });
  // 侧栏折叠由 shell.js 绑定（ribbon 图标按钮）
  // 设置页打开时刷新自动推送状态（F2）
  $('#btn-settings')?.addEventListener('click', () => renderAutopushSettings().catch(reportError));
  window.addEventListener('beforeunload', (e) => {
    if (state.dirty) {
      saveNote(false);
      e.preventDefault();
      e.returnValue = '';
    }
  });
}

function setSaveState(text) {
  const el = $('#save-state');
  el.textContent = text;
  el.dataset.kind = saveStateKind(text);
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
