/* 库导入（P0）：设置页「导入已有笔记库」区块。
 * 两步式：扫描预览（只读）→ 确认后落盘。源库全程只读，写盘走 VaultService。
 * DOM 事件一律委托到 document，设置页重建也不会丢绑定。
 */

import { api, reportError } from './api.js';
import { $, toast } from './utils.js';
import { askConfirm } from './dialog.js';

let initialized = false;

const CONFLICT_LABELS = {
  rename: '自动改名，保留两者',
  skip: '跳过已有笔记',
  overwrite: '覆盖已有笔记',
};

export function initImporter() {
  if (initialized) return;
  initialized = true;

  document.addEventListener('click', (e) => {
    const target = e.target instanceof Element ? e.target : null;
    if (!target) return;
    if (target.closest('#import-scan')) scanImport();
    else if (target.closest('#import-apply')) applyImport();
    else if (target.closest('#import-pick')) pickSource();
  });

  revealPicker();
}

/** pywebview 桥异步注入：等它就绪后放出「选择」按钮。 */
function revealPicker() {
  let tries = 0;
  const tick = () => {
    const bridge = window.pywebview && window.pywebview.api;
    const btn = $('#import-pick');
    if (bridge && typeof bridge.pick_folder === 'function' && btn) {
      btn.classList.remove('hidden');
      return;
    }
    if (tries++ < 20) setTimeout(tick, 300);
  };
  tick();
}

async function pickSource() {
  const bridge = window.pywebview && window.pywebview.api;
  if (!bridge || typeof bridge.pick_folder !== 'function') {
    toast('当前环境拿不到绝对路径，请直接粘贴目录', 'info');
    return;
  }
  try {
    const path = await bridge.pick_folder();
    const input = $('#import-source');
    if (path && input) {
      input.value = path;
      input.dispatchEvent(new Event('change'));
    }
  } catch (err) {
    reportError(err);
  }
}

function payload() {
  const source = $('#import-source');
  const conflict = $('#import-conflict');
  const atts = $('#import-attachments');
  return {
    source: (source ? source.value : '').trim(),
    conflict: conflict ? conflict.value : 'rename',
    include_attachments: atts ? !!atts.checked : true,
  };
}

function fmtBytes(n) {
  const size = Number(n) || 0;
  if (size >= 1024 * 1024 * 1024) return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size >= 1024) return `${Math.round(size / 1024)} KB`;
  return `${size} B`;
}

function setStatus(text) {
  const el = $('#import-status');
  if (el) el.textContent = text;
}

async function withBusy(selector, busyText, fn) {
  const btn = $(selector);
  const original = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = busyText; }
  try {
    await fn();
  } catch (err) {
    reportError(err);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

async function scanImport() {
  const body = payload();
  if (!body.source) {
    toast('请先填写要导入的源目录', 'error');
    return;
  }
  await withBusy('#import-scan', '扫描中…', async () => {
    const data = await api.post('/api/import/scan', body);
    const lines = [
      `待导入：${data.notes} 篇笔记 · ${data.attachments} 个附件 · ${fmtBytes(data.total_bytes)}`,
      `同名冲突：${data.conflicts} 个（${CONFLICT_LABELS[data.conflict] || data.conflict}）`,
      `链接：${data.links_total} 个，其中断链 ${data.links_broken} 处`,
    ];
    if (data.duplicate_names && data.duplicate_names.length) {
      lines.push(`重名笔记：${data.duplicate_names.length} 组（导入后双链可能歧义）`);
    }
    if (data.skipped_files) {
      lines.push(`跳过：${data.skipped_files} 个非 Markdown 文件`);
    }
    if (data.link_scan_limited) {
      lines.push('大库提示：断链只扫描了前 3000 篇');
    }
    if (data.broken_samples && data.broken_samples.length) {
      const first = data.broken_samples[0];
      lines.push(`断链示例：${first.path} → [[${first.target}]]`);
    }
    setStatus(lines.join('\n'));
    toast(`扫描完成：${data.notes} 篇笔记 / ${data.attachments} 个附件`, 'success');
  });
}

async function applyImport() {
  const body = payload();
  if (!body.source) {
    toast('请先填写要导入的源目录', 'error');
    return;
  }
  const label = CONFLICT_LABELS[body.conflict] || body.conflict;
  const ok = await askConfirm({
    title: '确认导入',
    message: `从 ${body.source} 导入到当前 vault。同名冲突处理：${label}。源库不会被修改。`,
    confirmText: '开始导入',
  });
  if (!ok) return;

  await withBusy('#import-apply', '导入中…', async () => {
    const data = await api.post('/api/import/apply', body);
    const bits = [
      `新建 ${data.notes_created} 篇`,
      `覆盖 ${data.notes_overwritten} 篇`,
      `改名 ${data.notes_renamed} 篇`,
      `跳过 ${data.notes_skipped} 篇`,
    ];
    if (data.attachments_copied || data.attachments_renamed) {
      bits.push(`附件 ${data.attachments_copied + data.attachments_renamed} 个`);
    }
    setStatus(`导入完成：${bits.join(' · ')}${data.failed_total ? `；失败 ${data.failed_total} 个` : ''}`);
    toast(`导入完成：新建 ${data.notes_created} 篇笔记`, 'success');
    if (data.failed_total) {
      const first = data.failed && data.failed[0];
      toast(`有 ${data.failed_total} 个文件失败${first ? `：${first.path}（${first.error}）` : ''}`, 'error', 9000);
    }
  });
}
