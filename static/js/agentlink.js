/* Agent Link 设置页（M2）：MCP 启动命令、访问 token、Provider 配置、git 快照开关。 */

import { api, reportError, renewToken } from './api.js';
import { state, emit } from './state.js';
import { saveSettings } from './settings/index.js';
import { $, escapeHtml, toast, confirmDialog } from './utils.js';

const LS_TOKEN = 'kwey.agent.token';
let initialized = false;

function token() { return localStorage.getItem(LS_TOKEN) || ''; }
function setToken(value) {
  if (value) localStorage.setItem(LS_TOKEN, value);
  else localStorage.removeItem(LS_TOKEN);
}

/** 带 X-Kwey-Token 的请求；401 时共享 api.js 的续签逻辑并重放一次。 */
async function agentFetch(path, { method = 'GET', body = null } = {}) {
  const once = async () => {
    const headers = { 'Content-Type': 'application/json' };
    const tk = token();
    if (tk) headers['X-Kwey-Token'] = tk;
    const res = await fetch(path, {
      method, headers,
      body: body === null ? undefined : JSON.stringify(body),
    });
    let payload;
    try { payload = await res.json(); } catch { payload = {}; }
    return { ok: res.ok && payload.code === 0, status: res.status, payload };
  };
  let out = await once();
  if (!out.ok && out.status === 401 && !path.startsWith('/api/agent/bootstrap')) {
    if (await renewToken()) out = await once();
  }
  if (!out.ok) throw new Error(out.payload.message || out.payload.detail || `请求失败 (${out.status})`);
  return out.payload.data;
}

export function getAgentToken() { return token(); }

export function initAgentLink() {
  if (initialized) return;
  initialized = true;

  $('#btn-agent-token').addEventListener('click', issueToken);
  $('#btn-agent-provider-save').addEventListener('click', saveProvider);
  // API Key 显隐切换（长 key 用 textarea 展示，默认圆点遮盖）
  const keyEl = $('#agent-provider-key');
  const keyToggle = $('#btn-agent-key-toggle');
  if (keyEl) keyEl.style.webkitTextSecurity = 'disc';
  keyToggle?.addEventListener('click', () => {
    const hidden = keyEl.style.webkitTextSecurity === 'disc';
    keyEl.style.webkitTextSecurity = hidden ? 'none' : 'disc';
    keyToggle.textContent = hidden ? '隐藏' : '显示';
  });
  $('#settings-snapshots-enabled').addEventListener('change', toggleSnapshots);
  // 设置页每次打开都刷新（settings.js 自身也监听同一按钮，二者互不影响）
  $('#btn-settings').addEventListener('click', refresh);
  refresh().catch(reportError);
}

async function refresh() {
  try {
    const info = await api.get('/api/vault/info');
    $('#agent-mcp-cmd').textContent = `python kwey_mcp.py --vault "${info.path}"`;
  } catch {
    $('#agent-mcp-cmd').textContent = 'python kwey_mcp.py --vault "<vault 绝对路径>"';
  }

  const snap = state.settings.snapshots || {};
  $('#settings-snapshots-enabled').checked = !!snap.enabled;
  await refreshSnapshotStatus();

  try {
    // A4 起 provider-config 需 write token；本地 UI 无 token 时先 bootstrap（本地即信任边界）
    if (!token()) {
      try {
        const boot = await agentFetch('/api/agent/bootstrap', { method: 'POST' });
        if (boot && boot.token) setToken(boot.token);
      } catch { /* bootstrap 失败则后续请求会给出明确错误 */ }
    }
    const cfg = await agentFetch('/api/agent/provider-config');
    $('#agent-provider-endpoint').value = cfg.endpoint || '';
    // 异步 refresh 不覆盖用户正在输入的 key（后端只回掩码/空，且 long key 输入耗时）
    if (!$('#agent-provider-key').value.trim()) $('#agent-provider-key').value = cfg.api_key || '';
    $('#agent-provider-model').value = cfg.model || 'gpt-4o-mini';
    $('#agent-provider-protocol').value = cfg.protocol || 'openai';
  } catch (err) {
    $('#agent-provider-endpoint').value = '';
  }

  $('#agent-token').value = mask(token());
  await refreshTokens();
}

