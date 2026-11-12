/* 设置页：主题切换、附件目录、vault 管理（P0-13；M2/M3 区块灰置）。 */

import { api, reportError } from './api.js';
import { state, emit } from './state.js';
import { toast, $ } from './utils.js';

export function initSettings() {
  $('#btn-settings').addEventListener('click', openSettings);
  $('#btn-open-vault').addEventListener('click', openSettings);
  $('#btn-settings-close').addEventListener('click', closeSettings);

  $('#btn-vault-open').addEventListener('click', async () => {
    const path = $('#settings-vault-path').value.trim();
    if (!path) { toast('请输入 vault 绝对路径', 'error'); return; }
    try {
      const info = await api.post('/api/vault/open', { path });
      toast(`Vault 已打开: ${info.name}（${info.note_count} 篇笔记）`, 'success');
      closeSettings();
      emit('vault-opened', info);
    } catch (err) { reportError(err); }
  });

  $('#settings-theme').addEventListener('change', async () => {
    const theme = $('#settings-theme').value;
    await saveSettings({ theme });
    emit('theme-changed', theme);
  });
}

export function openSettings() {
  $('#settings-theme').value = state.settings.theme || 'light';
  $('#settings-attachment-dir').value = state.settings.attachment_dir || 'attachments';
  $('#settings-vault-path').value = '';
  const info = $('#settings-vault-info');
  info.textContent = state.settings && info.dataset && info.dataset.name
    ? info.dataset.name : '';
  $('#settings-page').classList.remove('hidden');
  refreshVaultInfo();
}

export function closeSettings() {
  $('#settings-page').classList.add('hidden');
}

async function refreshVaultInfo() {
  const info = $('#settings-vault-info');
  try {
    const data = await api.get('/api/vault/info');
    info.textContent = `当前 vault：${data.path}（${data.note_count} 篇笔记 / ${data.tag_count} 个标签）`;
  } catch {
    info.textContent = '当前未打开 vault';
  }
}

/** 保存设置（部分字段），并同步内存 state。 */
export async function saveSettings(partial) {
  try {
    state.settings = await api.put('/api/settings', partial);
    return state.settings;
  } catch (err) {
    reportError(err);
    return null;
  }
}

/** 保存附件目录（独立按钮入口由 app.js 绑定）。 */
export async function saveAttachmentDir() {
  const value = $('#settings-attachment-dir').value.trim() || 'attachments';
  await saveSettings({ attachment_dir: value });
  toast(`附件目录已保存: ${value}`, 'success');
}
