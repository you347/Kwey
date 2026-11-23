/* Bases 式结构化视图：frontmatter → 表格 / 卡片 / 列表。
 *
 * 定位：对标 Obsidian Bases（2025 爆款），差异点是「自然语言建视图」——
 * 描述一句"这周要交的项目按截止日期排"，AI 产出定义，人确认后落盘。
 *
 * 约束：不新增 index.html 面板依赖（工具栏/侧栏都在浮层内），样式注入走主题变量，
 * 零第三方依赖；所有写操作经 /api/views/cell → VaultService 统一写路径。
 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, $all, escapeHtml, debounce, toast } from './utils.js';
import { registerCommand } from './palette.js';
import { askConfirm } from './dialog.js';

export const COMMAND_ID = 'views.open';
const STYLE_ID = 'bases-style';
const LAST_SLUG = 'kwey.views.last';
const PAGE = 200;

const bs = {
  initialized: false,
  open: false,
  views: [],
  slug: null,
  def: null,
  result: null,
  schema: null,
  offset: 0,
  prevFocus: null,
  dirty: false,
  loading: false,
};

let dom = null;

/* ------------------------------------------------------------------ 初始化 */
export function initBases() {
  if (bs.initialized) return;
  bs.initialized = true;
  registerCommand({
    id: COMMAND_ID,
    title: '打开视图（Bases）',
    run: () => { openBases().catch(reportError); },
  });
  const ribbon = $('#btn-bases');
  if (ribbon) ribbon.addEventListener('click', () => { openBases().catch(reportError); });
}

export async function openBases(slug) {
  ensureDom();
  bs.open = true;
  bs.prevFocus = document.activeElement;
  dom.overlay.classList.remove('hidden');
  dom.search.value = '';
  await reloadList(slug);
}

export function closeBases() {
  if (!bs.open) return;
  bs.open = false;
  dom.overlay.classList.add('hidden');
  hideModal();
  try { bs.prevFocus && bs.prevFocus.focus({ preventScroll: true }); } catch { /* 已卸载 */ }
}

/* ------------------------------------------------------------------ 数据 */
async function reloadList(prefer) {
  try {
    const data = await api.get('/api/views');
    bs.views = data.items || [];
  } catch (err) {
    renderEmpty(`视图列表读取失败：${err.message || err}`);
    return;
  }
  renderList();
  if (!bs.views.length) {
    bs.slug = null;
    bs.def = null;
    bs.result = null;
    renderEmpty();
    renderHeadMeta();
    return;
  }
  const last = localStorage.getItem(LAST_SLUG);
  const target =
    (prefer && prefer.definition ? prefer : null) ||
    (prefer && bs.views.find((v) => v.slug === prefer)) ||
    bs.views.find((v) => v.slug === bs.slug) ||
    bs.views.find((v) => v.slug === last) ||
    bs.views[0];
  await selectView(target.slug);
}

async function selectView(slug) {
  const item = bs.views.find((v) => v.slug === slug);
  if (!item) return;
  bs.slug = slug;
  bs.def = item.definition;
  bs.offset = 0;
  bs.dirty = false;
  localStorage.setItem(LAST_SLUG, slug);
  renderList();
  await loadSchema();
  await query();
}

async function loadSchema() {
  if (bs.schema) return;
  try { bs.schema = await api.get('/api/views/schema'); } catch { bs.schema = { keys: [], total: 0 }; }
}

async function query(append = false) {
  if (!bs.def || bs.loading) return;
  bs.loading = true;
  if (!append) bs.offset = 0;
  try {
    const data = await api.post('/api/views/query', {
      slug: bs.slug,
      definition: bs.def,
      offset: bs.offset,
      limit: PAGE,
    });
    if (append && bs.result) {
      bs.result = { ...data, rows: [...bs.result.rows, ...data.rows] };
    } else {
      bs.result = data;
    }
    renderContent();
    renderFoot();
    renderHeadMeta();
  } catch (err) {
    renderEmpty(`查询失败：${err.message || err}`);
  } finally {
    bs.loading = false;
  }
}

