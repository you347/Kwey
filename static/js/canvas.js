/* C1 Canvas 无限画布：Obsidian 兼容 .canvas（JSON）的看图/编辑面板。
 *
 * 能力：
 * - 平移（拖空白）/ 缩放（滚轮，锚定光标）/ 适应视图；
 * - 卡片拖拽、右下角缩放、Shift+拖拽框选、Delete 删除；
 * - 双击空白新建文本卡；双击文本卡就地编辑；从文件树拖入笔记生成 file 节点；
 * - 节点四边手柄拖到另一节点建连线；
 * - 保存防抖 800ms + Ctrl/Cmd+S 立即保存，数据经 /api/canvas/save 落盘。
 *
 * 约束：不新增 index.html / CSS 文件——浮层 DOM 与样式都在本模块内注入，
 * 颜色全部走主题 CSS 变量（亮/暗自动适配）。零第三方依赖。
 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, debounce, toast, basename, stripMd } from './utils.js';
import { askText } from './dialog.js';
import { registerCommand } from './palette.js';

export const COMMAND_ID = 'canvas.open';

const LAST_KEY = 'kwey.canvas.last';
const MIN_SCALE = 0.2;
const MAX_SCALE = 2.5;
const DEFAULT_W = 240;
const DEFAULT_H = 120;
const MIN_W = 120;
const MIN_H = 60;
const MAX_TEXT = 20000;
const SIDES = ['n', 'e', 's', 'w'];
const STYLE_ID = 'canvas-style';

let initialized = false;
let dom = null;

/** 画布运行时状态（与 app 的 state 模块无关）。 */
const cv = {
  path: null,
  data: null,          // {nodes, edges, ...未知字段原样保留}
  list: [],            // /api/canvas/list 结果
  mtime: 0,
  scale: 1,
  tx: 40,
  ty: 40,
  selected: new Set(),
  editingId: null,
  editingEl: null,
  connecting: null,    // {fromId, side}
  els: new Map(),      // id -> element
  open: false,
  prevFocus: null,     // 打开前的焦点元素，关闭时归还
  saving: false,
  pending: false,
  dirty: false,
};

const saveSoon = debounce(() => { saveNow().catch(reportError); }, 800);

/* ------------------------------------------------------------------ 初始化 */
export function initCanvas() {
  if (initialized) return;
  initialized = true;
  registerCommand({
    id: COMMAND_ID,
    title: '打开画布（Canvas）',
    run: () => { openCanvas().catch(reportError); },
  });
  // 文件树里的 .canvas 是 file 节点（单击不打开笔记）：双击直接进画布
  document.addEventListener('dblclick', (e) => {
    const row = e.target.closest && e.target.closest('.tree-row[data-path]');
    if (!row) return;
    if (!String(row.dataset.path || '').toLowerCase().endsWith('.canvas')) return;
    e.preventDefault();
    openCanvas(row.dataset.path).catch(reportError);
  });
}

/** 打开画布面板；给 path 则打开指定画布，否则打开上次/第一块画布。 */
export async function openCanvas(path) {
  ensureDom();
  const wasOpen = cv.open;
  cv.open = true;
  if (!wasOpen) cv.prevFocus = document.activeElement;
  dom.overlay.classList.remove('hidden');
  // 把焦点从背后编辑器挪到画布：否则 Delete/输入会落到隐藏的笔记编辑器里
  dom.viewport.tabIndex = -1;
  try {
    dom.viewport.focus({ preventScroll: true });
  } catch {
    dom.viewport.focus();
  }
  try {
    await refreshList();
  } catch (err) {
    showEmpty(`画布列表读取失败：${err.message || err}`);
    setStatus('不可用');
    return;
  }

  let target = path;
  if (!target) {
    const last = localStorage.getItem(LAST_KEY);
    target = (last && cv.list.some((item) => item.path === last))
      ? last
      : (cv.list[0] && cv.list[0].path);
  }
  if (!target) {
    showEmpty('还没有画布：点击右上角「新建」创建第一块画布。');
    setStatus('');
    return;
  }
  if (wasOpen && cv.path === target && cv.data) {
    drawAll();
    setStatus(cv.dirty ? '未保存' : '已载入');
    return;
  }
  if (cv.editingId) commitEdit();       // 先把正在编辑的卡片文字提交，再保存/切换
  if (cv.path && cv.dirty) {
    const ok = await flushSave();
    if (!ok) return;                    // 保存失败不切换，避免丢改动
  }
  await loadCanvas(target);
}

/** 关闭面板（先落盘未保存改动；保存失败则留在画布，避免丢改动）。 */
export async function closeCanvas() {
  if (!dom) return;
  if (cv.editingId) commitEdit();
  cancelConnect();
  if (cv.dirty) {
    const ok = await flushSave();
    if (!ok) {
      toast('画布保存失败，已留在画布中，请重试保存', 'error');
      return;
    }
  }
  dom.overlay.classList.add('hidden');
  cv.open = false;
  const prev = cv.prevFocus;
  cv.prevFocus = null;
  if (prev && typeof prev.focus === 'function' && document.contains(prev)) {
    try { prev.focus({ preventScroll: true }); } catch { /* 焦点归还失败不影响关闭 */ }
  }
}

