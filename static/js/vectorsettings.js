/* 语义检索设置：本地 ONNX 模型下载 / 后端与 Embedder 选择 / 后台重建 / 试查。
 *
 * 交互原则：
 * - 保存配置后**不**自动重建（重建可能几分钟），而是把"需要重建"写在状态行；
 * - 下载模型是异步任务，前端 800ms 轮询一次进度；
 * - 重建也是后台任务，页面可关闭，回来时按 status.task 继续显示。
 */

import { api, reportError } from './api.js';
import { state } from './state.js';
import { saveSettings } from './settings/index.js';
import { $, toast } from './utils.js';
import { askText } from './dialog.js';

let initialized = false;
let pollTimer = null;

function numOr(value, fallback) {
  const n = Number(value);
  return value !== '' && Number.isFinite(n) ? n : fallback;
}

export function initVectorSettings() {
  if (initialized) return;
  initialized = true;
  $('#btn-vector-save')?.addEventListener('click', saveVectorSettings);
  $('#btn-vector-rebuild')?.addEventListener('click', rebuildVector);
  $('#btn-vector-download')?.addEventListener('click', downloadModel);
  $('#btn-vector-test')?.addEventListener('click', testSearch);
  $('#btn-settings')?.addEventListener('click', () => { refresh().catch(reportError); });
  refresh().catch(reportError);
}

async function refresh() {
  const v = state.settings.vector || {};
  const enabledEl = $('#vector-enabled');
  if (!enabledEl) return;
  enabledEl.checked = !!v.enabled;
  if ($('#vector-backend')) $('#vector-backend').value = v.backend || 'auto';
  if ($('#vector-embedder')) $('#vector-embedder').value = v.embedder || 'auto';
  $('#vector-model').value = v.model || 'bge-small-zh-v1.5';
  $('#vector-api-model').value = v.api_model || '';
  $('#vector-weight').value = v.weight ?? '';
  $('#vector-graph-weight').value = v.graph_weight ?? '';
  $('#vector-rrf-k').value = v.rrf_k ?? '';

  try {
    const s = await api.get('/api/vector/status');
    renderStatus(s);
    maybePoll(s);
  } catch (err) {
    $('#vector-status').textContent = `状态读取失败：${err.message}`;
  }
}

function renderStatus(s) {
  const label = s.available ? `${s.backend} · ${s.count} 篇` : '不可用：hybrid 降级为纯 FTS';
  $('#vector-status').textContent = `${s.enabled ? '已启用' : '未启用'} · ${label}`;

  const model = s.model || {};
  const progress = model.progress || {};
  const statusEl = $('#vector-model-status');
  if (statusEl) {
    if (model.ready) {
      statusEl.textContent = `模型已就绪 · ${model.size_mb}MB · 维度 ${model.dim} · ${model.dir}`;
    } else if (progress.state === 'downloading') {
      const pct = progress.total ? Math.round((progress.downloaded / progress.total) * 100) : 0;
      statusEl.textContent = `正在下载 ${progress.file || ''} · ${pct}%`;
    } else if (progress.state === 'error') {
      statusEl.textContent = `下载失败：${progress.error || '未知错误'}（可重试）`;
    } else {
      statusEl.textContent = `模型未下载（约 ${model.expected_mb || 24}MB）· 点「下载模型」`;
    }
  }

  const task = s.task || {};
  if (task.state === 'running') {
    $('#vector-status').textContent += ` · 重建中 ${task.done}/${task.total || '?'}`;
  } else if (task.state === 'done' && task.message) {
    $('#vector-status').textContent += ` · 上次重建：${task.message}`;
  } else if (task.state === 'error') {
    $('#vector-status').textContent += ` · 重建失败：${task.message}`;
  }
}

function maybePoll(s) {
  const downloading = (s.model && s.model.progress && s.model.progress.state === 'downloading');
  const running = s.task && s.task.state === 'running';
  if (!downloading && !running) {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    return;
  }
  if (pollTimer) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    try {
      const next = await api.get('/api/vector/status');
      renderStatus(next);
      if (next.model && next.model.ready && (s.model.progress || {}).state === 'downloading') {
        toast('模型下载完成，可以重建索引了', 'success');
      }
      if (next.task && next.task.state === 'done' && (s.task || {}).state === 'running') {
        toast(`向量索引重建完成：${next.task.message}`, 'success');
      }
      maybePoll(next);
    } catch { /* 网络抖动：下次打开设置再续 */ }
  }, 900);
}

async function saveVectorSettings() {
  try {
    const v = {
      enabled: !!$('#vector-enabled').checked,
      backend: $('#vector-backend')?.value || 'auto',
      embedder: $('#vector-embedder')?.value || 'auto',
      model: $('#vector-model').value.trim() || 'bge-small-zh-v1.5',
      api_model: $('#vector-api-model').value.trim(),
      weight: numOr($('#vector-weight').value, 1.0),
      graph_weight: numOr($('#vector-graph-weight').value, 0.5),
      rrf_k: numOr($('#vector-rrf-k').value, 60),
    };
    await saveSettings({ vector: v });
    state.settings.vector = { ...(state.settings.vector || {}), ...v };
    toast(v.enabled ? '已保存 · 点「重建索引」让配置生效' : '已保存（向量未启用）', 'success');
    await refresh();
  } catch (err) {
    reportError(err);
  }
}

async function downloadModel() {
  const btn = $('#btn-vector-download');
  if (btn) btn.disabled = true;
  try {
    await api.post('/api/vector/model/download', {});
    toast('开始下载模型（约 24MB，可离开此页）', 'info');
    await refresh();
  } catch (err) {
    toast(`下载失败：${err.message}`, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function rebuildVector() {
  const btn = $('#btn-vector-rebuild');
  if (btn) btn.disabled = true;
  try {
    const s = await api.post('/api/vector/reindex', {});
    const task = s.task || {};
    if (task.state === 'running') toast('已在后台重建，进度会显示在状态行', 'info');
    await refresh();
  } catch (err) {
    toast(`重建失败：${err.message}`, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function testSearch() {
  const query = await askText({
    title: '语义试查',
    label: '输入一句话（不必和笔记用词一致）',
    placeholder: '例如：怎么让搜索理解我的意思',
    confirmText: '搜索',
  });
  if (!query) return;
  const out = $('#vector-test-result');
  try {
    const data = await api.get('/api/vector/search', { q: query, limit: 5 });
    if (!data.items.length) {
      out.textContent = `没有命中（后端 ${data.backend}）——可能索引为空，先重建。`;
      return;
    }
    out.innerHTML = data.items
      .map((item) => `${(item.score * 100).toFixed(0)}% · ${escapeText(item.title)}`)
      .join('　');
  } catch (err) {
    out.textContent = `试查失败：${err.message}`;
  }
}

function escapeText(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
