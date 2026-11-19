/* 内置智能体聊天面板（卡B W2）：fetch 流式消费 POST /api/agent/chat SSE。
 * 服务端无状态：历史由 done.messages 回传、前端续带；危险操作弹 askConfirm，
 * 允许 → resume={"id"} 强制执行；拒绝 → 以 tool 消息回喂内核让模型作答。 */

import { api, authHeaders, renewToken } from './api.js';
import { state, on, emit } from './state.js';
import { activatePanel } from './shell.js';
import { $, escapeHtml, toast, copyText, confirmDialog } from './utils.js';
import { askConfirm } from './dialog.js';
import { openSettings } from './settings/index.js';

let initialized = false;
let history = [];
let streaming = false;
let aborter = null;
let providerReady = false;

const EMPTY_HINT = '<div class="panel-empty">让内置智能体替你读写、整理笔记；打开的笔记会自动成为对话上下文。</div>';

/* ------------------------------------------------------------------ 工作区上下文 */
/* 跟随界面自动识别：打开笔记 → 本篇；切到「文件夹」→ 所在目录；智能体定位目录 /
   文件树右键「作为对话上下文」→ 直接钉住该目录。只随请求发送路径，权限仍由工具把关。 */
let ctx = { path: null, kind: 'note', scope: 'note', muted: false };
let lastSentContext = null;   // 上一条消息携带的上下文，用于在切换处插一条提示行
let activeContext = null;     // 本轮对话的上下文（危险操作 resume 时原样带回）

function parentDir(path) {
  const rel = String(path || '').replace(/\\/g, '/').replace(/\/+$/, '');
  const i = rel.lastIndexOf('/');
  return i === -1 ? '' : rel.slice(0, i);
}

function baseName(path) {
  const rel = String(path || '').replace(/\\/g, '/').replace(/\/+$/, '');
  return rel.slice(rel.lastIndexOf('/') + 1) || rel;
}

/** 按 scope 解析出真正要告诉模型的对象（根目录笔记没有「文件夹」范围）。 */
function resolveContext() {
  if (!ctx.path) return null;
  if (ctx.kind === 'dir') return { path: ctx.path, kind: 'dir' };
  if (ctx.scope === 'dir' && parentDir(ctx.path)) {
    return { path: parentDir(ctx.path), kind: 'dir' };
  }
  return { path: ctx.path, kind: 'note' };
}

function focusedPath() {
  const split = state.split;
  if (split && split.open && split.focused === 'secondary') {
    return (split.secondary && split.secondary.activePath) || null;
  }
  return state.activePath || null;
}

function effectiveContext() {
  if (ctx.muted) return null;
  return resolveContext();
}

/** 设置当前对象；跨目录时回到「本篇」，同目录内切换保留「文件夹」范围。 */
function setContext(path, kind = 'note') {
  if (!path) return;
  const prevDir = ctx.path ? parentDir(ctx.path) : null;
  let scope = 'note';
  if (kind === 'dir') scope = 'dir';
  else if (ctx.scope === 'dir' && prevDir && prevDir === parentDir(path)) scope = 'dir';
  ctx = { path, kind, scope, muted: false };
  renderContext();
}

function renderContext() {
  const bar = $('#chat-context');
  if (!bar) return;
  const target = resolveContext();
  bar.classList.toggle('hidden', !target);
  if (!target) return;

  const isDir = target.kind === 'dir';
  $('#chat-context-name').textContent = isDir ? `${baseName(target.path)}/` : baseName(target.path);
  $('#chat-context-dir').textContent = parentDir(target.path) ? `${parentDir(target.path)}/` : '';
  const use = $('#chat-context-open use');
  if (use) use.setAttribute('href', isDir ? '#i-folder' : '#i-file');
  $('#chat-context-open').title = isDir ? `在左栏定位 ${target.path}/` : `打开 ${target.path}`;
  bar.classList.toggle('chat-context--off', ctx.muted);

  // 范围：笔记可切「本篇 / 文件夹」；根目录笔记没有文件夹层级；钉住的目录只有「文件夹」
  const scopeEl = $('#chat-context-scope');
  const canDir = ctx.kind === 'dir' || !!parentDir(ctx.path);
  scopeEl.querySelector('option[value="note"]').disabled = ctx.kind === 'dir';
  scopeEl.querySelector('option[value="dir"]').disabled = !canDir;
  scopeEl.value = ctx.muted ? 'off' : (ctx.scope === 'dir' && canDir ? 'dir' : 'note');
  if (ctx.kind === 'dir') scopeEl.value = ctx.muted ? 'off' : 'dir';
}