/* ------------------------------------------------------------------ 渲染：外壳 */
function ensureDom() {
  if (dom) return;
  injectStyle();

  const overlay = document.createElement('div');
  overlay.id = 'overlay-bases';
  overlay.className = 'overlay hidden';
  overlay.innerHTML = `
    <div id="bases-panel" class="overlay__panel" style="width:min(1280px,96vw);height:min(88vh,900px);max-height:none;display:flex;flex-direction:column;padding:0;overflow:hidden">
      <div id="bases-head">
        <svg class="icon" aria-hidden="true"><use href="#i-columns"/></svg>
        <span class="bases-title">视图</span>
        <span id="bases-head-meta" class="settings-muted"></span>
        <span class="bases-head-actions">
          <button id="btn-bases-ai" class="btn btn--mini" type="button" title="用一句话描述想看的笔记">AI 建视图</button>
          <button id="btn-bases-def" class="btn btn--mini" type="button" title="编辑当前视图的定义（JSON）">定义</button>
          <button id="btn-bases-new" class="btn btn--mini" type="button">新建</button>
          <button id="btn-bases-refresh" class="btn btn--mini" type="button" title="重新读取索引">刷新</button>
          <button id="btn-bases-close" class="btn btn--ghost btn--mini" type="button" title="关闭 (Esc)">关闭</button>
        </span>
      </div>
      <div id="bases-body">
        <aside id="bases-side">
          <div class="bases-side__head">我的视图</div>
          <div id="bases-list" class="bases-list"></div>
          <div class="bases-side__foot">
            <button id="btn-bases-new2" class="btn btn--mini" type="button">＋ 新建视图</button>
          </div>
        </aside>
        <main id="bases-main">
          <div id="bases-toolbar">
            <input id="bases-search" type="search" placeholder="在结果中筛选…" aria-label="筛选">
            <select id="bases-mode" title="展示方式" aria-label="展示方式">
              <option value="table">表格</option>
              <option value="cards">卡片</option>
              <option value="list">列表</option>
            </select>
            <select id="bases-group" title="分组" aria-label="分组"></select>
            <span id="bases-toolbar-meta" class="settings-muted"></span>
          </div>
          <div id="bases-content" class="bases-content"></div>
          <div id="bases-foot" class="bases-foot"></div>
        </main>
      </div>
      <div class="overlay__hint">
        单击表头排序 · 单击单元格直接改属性（走统一写路径，自动进快照）· 双击标题打开笔记 · Esc 关闭
      </div>
      <div id="bases-modal" class="bases-modal hidden"></div>
    </div>`;
  document.body.appendChild(overlay);

  dom = {
    overlay,
    panel: $('#bases-panel', overlay),
    headMeta: $('#bases-head-meta', overlay),
    list: $('#bases-list', overlay),
    search: $('#bases-search', overlay),
    mode: $('#bases-mode', overlay),
    group: $('#bases-group', overlay),
    toolbarMeta: $('#bases-toolbar-meta', overlay),
    content: $('#bases-content', overlay),
    foot: $('#bases-foot', overlay),
    modal: $('#bases-modal', overlay),
  };

  $('#btn-bases-close', overlay).addEventListener('click', closeBases);
  $('#btn-bases-refresh', overlay).addEventListener('click', () => {
    bs.schema = null;
    query().catch(reportError);
    toast('已刷新', 'success', 1200);
  });
  $('#btn-bases-new', overlay).addEventListener('click', openNewDialog);
  $('#btn-bases-new2', overlay).addEventListener('click', openNewDialog);
  $('#btn-bases-ai', overlay).addEventListener('click', openAiDialog);
  $('#btn-bases-def', overlay).addEventListener('click', openDefinitionDialog);

  dom.mode.addEventListener('change', () => {
    if (!bs.def) return;
    bs.def.mode = dom.mode.value;
    persistDef().catch(reportError);
    renderContent();
  });
  dom.group.addEventListener('change', () => {
    if (!bs.def) return;
    bs.def.group_by = dom.group.value;
    persistDef().catch(reportError);
    query().catch(reportError);
  });
  dom.search.addEventListener('input', debounce(() => {
    if (!bs.def) return;
    bs.def.search = dom.search.value.trim();
    query().catch(reportError);
  }, 260));

  overlay.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!dom.modal.classList.contains('hidden')) { hideModal(); e.stopPropagation(); return; }
    closeBases();
    e.stopPropagation();
  });
}

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
#bases-panel { background: var(--bg); color: var(--text); }
#bases-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  border-bottom: 1px solid var(--border); flex: none; }
#bases-head .bases-title { font-weight: 600; font-size: var(--fs-md); }
.bases-head-actions { margin-left: auto; display: flex; gap: 6px; align-items: center; }
#bases-body { display: flex; flex: 1; min-height: 0; }
#bases-side { width: 208px; flex: none; border-right: 1px solid var(--border);
  display: flex; flex-direction: column; background: var(--bg-panel); }
.bases-side__head { padding: 10px 12px 6px; font-size: var(--fs-xs); color: var(--text-dim);
  letter-spacing: .04em; }
.bases-list { flex: 1; overflow: auto; padding: 0 6px 8px; }
.bases-item { display: flex; align-items: center; gap: 6px; padding: 6px 8px; border-radius: var(--radius-sm);
  cursor: pointer; font-size: var(--fs-sm); color: var(--text); }
.bases-item:hover { background: var(--bg-hover, rgba(127,127,127,.08)); }
.bases-item.is-active { background: var(--accent-faint); color: var(--text); font-weight: 600; }
.bases-item__name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bases-item__count { font-size: var(--fs-xs); color: var(--text-dim); }
.bases-side__foot { padding: 8px; border-top: 1px solid var(--border-faint); }
.bases-side__foot .btn { width: 100%; }
#bases-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
#bases-toolbar { display: flex; gap: 8px; align-items: center; padding: 8px 12px;
  border-bottom: 1px solid var(--border-faint); flex: none; }
