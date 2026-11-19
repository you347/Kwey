/* F3 个人信息库面板：三分区记忆（项目 50% / 画像 30% / 经验 20%）+ 配额注入预览。
 *
 * 设计约束：
 * - 面板 DOM 在本模块内动态创建（index.html 属共享文件，不新增浮层节点）；
 * - 记忆是普通 Markdown（.kwey/memory/*.md），面板只是"看得见的编辑器"，
 *   用户随时可以在外部编辑器改同一份文件；
 * - 命令 id 固定为 `memory.open`，由 initMemory() 注册到命令面板。
 */

import { api, reportError } from './api.js';
import { registerCommand } from './palette.js';
import { $, escapeHtml, toast, confirmDialog } from './utils.js';

export const COMMAND_ID = 'memory.open';

const SECTIONS = [
  { key: 'projects', label: '项目记忆', percent: 50 },
  { key: 'profile', label: '用户画像', percent: 30 },
  { key: 'lessons', label: '经验教训', percent: 20 },
];

let initialized = false;
let overlayEl = null;
let current = 'projects';
let data = null;          // GET /api/memory 的 data
let editingId = null;     // null = 新增条目
let loading = false;

/* ------------------------------------------------------------------ 初始化 */
export function initMemory() {
  if (initialized) return;
  initialized = true;
  ensureDom();
  registerCommand({
    id: COMMAND_ID,
    title: '个人信息库（记忆配额）',
    run: () => { openMemory().catch(reportError); },
  });
}

/** 打开面板；可选直接切到某个分区（projects / profile / lessons）。 */
export async function openMemory(section) {
  ensureDom();
  if (section && SECTIONS.some((s) => s.key === section)) current = section;
  overlayEl.classList.remove('hidden');
  await load();
}

export function closeMemory() {
  if (overlayEl) overlayEl.classList.add('hidden');
}

/* ------------------------------------------------------------------ DOM */
function ensureDom() {
  if (overlayEl) return;
  const tabs = SECTIONS.map(
    (s) => `<button type="button" role="tab" data-section="${s.key}">${s.label} ${s.percent}%</button>`
  ).join('');

  overlayEl = document.createElement('div');
  overlayEl.id = 'overlay-memory';
  overlayEl.className = 'overlay hidden';
  overlayEl.innerHTML = `
    <div class="overlay__panel overlay__panel--wide">
      <div class="graph-head">
        <span>个人信息库 · 记忆配额</span>
        <span id="memory-count" class="graph-head__stats"></span>
        <button id="btn-memory-reload" class="btn btn--mini" type="button" title="重新读取磁盘上的记忆文件">刷新</button>
        <button id="btn-memory-close" class="btn btn--ghost" type="button">
          <svg class="icon" aria-hidden="true"><use href="#i-close"/></svg> 关闭
        </button>
      </div>
      <div id="memory-tabs" class="panel-tabs" role="tablist">${tabs}</div>
      <div id="memory-body" class="overlay__results">
        <div id="memory-file" class="settings-muted"></div>
        <div id="memory-list"></div>
        <div style="padding:10px 14px 0;display:flex;flex-direction:column;gap:6px">
          <textarea id="memory-editor" rows="3" placeholder="新增一条记忆，例如：用户偏好先给结论再解释"></textarea>
          <div style="display:flex;gap:8px;align-items:center">
            <label style="display:flex;gap:6px;align-items:center;font-size:12px">
              权重
              <input id="memory-weight" type="number" min="0.05" max="5" step="0.1" value="1" style="width:80px"/>
            </label>
            <button id="btn-memory-save" class="btn btn--primary btn--mini" type="button">添加条目</button>
            <button id="btn-memory-cancel" class="btn btn--mini hidden" type="button">取消编辑</button>
          </div>
        </div>
        <div style="padding:10px 14px 14px;border-top:1px solid var(--border-faint);margin-top:10px">
          <div style="display:flex;gap:8px;align-items:center">
            <label style="display:flex;gap:6px;align-items:center;font-size:12px">
              注入预算
              <input id="memory-budget" type="number" min="200" max="20000" step="200" value="4000" style="width:110px"/>
            </label>
            <button id="btn-memory-preview" class="btn btn--mini" type="button">注入预览</button>
            <span id="memory-preview-stats" class="settings-muted"></span>
          </div>
          <pre id="memory-preview" style="max-height:220px;overflow:auto;white-space:pre-wrap;font-size:12px;margin:8px 0 0"></pre>
        </div>
      </div>
      <div class="overlay__hint">
        记忆存放在 .kwey/memory/*.md，可直接用任意编辑器修改 · <kbd>Esc</kbd> 关闭
      </div>
    </div>`;

  overlayEl.addEventListener('mousedown', (e) => {
    if (e.target === overlayEl) closeMemory();
  });
  document.body.appendChild(overlayEl);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !overlayEl.classList.contains('hidden')) closeMemory();
  });

  $('#btn-memory-close', overlayEl).addEventListener('click', closeMemory);
  $('#btn-memory-reload', overlayEl).addEventListener('click', () => load().catch(reportError));
  $('#btn-memory-save', overlayEl).addEventListener('click', () => saveEntry().catch(reportError));
  $('#btn-memory-cancel', overlayEl).addEventListener('click', resetEditor);
  $('#btn-memory-preview', overlayEl).addEventListener('click', () => preview().catch(reportError));

  $('#memory-tabs', overlayEl).addEventListener('click', (e) => {
    const btn = e.target.closest('[data-section]');
    if (!btn) return;
    current = btn.dataset.section;
    resetEditor();
    render();
  });

  $('#memory-list', overlayEl).addEventListener('click', (e) => {
    const action = e.target.closest('[data-act]');
    if (action) {
      const row = action.closest('[data-id]');
      const entry = findEntry(row && row.dataset.id);
      if (!entry) return;
      if (action.dataset.act === 'edit') startEdit(entry);
      else if (action.dataset.act === 'del') deleteEntry(entry).catch(reportError);
      return;
    }
    const row = e.target.closest('[data-id]');
    if (row) {
      const entry = findEntry(row.dataset.id);
      if (entry) startEdit(entry);
    }
  });
}

