/* 设置模块出口：对外只暴露 initSettings / openSettings / closeSettings / saveSettings / saveAttachmentDir。
 * 数据流：openSettings 拉 /api/settings → state.settings → syncControls()；
 *         控件只派发 action → 本文件 HANDLERS → API → 事件总线（theme-changed / vault-opened）。
 * 渲染过程不读 API，只有 openSettings 会取数，因此重复打开不会产生重复 DOM。
 */

import { api, reportError } from '../api.js';
import { state, emit } from '../state.js';
import { $, toast, copyText, bindFolderPicker, escapeHtml } from '../utils.js';
import { APP_VERSION } from './schema.js';
import { initView, renderAll, syncControls, setGroup } from './render.js';
import { refreshSections } from './registry.js';
import { openCompile } from '../compile.js';

let initialized = false;

/* ------------------------------------------------------------------ 生命周期 */

export function initSettings() {
  if (initialized) return;
  if (!$('#settings-page')) return;
  initialized = true;

  initView({ handlers: HANDLERS, mounts: MOUNTS, getSettings: () => state.settings || {} });
  renderAll();

  $('#btn-settings')?.addEventListener('click', () => { openSettings(); });
  $('#btn-open-vault')?.addEventListener('click', () => { openSettings({ group: 'files' }); });
  bindFolderPicker('settings-vault-pick', 'settings-vault-path');
  $('#settings-page').addEventListener('keydown', onPageKeydown);
}

/** 打开设置：固定从「常规」进入（命令面板 / 工具栏行为一致），并拉一次最新设置。 */
export async function openSettings(opts = {}) {
  const page = $('#settings-page');
  if (!page) return;
  page.classList.remove('hidden');
  setGroup(opts.group || 'general');
  try {
    state.settings = await api.get('/api/settings');
  } catch { /* 未打开 vault：沿用上次内存快照，控件用 fallback */ }
  syncControls();
  refreshVaultInfo();
  refreshSections();
}

export function closeSettings() {
  const page = $('#settings-page');
  if (!page || page.classList.contains('hidden')) return;
  page.classList.add('hidden');
  if (page.contains(document.activeElement)) document.activeElement.blur();
}

function onPageKeydown(e) {
  if (e.key === 'Escape') {
    e.stopPropagation();
    closeSettings();
  }
}

/* ------------------------------------------------------------------ 保存 */

/** 保存设置（部分字段）并同步内存 state；失败已提示，返回 null。 */
export async function saveSettings(partial) {
  try {
    state.settings = await api.put('/api/settings', partial);
    return state.settings;
  } catch (err) {
    reportError(err);
    return null;
  }
}

/** 保存附件目录（保留导出，app.js 之外的调用方仍可用）。 */
export async function saveAttachmentDir() {
  return saveDir($('#settings-attachment-dir'));
}

/** 目录类设置统一提交：空值回落默认值，保存成功后回写规范化后的值。 */
async function saveDir(el) {
  if (!el) return null;
  const key = el.dataset.setting;
  const fallback = el.dataset.fallback || '';
  const value = String(el.value || '').trim() || fallback;
  const saved = await saveSettings({ [key]: value });
  if (!saved) return null;
  el.value = value;
  toast(`${el.dataset.label || '目录'}已保存: ${value}`, 'success');
  return saved;
}

/* ------------------------------------------------------------------ 行为 */

async function openVault() {
  const input = $('#settings-vault-path');
  const path = (input?.value || '').trim();
  if (!path) { toast('请输入 vault 绝对路径', 'error'); return; }
  try {
    const info = await api.post('/api/vault/open', { path });
    toast(`Vault 已打开: ${info.name}（${info.note_count} 篇笔记）`, 'success');
    closeSettings();
    emit('vault-opened', info);
  } catch (err) { reportError(err); }
}

async function copyMcpCommand() {
  const text = $('#agent-mcp-cmd')?.textContent || '';
  const ok = await copyText(text);
  toast(ok ? '启动命令已复制' : '复制失败：剪贴板不可用', ok ? 'success' : 'error', 1800);
}