#bases-search { flex: 1; min-width: 120px; max-width: 340px; }
#bases-toolbar select { max-width: 170px; }
#bases-toolbar-meta { margin-left: auto; font-size: var(--fs-xs); }
.bases-content { flex: 1; overflow: auto; min-height: 0; }
.bases-foot { flex: none; padding: 6px 12px; border-top: 1px solid var(--border-faint);
  font-size: var(--fs-xs); color: var(--text-dim); display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.bases-table { border-collapse: separate; border-spacing: 0; width: max-content; min-width: 100%; font-size: var(--fs-sm); }
.bases-table th, .bases-table td { border-bottom: 1px solid var(--border-faint);
  border-right: 1px solid var(--border-faint); padding: 0; text-align: left; vertical-align: top; }
.bases-table th { position: sticky; top: 0; z-index: 2; background: var(--bg-panel);
  font-weight: 600; white-space: nowrap; cursor: pointer; user-select: none; }
.bases-table th .bases-th { display: flex; gap: 6px; align-items: center; padding: 7px 10px; font-size: var(--fs-xs); }
.bases-th__type { color: var(--text-dim); font-weight: 400; font-size: 10px; }
.bases-th__sort { color: var(--accent); }
.bases-table td .bases-cell { padding: 6px 10px; min-height: 30px; box-sizing: border-box;
  cursor: text; white-space: pre-wrap; word-break: break-word; min-width: 60px; }
.bases-table td.is-title .bases-cell { color: var(--accent); cursor: pointer; font-weight: 500; }
.bases-table td.is-readonly .bases-cell { cursor: default; color: var(--text-dim); }
.bases-cell.is-empty { color: var(--text-dim); opacity: .55; }
.bases-cell--num { text-align: right; font-variant-numeric: tabular-nums; }
.bases-cell--check { font-size: 15px; text-align: center; }
.bases-cell--check.is-on { color: var(--accent); }
.bases-tag { display: inline-block; padding: 0 6px; margin: 1px 3px 1px 0; border-radius: 999px;
  background: var(--accent-faint); font-size: var(--fs-xs); color: var(--text); }
.bases-cell-input { width: 100%; box-sizing: border-box; padding: 4px 6px; font-size: var(--fs-sm);
  background: var(--bg); color: var(--text); border: 1px solid var(--accent); border-radius: var(--radius-sm); }
.bases-group-head { position: sticky; left: 0; padding: 8px 12px; margin: 0; font-size: var(--fs-xs);
  color: var(--text-dim); background: var(--bg-panel); border-bottom: 1px solid var(--border); }
.bases-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px; padding: 12px; }
.bases-card { border: 1px solid var(--border); border-radius: var(--radius-md); padding: 10px 12px;
  background: var(--bg-panel); cursor: pointer; display: flex; flex-direction: column; gap: 6px; }
