/* 导出：把笔记下载为原始 Markdown / 独立 HTML / PDF，或打开打印页（无第三方依赖）。 */

import { api, reportError } from './api.js';
import { state } from './state.js';
import { toast } from './utils.js';

const MIME = {
  md: 'text/markdown; charset=utf-8',
  html: 'text/html; charset=utf-8',
  pdf: 'application/pdf',
};

/** 内容 → Blob 触发下载；pdf 服务端返回 base64，先解码为字节。 */
function download(content, mime, filename, encoding) {
  let payload = content == null ? '' : content;
  if (encoding === 'base64') {
    const bin = atob(payload);
    payload = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) payload[i] = bin.charCodeAt(i);
  }
  const blob = new Blob([payload], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 交给下载动作启动后再释放，避免个别浏览器来不及取流
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** 导出指定笔记：path 缺省取当前激活笔记，format 支持 md / html / pdf。 */
export async function exportNote(path, format = 'md') {
  const target = path || state.activePath;
  if (!target) {
    toast('当前没有打开的笔记，无法导出', 'error');
    return;
  }
  const fmt = format === 'html' || format === 'pdf' ? format : 'md';
  try {
    const data = await api.get('/api/export', { path: target, format: fmt });
    const filename = data.filename || `note.${fmt}`;
    download(data.content, data.mime || MIME[fmt], filename, data.encoding);
    toast(`已导出 ${filename}`, 'success');
  } catch (err) {
    reportError(err);
  }
}

/** 供命令面板注册：打开打印页（window.open + 自动 print），可另存为 PDF。 */
export async function exportPrint(path) {
  const target = path || state.activePath;
  if (!target) {
    toast('当前没有打开的笔记，无法导出', 'error');
    return;
  }
  // 同步开窗保留用户手势，避免 await 之后被弹窗拦截器拦下
  const win = window.open('', '_blank');
  if (!win) {
    toast('打印窗口被浏览器拦截，请允许弹窗后重试', 'error');
    return;
  }
  let data;
  try {
    data = await api.get('/api/export', { path: target, format: 'print' });
  } catch (err) {
    win.close();
    reportError(err);
    return;
  }
  const html = String(data.content || '').replace(
    '</body>',
    '<script>window.addEventListener("load",function(){window.focus();window.print();});</script></body>',
  );
  const url = URL.createObjectURL(new Blob([html], { type: 'text/html; charset=utf-8' }));
  win.location.href = url;
  win.addEventListener('afterprint', () => URL.revokeObjectURL(url));
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

/** 供命令面板注册：导出当前笔记。 */
export function exportCurrent(format) {
  return exportNote(state.activePath, format);
}