const HANDLERS = {
  'settings.close': () => closeSettings(),

  'theme.change': async (value) => {
    const saved = await saveSettings({ theme: value });
    if (saved) emit('theme-changed', value);
  },

  'autosave.change': async (value) => {
    const ms = Number(value);
    const saved = await saveSettings({ autosave_delay_ms: ms });
    if (saved) toast(`自动保存延迟已保存: ${ms}ms`, 'success', 1800);
  },

  'fonts.change': async (value, el) => {
    const key = String((el && el.dataset.setting) || '').split('.')[1] || 'ui';
    const size = Number(value);
    if (!Number.isFinite(size)) return;
    const fonts = { ...(state.settings.fonts || {}), [key]: size };
    const saved = await saveSettings({ fonts });
    if (!saved) return;
    emit('fonts-changed', saved.fonts);
    toast(`${(el && el.dataset.label) || '字号'}已保存: ${size}px`, 'success', 1400);
  },

  'fonts.reset': async () => {
    const fonts = { ui: 13, sidebar: 13, editor: 15, chat: 13 };
    const saved = await saveSettings({ fonts });
    if (!saved) return;
    emit('fonts-changed', saved.fonts);
    syncControls();
    toast('字号已恢复默认', 'success', 1600);
  },

  'dir.change': (value, el) => saveDir(el),
  'vault.open': () => openVault(),
  'agent.copy-cmd': () => copyMcpCommand(),

  'goto.hotkeys': () => {
    setGroup('general');
    requestAnimationFrame(() => {
      $('#hotkeys-list')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  },

  'compile.open': () => {
    closeSettings();
    openCompile();
  },

  'about.copy-doc': async () => {
    const ok = await copyText('docs/ARCHITECTURE.md');
    toast(ok ? '文档路径已复制：docs/ARCHITECTURE.md' : '复制失败：剪贴板不可用',
      ok ? 'success' : 'error', 2400);
  },
};

/* ------------------------------------------------------- 复合行（escape hatch） */

const MOUNTS = {
  /** vault 绝对路径 + 系统文件夹选择 + 打开。 */
  'vault-row'(el) {
    el.innerHTML = `
      <input id="settings-vault-path" class="settings-input" type="text" spellcheck="false"
        placeholder="D:/notes" aria-label="Vault 绝对路径"/>
      <button type="button" id="settings-vault-pick" class="btn btn--ghost btn--mini hidden"
        title="选择文件夹">选择文件夹</button>
      <button type="button" id="btn-vault-open" class="btn btn--primary" data-action="vault.open">打开</button>`;
  },

  /** MCP 启动命令：等宽代码块 + 复制。 */
  'mcp-cmd'(el) {
    el.innerHTML = `
      <div class="settings-code">
        <code id="agent-mcp-cmd" class="agent-cmd">python kwey_mcp.py --vault "&lt;vault 绝对路径&gt;"</code>
        <button type="button" class="btn btn--mini" data-action="agent.copy-cmd">复制</button>
      </div>`;
  },

  /** 访问 Token：只读展示 + 新 token 权限 + 生成（事件由 agentlink.js 绑定）。 */
  'token-issue'(el) {
    el.innerHTML = `
      <input id="agent-token" class="settings-input" type="text" readonly
        placeholder="首次点击「生成」创建 admin token" aria-label="当前访问 token"/>
      <select id="agent-token-role" class="settings-input" aria-label="新 token 权限">
        <option value="read">只读</option>
        <option value="write" selected>读写</option>
        <option value="admin">管理</option>
      </select>
      <button type="button" id="btn-agent-token" class="btn">生成</button>`;
  },

  /** 关于：版本 / 当前 vault / 索引规模（vault 信息由 refreshVaultInfo 回填）。 */
  'about-meta'(el) {
    el.innerHTML = `
      <dl class="settings-meta">
        <div><dt>版本</dt><dd>${escapeHtml(APP_VERSION)}</dd></div>
        <div><dt>当前 vault</dt><dd id="about-vault" class="is-mono">—</dd></div>
        <div><dt>索引规模</dt><dd id="about-index">—</dd></div>
      </dl>`;
  },
};

/* ------------------------------------------------------------------ vault 信息 */

async function refreshVaultInfo() {
  const info = $('#settings-vault-info');
  const input = $('#settings-vault-path');
  const vault = $('#about-vault');
  const index = $('#about-index');
  try {
    const data = await api.get('/api/vault/info');
    if (info) info.textContent = `当前 vault：${data.path}（${data.note_count} 篇笔记 / ${data.tag_count} 个标签）`;
    if (input && !input.value) input.value = data.path || '';
    if (vault) vault.textContent = data.path || '—';
    if (index) index.textContent = `${data.note_count} 篇笔记 · ${data.tag_count} 个标签`;
  } catch {
    if (info) info.textContent = '当前未打开 vault';
    if (vault) vault.textContent = '—';
    if (index) index.textContent = '—';
  }
}
