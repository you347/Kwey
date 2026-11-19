/* fetch 封装（统一 {code,data,message}）+ WS 客户端 + 自动重连。 */

import { toast } from './utils.js';

// 与 agentlink.js 共用同一个 localStorage key；此处直接读取避免模块循环依赖。
const TOKEN_KEY = 'kwey.agent.token';

/** 读取本地 token（若存在）。所有同源请求统一注入，受保护端点无需各模块自行拼 header。 */
function localToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

export class ApiError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
  }
}

/** 发起单次请求并返回 {ok, status, payload}；不抛业务错误，交给上层决定。 */
async function sendOnce(url, opts) {
  let res;
  try {
    res = await fetch(url, opts);
  } catch (err) {
    throw new ApiError(0, `网络错误: ${err.message}`);
  }
  let payload;
  try {
    payload = await res.json();
  } catch {
    payload = { code: res.status, data: null, message: res.statusText };
  }
  return { ok: res.ok && payload.code === 0, status: res.status, payload };
}

/**
 * token 失效自愈：本地 UI 的 token 只存 hash，浏览器若存着被吊销的旧 token
 * （例如重新 bootstrap 后），所有请求会一直 401。此处重新 bootstrap 拿新 token
 * 并覆盖 localStorage，供本次重试与后续请求使用。失败则静默（由原请求抛出 401）。
 */
let renewing = null;  // 并发 401 共享同一次续签，避免反复 bootstrap 互相吊销 token
/** 续签本地 token（供 agentlink 等自定义 fetch 复用）；并发调用共享同一次结果。 */
export function renewToken() {
  if (renewing) return renewing;
  renewing = (async () => {
    try {
      const res = await fetch('/api/agent/bootstrap', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      if (!res.ok) return false;
      const data = (await res.json()).data;
      if (data && data.token) {
        localStorage.setItem(TOKEN_KEY, data.token);
        return true;
      }
    } catch { /* 网络/存储异常：放弃自愈，原 401 照常抛出 */ }
    return false;
  })().finally(() => { renewing = null; });
  return renewing;
}

async function request(method, path, { query = null, body = undefined, form = null } = {}) {
  let url = path;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null) qs.set(k, v);
    }
    const s = qs.toString();
    if (s) url += `?${s}`;
  }
  const buildOpts = () => {
    const opts = { method, headers: {} };
    const tk = localToken();
    if (tk) opts.headers['X-Kwey-Token'] = tk;
    if (form) opts.body = form;
    else if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return opts;
  };

  let out = await sendOnce(url, buildOpts());
  // 401 且非 bootstrap 自身 → 续签一次并重放（避免死循环只重试一次）
  if (!out.ok && out.status === 401 && !path.startsWith('/api/agent/bootstrap')) {
    if (await renewToken()) out = await sendOnce(url, buildOpts());
  }
  if (!out.ok) {
    // FastAPI HTTPException 返回 {detail}，统一透出真实原因
    throw new ApiError(
      out.payload.code ?? out.status,
      out.payload.message || out.payload.detail || out.payload.statusText || '请求失败',
    );
  }
  return out.payload.data;
}

/** 当前本地 token 注入（供流式 / 自定义 fetch 复用，与 api.request 同源）。 */
export function authHeaders(extra = {}) {
  const headers = { ...extra };
  const tk = localToken();
  if (tk) headers['X-Kwey-Token'] = tk;
  return headers;
}

export const api = {
  get: (path, query) => request('GET', path, { query }),
  post: (path, body) => request('POST', path, { body }),
  put: (path, body) => request('PUT', path, { body }),
  del: (path, query) => request('DELETE', path, { query }),
  upload: (file) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    return request('POST', '/api/attachments', { form: fd });
  },
};

/**
 * 连接 /ws/events，事件回调 handler({event,path,old_path,ts})；
 * 断线自动重连（1.5s 退避）。
 */
export function connectWS(handler) {
  let ws = null;
  let closed = false;
  const RETRY_MS = 1500;

  function open() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/events`);
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.event && !['ping', 'pong', 'connected'].includes(msg.event)) handler(msg);
      } catch { /* 忽略非 JSON 帧 */ }
    };
    ws.onclose = () => { if (!closed) setTimeout(open, RETRY_MS); };
    ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
  }
  open();
  return () => { closed = true; if (ws) ws.close(); };
}

/** 统一错误提示（未捕获的 ApiError 都走这里）。 */
export function reportError(err) {
  const message = err && err.message ? err.message : String(err);
  toast(message, 'error');
  console.error('[kwey]', err);
}