export function initChat() {
  if (initialized) return;
  initialized = true;
  $('#btn-chat-send').addEventListener('click', () => send());
  $('#btn-chat-stop').addEventListener('click', () => { if (aborter) aborter.abort(); });
  $('#btn-chat-clear').addEventListener('click', clearChat);
  $('#btn-chat-setup').addEventListener('click', () => openSettings());
  $('#chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $('#chat-messages').addEventListener('click', (e) => { onChatAction(e); });
  // 上下文条：切换范围（不使用 = 本条消息不带上下文）/ 点击路径直接打开
  $('#chat-context-scope').addEventListener('change', (e) => {
    const value = e.target.value;
    ctx.muted = value === 'off';
    if (!ctx.muted) ctx.scope = value === 'dir' ? 'dir' : 'note';
    lastSentContext = null;
    renderContext();
  });
  $('#chat-context-open').addEventListener('click', () => {
    const target = resolveContext();
    if (!target) return;
    if (target.kind === 'dir') emit('open-folder', { path: target.path, kind: 'dir' });
    else emit('open-note', target.path);
  });
  // 自动识别：焦点屏的激活标签 / 智能体定位目录 / 文件树右键
  on('tab-activated', (path) => {
    if (path && !(state.split && state.split.open && state.split.focused === 'secondary')) {
      setContext(path, 'note');
    }
  });
  on('secondary-tab-activated', (path) => {
    if (path && state.split && state.split.open && state.split.focused === 'secondary') {
      setContext(path, 'note');
    }
  });
  on('pane-focused', () => {
    const path = focusedPath();
    if (path) setContext(path, 'note');
  });
  on('open-folder', (payload) => {
    const path = typeof payload === 'string' ? payload : payload && payload.path;
    const kind = typeof payload === 'string' ? 'dir' : payload && payload.kind;
    if (path && kind === 'dir') setContext(path, 'dir');   // 附件等文件不在编辑器里，不当作上下文
  });
  on('chat-context', (payload = {}) => {
    const { path, kind, focus } = payload;
    if (!path) return;
    setContext(path, kind === 'dir' ? 'dir' : 'note');
    if (focus !== false) activatePanel('right', 'chat');
  });
  if (focusedPath()) setContext(focusedPath(), 'note');
  on('panel-activated', ({ side, name }) => {
    if (side === 'right' && name === 'chat') {
      refreshProvider()
        .then(() => { if (providerReady) $('#chat-input').focus(); })
        .catch(() => {});
    }
  });
  on('provider-changed', () => { refreshProvider().catch(() => {}); });
  refreshProvider().catch(() => {});
}

function clearChat() {
  if (streaming) { toast('回答进行中，请先停止', 'error'); return; }
  history = [];
  lastSentContext = null;
  $('#chat-messages').innerHTML = EMPTY_HINT;
}

/* ------------------------------------------------------------------ Provider 就绪态 */
async function refreshProvider() {
  const banner = $('#chat-setup');
  const text = $('#chat-setup-text');
  try {
    const cfg = await api.get('/api/agent/provider-config');
    if (!cfg.configured) {
      providerReady = false;
      text.textContent = '尚未配置 Agent Provider，内置智能体不可用。';
    } else if (cfg.protocol !== 'openai') {
      providerReady = false;
      text.textContent = '聊天需要 openai 协议的 Provider（当前为 http，仅支持整理指令）。';
    } else {
      providerReady = true;
    }
  } catch {
    providerReady = false;
    text.textContent = 'Provider 配置读取失败，请检查 vault 是否已打开。';
  }
  banner.classList.toggle('hidden', providerReady);
  syncButtons();
}

function syncButtons() {
  $('#btn-chat-send').disabled = !providerReady || streaming;
  $('#btn-chat-stop').classList.toggle('hidden', !streaming);
  $('#chat-busy').classList.toggle('hidden', !streaming);
}

/* ------------------------------------------------------------------ 渲染 */
function scrollBottom() {
  const box = $('#chat-messages');
  box.scrollTop = box.scrollHeight;
}

/** 用户是否仍贴着底部：异步渲染撑高气泡后，只在原本贴底时跟随滚动。 */
function isNearBottom() {
  const box = $('#chat-messages');
  return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
}

function firstHintRemoved() {
  const box = $('#chat-messages');
  const hint = box.querySelector('.panel-empty');
  if (hint) hint.remove();
}

let msgSeq = 0;
function messageId() {
  if (globalThis.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  msgSeq += 1;
  return `m${Date.now().toString(36)}-${msgSeq}`;
}

/** 消息 = 气泡 + 紧贴下方的操作条（复制 / 有用 / 没用 / 撤回）。 */
function buildMessage(role, text, cut) {
  firstHintRemoved();
  const el = document.createElement('div');
  el.className = `chat-msg chat-msg--${role}`;
  el.dataset.id = messageId();
  if (role === 'user' && Number.isFinite(cut)) el.dataset.cut = String(cut);

  const bubble = document.createElement('div');
  bubble.className = 'chat-msg__bubble';
  bubble.textContent = text;
  el.appendChild(bubble);

  const bar = document.createElement('div');
  bar.className = 'chat-actions';
  const action = (act, label, title) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chat-action';
    btn.dataset.act = act;
    btn.title = title;
    btn.textContent = label;
    if (act === 'like' || act === 'dislike') btn.setAttribute('aria-pressed', 'false');
    bar.appendChild(btn);
  };
  action('copy', '复制', '复制这条消息');
  if (role === 'user') {
    action('undo', '撤回', '撤回这条消息及其后的对话');
  } else if (role === 'assistant') {
    action('like', '有用', '这条回复有帮助（用于调整回复风格）');
    action('dislike', '没用', '这条回复没帮助（用于调整回复风格）');
  }
  el.appendChild(bar);

  $('#chat-messages').appendChild(el);
  scrollBottom();
  // 助手回复补一次 Markdown 渲染（同步先塞纯文本兜底；用户/报错气泡保持纯文本）
  if (role === 'assistant') renderAssistantMarkdown(bubble, text);
  return el;
}

/* ------------------------------------------------------------------ Markdown 渲染 */
const RENDER_LIMIT = 200_000;          // 与后端 RenderRequest.content 上限一致
const APPEND_RENDER_DELAY = 120;       // 追加片段的重渲染防抖：避免每个 fragment 打一次 /api/agent/render

/** 助手气泡异步升级为 Markdown；失败保持纯文本，聊天不因渲染失败而中断。 */
async function renderAssistantMarkdown(bubble, text) {
  if (!text || text.length > RENDER_LIMIT) return;
  const stick = isNearBottom();
  // 同一气泡可能先后发起多次渲染（流式追加）：只认最后一次，过期结果直接丢弃
  const seq = String((Number(bubble.dataset.renderSeq) || 0) + 1);
  bubble.dataset.renderSeq = seq;
  try {
    const data = await postRender(text);
    if (!data || !bubble.isConnected) return;   // 已撤回 / 对话已清空：丢弃结果
    if (bubble.dataset.renderSeq !== seq) return;
    bubble.innerHTML = data.html;
    bubble.classList.add('chat-markdown');
    addCodeCopyButtons(bubble);
    markScrollableTables(bubble);
    if (stick) scrollBottom();
  } catch { /* 渲染是增强项，静默失败 */ }
}

async function postRender(text) {
  const doFetch = () => fetch('/api/agent/render', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ content: text }),
  });
  let res = await doFetch();
  if (res.status === 401 && await renewToken()) res = await doFetch();   // 同 stream() 策略
  if (!res.ok) return null;
  const payload = await res.json();
  return (payload && payload.data) || null;
}

