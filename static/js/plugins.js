/* 插件系统前端（C3 / P2）：设置页插件列表 + 受控能力注入 + Blob URL 动态加载。
 *
 * 安全模型（受信插件，非沙箱）：
 * - 插件 main.js 经 Blob URL 动态 import，运行在主页面 JS 上下文；
 * - 能力面仅限注入的 api 对象，且按后端下发的 permissions 白名单逐项开启
 *   （未声明的能力属性不出现，而非置空——杜绝静默失败误导插件作者）；
 * - 命令注册强制 `plugin:<id>:<cmd>` 前缀，禁用即 unregisterCommand 全量撤销；
 * - 后端仅在「已启用」时下发入口代码（未启用 GET /entry → 403）。
 * 插件技术上仍可触碰 window 等全局（同 realm 不可硬隔离），因此本系统
 * 定位为「受信插件」：不加载来路不明的插件，能力以最小授予为原则。
 */

import { api, reportError } from './api.js';
import { registerCommand, unregisterCommand } from './palette.js';
import { on, emit } from './state.js';
import { escapeHtml, toast, confirmDialog } from './utils.js';
import { registerSection } from './settings/registry.js';

const loaded = new Map(); // id -> { commands:[], mod }

const PERM_LABELS = {
  read_notes: '读笔记',
  write_notes: '写笔记',
  read_graph: '读图谱',
  ui_command: '命令',
};

let listEl = null; // 插件列表容器（设置页自绘）

/** 设置页挂载入口：把插件市场注册为「扩展与外观」下的一张设置卡；vault 打开后刷新列表。 */
export function initPlugins() {
  if (listEl) return; // 已注册
  registerSection({
    id: 'plugins',
    domId: 'plugins-section',
    group: 'extension',
    order: 10,
    title: '插件市场',
    description: '发现并安装内置插件，拓展 Kwey 能力。插件以「受信」模式运行，权限白名单受控。',
    mount: (el) => {
      el.innerHTML = `
    <div class="market-toolbar">
      <div class="market-tabs" id="market-tabs">
        <button class="market-tab market-tab--active" data-cat="全部">全部</button>
        <button class="market-tab" data-cat="推荐">推荐</button>
        <button class="market-tab" data-cat="开发工具">开发工具</button>
        <button class="market-tab" data-cat="效率提升">效率提升</button>
        <button class="market-tab" data-cat="其他">其他</button>
      </div>
      <input id="market-search" class="market-search" type="text" placeholder="搜索插件…"/>
    </div>
    <div id="market-featured" class="market-featured"></div>
    <h4 class="market-subtitle">推荐</h4>
    <div id="plugins-list" class="market-grid"></div>
    <div class="market-manage">
      <strong>已安装</strong>
      <div id="plugins-installed" class="plugins-list"></div>
    </div>`;
      listEl = el.querySelector('#plugins-list');

      el.querySelector('#market-tabs').addEventListener('click', (e) => {
        const btn = e.target.closest('.market-tab');
        if (!btn) return;
        el.querySelectorAll('.market-tab').forEach((b) => b.classList.remove('market-tab--active'));
        btn.classList.add('market-tab--active');
        marketCategory = btn.dataset.cat;
        renderMarket();
      });
      el.querySelector('#market-search').addEventListener('input', (e) => {
        marketQuery = e.target.value.trim().toLowerCase();
        renderMarket();
      });
      listEl.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-install-id]');
        if (btn) installPlugin(btn.dataset.installId).catch(reportError);
      });
      const installedEl = el.querySelector('#plugins-installed');
      installedEl.addEventListener('change', (e) => {
        const box = e.target.closest('input[data-plugin-id]');
        if (box) togglePlugin(box.dataset.pluginId, box.checked).catch(reportError);
      });
      installedEl.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-uninstall-id]');
        if (btn) uninstallPlugin(btn.dataset.uninstallId).catch(reportError);
      });
    },
    refresh: () => refresh().catch(() => {}),
  });

  on('vault-opened', () => { refresh().catch(() => {}); });
  refresh().catch(() => {}); // 启动时已打开 vault 的场景
}

