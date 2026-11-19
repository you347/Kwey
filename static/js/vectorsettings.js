/* 向量检索设置（A2）：开关、模型、RRF 权重、重建索引与后端状态。 */

import { api, reportError } from './api.js';
import { state } from './state.js';
import { saveSettings } from './settings/index.js';
import { $, toast } from './utils.js';

let initialized = false;

function numOr(value, fallback) {
  const n = Number(value);
  return value !== '' && Number.isFinite(n) ? n : fallback;
}

export function initVectorSettings() {
  if (initialized) return;
  initialized = true;
  $('#btn-vector-save')?.addEventListener('click', saveVectorSettings);
  $('#btn-vector-rebuild')?.addEventListener('click', rebuildVector);
  $('#btn-settings')?.addEventListener('click', () => { refresh().catch(reportError); });
  refresh().catch(reportError);
}

async function refresh() {
  const v = state.settings.vector || {};
  const enabledEl = $('#vector-enabled');
  if (!enabledEl) return;
  enabledEl.checked = !!v.enabled;
  $('#vector-model').value = v.model || '';
  $('#vector-weight').value = v.weight ?? '';
  $('#vector-graph-weight').value = v.graph_weight ?? '';
  $('#vector-rrf-k').value = v.rrf_k ?? '';
  try {
    const s = await api.get('/api/search/vector/status');
    const label = s.available ? `可用 · ${s.backend} · ${s.count} 篇` : `降级为 FTS · ${s.backend}`;
    $('#vector-status').textContent = `${s.enabled ? '已启用' : '未启用'} · ${label}`;
  } catch (err) {
    $('#vector-status').textContent = `状态读取失败：${err.message}`;
  }
}

async function saveVectorSettings() {
  try {
    const v = {
      enabled: !!$('#vector-enabled').checked,
      model: $('#vector-model').value.trim() || 'bge-small-zh-v1.5',
      weight: numOr($('#vector-weight').value, 1.0),
      graph_weight: numOr($('#vector-graph-weight').value, 0.5),
      rrf_k: numOr($('#vector-rrf-k').value, 60),
    };
    await saveSettings({ vector: v });
    toast('向量配置已保存（启用后请重建索引）', 'success');
    if (v.enabled) await rebuildVector(false);
    else await refresh();
  } catch (err) {
    reportError(err);
  }
}

async function rebuildVector(notify = true) {
  const btn = $('#btn-vector-rebuild');
  if (btn) btn.disabled = true;
  try {
    const s = await api.post('/api/search/vector/rebuild', {});
    if (notify) {
      toast(s.available
        ? `向量索引已重建：${s.count} 篇（${s.backend}）`
        : '向量后端不可用，hybrid 将降级为纯 FTS（装 chromadb 后可启用）',
        s.available ? 'success' : 'info');
    }
    await refresh();
  } catch (err) {
    reportError(err);
  } finally {
    if (btn) btn.disabled = false;
  }
}
