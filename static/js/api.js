/* fetch 封装（统一 {code,data,message}）+ WS 客户端 + 自动重连。 */

import { toast } from './utils.js';

export class ApiError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
  }
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
  const opts = { method, headers: {} };
  if (form) {
    opts.body = form;
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
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
  if (!res.ok || payload.code !== 0) {
    throw new ApiError(payload.code ?? res.status, payload.message || '请求失败');
  }
  return payload.data;
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
