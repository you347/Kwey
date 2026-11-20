/* F2 每日自动推送（Gitee/GitHub）：设置页区块渲染 + 命令面板入口。
 * 远端绑定：地址由后端写入仓库 .git/config（git 自己的位置），Kwey 不存 URL
 * 副本、不采集 token；凭据仍交给系统 git credential helper。
 * 元素缺失时本模块安全降级。
 */

import { api, reportError } from './api.js';
import { $, toast } from './utils.js';
import { registerCommand } from './palette.js';
import { on } from './state.js';

let initialized = false;

const BIND_TEXT = '绑定仓库';

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

  // 绑定按钮走委托：设置页 DOM 重建后依然有效
  document.addEventListener('click', (e) => {
    const btn = e.target instanceof Element ? e.target.closest('#autopush-bind') : null;
    if (btn) bindRemote();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const url = e.target instanceof Element ? e.target.closest('#autopush-remote-url') : null;
    if (url) bindRemote();
  });

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

/** 设置页打开 / vault 切换时调用：拉取配置、状态、已绑定远端并填充表单。 */
export async function renderAutopushSettings() {
  if (!$('#autopush-section')) return;
  let cfg;
  let status;
  let repoInfo;
  try {
    [cfg, status, repoInfo] = await Promise.all([
      api.get('/api/autopush/config'),
      api.get('/api/autopush/status'),
      api.get('/api/autopush/remotes').catch(() => null),
    ]);
  } catch {
    const el = $('#autopush-status');
    if (el) el.textContent = '当前未打开 vault，自动推送不可用';
    return;
  }
  const info = repoInfo || { remotes: [], current_branch: '', repo: status.repo, git_available: status.git_available };

  $('#autopush-enabled').checked = !!cfg.enabled;
  renderRemoteSelect(cfg, info);
  renderBindHint(info);

  const branchEl = $('#autopush-branch');
  if (branchEl) {
    branchEl.value = cfg.branch || '';
    const cur = status.effective_branch || info.current_branch;
    branchEl.placeholder = cur ? `留空 = 当前分支（${cur}）` : '留空 = 当前分支';
  }
  $('#autopush-time').value = cfg.time || '22:00';
  $('#autopush-commit-template').value = cfg.commit_template || '';
  $('#autopush-include-dirs').value = (cfg.include_dirs || []).join(', ');

  const urlEl = $('#autopush-remote-url');
  if (urlEl) {
    const current = (info.remotes || []).find((r) => r.name === cfg.remote)
      || (info.remotes || [])[0];
    urlEl.value = current ? current.url : '';
  }
  renderStatus(status);
}

/** URL 截断显示：去掉协议前缀，过长保留头尾。 */
function shortUrl(url, max = 46) {
  if (!url) return '地址未知';
  const clean = String(url).replace(/^[a-z+]+:\/\//i, '');
  return clean.length > max ? `${clean.slice(0, max - 1)}…` : clean;
}

function renderRemoteSelect(cfg, info) {
  const sel = $('#autopush-remote');
  if (!sel) return;
  const remotes = info.remotes || [];
  const names = remotes.map((r) => r.name);
  if (cfg.remote && !names.includes(cfg.remote)) names.unshift(cfg.remote);
  if (!names.length) names.push('origin');
  sel.innerHTML = '';
  names.forEach((name) => {
    const found = remotes.find((r) => r.name === name);
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = found ? `${name}（${shortUrl(found.url, 34)}）` : `${name}（未绑定）`;
    sel.appendChild(opt);
  });
  sel.value = cfg.remote || names[0];
}

function renderBindHint(info) {
  const el = $('#autopush-bind-hint');
  if (!el) return;
  if (!info.git_available) {
    el.textContent = '系统未安装 git，无法绑定仓库';
    return;
  }
  if (!info.repo) {
    el.textContent = 'vault 还没初始化 git 仓库，请先在「文件与快照」里开启 git 快照';
    return;
  }
  const remotes = info.remotes || [];
  if (!remotes.length) {
    el.textContent = '尚未绑定远程仓库：填入地址后点「绑定仓库」';
    return;
  }
  el.textContent = `已绑定：${remotes.map((r) => `${r.name} → ${shortUrl(r.url, 60)}`).join('　｜　')}`;
}

/** 绑定 / 替换远端地址（同名已存在即视为用户确认更换）。 */
async function bindRemote() {
  const urlEl = $('#autopush-remote-url');
  const url = (urlEl ? urlEl.value : '').trim();
  if (!url) {
    toast('请先填写仓库地址', 'error');
    if (urlEl) urlEl.focus();
    return;
  }
  const sel = $('#autopush-remote');
  const name = (sel && sel.value ? sel.value : 'origin').trim() || 'origin';
  const btn = $('#autopush-bind');
  if (btn) { btn.disabled = true; btn.textContent = '绑定中…'; }
  try {
    const data = await api.post('/api/autopush/bind', { url, name, replace: true });
    toast(data.replaced ? `已更新 ${data.name} 的仓库地址` : `已绑定 ${data.name}`, 'success');
    if (data.warning) toast(data.warning, 'error', 9000);
    await renderAutopushSettings();
  } catch (err) {
    reportError(err);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = BIND_TEXT; }
  }
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
  if (s.enabled && s.effective_branch) parts.push(`推送分支：${s.effective_branch}`);
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
    branch: $('#autopush-branch').value.trim(),
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