/* ------------------------------------------------------------------ 加载 */
async function load() {
  if (loading) return;
  loading = true;
  const listEl = $('#memory-list', overlayEl);
  if (!data && listEl) listEl.innerHTML = '<div class="panel-empty">读取记忆…</div>';
  try {
    data = await api.get('/api/memory');
    render();
  } catch (err) {
    if (listEl) listEl.innerHTML = `<div class="panel-empty">记忆读取失败：${escapeHtml(err.message || err)}</div>`;
  } finally {
    loading = false;
  }
}

function sectionMeta(key) {
  return SECTIONS.find((s) => s.key === key) || SECTIONS[0];
}

function currentSection() {
  return (data && data.sections && data.sections[current]) || null;
}

function entriesOfCurrent() {
  const section = currentSection();
  return section && Array.isArray(section.entries) ? section.entries : [];
}

function findEntry(id) {
  return entriesOfCurrent().find((e) => e.id === id) || null;
}

/* ------------------------------------------------------------------ 渲染 */
function render() {
  if (!overlayEl) return;
  const section = currentSection();
  const meta = sectionMeta(current);

  overlayEl.querySelectorAll('#memory-tabs button').forEach((btn) => {
    btn.classList.toggle('panel-tab--active', btn.dataset.section === current);
  });

  const countEl = $('#memory-count', overlayEl);
  const total = data ? Object.values(data.sections).reduce((n, s) => n + (s.count || 0), 0) : 0;
  if (countEl) countEl.textContent = data ? `共 ${total} 条 · ${meta.label} ${meta.percent}%` : '';

  const fileEl = $('#memory-file', overlayEl);
  if (fileEl) {
    fileEl.textContent = section
      ? `${section.path} · 配额 ${section.percent}%（${section.count} 条，按权重×时间衰减排序）`
      : '';
  }

  const listEl = $('#memory-list', overlayEl);
  if (!listEl) return;
  if (!section) {
    listEl.innerHTML = '<div class="panel-empty">尚无记忆数据</div>';
    return;
  }
  const entries = entriesOfCurrent();
  if (!entries.length) {
    listEl.innerHTML = `<div class="panel-empty">${meta.label}还是空的：在下方添加，或直接编辑 ${escapeHtml(section.path)}</div>`;
    return;
  }
  listEl.innerHTML = entries.map((entry) => `
    <div class="result-item" data-id="${escapeHtml(entry.id)}" title="点击编辑">
      <span class="result-item__title">${escapeHtml(entry.content)}</span>
      <span class="result-item__path">
        ${escapeHtml(entry.category || '')} · 权重 ${Number(entry.weight).toFixed(2)} · 分数 ${Number(entry.score || 0).toFixed(2)}
        · ${escapeHtml(String(entry.last_used || '').slice(0, 10))} · ${escapeHtml(entry.source || 'manual')}
      </span>
      <span style="display:flex;gap:6px;margin-top:2px">
        <button type="button" class="panel-list__action" data-act="edit">编辑</button>
        <button type="button" class="panel-list__action" data-act="del">删除</button>
      </span>
    </div>`).join('');
}