let marketCategory = '全部';
let marketQuery = '';
let catalogItems = [];

/** 拉取市场 + 已安装列表并渲染。 */
export async function refresh() {
  if (!listEl) return;
  const [cat, installed] = await Promise.all([
    api.get('/api/plugins/catalog'),
    api.get('/api/plugins/list'),
  ]);
  catalogItems = cat.plugins || [];
  renderMarket();
  renderInstalled(installed.plugins || []);
  // 恢复已启用插件的命令（设置页重开 / vault 切换后）
  for (const p of installed.plugins || []) {
    if (p.enabled) loadPlugin(p.id).catch(() => {});
  }
}

/** 市场网格：分类 tab + 搜索过滤 + 安装按钮。 */
function renderMarket() {
  if (!listEl) return;
  let items = catalogItems;
  if (marketCategory === '推荐') items = items.filter((p) => p.recommended);
  else if (marketCategory !== '全部') items = items.filter((p) => p.category === marketCategory);
  if (marketQuery) {
    items = items.filter((p) =>
      `${p.name} ${p.description || ''} ${p.id}`.toLowerCase().includes(marketQuery));
  }
  // 顶部推荐大卡片（仅「全部」分类展示）
  const featured = document.querySelector('#market-featured');
  if (featured) {
    const top = catalogItems.filter((p) => p.recommended).slice(0, 2);
    featured.innerHTML = marketCategory === '全部' && top.length
      ? `<div class="market-featured__row">${top.map((p) => `
          <div class="market-featured__card">
            <div class="market-featured__icon">${escapeHtml(p.icon || '🧩')}</div>
            <div><strong>${escapeHtml(p.name)}</strong>
              <div class="settings-muted">${escapeHtml(p.description || '')}</div></div>
          </div>`).join('')}</div>`
      : '';
  }
  if (!items.length) {
    listEl.innerHTML = '<p class="settings-muted">没有匹配的插件。</p>';
    return;
  }
  listEl.innerHTML = items.map((p) => `
    <div class="market-card" data-plugin="${escapeHtml(p.id)}">
      <div class="market-card__icon">${escapeHtml(p.icon || '🧩')}</div>
      <div class="market-card__body">
        <div class="market-card__title">${escapeHtml(p.name)} <span class="settings-muted">v${escapeHtml(p.version)}</span></div>
        <div class="market-card__desc">${escapeHtml(p.description || '')}</div>
        <div class="market-card__perms">${(p.permissions || []).map(
          (perm) => `<span class="result-item__hotkey">${escapeHtml(PERM_LABELS[perm] || perm)}</span>`,
        ).join(' ')}</div>
      </div>
      <button class="btn btn--mini ${p.installed ? '' : 'btn--primary'}" data-install-id="${escapeHtml(p.id)}"
        ${p.installed ? 'disabled' : ''}>${p.installed ? '已安装' : '安装'}</button>
    </div>`).join('');
}

/** 已安装区：启用开关 + 卸载。 */
function renderInstalled(plugins) {
  const box = document.querySelector('#plugins-installed');
  if (!box) return;
  if (!plugins.length) {
    box.innerHTML = '<p class="settings-muted">尚未安装插件。从上方市场安装。</p>';
    return;
  }
  box.innerHTML = plugins.map((p) => `
    <div class="settings-row" data-plugin-row="${escapeHtml(p.id)}">
      <div style="flex:1;min-width:0">
        <div><strong>${escapeHtml(p.name)}</strong>
          <span class="settings-muted">v${escapeHtml(p.version)} · ${escapeHtml(p.id)}</span></div>
        ${p.description ? `<div class="settings-muted">${escapeHtml(p.description)}</div>` : ''}
      </div>
      <label><input type="checkbox" data-plugin-id="${escapeHtml(p.id)}"
        ${p.enabled ? 'checked' : ''}/> 启用</label>
      <button class="btn btn--mini" data-uninstall-id="${escapeHtml(p.id)}">卸载</button>
    </div>`).join('');
}

