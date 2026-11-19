/* F2 每日自动推送（Gitee/GitHub）：设置页区块渲染 + 命令面板入口。
 * DOM 由接线人按约定 id 加入设置页（见卡片报告）；元素缺失时本模块安全降级。
 * 凭据说明：只配置 remote 名，token 交给系统 git credential helper，前端不采集 token。
 */

import { api, reportError } from './api.js';
import { $, toast } from './utils.js';
import { registerCommand } from './palette.js';
import { on } from './state.js';

let initialized = false;

export function initAutopush() {
  if (initialized) return;
  initialized = true;

  const enabled = $('#autopush-enabled');
  if (enabled) {
    enabled.addEventListener('change', async () => {
      try {
        await api.put('/api/autopush/config', { enabled: enabled.checked });
        toast(enabled.checked ? '每日自动推送已开启' : '每日自动推送已关闭', 'success');
        await renderAutopushSettings();
      } catch (err) { reportError(err); }
    });
  }
  const saveBtn = $('#autopush-save');
  if (saveBtn) saveBtn.addEventListener('click', saveConfig);
  const runBtn = $('#autopush-run');
  if (runBtn) runBtn.addEventListener('click', runNow);

  registerCommand({ id: 'autopush.run', title: '自动推送：立即推送', run: runNow });
  registerCommand({
    id: 'autopush.settings',
    title: '自动推送：打开设置',
    run: () => {
      const btn = $('#btn-settings');
      if (btn) btn.click();
      renderAutopushSettings().catch(reportError);
    },
  });
  on('vault-opened', () => { renderAutopushSettings().catch(() => {}); });
}

/** 设置页打开 / vault 切换时调用：拉取配置与状态并填充表单。 */
export async function renderAutopushSettings() {
  if (!$('#autopush-section')) return;
  let cfg;
  let status;
  try {
    [cfg, status] = await Promise.all([
      api.get('/api/autopush/config'),
      api.get('/api/autopush/status'),
    ]);
  } catch {
    const el = $('#autopush-status');
    if (el) el.textContent = '当前未打开 vault，自动推送不可用';
    return;
  }
  $('#autopush-enabled').checked = !!cfg.enabled;
  $('#autopush-remote').value = cfg.remote || 'origin';
  $('#autopush-branch').value = cfg.branch || 'main';
  $('#autopush-time').value = cfg.time || '22:00';
  $('#autopush-commit-template').value = cfg.commit_template || '';
  $('#autopush-include-dirs').value = (cfg.include_dirs || []).join(', ');
  renderStatus(status);
}

function fmtTime(iso) {
  return iso ? String(iso).replace('T', ' ') : '—';
}

function renderStatus(s) {
  const el = $('#autopush-status');
  if (!el) return;
  const stateText = {
    idle: '待命', running: '推送中…', paused: '已暂停（冲突）', error: '上次推送失败',
  }[s.state] || s.state;
  const parts = [`状态：${stateText}`];
  if (s.enabled) parts.push(`下次推送：${fmtTime(s.next_run_iso)}`);
  parts.push(`上次推送：${fmtTime(s.last_push_iso)}`);
  if (s.catchup_pending) parts.push('存在错过的计划，启动后将补推');
  if (!s.git_available) parts.push('系统未安装 git');
  let text = parts.join(' ｜ ');
  if ((s.state === 'paused' || s.state === 'error') && s.last_result && s.last_result.message) {
    text += `\n${s.last_result.message}`;
  }
  el.textContent = text;
}

async function saveConfig() {
  const dirs = $('#autopush-include-dirs').value
    .split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  const body = {
    remote: $('#autopush-remote').value.trim() || 'origin',
    branch: $('#autopush-branch').value.trim() || 'main',
    time: $('#autopush-time').value || '22:00',
    commit_template: $('#autopush-commit-template').value.trim(),
    include_dirs: dirs,
  };
  try {
    await api.put('/api/autopush/config', body);
    toast('自动推送配置已保存', 'success');
    await renderAutopushSettings();
  } catch (err) { reportError(err); }
}

/** 立即推送（设置页按钮 / 命令面板共用）。 */
export async function runNow() {
  try {
    const result = await api.post('/api/autopush/run', {});
    if (result.status === 'pushed') {
      toast(result.committed ? `推送成功（新提交 ${result.commit || ''}）` : '推送成功（无新改动）', 'success');
    } else if (result.status === 'paused') {
      toast(result.message, 'error', 8000);
    } else if (result.status === 'skipped') {
      toast(result.message, 'info');
    } else {
      toast(result.message || '推送失败', 'error', 6000);
    }
    await renderAutopushSettings();
  } catch (err) { reportError(err); }
}