/** 契约导出：按当前内存数据重绘。 */
export function renderCanvas() {
  if (!dom) return;
  drawAll();
}

/* ------------------------------------------------------------------ DOM */
function ensureDom() {
  if (dom) return;
  injectStyle();

  const overlay = document.createElement('div');
  overlay.id = 'overlay-canvas';
  overlay.className = 'overlay hidden';
  overlay.innerHTML = `
    <div id="canvas-panel" class="overlay__panel"
         style="width:min(1180px,94vw);height:min(80vh,860px);max-height:none;display:flex;flex-direction:column">
      <div id="canvas-head">
        <span style="font-weight:600">画布</span>
        <select id="canvas-select" title="切换画布"></select>
        <span id="canvas-status" class="settings-muted"></span>
        <span style="margin-left:auto;display:flex;gap:6px;align-items:center">
          <span id="canvas-zoom" class="settings-muted"></span>
          <button id="btn-canvas-add" class="btn btn--mini" type="button" title="在视图中央新建文本卡">文本卡</button>
          <button id="btn-canvas-new" class="btn btn--mini" type="button">新建</button>
          <button id="btn-canvas-fit" class="btn btn--mini" type="button" title="缩放到全部卡片可见">适应</button>
          <button id="btn-canvas-save" class="btn btn--primary btn--mini" type="button" title="立即保存 (Ctrl+S)">保存</button>
          <button id="btn-canvas-close" class="btn btn--ghost" type="button" title="关闭 (Esc)">关闭</button>
        </span>
      </div>
      <div id="canvas-viewport">
        <div id="canvas-world"><svg id="canvas-edges" xmlns="http://www.w3.org/2000/svg"></svg></div>
        <div id="canvas-marquee" class="cv-marquee hidden"></div>
        <div id="canvas-empty" class="cv-empty hidden"></div>
      </div>
      <div class="overlay__hint">
        双击空白新建文本卡 · 拖空白平移 · 滚轮缩放 · Shift+拖拽框选 · 从文件树拖入笔记 · 拖卡片边缘圆点连线 · Delete 删除
      </div>
    </div>`;
  document.body.appendChild(overlay);

  dom = {
    overlay,
    panel: $('#canvas-panel', overlay),
    head: $('#canvas-head', overlay),
    select: $('#canvas-select', overlay),
    status: $('#canvas-status', overlay),
    zoom: $('#canvas-zoom', overlay),
    viewport: $('#canvas-viewport', overlay),
    world: $('#canvas-world', overlay),
    edgesSvg: $('#canvas-edges', overlay),
    marquee: $('#canvas-marquee', overlay),
    empty: $('#canvas-empty', overlay),
  };

  $('#btn-canvas-close', overlay).addEventListener('click', () => { closeCanvas().catch(reportError); });
  $('#btn-canvas-save', overlay).addEventListener('click', () => { saveNow().catch(reportError); });
  $('#btn-canvas-fit', overlay).addEventListener('click', fitView);
  $('#btn-canvas-new', overlay).addEventListener('click', () => { newCanvas().catch(reportError); });
  $('#btn-canvas-add', overlay).addEventListener('click', () => { addTextCardAtCenter(); });
  dom.select.addEventListener('change', () => {
    const path = dom.select.value;
    if (path) openCanvas(path).catch(reportError);
  });

  bindViewport();
  bindKeyboard();
}

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
#canvas-panel .overlay__hint { flex: none; }
#canvas-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 8px 12px;
  border-bottom: 1px solid var(--border-faint); flex: none; }
#canvas-head select { max-width: 220px; min-width: 0; min-height: 26px; padding: 2px 6px; font-size: var(--fs-xs); }
#canvas-viewport { position: relative; flex: 1; overflow: hidden; background: var(--bg-panel);
  cursor: grab; touch-action: none; user-select: none;
  background-image: radial-gradient(var(--border-faint) 1px, transparent 1px);
  background-size: 24px 24px; }
#canvas-viewport.is-panning { cursor: grabbing; }
#canvas-viewport:focus { outline: none; }
#canvas-viewport.is-drop { outline: 2px dashed var(--accent); outline-offset: -6px; }
#canvas-world { position: absolute; left: 0; top: 0; width: 0; height: 0; transform-origin: 0 0; }
#canvas-edges { position: absolute; left: 0; top: 0; width: 1px; height: 1px; overflow: visible;
  pointer-events: none; z-index: 0; }
.cv-edge { stroke: var(--text-dim); stroke-width: 2; fill: none; }
.cv-edge.is-temp { stroke: var(--accent); stroke-dasharray: 6 4; }
.cv-node { position: absolute; box-sizing: border-box; border: 1px solid var(--border);
  border-radius: var(--radius-md); background: var(--bg); color: var(--text);
  font-size: var(--fs-sm); overflow: visible; cursor: move; user-select: none;
  box-shadow: var(--shadow-soft); z-index: 2; }
.cv-node.is-selected { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-faint); }
.cv-node.is-editing { cursor: text; user-select: text; border-color: var(--accent); }
.cv-node__text { padding: 10px 12px; height: 100%; box-sizing: border-box; overflow: hidden;
  border-radius: var(--radius-md); white-space: pre-wrap; word-break: break-word;
  line-height: 1.5; outline: none; }