/** 安装内置插件（写盘由后端完成），刷新列表。 */
async function installPlugin(id) {
  await api.post('/api/plugins/install', { id });
  toast('插件已安装，可在下方启用', 'success');
  await refresh();
}

/** 卸载插件：先撤销命令，再删盘。 */
async function uninstallPlugin(id) {
  if (!await confirmDialog(`确认卸载插件 ${id}？其注册的命令将一并移除。`, {
    title: '卸载插件', danger: true, confirmText: '卸载',
  })) return;
  unloadPlugin(id);
  await api.post('/api/plugins/uninstall', { id });
  toast('插件已卸载', 'success');
  await refresh();
}

/** 启用/禁用：以后端 toggle 为准，成功后本地加载 / 卸载。 */
async function togglePlugin(id, enabled) {
  await api.post('/api/plugins/toggle', { id, enabled });
  if (enabled) {
    await loadPlugin(id);
    toast(`插件已启用: ${id}`);
  } else {
    unloadPlugin(id);
    toast(`插件已禁用: ${id}`);
  }
  await refresh();
}

/** 拉入口代码（含 manifest）→ Blob URL 动态 import → 注入受控 API → 调 setup。 */
async function loadPlugin(id) {
  if (loaded.has(id)) return;
  const manifest = await api.get('/api/plugins/entry', { id });
  const blob = new Blob([manifest.code], { type: 'text/javascript' });
  const url = URL.createObjectURL(blob);
  try {
    const mod = await import(url); // 模块已实例化，revoke 不影响已加载副本
    const papi = buildPluginApi(manifest);
    loaded.set(id, { commands: papi.__commands, mod });
    if (typeof mod.setup === 'function') await mod.setup(papi);
  } catch (err) {
    unloadPlugin(id); // setup 抛错也撤销已注册命令，不留半挂状态
    throw err;
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** 卸载：撤销全部命令注册 + 调插件 unload 钩子（若有）。 */
function unloadPlugin(id) {
  const rec = loaded.get(id);
  if (!rec) return;
  for (const cid of rec.commands) unregisterCommand(cid);
  try {
    if (rec.mod && typeof rec.mod.unload === 'function') rec.mod.unload();
  } catch (err) { console.error('[kwey] 插件 unload 失败', id, err); }
  loaded.delete(id);
}

/**
 * 按权限白名单构造注入给插件的 API 对象：未声明的能力属性不出现。
 * 命令 id 强制 `plugin:<id>:<cmd>` 前缀，标题带插件名便于面板辨识。
 */
function buildPluginApi(manifest) {
  const { id, name, permissions } = manifest;
  const commands = [];
  const apiObj = {
    id, name,
    // 基础 UI 能力：不需额外权限声明（仅提示与打开笔记，不越权读写）
    toast: (msg, type, ms) => toast(msg, type, ms),
    openNote: (path) => emit('open-note', path),
  };
  let notes;

  if (permissions.includes('ui_command')) {
    apiObj.registerCommand = (cmdId, def) => {
      const full = `plugin:${id}:${cmdId}`;
      registerCommand({
        id: full,
        title: `[${name}] ${def && def.title ? def.title : cmdId}`,
        hotkey: def && def.hotkey,
        run: def && def.run,
      });
      commands.push(full);
    };
  }
  if (permissions.includes('read_notes')) {
    notes = { read: (path) => api.get('/api/note', { path }) };
    apiObj.search = (q, limit = 50) => api.get('/api/search', { q, limit });
  }
  if (permissions.includes('write_notes')) {
    notes = notes || {};
    // 直通 VaultService 统一写路径：POST 建 / PUT 存（base_mtime 冲突检测）
    notes.create = (path, content, frontmatter) =>
      api.post('/api/note', { path, content, frontmatter });
    notes.write = (path, content, frontmatter, base_mtime) =>
      api.put('/api/note', { path, content, frontmatter, base_mtime });
  }
  if (notes) apiObj.notes = notes;
  if (permissions.includes('read_graph')) {
    apiObj.graph = {
      local: (path, depth = 1) => api.get('/api/graph/local', { path, depth }),
    };
  }
  apiObj.__commands = commands; // 供卸载撤销
  return apiObj;
}
