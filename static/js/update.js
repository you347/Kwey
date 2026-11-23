/* 版本检查（告知式）：启动后静默检查一次，设置页「关于」可手动强制检查。
 *
 * 不做静默下载/替换：本地优先产品的更新权交给用户；发现新版本只给版本号与下载页。
 */

import { api } from './api.js';
import { state } from './state.js';
import { $, toast } from './utils.js';

let inFlight = false;
let autoDone = false;

export function initUpdate() {
  const btn = $('#btn-update-check');
  if (btn) btn.addEventListener('click', () => { run(true).catch(() => {}); });
  setTimeout(() => { maybeAuto(); }, 4000);
}

function maybeAuto() {
  if (autoDone) return;
  autoDone = true;
  const cfg = (state.settings && state.settings.update) || {};
  if (cfg.check_on_start === false) return;
  run(false).catch(() => {});
}

async function run(force) {
  if (inFlight) return;
  inFlight = true;
  const status = $('#update-status');
  if (status && force) status.textContent = '正在检查…';
  try {
    const data = await api.get('/api/update/check', force ? { force: '1' } : {});
    render(data, force);
  } catch (err) {
    if (status) status.textContent = `检查失败：${err.message}`;
  } finally {
    inFlight = false;
  }
}

function render(data, force) {
  const status = $('#update-status');
  const current = `v${data.current || '?'}`;
  if (!status) return;
  if (!data.ok) {
    status.textContent = `${current} · ${data.message || '检查更新失败'}`;
    if (force) toast('检查更新失败：网络不可达', 'error');
    return;
  }
  if (!data.has_update) {
    status.textContent = `${current} · 已是最新（远端 ${data.latest}）`;
    if (force) toast('已是最新版本', 'success');
    return;
  }
  const url = /^https?:\/\//i.test(data.url || '') ? data.url : '';
  const size = data.latest_version ? `v${data.latest_version}` : data.latest;
  status.innerHTML = `${current} → <b>${escapeHtml(size)}</b> 可用　` +
    (url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">下载更新</a>` : '');
  toast(`发现新版本 ${size}，可在设置 → 关于 查看`, 'info', 6000);
}

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