function mask(tk) { return tk ? `${tk.slice(0, 6)}…${tk.slice(-4)}` : ''; }

async function refreshSnapshotStatus() {
  const el = $('#agent-snapshot-status');
  if (!el) return;
  try {
    const s = await api.get('/api/history/status');
    el.textContent = s.available
      ? `git 可用 · 仓库${s.repo ? '已初始化' : '未初始化'} · ${s.count} 条快照`
      : '系统未安装 git，快照不可用';
  } catch (err) {
    el.textContent = `快照状态读取失败：${err.message}`;
  }
}

async function issueToken() {
  try {
    if (!token()) {
      const data = await agentFetch('/api/agent/bootstrap', { method: 'POST' });
      setToken(data.token);
      toast('已创建首个 admin token（仅保存在本机浏览器）', 'success');
    } else {
      const data = await agentFetch('/api/agent/tokens', {
        method: 'POST',
        body: { action: 'issue', role: $('#agent-token-role').value, name: 'ui' },
      });
      toast(`已生成 ${data.role} token`, 'success');
    }
    $('#agent-token').value = mask(token());
    await refreshTokens();
  } catch (err) { reportError(err); }
}

async function refreshTokens() {
  const box = $('#agent-token-list');
  if (!box) return;
  if (!token()) {
    box.innerHTML = '<div class="panel-empty">尚未生成访问 token</div>';
    return;
  }
  try {
    const data = await agentFetch('/api/agent/tokens', {
      method: 'POST', body: { action: 'list' },
    });
    const list = data.tokens || [];
    if (!list.length) {
      box.innerHTML = '<div class="panel-empty">尚未生成访问 token</div>';
      return;
    }
    box.innerHTML = list.map((t) => `
      <div class="agent-token-row">
        <span class="agent-token-row__role">${escapeHtml(t.role)}</span>
        <span class="agent-token-row__name">${escapeHtml(t.name || '—')}</span>
        <code>${escapeHtml(mask(t.token))}</code>
        <button class="panel-list__action" data-token="${escapeHtml(t.token)}" title="撤销此 token">撤销</button>
      </div>`).join('');
    box.querySelectorAll('[data-token]').forEach((btn) => {
      btn.addEventListener('click', () => revoke(btn.dataset.token));
    });
  } catch (err) {
    box.innerHTML = `<div class="panel-empty">${escapeHtml(err.message)}</div>`;
  }
}

async function revoke(value) {
  if (!await confirmDialog('确认撤销该访问 token？使用它的 Agent 将立即失去访问权限。', {
    title: '撤销 token', danger: true, confirmText: '撤销',
  })) return;
  try {
    await agentFetch('/api/agent/tokens', {
      method: 'POST', body: { action: 'revoke', token: value },
    });
    if (value === token()) setToken('');
    $('#agent-token').value = mask(token());
    await refreshTokens();
    toast('已撤销 token', 'success');
  } catch (err) { reportError(err); }
}

async function saveProvider() {
  try {
    await agentFetch('/api/agent/provider-config', {
      method: 'PUT',
      body: {
        endpoint: $('#agent-provider-endpoint').value.trim(),
        api_key: $('#agent-provider-key').value.replace(/\s+/g, ''),
        model: $('#agent-provider-model').value.trim(),
        protocol: $('#agent-provider-protocol').value,
      },
    });
    toast('Provider 配置已保存到 .kwey/provider.json', 'success');
    emit('provider-changed');     // 聊天面板据此重算就绪态（协议切换不必切面板）
    await refresh();
  } catch (err) { reportError(err); }
}

async function toggleSnapshots() {
  const enabled = $('#settings-snapshots-enabled').checked;
  try {
    await saveSettings({ snapshots: { enabled, auto_commit: true } });
    await refreshSnapshotStatus();
    toast(enabled ? '已开启 git 快照（写盘自动提交）' : '已关闭 git 快照', 'success');
  } catch (err) { reportError(err); }
}