/** 围栏代码块包一层 .chat-code：按钮挂在包层上，代码横向滚动时按钮不跟着跑；
 *  事件统一走 #chat-messages 的点击委托。 */
function addCodeCopyButtons(root) {
  root.querySelectorAll('pre').forEach((pre) => {
    const host = pre.parentElement && pre.parentElement.classList.contains('chat-code')
      ? pre.parentElement
      : null;
    if (host) return;   // 已包过：重复渲染兜底
    const wrap = document.createElement('div');
    wrap.className = 'chat-code';
    pre.replaceWith(wrap);
    wrap.appendChild(pre);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chat-code-copy';
    btn.dataset.copyCode = '';
    btn.textContent = '复制';
    wrap.appendChild(btn);
  });
}

/** 宽表格在窄侧栏里只能横滚：给可滚动容器加类，由 CSS 在右缘做渐隐提示。 */
function markScrollableTables(root) {
  root.querySelectorAll('table').forEach((table) => {
    // table 自身是 display:block + overflow-x:auto 的滚动容器；
    // 父级是气泡本身，给父级加遮罩会连正文一起淡化，所以类加在 table 上。
    table.classList.toggle('is-scrollable', table.scrollWidth > table.clientWidth + 1);
  });
}

async function copyCodeBlock(btn) {
  const host = btn.closest('.chat-code');
  const code = host && host.querySelector('pre > code');
  if (!code) return;
  const ok = await copyText(code.textContent);
  toast(ok ? '已复制' : '复制失败：剪贴板不可用', ok ? 'success' : 'error', 1400);
  if (!ok) return;
  btn.textContent = '已复制';
  window.setTimeout(() => { if (btn.isConnected) btn.textContent = '复制'; }, 1200);
}