.bases-card:hover { border-color: var(--accent); }
.bases-card__title { font-weight: 600; font-size: var(--fs-sm); }
.bases-card__excerpt { font-size: var(--fs-xs); color: var(--text-dim); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.bases-card__props { display: flex; flex-wrap: wrap; gap: 4px 8px; font-size: var(--fs-xs); color: var(--text-dim); }
.bases-card__prop b { color: var(--text); font-weight: 500; }
.bases-list__row { display: flex; gap: 8px; align-items: baseline; padding: 7px 12px;
  border-bottom: 1px solid var(--border-faint); cursor: pointer; font-size: var(--fs-sm); }
.bases-list__row:hover { background: var(--bg-hover, rgba(127,127,127,.06)); }
.bases-list__meta { margin-left: auto; font-size: var(--fs-xs); color: var(--text-dim); white-space: nowrap; }
.bases-empty { padding: 48px 24px; text-align: center; color: var(--text-dim); font-size: var(--fs-sm); }
.bases-empty h3 { color: var(--text); font-size: var(--fs-md); margin: 0 0 8px; }
.bases-empty p { margin: 0 auto 14px; max-width: 460px; line-height: 1.7; }
.bases-empty__actions { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
.bases-more { padding: 10px; text-align: center; }
.bases-modal { position: absolute; inset: 0; background: rgba(0,0,0,.42); display: flex;
  align-items: center; justify-content: center; z-index: 20; padding: 20px; }
.bases-modal.hidden { display: none; }
.bases-dialog { width: min(560px, 92vw); max-height: 82vh; overflow: auto; background: var(--bg);
  border: 1px solid var(--border); border-radius: var(--radius-md); box-shadow: var(--shadow-soft);
  padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.bases-dialog h3 { margin: 0; font-size: var(--fs-md); }
.bases-dialog label { font-size: var(--fs-xs); color: var(--text-dim); display: block; margin-bottom: 3px; }
.bases-dialog input[type=text], .bases-dialog textarea, .bases-dialog select {
  width: 100%; box-sizing: border-box; }
.bases-dialog textarea { min-height: 66px; resize: vertical; }
.bases-dialog__grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.bases-dialog__actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px; }
.bases-dialog__hint { font-size: var(--fs-xs); color: var(--text-dim); line-height: 1.6; }
.bases-dialog__explain { padding: 8px 10px; border-radius: var(--radius-sm); background: var(--accent-faint);
  font-size: var(--fs-sm); line-height: 1.6; }
.bases-preview { border: 1px solid var(--border-faint); border-radius: var(--radius-sm);
  max-height: 180px; overflow: auto; font-size: var(--fs-xs); }
.bases-preview__row { display: flex; gap: 8px; padding: 5px 8px; border-bottom: 1px solid var(--border-faint); }
.bases-preview__row span:last-child { margin-left: auto; color: var(--text-dim); white-space: nowrap; }
`;
  document.head.appendChild(style);
}

/* ------------------------------------------------------------------ 渲染：列表/头部 */
function renderList() {
  if (!dom) return;
  dom.list.innerHTML = bs.views.map((item) => {
    const active = item.slug === bs.slug ? ' is-active' : '';
    const modeIcon = item.mode === 'cards' ? '▦' : item.mode === 'list' ? '☰' : '▤';
    return `<div class="bases-item${active}" data-slug="${escapeHtml(item.slug)}" title="${escapeHtml(item.path)}">
      <span>${modeIcon}</span>
      <span class="bases-item__name">${escapeHtml(item.name || item.slug)}</span>
    </div>`;
  }).join('');
  for (const el of $all('.bases-item', dom.list)) {
    el.addEventListener('click', () => selectView(el.dataset.slug).catch(reportError));
  }
}

function renderHeadMeta() {
  if (!dom) return;
  if (!bs.def) { dom.headMeta.textContent = ''; return; }
  const count = bs.result ? bs.result.total : '…';
  dom.headMeta.textContent = `${bs.def.name} · ${count} 篇`;
  dom.mode.value = bs.def.mode || 'table';
  renderGroupOptions();
}

function renderGroupOptions() {
  if (!dom || !bs.def) return;
  const keys = (bs.schema && bs.schema.keys) || [];
  const options = ['<option value="">不分组</option>'];
  for (const key of keys) {
    if (!key || key.builtin && !['tags'].includes(key.key)) continue;
    const selected = bs.def.group_by === key.key ? ' selected' : '';
    options.push(`<option value="${escapeHtml(key.key)}"${selected}>按 ${escapeHtml(key.label || key.key)}</option>`);
  }
  dom.group.innerHTML = options.join('');
  dom.group.value = bs.def.group_by || '';
}

/* ------------------------------------------------------------------ 渲染：内容 */
function renderContent() {
  if (!dom) return;
  const result = bs.result;
  if (!result || !result.rows) { renderEmpty(); return; }
  if (!result.rows.length) { renderEmpty('没有匹配的笔记', true); return; }

  if (result.mode === 'cards') return renderCards(result);
  if (result.mode === 'list') return renderListMode(result);
  renderTable(result);
}

function showEmpty(message, inline = false) {
  const actions = inline ? '' : `
    <div class="bases-empty__actions">
      <button class="btn btn--mini btn--primary" data-act="ai">用一句话建视图</button>
      <button class="btn btn--mini" data-act="new">手动新建</button>
      <button class="btn btn--mini" data-act="template">从模板开始</button>
    </div>`;
  dom.content.innerHTML = `<div class="bases-empty">
    <h3>${inline ? escapeHtml(message) : '还没有视图'}</h3>
    ${inline ? '' : `<p>视图把笔记的 frontmatter 变成表格/卡片：按标签、目录、属性筛选，直接在格子里改值。
      也可以让 AI 听懂一句话——「这周要交的项目，按截止日期排」。</p>`}
    ${actions}
  </div>`;
  for (const btn of $all('[data-act]', dom.content)) {
    btn.addEventListener('click', () => {
      const act = btn.dataset.act;
      if (act === 'ai') openAiDialog();
      else if (act === 'new') openNewDialog();
      else createFromTemplate();
    });
  }
}

function renderTable(result) {
  const cols = result.columns || [];
  const sortMap = new Map((bs.def.sort || []).map((s) => [s.key, s.dir]));
  const head = cols.map((col) => {
    const dir = sortMap.get(col.key);
    const mark = dir === 'asc' ? '↑' : dir === 'desc' ? '↓' : '';
    return `<th data-key="${escapeHtml(col.key)}" title="单击排序：升序 → 降序 → 取消">
      <div class="bases-th">
        <span>${escapeHtml(col.label || col.key)}</span>
        <span class="bases-th__type">${typeLabel(col.type)}</span>
        <span class="bases-th__sort">${mark}</span>
      </div></th>`;
  }).join('');

  const groups = groupRows(result);
  const body = groups.map((group) => {
    const headRow = group.label === null ? '' :
      `<tr class="is-group"><td colspan="${cols.length}"><div class="bases-group-head">${escapeHtml(group.label)} · ${group.rows.length}</div></td></tr>`;
    const rows = group.rows.map((row) => {
      const cells = cols.map((col) => {
        const cls = col.key === 'title' ? 'is-title' : '';
        return `<td class="${cls}" data-path="${escapeHtml(row.path)}" data-key="${escapeHtml(col.key)}">
          ${cellHtml(row, col)}</td>`;
      }).join('');
      return `<tr>${cells}</tr>`;
    }).join('');
    return headRow + rows;
  }).join('');

  dom.content.innerHTML = `<table class="bases-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>` +
    (result.returned < result.total ? `<div class="bases-more"><button class="btn btn--mini" id="bases-more">加载更多（还有 ${result.total - result.returned} 篇）</button></div>` : '');

  for (const th of $all('th[data-key]', dom.content)) {
    th.addEventListener('click', () => toggleSort(th.dataset.key));
  }
  for (const td of $all('td[data-path]', dom.content)) {
    td.addEventListener('click', (e) => onCellClick(td, e));
  }
  const more = $('#bases-more', dom.content);
  if (more) more.addEventListener('click', () => {
    bs.offset = bs.result.returned || 0;
    query(true).catch(reportError);
  });
}

function groupRows(result) {
  if (!result.group_by || !result.groups || !result.groups.length) {
    return [{ label: null, rows: result.rows }];
  }
  const buckets = new Map();
  for (const row of result.rows) {
    const raw = row.values[result.group_by];
    const label = raw === null || raw === undefined || raw === '' || (Array.isArray(raw) && !raw.length)
      ? '（空）'
      : Array.isArray(raw) ? raw.join(', ') : String(raw);
    if (!buckets.has(label)) buckets.set(label, []);
    buckets.get(label).push(row);
  }
  const order = new Map((result.groups || []).map((g, index) => [g.label, index]));
  return [...buckets.entries()]
    .sort((a, b) => (order.get(a[0]) ?? 99) - (order.get(b[0]) ?? 99))
    .map(([label, rows]) => ({ label, rows }));
}

function typeLabel(type) {
  return { number: '#', date: '📅', checkbox: '☑', list: '⋯', text: 'Aa' }[type] || '';
}

function formatValue(value, type) {
  if (value === null || value === undefined || value === '') return '';
  if (Array.isArray(value)) return value.join(', ');
  if (type === 'date' && typeof value === 'string') return value.slice(0, 16).replace('T', ' ');
  if (type === 'number' && typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : String(Math.round(value * 1000) / 1000);
  }
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}

function cellHtml(row, col) {
  const value = row.values[col.key];
  const empty = value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length);
  if (col.type === 'checkbox' && !empty) {
    return `<div class="bases-cell bases-cell--check ${value ? 'is-on' : ''}">${value ? '☑' : '☐'}</div>`;
  }
  if (Array.isArray(value)) {
    const tags = value.map((v) => `<span class="bases-tag">${escapeHtml(String(v))}</span>`).join('');
    return `<div class="bases-cell">${tags || '<span class="bases-cell is-empty">—</span>'}</div>`;
  }
  const text = formatValue(value, col.type);
  const cls = ['bases-cell', col.type === 'number' ? 'bases-cell--num' : '', empty ? 'is-empty' : '']
    .filter(Boolean).join(' ');
  return `<div class="${cls}">${empty ? '—' : escapeHtml(text)}</div>`;
}

