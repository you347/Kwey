/* F1 知识编译浮层：raw/ 原始素材 → Provider ops → diff 逐项确认 → 统一写路径落盘。
 * 命令 id: compile.open（由 initCompile 注册到命令面板，供 Ctrl+P 调用）。 */

import { api } from './api.js';
import { emit } from './state.js';
import { $, escapeHtml, toast } from './utils.js';
import { registerCommand } from './palette.js';

export const COMPILE_COMMAND_ID = 'compile.open';

let initialized = false;
let lastOps = [];
let lastDiffs = [];

export function initCompile() {
  if (initialized) return;
  initialized = true;
  registerCommand({
    id: COMPILE_COMMAND_ID,
    title: 'F1 知识编译（raw → wiki 预演）',
    run: () => openCompile(),
  });

  const overlay = $('#overlay-compile');
  if (!overlay) return; // 浮层 HTML 尚未接入时静默降级，不影响其它模块
  $('#btn-compile-close').addEventListener('click', closeCompile);
  overlay.addEventListener('mousedown', (e) => {
    if (e.target === overlay) closeCompile();
  });
  $('#btn-compile-preview').addEventListener('click', () => preview().catch(showError));
  $('#btn-compile-health').addEventListener('click', () => refreshHealth().catch(showError));
  $('#btn-compile-apply').addEventListener('click', () => apply().catch(showError));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !overlay.classList.contains('hidden')) closeCompile();
  });
}

export function openCompile() {
  const overlay = $('#overlay-compile');
  if (!overlay) { toast('知识编译浮层未接入页面（缺少 #overlay-compile）', 'error'); return; }
  lastOps = [];
  lastDiffs = [];
  $('#compile-status').textContent = '仅预演，未写盘';
  $('#compile-ops').innerHTML = '<div class="panel-empty">填入编译指令后点击「生成预演」；raw/ 保持只读。</div>';
  $('#compile-diffs').innerHTML = '<div class="panel-empty">预演 diff 会显示在这里，可逐项勾选。</div>';
  $('#compile-health').innerHTML = '<div class="panel-empty">正在读取库体检…</div>';
  $('#btn-compile-apply').disabled = true;
  overlay.classList.remove('hidden');
  $('#compile-instruction').focus();
  refreshHealth().catch(showError);
}

export function closeCompile() {
  const overlay = $('#overlay-compile');
  if (overlay) overlay.classList.add('hidden');
}

function showError(err) {
  const message = err?.message || String(err);
  $('#compile-status').textContent = `错误：${message}`;
  toast(message, 'error');
}

/** 从表单组装 preview 请求体；路径按换行 / 逗号分隔，均收敛到 raw/ 内。 */
function buildBody() {
  const paths = $('#compile-paths').value
    .split(/[\n,，]+/).map((s) => s.trim()).filter(Boolean);
  return {
    instruction: $('#compile-instruction').value.trim() || undefined,
    raw_dir: $('#compile-raw-dir').value.trim() || 'raw',
    wiki_dir: $('#compile-wiki-dir').value.trim() || 'wiki',
    paths: paths.length ? paths : undefined,
  };
}

async function preview() {
  const box = $('#compile-diffs');
  $('#compile-status').textContent = '正在生成预演…';
  box.innerHTML = '<div class="panel-empty">正在计算 diff…</div>';
  const data = await api.post('/api/compile/preview', buildBody());
  lastOps = data.ops || [];
  lastDiffs = data.diffs || [];
  renderReport(data.report || {});
  renderDiffs();
  $('#compile-status').textContent = `预演完成：${lastDiffs.length} 项变更，尚未写盘`;
}

function renderReport(report) {
  const box = $('#compile-ops');
  const lines = [
    `源目录 ${escapeHtml(report.raw_dir || 'raw')}：${report.raw_notes ?? 0} 篇原始笔记`,
    `产物目录 ${escapeHtml(report.wiki_dir || 'wiki')}：${report.op_count ?? 0} 项 ops`,
    `实体页 ${(report.entity_pages || []).length} · 概念页 ${(report.concept_pages || []).length}`,
    `MOC 索引 ${escapeHtml(report.moc_path || 'wiki/MOC.md')}`,
  ];
  const warnings = (report.warnings || []).map((w) => `<li>${escapeHtml(w)}</li>`).join('');
  box.innerHTML = `<div class="panel-empty">${lines.join('<br/>')}</div>` +
    (warnings ? `<div class="panel-empty"><b>提示</b><ul>${warnings}</ul></div>` : '');
}

function renderDiffs() {
  const box = $('#compile-diffs');
  if (!lastDiffs.length) {
    box.innerHTML = '<div class="panel-empty">没有产生任何变更</div>';
    $('#btn-compile-apply').disabled = true;
    return;
  }
  // diff 与 ops 一一对应（preview_ops 每项 op 产出一个 diff），索引即可映射回 op
  box.innerHTML = lastDiffs.map((d, i) => `
    <label class="diff-item">
      <input type="checkbox" class="compile-op-check" data-index="${i}" checked />
      <span class="diff-item__op">${escapeHtml(d.op)}</span>
      <span class="diff-item__path">${escapeHtml(d.path)}</span>
      <span class="diff-item__action">${escapeHtml(d.action)}</span>
      <details>
        <summary>查看修改前后</summary>
        <div class="diff-item__cols">
          <div><b>修改前</b><pre>${escapeHtml(d.before == null ? '(文件不存在)' : d.before)}</pre></div>
          <div><b>修改后</b><pre>${escapeHtml(d.after == null ? '(内容不变 / 删除 / 移动)' : d.after)}</pre></div>
        </div>
      </details>
    </label>`).join('');
  $('#btn-compile-apply').disabled = false;
}

async function apply() {
  const picked = Array.from(document.querySelectorAll('.compile-op-check:checked'))
    .map((el) => lastOps[Number(el.dataset.index)]).filter(Boolean);
  if (!picked.length) { toast('请至少勾选一项变更', 'error'); return; }
  $('#compile-status').textContent = `正在应用 ${picked.length} 项变更…`;
  const data = await api.post('/api/compile/apply', { ops: picked, confirm: true });
  $('#compile-status').textContent = `已写入 ${data.applied} 项（统一写路径）`;
  closeCompile();
  emit('tree-refresh');
  emit('open-note', 'wiki/MOC.md');
  toast(`F1 编译完成：已写入 ${data.applied} 项 wiki 产物`, 'success');
}

async function refreshHealth() {
  const data = await api.get('/api/compile/health');
  renderHealth(data);
}

function renderHealth(data) {
  const box = $('#compile-health');
  const sections = [
    ['孤儿笔记', data.orphans || [], (x) => `${escapeHtml(x.path)} — ${escapeHtml(x.suggestion || '')}`],
    ['断链', data.broken_links || [], (x) => `${escapeHtml(x.source)}:${x.line} → [[${escapeHtml(x.target)}]] — ${escapeHtml(x.suggestion || '')}`],
    ['字段漂移', data.schema_drift || [], (x) => `${escapeHtml(x.path)} — 缺少 ${escapeHtml((x.missing || []).join(', '))}`],
    ['MOC 缺失', data.missing_moc || [], (x) => `${escapeHtml(x.path)} — ${escapeHtml(x.reason || '')}；${escapeHtml(x.suggestion || '')}`],
  ];
  box.innerHTML = sections.map(([title, items, render]) => `
    <div class="panel-empty"><b>${title}（${items.length}）</b></div>
    ${items.length ? items.map((x) => `<div class="diff-item">${render(x)}</div>`).join('') : '<div class="panel-empty">无</div>'}
  `).join('');
}