/** 整条消息复制：Markdown 气泡要剔除代码块复制按钮，避免把「复制」二字带进剪贴板。 */
function bubblePlainText(msg) {
  const bubble = msg.querySelector('.chat-msg__bubble');
  if (!bubble) return '';
  if (!bubble.classList.contains('chat-markdown')) return bubble.textContent || '';
  const clone = bubble.cloneNode(true);
  clone.querySelectorAll('[data-copy-code]').forEach((n) => n.remove());
  return clone.textContent || '';
}

/** 上下文切换处的一条细提示，便于回看某轮回复基于哪个文件。 */
function addContextNote(context) {
  firstHintRemoved();
  const el = document.createElement('div');
  el.className = 'chat-context-note';
  el.textContent = `上下文 · ${context.kind === 'dir' ? `${context.path}/` : context.path}`;
  el.title = '本轮消息携带的当前工作区';
  $('#chat-messages').appendChild(el);
  scrollBottom();
}

function addUser(text, cut) { return buildMessage('user', text, cut); }
function addAssistant(text) { return buildMessage('assistant', text); }
function addError(text) { return buildMessage('error', text); }

/* ------------------------------------------------------------------ 消息操作 */
async function onChatAction(e) {
  const codeBtn = e.target.closest('[data-copy-code]');
  if (codeBtn) { await copyCodeBlock(codeBtn); return; }
  const btn = e.target.closest('.chat-action');
  if (!btn) return;
  const msg = btn.closest('.chat-msg');
  if (!msg) return;
  const text = bubblePlainText(msg);
  const act = btn.dataset.act;

  if (act === 'copy') {
    const ok = await copyText(text);
    toast(ok ? '已复制' : '复制失败：剪贴板不可用', ok ? 'success' : 'error', 1400);
    return;
  }
  if (act === 'like' || act === 'dislike') { await rateMessage(msg, act, text); return; }
  if (act === 'undo') await undoMessage(msg);
}

