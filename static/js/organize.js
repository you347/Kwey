/* 整理工作台（M2 diff 确认）：内置规则或 Agent Provider 生成结构化 ops
 * → 预演 diff（不落盘）→ 用户确认后统一写路径落盘。 */

import { api, reportError } from './api.js';
import { state, emit } from './state.js';
import { $, escapeHtml, toast } from './utils.js';

let initialized = false;
let lastOps = [];

export function initOrganize() {
  if (initialized) return;
  initialized = true;
  $('#btn-organize-close').addEventListener('click', closeOrganize);
  $('#overlay-organize').addEventListener('mousedown', (e) => {
    if (e.target === $('#overlay-organize')) closeOrganize();
  });
  $('#btn-organize-preview').addEventListener('click', () => preview().catch(reportError));
  $('#btn-organize-apply').addEventListener('click', () => apply().catch(reportError));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#overlay-organize').classList.contains('hidden')) closeOrganize();
  });
}

export function openOrganize() {
  if (!state.activePath) { toast('请先打开一篇笔记', 'error'); return; }
  lastOps = [];
  $('#organize-path').textContent = state.activePath;
  $('#organize-diffs').innerHTML = '<div class="panel-empty">填入标签 / 链接目标，或用 Provider 指令生成结构化 ops；预演不会写盘。</div>';
  $('#btn-organize-apply').disabled = true;
  $('#overlay-organize').classList.remove('hidden');
  $('#organize-tags').focus();
}

export function closeOrganize() {
  $('#overlay-organize').classList.add('hidden');
}

/** 从 UI 输入构造 ops；未填规则时走 Provider 指令路径。 */
function buildBody() {
  const path = state.activePath;
  const ops = [];
  const tags = $('#organize-tags').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  for (const tag of tags) ops.push({ type: 'tag', path, tag });
  const link = $('#organize-link').value.trim();
  if (link) ops.push({ type: 'link', path, target: link });
  if (ops.length) return { ops, paths: [path] };

  const instruction = $('#organize-instruction').value.trim();
  if (instruction) return { instruction, paths: [path], use_provider: true };

  throw new Error('请填写标签 / 链接目标，或输入 Provider 整理指令');
}

async function preview() {
  let body;
  try { body = buildBody(); } catch (err) { toast(err.message, 'error'); return; }
  const box = $('#organize-diffs');
  box.innerHTML = '<div class="panel-empty">正在计算 diff…</div>';
  const data = await api.post('/api/agent/organize/preview', body);
  lastOps = data.ops || [];
  renderDiffs(data);
}

function renderDiffs(data) {
  const box = $('#organize-diffs');
  const diffs = data.diffs || [];
  if (!diffs.length) {
    box.innerHTML = '<div class="panel-empty">没有产生任何变更</div>';
    $('#btn-organize-apply').disabled = true;
    return;
  }
  box.innerHTML = `<div class="panel-empty">共 ${diffs.length} 项变更（仅预演，尚未写盘）</div>` +
    diffs.map((d) => `
      <div class="diff-item">
        <div class="diff-item__head">
          <span class="diff-item__op">${escapeHtml(d.op)}</span>
          <span class="diff-item__path">${escapeHtml(d.path)}</span>
          <span class="diff-item__action">${escapeHtml(d.action)}</span>
        </div>
        <details>
          <summary>查看修改前后</summary>
          <div class="diff-item__cols">
            <div><b>修改前</b><pre>${escapeHtml(d.before == null ? '(文件不存在)' : d.before)}</pre></div>
            <div><b>修改后</b><pre>${escapeHtml(d.after == null ? '(内容不变 / 删除 / 移动)' : d.after)}</pre></div>
          </div>
        </details>
      </div>`).join('');
  $('#btn-organize-apply').disabled = !lastOps.length;
}

async function apply() {
  if (!lastOps.length) return;
  const data = await api.post('/api/agent/organize/apply', { ops: lastOps, confirm: true });
  toast(`已应用 ${data.applied} 项变更（统一写路径）`, 'success');
  closeOrganize();
  emit('tree-refresh');
  // 重新装载当前笔记，让编辑器/预览/反链与磁盘一致
  emit('open-note', state.activePath);
}