.cv-node__file { padding: 10px 12px; height: 100%; box-sizing: border-box; overflow: hidden;
  border-radius: var(--radius-md); display: flex; flex-direction: column; gap: 4px; }
.cv-node__file-name { font-weight: 600; word-break: break-all; line-height: 1.4; }
.cv-node__file-kind { font-size: var(--fs-xs); color: var(--text-dim); }
.cv-handle { position: absolute; width: 10px; height: 10px; border-radius: 50%;
  background: var(--accent); border: 1px solid var(--bg); opacity: 0;
  transition: opacity 0.12s ease; z-index: 3; }
.cv-node:hover .cv-handle, .cv-node.is-selected .cv-handle { opacity: 1; }
.cv-handle--n { top: -5px; left: 50%; margin-left: -5px; cursor: crosshair; }
.cv-handle--s { bottom: -5px; left: 50%; margin-left: -5px; cursor: crosshair; }
.cv-handle--w { left: -5px; top: 50%; margin-top: -5px; cursor: crosshair; }
.cv-handle--e { right: -5px; top: 50%; margin-top: -5px; cursor: crosshair; }
.cv-resize { position: absolute; right: 0; bottom: 0; width: 14px; height: 14px;
  cursor: nwse-resize; opacity: 0; z-index: 4;
  background: linear-gradient(135deg, transparent 50%, var(--text-dim) 50%); }
.cv-node:hover .cv-resize, .cv-node.is-selected .cv-resize { opacity: 0.75; }
.cv-marquee { position: absolute; border: 1px dashed var(--accent);
  background: var(--accent-faint); pointer-events: none; z-index: 6; }