/** 点赞 / 点踩：同一条再点一次即取消（clear），落盘后用于系统提示调教。 */
async function rateMessage(msg, act, text) {
  const rating = act === 'like' ? 'up' : 'down';
  const current = msg.dataset.rating || '';
  const next = current === rating ? 'clear' : rating;
  try {
    await api.post('/api/agent/feedback', {
      id: msg.dataset.id, rating: next, content: text, role: 'assistant',
    });
    msg.dataset.rating = next === 'clear' ? '' : next;
    msg.querySelectorAll('.chat-action').forEach((b) => {
      const on = (b.dataset.act === 'like' && next === 'up')
        || (b.dataset.act === 'dislike' && next === 'down');
      if (b.dataset.act === 'like' || b.dataset.act === 'dislike') b.setAttribute('aria-pressed', String(on));
    });
    const word = next === 'clear' ? '已取消评价' : (next === 'up' ? '已记录：有用' : '已记录：没用');
    toast(word, 'success', 1400);
  } catch (err) {
    toast(err.message || '反馈保存失败', 'error');
  }
}

/** 撤回用户消息：移除该条及其后的全部对话（含工具步骤），history 同步截断。 */
async function undoMessage(msg) {
  const cut = Number(msg.dataset.cut);
  if (!Number.isFinite(cut)) return;
  const ok = await confirmDialog('撤回这条消息？其后的对话会一并移除。', {
    title: '撤回消息', confirmText: '撤回', danger: true,
  });
  if (!ok) return;
  history = history.slice(0, cut);
  let node = msg;
  while (node) {
    const next = node.nextElementSibling;
    node.remove();
    node = next;
  }
  if (!document.querySelector('#chat-messages .chat-msg')) {
    $('#chat-messages').innerHTML = EMPTY_HINT;
  }
  toast('已撤回', 'success', 1200);
}

function argsSummary(args) {
  const s = JSON.stringify(args || {});
  return s.length > 90 ? `${s.slice(0, 90)}…` : s;
}

function addToolStep(ev, status = 'run') {
  firstHintRemoved();
  let el = $(`.chat-step[data-id="${CSS.escape(ev.id)}"]`);
  if (el) {
    el.className = `chat-step chat-step--${status}`;
    scrollBottom();
    return;
  }
  el = document.createElement('div');
  el.className = `chat-step chat-step--${status}`;
  el.dataset.id = ev.id;
  el.innerHTML = `
    <span class="chat-step__status"></span>
    <span class="chat-step__name">${escapeHtml(ev.name)}</span>
    <span class="chat-step__args" title="${escapeHtml(JSON.stringify(ev.arguments || {}))}">${escapeHtml(argsSummary(ev.arguments))}</span>`;
  $('#chat-messages').appendChild(el);
  scrollBottom();
}

function updateToolStep(ev) {
  const el = $(`.chat-step[data-id="${CSS.escape(ev.id)}"]`);
  if (!el) return;
  el.classList.remove('chat-step--run', 'chat-step--wait');
  el.classList.add(ev.ok ? 'chat-step--ok' : 'chat-step--err');
  if (!ev.ok && ev.error) {
    const err = document.createElement('span');
    err.className = 'chat-step__error';
    err.textContent = ev.error;
    el.appendChild(err);
  }
  scrollBottom();
}

/* ------------------------------------------------------------------ 界面意图 */
/** 让结果落到产品里：笔记进中心编辑区，目录/附件在左栏定位。 */
function handleUiEvent(ev) {
  if (!ev || ev.action !== 'open' || !ev.path) return;
  if (ev.kind === 'dir' || ev.kind === 'file') emit('open-folder', { path: ev.path, kind: ev.kind });
  else emit('open-note', ev.path);
}