function renderCards(result) {
  const cols = (result.columns || []).filter((c) => c.key !== 'title');
  const html = result.rows.map((row) => {
    const props = cols.slice(0, 5).map((col) => {
      const text = formatValue(row.values[col.key], col.type);
      if (!text) return '';
      return `<span class="bases-card__prop">${escapeHtml(col.label || col.key)} <b>${escapeHtml(text)}</b></span>`;
    }).join('');
    return `<div class="bases-card" data-path="${escapeHtml(row.path)}">
      <div class="bases-card__title">${escapeHtml(row.title || row.path)}</div>
      <div class="bases-card__excerpt">${escapeHtml(row.excerpt || '（无正文）')}</div>
      <div class="bases-card__props">${props}</div>
    </div>`;
  }).join('');
  const more = result.returned < result.total
    ? `<div class="bases-more"><button class="btn btn--mini" id="bases-more">加载更多（还有 ${result.total - result.returned} 篇）</button></div>`
    : '';
  dom.content.innerHTML = `<div class="bases-cards">${html}</div>${more}`;
  for (const card of $all('.bases-card', dom.content)) {
    card.addEventListener('dblclick', () => openNote(card.dataset.path));
    card.addEventListener('click', () => openNote(card.dataset.path));
  }
  const btn = $('#bases-more', dom.content);
  if (btn) btn.addEventListener('click', () => { bs.offset = bs.result.returned; query(true).catch(reportError); });
}