.cv-empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  padding: 24px; text-align: center; color: var(--text-dim); font-size: var(--fs-sm); }
`;
  document.head.appendChild(style);
}

/* ------------------------------------------------------------------ 列表 / 载入 / 新建 */
async function refreshList() {
  const data = await api.get('/api/canvas/list');
  cv.list = Array.isArray(data.canvases) ? data.canvases : [];
  const options = ['<option value="">（选择画布）</option>'];
  for (const item of cv.list) {
    options.push(`<option value="${escapeAttr(item.path)}">${escapeHtml(item.name || item.path)}</option>`);
  }
  if (cv.path && !cv.list.some((item) => item.path === cv.path)) {
    options.push(`<option value="${escapeAttr(cv.path)}">${escapeHtml(basename(cv.path))}</option>`);
  }
  dom.select.innerHTML = options.join('');
  dom.select.value = cv.path || '';
}

async function loadCanvas(path) {
  setStatus('载入中…');
  try {
    const result = await api.get('/api/canvas/load', { path });
    cv.path = result.path;
    cv.data = result.data;
    cv.mtime = result.mtime;
    cv.selected.clear();
    cv.editingId = null;
    cv.editingEl = null;
    cv.connecting = null;
    cv.dirty = false;
    localStorage.setItem(LAST_KEY, path);
    hideEmpty();
    drawAll();
    requestAnimationFrame(fitView);
    setStatus('已载入');
    if (dom.select.value !== path) {
      dom.select.insertAdjacentHTML('beforeend',
        `<option value="${escapeAttr(path)}">${escapeHtml(basename(path))}</option>`);
      dom.select.value = path;
    }
  } catch (err) {
    cv.data = null;
    cv.path = null;
    cv.dirty = false;
    cv.selected.clear();
    drawAll();
    showEmpty(`画布载入失败：${err.message || err}`);
    setStatus('载入失败');
    toast(`画布载入失败：${err.message || err}`, 'error');
  }
}

async function newCanvas() {
  const name = await askText({
    title: '新建画布',
    label: '画布文件名（保存在 vault 根目录，自动补 .canvas）',
    value: '画布',
    confirmText: '创建',
  });
  if (!name) return;
  const path = `${sanitizeCanvasName(name)}.canvas`;
  if (cv.list.some((item) => item.path === path)) {
    toast('同名画布已存在，已为你打开', 'info');
    await openCanvas(path);
    return;
  }
  if (cv.editingId) commitEdit();
  if (cv.dirty) {
    const ok = await flushSave();
    if (!ok) return;
  }
  cv.path = path;
  cv.data = { nodes: [], edges: [] };
  cv.selected.clear();
  cv.scale = 1;
  cv.tx = 40;
  cv.ty = 40;
  cv.dirty = false;
  drawAll();
  setStatus('新建');
  await saveNow();
  await refreshList();
  dom.select.value = path;
}

/* ------------------------------------------------------------------ 绘制 */
function drawAll() {
  if (!dom) return;
  for (const el of cv.els.values()) el.remove();
  cv.els.clear();
  dom.edgesSvg.replaceChildren();
  if (!cv.data) {
    if (!dom.empty.textContent) showEmpty('没有打开的画布');
    return;
  }
  hideEmpty();
  for (const node of cv.data.nodes) {
    const el = buildNodeEl(node);
    dom.world.appendChild(el);
    cv.els.set(node.id, el);
  }
  drawEdges();
  refreshSelection();
  applyTransform();
  setStatus(cv.dirty ? '未保存' : '已载入');
}

function buildNodeEl(node) {
  const el = document.createElement('div');
  el.className = `cv-node cv-node--${node.type}`;
  el.dataset.id = node.id;
  el.style.left = `${number(node.x)}px`;
  el.style.top = `${number(node.y)}px`;
  el.style.width = `${number(node.width)}px`;
  el.style.height = `${number(node.height)}px`;

  if (node.type === 'file') {
    const box = document.createElement('div');
    box.className = 'cv-node__file';
    const name = document.createElement('div');
    name.className = 'cv-node__file-name';
    name.textContent = displayFileName(node.file);
    const kind = document.createElement('div');
    kind.className = 'cv-node__file-kind';
    kind.textContent = fileKind(node.file);
    box.appendChild(name);
    box.appendChild(kind);
    el.appendChild(box);
  } else {
    const text = document.createElement('div');
    text.className = 'cv-node__text';
    text.textContent = node.text || '';
    el.appendChild(text);
  }

  for (const side of SIDES) {
    const handle = document.createElement('div');
    handle.className = `cv-handle cv-handle--${side}`;
    handle.dataset.role = 'handle';
    handle.dataset.side = side;
    el.appendChild(handle);
  }
  const resize = document.createElement('div');
  resize.className = 'cv-resize';
  resize.dataset.role = 'resize';
  el.appendChild(resize);

  el.addEventListener('pointerdown', onNodePointerDown);
  el.addEventListener('dblclick', onNodeDblClick);
  return el;
}

function updateNodeEl(node) {
  const el = cv.els.get(node.id);
  if (!el) return;
  el.style.left = `${number(node.x)}px`;
  el.style.top = `${number(node.y)}px`;
  el.style.width = `${number(node.width)}px`;
  el.style.height = `${number(node.height)}px`;
  const textEl = el.querySelector('.cv-node__text');
  if (textEl && cv.editingId !== node.id) textEl.textContent = node.text || '';
}

function drawEdges() {
  if (!dom) return;
  dom.edgesSvg.replaceChildren();
  if (!cv.data) return;
  for (const edge of cv.data.edges) {
    const from = getNode(edge.fromNode);
    const to = getNode(edge.toNode);
    if (!from || !to) continue;
    const fromSide = edge.fromSide || sideToward(from, to);
    const toSide = edge.toSide || sideToward(to, from);
    const a = anchorPoint(from, fromSide);
    const b = anchorPoint(to, toSide);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('class', 'cv-edge');
    line.setAttribute('x1', a.x);
    line.setAttribute('y1', a.y);
    line.setAttribute('x2', b.x);
    line.setAttribute('y2', b.y);
    line.dataset.edgeId = edge.id;
    dom.edgesSvg.appendChild(line);
  }
  if (cv.connecting) {
    const node = getNode(cv.connecting.fromId);
    if (node) {
      const a = anchorPoint(node, cv.connecting.side);
      const cursor = cv.connecting.point || a;
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('class', 'cv-edge is-temp');
      line.setAttribute('x1', a.x);
      line.setAttribute('y1', a.y);
      line.setAttribute('x2', cursor.x);
      line.setAttribute('y2', cursor.y);
      dom.edgesSvg.appendChild(line);
    }
  }
}

function refreshSelection() {
  for (const [id, el] of cv.els) {
    const selected = cv.selected.has(id);
    el.classList.toggle('is-selected', selected);
    el.style.zIndex = selected ? '3' : '2';
  }
}

function applyTransform() {
  if (!dom) return;
  dom.world.style.transform = `translate(${cv.tx}px, ${cv.ty}px) scale(${cv.scale})`;
  dom.zoom.textContent = `${Math.round(cv.scale * 100)}%`;
}

function fitView() {
  if (!dom || !cv.data) return;
  const vw = dom.viewport.clientWidth || 800;
  const vh = dom.viewport.clientHeight || 600;
  const nodes = cv.data.nodes;
  if (!nodes.length) {
    cv.scale = 1;
    cv.tx = 40;
    cv.ty = 40;
    applyTransform();
    return;
  }
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of nodes) {
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x + node.width);
    maxY = Math.max(maxY, node.y + node.height);
  }
  const w = Math.max(1, maxX - minX);
  const h = Math.max(1, maxY - minY);
  const pad = 60;
  cv.scale = clamp(Math.min((vw - pad * 2) / w, (vh - pad * 2) / h, 1.25), MIN_SCALE, MAX_SCALE);
  cv.tx = (vw - w * cv.scale) / 2 - minX * cv.scale;
  cv.ty = (vh - h * cv.scale) / 2 - minY * cv.scale;
  applyTransform();
}

/* ------------------------------------------------------------------ 视口交互 */
function bindViewport() {
  const viewport = dom.viewport;

  viewport.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (e.target.closest && e.target.closest('.cv-node')) return;
    if (e.shiftKey) startMarquee(e);
    else startPan(e);
  });

  viewport.addEventListener('dblclick', (e) => {
    if (e.target.closest && e.target.closest('.cv-node')) return;
    if (!cv.data) return;
    const point = screenToWorld(e.clientX, e.clientY);
    addTextCard(point.x - DEFAULT_W / 2, point.y - 30);
  });

  viewport.addEventListener('wheel', (e) => {
    if (!cv.data) return;
    e.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = Math.exp(-e.deltaY * 0.0015);
    const next = clamp(cv.scale * factor, MIN_SCALE, MAX_SCALE);
    const k = next / cv.scale;
    cv.tx = mx - (mx - cv.tx) * k;
    cv.ty = my - (my - cv.ty) * k;
    cv.scale = next;
    applyTransform();
  }, { passive: false });

  viewport.addEventListener('dragover', (e) => {
    // 画布自己接管拖放：避免把文件拖到面板上时浏览器直接打开文件
    e.preventDefault();
    const types = Array.from((e.dataTransfer && e.dataTransfer.types) || []);
    if (!types.includes('text/kwey-path')) {
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'none';
      return;
    }
    e.dataTransfer.dropEffect = 'copy';
    viewport.classList.add('is-drop');
  });
  viewport.addEventListener('dragleave', (e) => {
    if (!viewport.contains(e.relatedTarget)) viewport.classList.remove('is-drop');
  });
  viewport.addEventListener('drop', (e) => {
    e.preventDefault();
    viewport.classList.remove('is-drop');
    if (!cv.data) return;
    const raw = e.dataTransfer && e.dataTransfer.getData('text/kwey-path');
    if (!raw) return;
    const point = screenToWorld(e.clientX, e.clientY);
    addFileCard(raw.trim(), point.x - DEFAULT_W / 2, point.y - 30);
  });

  // 面板尺寸变化时（窗口缩放/侧栏拖拽）不自动重排，但保证 transform 正确
  window.addEventListener('resize', () => applyTransform());
}

function startPan(e) {
  const start = { x: e.clientX, y: e.clientY, tx: cv.tx, ty: cv.ty };
  let moved = false;
  dom.viewport.classList.add('is-panning');
  const move = (ev) => {
    if (Math.abs(ev.clientX - start.x) + Math.abs(ev.clientY - start.y) > 3) moved = true;
    cv.tx = start.tx + (ev.clientX - start.x);
    cv.ty = start.ty + (ev.clientY - start.y);
    applyTransform();
  };
  const up = () => {
    dom.viewport.classList.remove('is-panning');
    window.removeEventListener('pointermove', move);
    if (!moved && cv.selected.size) {   // 单击空白 = 取消选择
      cv.selected.clear();
      refreshSelection();
    }
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up, { once: true });
}

function startMarquee(e) {
  const rect = dom.viewport.getBoundingClientRect();
  const startWorld = screenToWorld(e.clientX, e.clientY);
  const base = new Set(cv.selected);
  dom.marquee.classList.remove('hidden');
  const move = (ev) => {
    const left = Math.min(e.clientX, ev.clientX) - rect.left;
    const top = Math.min(e.clientY, ev.clientY) - rect.top;
    dom.marquee.style.left = `${left}px`;
    dom.marquee.style.top = `${top}px`;
    dom.marquee.style.width = `${Math.abs(ev.clientX - e.clientX)}px`;
    dom.marquee.style.height = `${Math.abs(ev.clientY - e.clientY)}px`;
  };
  move(e);
  const up = (ev) => {
    dom.marquee.classList.add('hidden');
    window.removeEventListener('pointermove', move);
    const endWorld = screenToWorld(ev.clientX, ev.clientY);
    const box = {
      x1: Math.min(startWorld.x, endWorld.x),
      y1: Math.min(startWorld.y, endWorld.y),
      x2: Math.max(startWorld.x, endWorld.x),
      y2: Math.max(startWorld.y, endWorld.y),
    };
    if (cv.data && (box.x2 - box.x1 > 4 || box.y2 - box.y1 > 4)) {
      for (const node of cv.data.nodes) {
        if (node.x < box.x2 && node.x + node.width > box.x1
          && node.y < box.y2 && node.y + node.height > box.y1) {
          base.add(node.id);
        }
      }
      cv.selected = base;
      refreshSelection();
    }
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up, { once: true });
}

/* ------------------------------------------------------------------ 节点交互 */
function onNodePointerDown(e) {
  const el = e.currentTarget;
  const node = getNode(el.dataset.id);
  if (!node) return;
  const role = e.target && e.target.dataset ? e.target.dataset.role : '';
  if (role === 'handle') {
    e.stopPropagation();
    startConnect(node, e.target.dataset.side, e);
    return;
  }
  if (role === 'resize') {
    e.stopPropagation();
    startResize(node, e);
    return;
  }
  if (e.target && e.target.isContentEditable) return;
  if (e.button !== 0) return;
  e.stopPropagation();

  if (e.shiftKey) {
    if (cv.selected.has(node.id)) cv.selected.delete(node.id);
    else cv.selected.add(node.id);
    refreshSelection();
    return;
  }
  if (!cv.selected.has(node.id)) {
    cv.selected = new Set([node.id]);
    refreshSelection();
  }
  startDrag(node, e);
}

function onNodeDblClick(e) {
  const el = e.currentTarget;
  const node = getNode(el.dataset.id);
  if (!node) return;
  e.stopPropagation();
  if (node.type === 'text') {
    beginEdit(node, el);
    return;
  }
  if (node.type === 'file') openFileTarget(node.file);
}

function startDrag(node, e) {
  const startX = e.clientX;
  const startY = e.clientY;
  const ids = cv.selected.has(node.id) ? Array.from(cv.selected) : [node.id];
  const origins = new Map();
  for (const id of ids) {
    const item = getNode(id);
    if (item) origins.set(id, { x: item.x, y: item.y });
  }
  let moved = false;
  const move = (ev) => {
    const dx = (ev.clientX - startX) / cv.scale;
    const dy = (ev.clientY - startY) / cv.scale;
    if (Math.abs(dx) + Math.abs(dy) > 1) moved = true;
    for (const [id, origin] of origins) {
      const item = getNode(id);
      if (!item) continue;
      item.x = round1(origin.x + dx);
      item.y = round1(origin.y + dy);
      updateNodeEl(item);
    }
    drawEdges();
  };
  const up = () => {
    window.removeEventListener('pointermove', move);
    if (moved) markDirty();
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up, { once: true });
}

function startResize(node, e) {
  const startX = e.clientX;
  const startY = e.clientY;
  const startW = node.width;
  const startH = node.height;
  let moved = false;
  const move = (ev) => {
    const dx = (ev.clientX - startX) / cv.scale;
    const dy = (ev.clientY - startY) / cv.scale;
    if (Math.abs(dx) + Math.abs(dy) > 1) moved = true;
    node.width = round1(Math.max(MIN_W, Math.min(2000, startW + dx)));
    node.height = round1(Math.max(MIN_H, Math.min(2000, startH + dy)));
    updateNodeEl(node);
    drawEdges();
  };
  const up = () => {
    window.removeEventListener('pointermove', move);
    if (moved) markDirty();
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up, { once: true });
}

function startConnect(node, side, e) {
  cv.connecting = { fromId: node.id, side, point: anchorPoint(node, side) };
  drawEdges();
  const move = (ev) => {
    if (!cv.connecting) return;
    cv.connecting.point = screenToWorld(ev.clientX, ev.clientY);
    drawEdges();
  };
  const up = (ev) => {
    window.removeEventListener('pointermove', move);
    const connecting = cv.connecting;
    cv.connecting = null;
    drawEdges();
    if (!connecting) return;
    const targetEl = document.elementFromPoint(ev.clientX, ev.clientY);
    const targetNodeEl = targetEl && targetEl.closest ? targetEl.closest('.cv-node') : null;
    if (!targetNodeEl) return;
    const target = getNode(targetNodeEl.dataset.id);
    const source = getNode(connecting.fromId);
    if (!target || !source || target.id === source.id) return;
    const exists = cv.data.edges.some((edge) => (
      (edge.fromNode === source.id && edge.toNode === target.id)
      || (edge.fromNode === target.id && edge.toNode === source.id)
    ));
    if (exists) {
      toast('这两张卡片已经连线了', 'info', 1600);
      return;
    }
    cv.data.edges.push({
      id: makeId('e'),
      fromNode: source.id,
      toNode: target.id,
      fromSide: connecting.side,
      toSide: sideToward(target, source),
    });
    drawEdges();
    markDirty();
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up, { once: true });
}

function cancelConnect() {
  if (!cv.connecting) return;
  cv.connecting = null;
  drawEdges();
}

function beginEdit(node, el) {
  const textEl = el.querySelector('.cv-node__text');
  if (!textEl) return;
  cv.editingId = node.id;
  cv.editingEl = textEl;
  textEl.contentEditable = 'true';
  el.classList.add('is-editing');
  textEl.focus();
  try {
    const range = document.createRange();
    range.selectNodeContents(textEl);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  } catch { /* 选区失败不影响编辑 */ }
  textEl.addEventListener('blur', commitEdit, { once: true });
  textEl.addEventListener('keydown', onEditKeyDown);
}

function onEditKeyDown(e) {
  if (e.key === 'Escape') {
    e.stopPropagation();
    e.preventDefault();
    if (cv.editingEl) cv.editingEl.blur();
  }
}

function commitEdit() {
  const textEl = cv.editingEl;
  const id = cv.editingId;
  if (!textEl || !id) return;
  const node = getNode(id);
  let text = String(textEl.innerText != null ? textEl.innerText : (textEl.textContent || ''))
    .replace(/\r\n?/g, '\n')
    .replace(/\u00a0/g, ' ')
    .replace(/\n+$/, '');
  if (text.length > MAX_TEXT) text = text.slice(0, MAX_TEXT);
  textEl.removeEventListener('keydown', onEditKeyDown);
  textEl.contentEditable = 'false';
  textEl.textContent = text;
  const el = cv.els.get(id);
  if (el) el.classList.remove('is-editing');
  cv.editingId = null;
  cv.editingEl = null;
  if (node && node.text !== text) {
    node.text = text;
    markDirty();
  }
}

function openFileTarget(file) {
  const lower = String(file || '').toLowerCase();
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) {
    closeCanvas().catch(reportError);
    emit('open-note', file);
    return;
  }
  if (lower.endsWith('.canvas')) {
    openCanvas(file).catch(reportError);
    return;
  }
  toast(`该文件类型暂不支持在编辑器中打开：${file}`, 'info');
}

/* ------------------------------------------------------------------ 新建卡片 */
function addTextCardAtCenter() {
  if (!cv.data) { toast('请先新建或打开一块画布', 'error'); return; }
  const rect = dom.viewport.getBoundingClientRect();
  const point = screenToWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
  addTextCard(point.x - DEFAULT_W / 2, point.y - DEFAULT_H / 2);
}

function addTextCard(x, y) {
  if (!cv.data) return;
  const pos = freePosition(x, y, DEFAULT_W, DEFAULT_H);
  const node = {
    id: makeId('n'),
    type: 'text',
    text: '',
    x: pos.x,
    y: pos.y,
    width: DEFAULT_W,
    height: DEFAULT_H,
  };
  cv.data.nodes.push(node);
  cv.selected = new Set([node.id]);
  drawAll();
  const el = cv.els.get(node.id);
  if (el) beginEdit(node, el);
  markDirty();
}

/** 新卡片与已有卡片重叠时向四周错开一整张卡片，避免把旧卡片整个遮住。 */
function freePosition(x, y, width, height) {
  const overlaps = (px, py) => cv.data.nodes.some((n) => (
    px < n.x + n.width && px + width > n.x
    && py < n.y + n.height && py + height > n.y
  ));
  const stepX = width + 24;
  const stepY = height + 24;
  const candidates = [
    [0, 0],
    [stepX, 0],
    [0, stepY],
    [stepX, stepY],
    [-stepX, 0],
    [0, -stepY],
  ];
  for (const [dx, dy] of candidates) {
    const px = round1(x + dx);
    const py = round1(y + dy);
    if (!overlaps(px, py)) return { x: px, y: py };
  }
  return { x: round1(x + stepX), y: round1(y + stepY) };
}

function addFileCard(file, x, y) {
  if (!cv.data || !file) return;
  const existing = cv.data.nodes.find((node) => node.type === 'file' && node.file === file);
  if (existing) {
    cv.selected = new Set([existing.id]);
    refreshSelection();
    toast('该笔记已在画布中，已选中对应卡片', 'info', 1800);
    return;
  }
  const pos = freePosition(x, y, 220, 100);
  const node = {
    id: makeId('n'),
    type: 'file',
    file,
    x: pos.x,
    y: pos.y,
    width: 220,
    height: 100,
  };
  cv.data.nodes.push(node);
  cv.selected = new Set([node.id]);
  drawAll();
  markDirty();
  autoLinkFileNode(node).catch(() => { /* 自动连线失败不影响主流程 */ });
}

/**
 * 按笔记正文里的 [[双链]] 自动连接画布上已有的 file 卡片（仅 md）。
 * 只扫新增卡片的出链；目标按 basename 匹配，已连线（任一方向）不重复加。
 */
async function autoLinkFileNode(node) {
  if (!/\.(md|markdown)$/i.test(node.file || '')) return;
  const dataAtStart = cv.data;
  let doc;
  try {
    doc = await api.get('/api/note', { path: node.file });
  } catch {
    return;  // 笔记读不到（已删除/非 UTF-8 等）时静默跳过
  }
  if (cv.data !== dataAtStart || !getNode(node.id)) return;  // 期间切换了画布/删了卡片
  const names = linkTargets(doc.content || '');
  if (!names.size) return;
  const selfName = stripMd(basename(node.file)).toLowerCase();
  let added = 0;
  for (const other of cv.data.nodes) {
    if (other.id === node.id || other.type !== 'file') continue;
    const target = stripMd(basename(other.file)).toLowerCase();
    if (!target || target === selfName || !names.has(target)) continue;
    const exists = cv.data.edges.some((edge) => (
      (edge.fromNode === node.id && edge.toNode === other.id)
      || (edge.fromNode === other.id && edge.toNode === node.id)
    ));
    if (exists) continue;
    cv.data.edges.push({
      id: makeId('e'),
      fromNode: node.id,
      toNode: other.id,
      fromSide: sideToward(node, other),
      toSide: sideToward(other, node),
    });
    added += 1;
  }
  if (added) {
    drawEdges();
    markDirty();
    toast(`已按笔记双链自动连接 ${added} 条`, 'success', 1800);
  }
}

/** 从 Markdown 正文提取 [[目标]] / [[目标|别名]] / [[目标#锚点]] 的 basename 集合。 */
function linkTargets(content) {
  const names = new Set();
  const re = /\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]/g;
  let match = re.exec(content);
  while (match) {
    const raw = String(match[1] || '').trim().replace(/\\/g, '/');
    const base = raw.split('/').pop().replace(/\.(md|markdown)$/i, '').toLowerCase();
    if (base) names.add(base);
    match = re.exec(content);
  }
  return names;
}

/* ------------------------------------------------------------------ 删除 */
function deleteSelected() {
  if (!cv.data || !cv.selected.size) return;
  const ids = new Set(cv.selected);
  cv.data.nodes = cv.data.nodes.filter((node) => !ids.has(node.id));
  cv.data.edges = cv.data.edges.filter(
    (edge) => !ids.has(edge.fromNode) && !ids.has(edge.toNode)
  );
  cv.selected.clear();
  drawAll();
  markDirty();
}

/* ------------------------------------------------------------------ 键盘 */
function bindKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (!dom || dom.overlay.classList.contains('hidden')) return;
    const active = document.activeElement;
    const typing = !!(active && (
      active.isContentEditable
      || /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName || '')
    ));
    const mod = e.ctrlKey || e.metaKey;

    if (mod && String(e.key).toLowerCase() === 's') {
      e.preventDefault();
      e.stopPropagation();
      saveNow().catch(reportError);
      return;
    }
    if (e.key === 'Escape') {
      if (cv.connecting) { e.stopPropagation(); cancelConnect(); return; }
      if (typing) return;  // 编辑态 Esc 交给编辑器处理
      e.stopPropagation();
      closeCanvas().catch(reportError);
      return;
    }
    if (typing) return;
    if ((e.key === 'Delete' || e.key === 'Backspace') && cv.selected.size) {
      e.preventDefault();
      e.stopPropagation();
      deleteSelected();
    }
  }, true);
}

/* ------------------------------------------------------------------ 保存 */
function serialize() {
  const data = cv.data || { nodes: [], edges: [] };
  return {
    ...data,
    nodes: (data.nodes || []).map((node) => ({
      ...node,
      x: round1(node.x),
      y: round1(node.y),
      width: round1(node.width),
      height: round1(node.height),
    })),
    edges: (data.edges || []).map((edge) => ({ ...edge })),
  };
}

async function saveNow() {
  saveSoon.cancel();
  if (!cv.path || !cv.data) {
    toast('请先新建或打开一块画布', 'error');
    return false;
  }
  if (cv.saving) {
    cv.pending = true;
    return false;
  }
  cv.saving = true;
  setStatus('保存中…');
  let saved = false;
  try {
    const result = await api.post('/api/canvas/save', { path: cv.path, data: serialize() });
    cv.mtime = result.mtime;
    cv.dirty = false;
    saved = true;
    setStatus(`已保存 ${timeLabel()}`);
  } catch (err) {
    cv.dirty = true;
    setStatus('保存失败');
    toast(`画布保存失败：${err.message || err}`, 'error');
  } finally {
    cv.saving = false;
    if (cv.pending) {
      cv.pending = false;
      saveSoon();
    }
  }
  if (saved) {
    // 列表只是辅助信息：刷新失败不影响保存结果
    refreshList().catch(() => {});
  }
  return saved;
}

async function flushSave() {
  saveSoon.cancel();
  if (!cv.dirty) return true;
  return saveNow();
}

function markDirty() {
  cv.dirty = true;
  setStatus('未保存');
  saveSoon();
}

/* ------------------------------------------------------------------ 辅助 */
function getNode(id) {
  if (!cv.data) return null;
  return cv.data.nodes.find((node) => node.id === id) || null;
}

function screenToWorld(clientX, clientY) {
  const rect = dom.viewport.getBoundingClientRect();
  return {
    x: (clientX - rect.left - cv.tx) / cv.scale,
    y: (clientY - rect.top - cv.ty) / cv.scale,
  };
}

function sideToward(node, target) {
  if (!node || !target) return 'e';
  const dx = (target.x + target.width / 2) - (node.x + node.width / 2);
  const dy = (target.y + target.height / 2) - (node.y + node.height / 2);
  if (Math.abs(dx) >= Math.abs(dy)) return dx >= 0 ? 'e' : 'w';
  return dy >= 0 ? 's' : 'n';
}

function anchorPoint(node, side) {
  const cx = node.x + node.width / 2;
  const cy = node.y + node.height / 2;
  if (side === 'n') return { x: cx, y: node.y };
  if (side === 's') return { x: cx, y: node.y + node.height };
  if (side === 'w') return { x: node.x, y: cy };
  return { x: node.x + node.width, y: cy };
}

function displayFileName(file) {
  const name = basename(file || '');
  return /\.(md|markdown)$/i.test(name) ? stripMd(name) : (name || '未命名');
}

function fileKind(file) {
  const lower = String(file || '').toLowerCase();
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return '笔记';
  if (lower.endsWith('.canvas')) return '画布';
  return '文件';
}

function setStatus(label) {
  if (!dom) return;
  const nodes = cv.data && Array.isArray(cv.data.nodes) ? cv.data.nodes.length : 0;
  const edges = cv.data && Array.isArray(cv.data.edges) ? cv.data.edges.length : 0;
  dom.status.textContent = `${nodes} 卡 · ${edges} 线${label ? ` · ${label}` : ''}`;
}

function showEmpty(message) {
  if (!dom) return;
  dom.empty.textContent = message;
  dom.empty.classList.remove('hidden');
}

function hideEmpty() {
  if (!dom) return;
  dom.empty.classList.add('hidden');
  dom.empty.textContent = '';
}

function sanitizeCanvasName(raw) {
  let name = String(raw || '').trim().replace(/[\\/:*?"<>|]/g, '_').replace(/\s+/g, ' ');
  name = name.replace(/^[. ]+|[. ]+$/g, '');
  if (name.toLowerCase().endsWith('.canvas')) name = name.slice(0, -7);
  if (!name) name = '画布';
  if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(name)) name = `_${name}`;
  return name;
}

function makeId(prefix) {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return `${prefix}-${window.crypto.randomUUID().slice(0, 8)}`;
  }
  return `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

function timeLabel() {
  const now = new Date();
  const pad = (v) => String(v).padStart(2, '0');
  return `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

function number(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function round1(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.round(n * 10) / 10 : 0;
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function escapeAttr(text) {
  return escapeHtml(text);
}