/* ------------------------------------------------------------------ 条目编辑 */
function startEdit(entry) {
  editingId = entry.id;
  const editor = $('#memory-editor', overlayEl);
  if (editor) {
    editor.value = entry.content;
    editor.focus();
  }
  const weight = $('#memory-weight', overlayEl);
  if (weight) weight.value = String(entry.weight ?? 1);
  const save = $('#btn-memory-save', overlayEl);
  if (save) save.textContent = '更新条目';
  const cancel = $('#btn-memory-cancel', overlayEl);
  if (cancel) cancel.classList.remove('hidden');
}

function resetEditor() {
  editingId = null;
  const editor = $('#memory-editor', overlayEl);
  if (editor) editor.value = '';
  const weight = $('#memory-weight', overlayEl);
  if (weight) weight.value = '1';
  const save = $('#btn-memory-save', overlayEl);
  if (save) save.textContent = '添加条目';
  const cancel = $('#btn-memory-cancel', overlayEl);
  if (cancel) cancel.classList.add('hidden');
}

async function saveEntry() {
  const editor = $('#memory-editor', overlayEl);
  const content = (editor ? editor.value : '').trim();
  if (!content) { toast('请先填写记忆内容', 'error'); return; }
  const weightInput = $('#memory-weight', overlayEl);
  const weight = Math.max(0.05, Math.min(5, Number(weightInput && weightInput.value) || 1));

  const entries = entriesOfCurrent().map((e) => ({
    id: e.id,
    content: e.content,
    category: e.category,
    weight: e.weight,
    last_used: e.last_used,
    source: e.source,
  }));

  const target = editingId ? entries.find((e) => e.id === editingId) : null;
  if (target) {
    target.content = content;
    target.weight = weight;
    target.last_used = new Date().toISOString();
  } else {
    // 新增，或正在编辑的条目已被外部删除 → 退化为新增，避免静默丢失输入
    entries.push({ content, weight, source: 'manual' });
  }

  await api.put(`/api/memory/${current}`, { entries });
  const wasEditing = !!target;
  resetEditor();
  await load();          // 重新拉全量：三分区计数、分数排序与磁盘一致
  toast(wasEditing ? '已更新记忆' : '已添加记忆', 'success', 1600);
}

async function deleteEntry(entry) {
  if (!await confirmDialog(`删除这条记忆？\n${entry.content}`, {
    title: '删除记忆', danger: true, confirmText: '删除',
  })) return;
  const entries = entriesOfCurrent()
    .filter((e) => e.id !== entry.id)
    .map((e) => ({
      id: e.id,
      content: e.content,
      category: e.category,
      weight: e.weight,
      last_used: e.last_used,
      source: e.source,
    }));
  await api.put(`/api/memory/${current}`, { entries });
  if (editingId === entry.id) resetEditor();
  await load();
  toast('已删除记忆', 'success', 1600);
}

/* ------------------------------------------------------------------ 注入预览 */
async function preview() {
  const budgetInput = $('#memory-budget', overlayEl);
  const maxChars = Math.max(200, Math.min(20000, Number(budgetInput && budgetInput.value) || 4000));
  if (budgetInput) budgetInput.value = String(maxChars);

  const box = $('#memory-preview', overlayEl);
  const stats = $('#memory-preview-stats', overlayEl);
  if (box) box.textContent = '正在按 50/30/20 配额拼装…';
  try {
    const result = await api.post('/api/memory/inject', { max_chars: maxChars });
    if (box) {
      box.textContent = result.text || '（没有可注入的记忆：先在三个分区里各加几条）';
    }
    if (stats) {
      const parts = SECTIONS.map((s) => {
        const detail = result.sections && result.sections[s.key];
        if (!detail) return null;
        return `${s.label} ${detail.used}/${detail.quota}`;
      }).filter(Boolean);
      stats.textContent = `已用 ${result.used}/${result.budget} 字 · ${parts.join(' · ')}`;
    }
  } catch (err) {
    if (box) box.textContent = '';
    if (stats) stats.textContent = '';
    throw err;
  }
}