/* ------------------------------------------------------------------ SSE 流 */
async function stream(body, onEvent) {
  aborter = new AbortController();
  const payload = JSON.stringify(body);
  const doFetch = () => fetch('/api/agent/chat', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: payload,
    signal: aborter.signal,
  });
  let res = await doFetch();
  // 本地 token 可能被吊销/清空：重签一次并重放（与 api.js 的 401 自愈同策略）
  if (res.status === 401 && await renewToken()) res = await doFetch();
  if (!res.ok) {
    let detail = `请求失败 (${res.status})`;
    try { detail = (await res.json()).detail || detail; } catch { /* 非 JSON 响应 */ }
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = frame.split('\n').find((l) => l.startsWith('data: '));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

function setStreaming(on_) {
  streaming = on_;
  syncButtons();
}

async function send() {
  if (streaming) return;
  const input = $('#chat-input');
  const text = input.value.trim();
  if (!text) return;
  if (!providerReady) { toast('请先配置 openai 协议的 Agent Provider', 'error'); return; }
  input.value = '';
  const context = effectiveContext();
  if (context) {
    const key = `${context.kind}:${context.path}`;
    if (key !== lastSentContext) { addContextNote(context); lastSentContext = key; }
  } else {
    lastSentContext = null;
  }
  activeContext = context;
  const cut = history.length;
  addUser(text, cut);
  history = [...history, { role: 'user', content: text }];
  await run({ messages: history, context });
}

async function run(body) {
  setStreaming(true);
  let doneData = null;
  // 本轮回复共用的助手气泡：流式输出会把一条回复拆成多条 message，合并进同一气泡
  let assistantEl = null;
  let assistantText = '';
  let renderTimer = null;

  const flushAssistantRender = () => {
    renderTimer = null;
    if (!assistantEl || !assistantEl.isConnected) return;
    const bubble = assistantEl.querySelector('.chat-msg__bubble');
    if (bubble) renderAssistantMarkdown(bubble, assistantText);
  };

  /** 首条 message 新建气泡（buildMessage 内已渲染一次）；后续片段追加文本并防抖重渲染。 */
  const appendAssistant = (text) => {
    if (!assistantEl || !assistantEl.isConnected) {
      assistantEl = addAssistant(text);
      assistantText = text;
      return;
    }
    assistantText += text;
    const bubble = assistantEl.querySelector('.chat-msg__bubble');
    if (bubble) {
      // 防抖未触发前先显示纯文本，保证片段不丢字
      bubble.classList.remove('chat-markdown');
      bubble.textContent = assistantText;
    }
    if (isNearBottom()) scrollBottom();
    window.clearTimeout(renderTimer);
    renderTimer = window.setTimeout(flushAssistantRender, APPEND_RENDER_DELAY);
  };

  try {
    await stream(body, (ev) => {
      switch (ev.type) {
        case 'tool_call': addToolStep(ev); break;
        case 'tool_result': updateToolStep(ev); break;
        case 'confirm_required': addToolStep(ev, 'wait'); break;
        case 'message': if (ev.content) appendAssistant(ev.content); break;
        case 'ui': handleUiEvent(ev); break;
        case 'error': addError(ev.message || '未知错误'); break;
        case 'done': doneData = ev; break;
        default: break;
      }
    });
  } catch (err) {
    if (err.name === 'AbortError') addError('已停止本次回答');
    else addError(err.message || String(err));
  } finally {
    // 收尾：把待渲染的合并文本立刻补渲染，再结束本轮（done / error / AbortError 都走这里）
    if (renderTimer) {
      window.clearTimeout(renderTimer);
      flushAssistantRender();
    }
    assistantEl = null;
    assistantText = '';
    setStreaming(false);
    aborter = null;
  }
  if (!doneData) return;
  history = doneData.messages || history;
  if (doneData.pending) {
    await handleConfirm(doneData);
  } else {
    emit('tree-refresh');
  }
}

/** 危险操作确认：允许 → resume 强制执行；拒绝 → tool 消息回喂，模型继续作答。 */
async function handleConfirm(done) {
  const p = done.pending;
  const allowed = await askConfirm({
    title: '智能体危险操作确认',
    message: `即将执行 ${p.name}，参数：${JSON.stringify(p.arguments || {})}`,
    danger: true,
    confirmText: '允许执行',
    cancelText: '拒绝',
  });
  if (allowed) {
    await run({ messages: done.messages, resume: { id: p.id }, context: activeContext });
    return;
  }
  addError(`已拒绝执行 ${p.name}`);
  const msgs = [...done.messages, {
    role: 'tool',
    tool_call_id: p.id,
    content: JSON.stringify({ code: 1, data: null, message: '用户拒绝了此操作' }),
  }];
  await run({ messages: msgs, context: activeContext });
}