function renderListMode(result) {
  const cols = (result.columns || []).filter((c) => c.key !== 'title').slice(0, 3);
  const html = result.rows.map((row) => {
    const meta = cols.map((col) => formatValue(row.values[col.key], col.type)).filter(Boolean).join(' · ');
    return `<div class="bases-list__row" data-path="${escapeHtml(row.path)}">
      <span>${escapeHtml(row.title || row.path)}</span>
      <span class="bases-list__meta">${escapeHtml(meta)}</span>
    </div>`;
  }).join('');
  dom.content.innerHTML = `<div class="bases-listview">${html}</div>`;
  for (const row of $all('.bases-list__row', dom.content)) {
    row.addEventListener('click', () => openNote(row.dataset.path));
  }
}

function renderFoot() {
  if (!dom) return;
  const result = bs.result;
  if (!result) { dom.foot.innerHTML = ''; return; }
  const parts = [`命中 <b>${result.total}</b> 篇`, `显示 ${result.returned}`];
  const agg = result.aggregates || {};
  for (const col of result.columns || []) {
    const stats = agg[col.key];
    if (!stats || stats.sum === undefined) continue;
    parts.push(`${escapeHtml(col.label || col.key)} 合计 <b>${stats.sum}</b>`);
  }
  if (bs.def && bs.def.folder) parts.push(`目录 <code>${escapeHtml(bs.def.folder)}</code>`);
  if (bs.def && (bs.def.tags || []).length) parts.push(`标签 ${bs.def.tags.map((t) => `#${escapeHtml(t)}`).join(' ')}`);
  dom.toolbarMeta.textContent = bs.def && bs.def.mode === 'table' ? '单击表头排序' : '';
  dom.foot.innerHTML = parts.join(' <span style="opacity:.4">·</span> ');
}

/* ------------------------------------------------------------------ 交互 */
function toggleSort(key) {
  if (!bs.def) return;
  const current = (bs.def.sort || []).find((s) => s.key === key);
  if (!current) bs.def.sort = [{ key, dir: 'asc' }];
  else if (current.dir === 'asc') bs.def.sort = [{ key, dir: 'desc' }];
  else bs.def.sort = [];
  persistDef().catch(reportError);
  query().catch(reportError);
}

function onCellClick(td, event) {
  if (td.classList.contains('is-title')) {
    openNote(td.dataset.path);
    return;
  }
  const key = td.dataset.key;
  const path = td.dataset.path;
  const col = (bs.result.columns || []).find((c) => c.key === key) || { key, type: 'text' };
  if (['path', 'folder', 'excerpt', 'mtime', 'size'].includes(key)) return;
  const row = bs.result.rows.find((r) => r.path === path);
  if (!row) return;

  if (col.type === 'checkbox') {
    commitCell(td, row, key, !row.values[key]);
    return;
  }
  if (td.querySelector('input')) return;

  const original = row.values[key];
  const text = Array.isArray(original) ? original.join(', ') : (original ?? '');
  const input = document.createElement('input');
  input.className = 'bases-cell-input';
  input.type = col.type === 'number' ? 'number' : col.type === 'date' ? 'date' : 'text';
  input.value = col.type === 'date' ? String(text).slice(0, 10) : String(text);
  const listId = `bases-suggest-${key.replace(/[^\w-]/g, '')}`;
  if (col.type === 'text' && bs.schema) {
    const meta = (bs.schema.keys || []).find((k) => k.key === key);
    if (meta && meta.values && meta.values.length) {
      input.setAttribute('list', listId);
      ensureDatalist(listId, meta.values.map((v) => v.value));
    }
  }
  td.innerHTML = '';
  td.appendChild(input);
  input.focus();
  input.select();

  let done = false;
  const finish = (commit) => {
    if (done) return;
    done = true;
    if (commit && input.value !== String(text)) {
      const value = col.type === 'number'
        ? (input.value === '' ? null : Number(input.value))
        : input.value;
      commitCell(td, row, key, value);
    } else {
      td.innerHTML = cellHtml(row, col);
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

function ensureDatalist(id, values) {
  let list = document.getElementById(id);
  if (!list) {
    list = document.createElement('datalist');
    list.id = id;
    document.body.appendChild(list);
  }
  list.innerHTML = values.map((v) => `<option value="${escapeHtml(String(v))}"></option>`).join('');
}

async function commitCell(td, row, key, value) {
  const col = (bs.result.columns || []).find((c) => c.key === key) || { key, type: 'text' };
  td.innerHTML = '<div class="bases-cell" style="opacity:.5">保存中…</div>';
  try {
    const res = await api.put('/api/views/cell', { path: row.path, key, value });
    row.values[key] = res.value;
    td.innerHTML = cellHtml(row, col);
    td.style.transition = 'background .4s';
    td.style.background = 'var(--accent-faint)';
    setTimeout(() => { td.style.background = ''; }, 600);
    renderFoot();
  } catch (err) {
    td.innerHTML = cellHtml(row, col);
    toast(`写入失败：${err.message}`, 'error');
  }
}

function openNote(path) {
  closeBases();
  emit('open-note', path);
}

async function persistDef() {
  if (!bs.def || !bs.slug) return;
  bs.dirty = false;
  try {
    const saved = await api.put(`/api/views/${encodeURIComponent(bs.slug)}`, { definition: bs.def });
    const item = bs.views.find((v) => v.slug === bs.slug);
    if (item) { item.definition = saved.definition; item.name = saved.definition.name; item.mode = saved.definition.mode; }
    renderHeadMeta();
  } catch (err) {
    toast(`视图保存失败：${err.message}`, 'error');
  }
}

/* ------------------------------------------------------------------ 弹窗 */
function hideModal() {
  if (dom) dom.modal.classList.add('hidden');
}

function showModal(html) {
  dom.modal.innerHTML = html;
  dom.modal.classList.remove('hidden');
  return dom.modal;
}

function openNewDialog() {
  const html = `<div class="bases-dialog">
    <h3>新建视图</h3>
    <div><label>名字</label><input id="bv-name" type="text" placeholder="项目进度" value=""></div>
    <div class="bases-dialog__grid">
      <div><label>目录（留空=全库）</label><input id="bv-folder" type="text" placeholder="projects"></div>
      <div><label>标签（逗号分隔）</label><input id="bv-tags" type="text" placeholder="project, 进行中"></div>
    </div>
    <div class="bases-dialog__grid">
      <div><label>展示方式</label>
        <select id="bv-mode"><option value="table">表格</option><option value="cards">卡片</option><option value="list">列表</option></select>
      </div>
      <div><label>每页条数</label><input id="bv-limit" type="number" min="1" max="2000" value="200"></div>
    </div>
    <div><label>列（逗号分隔，留空自动选）</label><input id="bv-columns" type="text" placeholder="title, status, due"></div>
    <div><label>筛选条件（可空）</label><input id="bv-where" type="text" placeholder="status != done and priority >= 2"></div>
    <div class="bases-dialog__hint">条件支持的运算符：= != &gt; &gt;= &lt; &lt;= contains in exists empty before after，用 and 连接。</div>
    <div class="bases-dialog__actions">
      <button class="btn btn--mini" data-close>取消</button>
      <button class="btn btn--primary btn--mini" id="bv-save">创建</button>
    </div>
  </div>`;
  showModal(html);
  const root = dom.modal;
  $('#bv-name', root).focus();
  $('[data-close]', root).addEventListener('click', hideModal);
  $('#bv-save', root).addEventListener('click', async () => {
    const name = $('#bv-name', root).value.trim();
    if (!name) { toast('请填视图名字', 'error'); return; }
    const definition = {
      name,
      folder: $('#bv-folder', root).value.trim(),
      tags: $('#bv-tags', root).value.split(/[,，]/).map((t) => t.trim().replace(/^#/, '')).filter(Boolean),
      mode: $('#bv-mode', root).value,
      limit: Number($('#bv-limit', root).value) || 200,
      columns: $('#bv-columns', root).value.split(/[,，]/).map((c) => c.trim()).filter(Boolean),
      where: $('#bv-where', root).value.trim(),
    };
    try {
      const saved = await api.post('/api/views', { definition });
      hideModal();
      await reloadList(saved.slug);
      toast('视图已创建', 'success');
    } catch (err) {
      toast(`创建失败：${err.message}`, 'error');
    }
  });
}

function openDefinitionDialog() {
  if (!bs.def) return;
  const json = JSON.stringify(bs.def, null, 2);
  showModal(`<div class="bases-dialog">
    <h3>视图定义（JSON）</h3>
    <div class="bases-dialog__hint">这是落盘到 <code>${escapeHtml(bs.slug)}.base</code> 的内容，可直接改。
      支持 key/op/value 条件、sort、group_by、columns。</div>
    <textarea id="bd-json" style="min-height:260px;font-family:var(--font-mono,monospace);font-size:var(--fs-xs)">${escapeHtml(json)}</textarea>
    <div class="bases-dialog__actions">
      <button class="btn btn--mini btn--danger" id="bd-delete">删除视图</button>
      <span style="flex:1"></span>
      <button class="btn btn--mini" data-close>取消</button>
      <button class="btn btn--primary btn--mini" id="bd-save">保存</button>
    </div>
  </div>`);
  const root = dom.modal;
  $('[data-close]', root).addEventListener('click', hideModal);
  $('#bd-save', root).addEventListener('click', async () => {
    let data;
    try { data = JSON.parse($('#bd-json', root).value); }
    catch (err) { toast(`JSON 不合法：${err.message}`, 'error'); return; }
    try {
      const saved = await api.put(`/api/views/${encodeURIComponent(bs.slug)}`, { definition: data });
      bs.def = saved.definition;
      hideModal();
      await reloadList(bs.slug);
      toast('定义已保存', 'success');
    } catch (err) { toast(`保存失败：${err.message}`, 'error'); }
  });
  $('#bd-delete', root).addEventListener('click', async () => {
    const ok = await askConfirm({ title: '删除视图', message: `确定删除「${bs.def.name}」？只删视图定义，笔记不受影响。`, confirmText: '删除', danger: true });
    if (!ok) return;
    try {
      await api.del(`/api/views/${encodeURIComponent(bs.slug)}`);
      hideModal();
      bs.slug = null;
      await reloadList();
      toast('已删除', 'success');
    } catch (err) { toast(`删除失败：${err.message}`, 'error'); }
  });
}

function openAiDialog() {
  showModal(`<div class="bases-dialog">
    <h3>用一句话建视图</h3>
    <div class="bases-dialog__hint">描述你想看的笔记，AI 会翻成筛选条件（只读取字段名，不会编造属性）。</div>
    <textarea id="ba-prompt" placeholder="例如：这周要交的项目，按截止日期排，只要没完成的"></textarea>
    <div id="ba-result"></div>
    <div class="bases-dialog__actions">
      <button class="btn btn--mini" data-close>取消</button>
      <button class="btn btn--primary btn--mini" id="ba-go">生成</button>
    </div>
  </div>`);
  const root = dom.modal;
  $('#ba-prompt', root).focus();
  $('[data-close]', root).addEventListener('click', hideModal);
  const btn = $('#ba-go', root);
  btn.addEventListener('click', async () => {
    const prompt = $('#ba-prompt', root).value.trim();
    if (!prompt) { toast('先描述一下想看的笔记', 'error'); return; }
    btn.disabled = true;
    btn.textContent = '生成中…';
    try {
      const data = await api.post('/api/views/ai', { prompt });
      renderAiResult(root, data);
    } catch (err) {
      toast(`生成失败：${err.message}`, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '生成';
    }
  });
}

function renderAiResult(root, data) {
  const def = data.definition;
  const preview = data.preview;
  const rows = (preview && preview.rows) || [];
  const previewHtml = rows.length
    ? `<div class="bases-preview">${rows.slice(0, 8).map((row) =>
        `<div class="bases-preview__row"><span>${escapeHtml(row.title)}</span>
         <span>${escapeHtml(row.path)}</span></div>`).join('')}</div>`
    : '<div class="bases-dialog__hint">预览为空——可能条件太严，或字段名不存在。</div>';
  $('#ba-result', root).innerHTML = `
    <div class="bases-dialog__explain">${escapeHtml(data.explain || '已生成视图定义')}</div>
    <div class="bases-dialog__hint">
      命中 <b>${preview ? preview.total : 0}</b> 篇 · 名字「${escapeHtml(def.name)}」
      · ${def.mode === 'cards' ? '卡片' : def.mode === 'list' ? '列表' : '表格'}
      · 条件 ${def.where.length} 条
    </div>
    ${previewHtml}
    <div class="bases-dialog__actions">
      <button class="btn btn--mini" data-close>放弃</button>
      <button class="btn btn--primary btn--mini" id="ba-save">保存并打开</button>
    </div>`;
  $('[data-close]', root).addEventListener('click', hideModal);
  $('#ba-save', root).addEventListener('click', async () => {
    try {
      const saved = await api.post('/api/views', { definition: def });
      hideModal();
      await reloadList(saved.slug);
      toast('视图已保存', 'success');
    } catch (err) { toast(`保存失败：${err.message}`, 'error'); }
  });
}

function createFromTemplate() {
  const templates = [
    { name: '全部笔记', mode: 'table', limit: 200 },
    { name: '最近修改', mode: 'table', sort: [{ key: 'mtime', dir: 'desc' }], limit: 50 },
    { name: '待办事项', mode: 'table', columns: ['title', 'status', 'due', 'priority'],
      where: [{ key: 'status', op: 'in', value: ['todo', 'doing', '进行中', '待办'] }],
      sort: [{ key: 'due', dir: 'asc' }] },
    { name: '未打标签', mode: 'list', tags: [], limit: 100 },
  ];
  // 选一个还没被占用的模板
  const exists = new Set(bs.views.map((v) => v.name));
  const pick = templates.find((t) => !exists.has(t.name)) || templates[0];
  api.post('/api/views', { definition: pick })
    .then((saved) => reloadList(saved.slug))
    .then(() => toast(`已创建「${pick.name}」`, 'success'))
    .catch(reportError);
}
