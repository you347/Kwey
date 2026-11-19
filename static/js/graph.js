/* 图谱：自研力导向布局（全局浮层 + 右栏本地图谱），无第三方依赖。 */

import { api, reportError } from './api.js';
import { state, on, emit } from './state.js';
import { $, escapeHtml, toast } from './utils.js';
import * as shell from './shell.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const DEPTH_KEY = 'kwey-graph-depth';

let initialized = false;
let overlayEl = null;
let globalEl = null;
let localEl = null;
let statsEl = null;
let currentPath = null;  // tab-activated 记录的当前笔记
let localDepth = clampDepth(Number(localStorage.getItem(DEPTH_KEY)) || 1);

function clampDepth(d) {
  return Math.min(3, Math.max(1, Number.isFinite(d) ? d : 1));
}

/** 右栏「图谱」面板是否激活（供 tab / WS 联动判断）。 */
function isLocalActive() {
  return shell.currentPanel('right') === 'graph';
}

/** 全局图谱浮层是否可见。 */
function isGlobalVisible() {
  return !!overlayEl && !overlayEl.classList.contains('hidden');
}

/** 初始化一次：按钮、浮层关闭、事件订阅。 */
export function initGraph() {
  if (initialized) return;
  initialized = true;

  overlayEl = $('#overlay-graph');
  globalEl = $('#graph-global');
  localEl = $('#local-graph');
  statsEl = $('#graph-stats');

  const closeBtn = $('#btn-graph-close');
  if (closeBtn) closeBtn.addEventListener('click', closeGlobalGraph);
  const refreshBtn = $('#btn-graph-refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', () => refreshLocalGraph());

  const depthBtns = Array.from(document.querySelectorAll('#panel-graph .graph-depth__btn'));
  const syncDepthUI = () => {
    for (const btn of depthBtns) {
      btn.classList.toggle('is-active', Number(btn.dataset.depth) === localDepth);
    }
  };
  for (const btn of depthBtns) {
    btn.addEventListener('click', () => {
      const d = clampDepth(Number(btn.dataset.depth));
      if (d === localDepth) return;
      localDepth = d;
      localStorage.setItem(DEPTH_KEY, String(d));
      syncDepthUI();
      refreshLocalGraph();
    });
  }
  syncDepthUI();

  if (overlayEl) {
    overlayEl.addEventListener('mousedown', (e) => {
      if (e.target === overlayEl) closeGlobalGraph();
    });
  }
  // Escape 仅在全局浮层可见时关闭，避免与其它浮层冲突
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isGlobalVisible()) closeGlobalGraph();
  });

  on('tab-activated', (path) => {
    currentPath = path || null;
    if (isLocalActive()) refreshLocalGraph(path);
  });

  on('panel-activated', ({ side, name } = {}) => {
    if (side === 'right' && name === 'graph') refreshLocalGraph();
  });

  const onWs = () => {
    if (isGlobalVisible()) openGlobalGraph();
    if (isLocalActive()) refreshLocalGraph();
  };
  on('ws:links_changed', onWs);
  on('ws:created', onWs);
  on('ws:deleted', onWs);
  on('ws:moved', onWs);

  // 若图谱面板在初始化时已激活（例如从 settings 恢复），先渲染一次
  if (isLocalActive()) refreshLocalGraph();
}

/** 打开全库图谱浮层。 */
export async function openGlobalGraph() {
  if (!initialized) initGraph();
  if (!overlayEl || !globalEl) return;
  overlayEl.classList.remove('hidden');  // 先显示，才能读到容器真实尺寸
  try {
    const data = await api.get('/api/graph/full');
    const w = Math.max(240, globalEl.clientWidth || 240);
    const h = Math.max(240, globalEl.clientHeight || 240);
    renderGraph(globalEl, data, { focusPath: null, width: w, height: h });
    if (statsEl) {
      const n = Array.isArray(data.nodes) ? data.nodes.length : 0;
      const m = Array.isArray(data.edges) ? data.edges.length : 0;
      statsEl.textContent = `${n} 个节点 · ${m} 条连线`;
    }
  } catch (err) {
    reportError(err);
  }
}

/** 关闭全库图谱浮层。 */
export function closeGlobalGraph() {
  if (overlayEl) overlayEl.classList.add('hidden');
}

/** 刷新右栏本地图谱；无笔记时清空并显示提示。 */
export async function refreshLocalGraph(path) {
  if (!initialized) initGraph();
  if (!localEl) return;
  const target = path === undefined ? (state.activePath || currentPath) : path;
  if (!target) {
    localEl.innerHTML = '<div class="graph-empty">打开一篇笔记查看本地图谱</div>';
    return;
  }
  try {
    const data = await api.get('/api/graph/local', { path: target, depth: localDepth });
    const w = Math.max(240, localEl.clientWidth || 240);
    const h = Math.max(240, localEl.clientHeight || 240);
    renderGraph(localEl, data, { focusPath: target, width: w, height: h });
  } catch (err) {
    localEl.innerHTML = `<div class="graph-empty">${escapeHtml(err.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ 力导向渲染 */

/**
 * 通用渲染：一次性算完力导向布局再绘制原生 SVG。
 * 容器 resize 不自动重绘（避免频繁重排），但每次刷新都会重新读取尺寸。
 */
function renderGraph(container, data, { focusPath = null, width = 0, height = 0 } = {}) {
  if (!container) return;
  const W = Math.max(240, width || container.clientWidth || 240);
  const H = Math.max(240, height || container.clientHeight || 240);
  const nodes = (data && Array.isArray(data.nodes)) ? data.nodes : [];
  const edges = (data && Array.isArray(data.edges)) ? data.edges : [];

  container.innerHTML = '';
  if (!nodes.length) {
    const empty = document.createElement('div');
    empty.className = 'graph-empty';
    empty.textContent = '暂无图谱数据';
    container.appendChild(empty);
    return;
  }

  const cx = W / 2;
  const cy = H / 2;
  const PADDING = 26;
  const BASE_R = 16;
  const FOCUS_R = 22;

  // 圆周初始分布 + 基于 index 的确定性抖动（不用 Math.random，刷新不乱跳）
  const spread = Math.min(W, H) * 0.36;
  const list = nodes.map((node, i) => {
    const angle = (i / nodes.length) * Math.PI * 2;
    const jitter = pseudoRandom(i);
    const radius = spread * (0.72 + jitter * 0.4);
    const isFocus = !!focusPath && node.path === focusPath;
    return {
      path: node.path,
      title: node.title == null ? node.path : node.title,
      x: cx + Math.cos(angle) * radius + (pseudoRandom(i + 97) - 0.5) * 20,
      y: cy + Math.sin(angle) * radius + (pseudoRandom(i + 193) - 0.5) * 20,
      vx: 0,
      vy: 0,
      r: isFocus ? FOCUS_R : BASE_R,
      isFocus,
      el: null,
      circle: null,
      text: null,
    };
  });

  // edges 的端点可能不在 nodes（附件），跳过不存在的端点与自环
  const byPath = new Map(list.map((n) => [n.path, n]));
  const links = [];
  for (const edge of edges) {
    const a = byPath.get(edge.source);
    const b = byPath.get(edge.target);
    if (!a || !b || a === b) continue;
    links.push({ a, b, broken: false, el: null });
  }

  // unresolved（断链）→ 虚线幽灵节点：同名 target 合并为一个，初始位置挂在源节点旁
  const unresolved = (data && Array.isArray(data.unresolved)) ? data.unresolved : [];
  const ghostByTarget = new Map();
  for (const u of unresolved) {
    const src = byPath.get(u.source);
    if (!src || !u.target) continue;
    let ghost = ghostByTarget.get(u.target);
    if (!ghost) {
      const gi = list.length;
      ghost = {
        path: null,
        target: u.target,
        title: u.target,
        x: src.x + (pseudoRandom(gi + 311) - 0.5) * 60,
        y: src.y + (pseudoRandom(gi + 419) - 0.5) * 60,
        vx: 0,
        vy: 0,
        r: BASE_R,
        isFocus: false,
        broken: true,
        el: null,
        circle: null,
        text: null,
      };
      ghostByTarget.set(u.target, ghost);
      list.push(ghost);
    }
    links.push({ a: src, b: ghost, broken: true, el: null });
  }

  simulate(list, links, cx, cy, W, H, PADDING);

  // ---- 绘制：边在下、节点在上；颜色全部交给 CSS 类，不硬编码 ----
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  for (const link of links) {
    const line = document.createElementNS(SVG_NS, 'line');
    line.setAttribute('class', link.broken ? 'graph-link graph-link--broken' : 'graph-link');
    if (!link.broken && focusPath && (link.a.path === focusPath || link.b.path === focusPath)) {
      line.classList.add('graph-link--focus');
    }
    setLine(line, link);
    svg.appendChild(line);
    link.el = line;
  }

  for (const n of list) {
    const g = document.createElementNS(SVG_NS, 'g');
    let cls = 'graph-node';
    if (n.broken) cls += ' graph-node--broken';
    else if (n.isFocus) cls += ' graph-node--focus';
    g.setAttribute('class', cls);
    if (n.broken) g.setAttribute('data-target', n.target);
    else g.setAttribute('data-path', n.path);

    const tip = document.createElementNS(SVG_NS, 'title');
    tip.textContent = n.broken ? `断链：${n.target}` : `${n.title}\n${n.path}`;
    g.appendChild(tip);

    const circle = document.createElementNS(SVG_NS, 'circle');
    circle.setAttribute('r', n.r);
    circle.setAttribute('cx', n.x);
    circle.setAttribute('cy', n.y);

    const text = document.createElementNS(SVG_NS, 'text');
    text.setAttribute('x', n.x);
    text.setAttribute('y', n.y + n.r + 13);
    text.textContent = truncate(n.title, 12);

    g.appendChild(circle);
    g.appendChild(text);
    svg.appendChild(g);
    n.el = g;
    n.circle = circle;
    n.text = text;
    attachDrag(n, links, svg, W, H, PADDING);
  }

  container.appendChild(svg);
}

/**
 * 力导向迭代（约 180 步）：① 节点间斥力 ② 有边节点间弹簧引力
 * ③ 向中心的弱向心力 ④ 速度衰减 + 边界约束。一次性算完，无逐帧动画。
 */
function simulate(list, links, cx, cy, W, H, padding) {
  const STEPS = 180;
  const REPULSION = 2200;
  const SPRING_LEN = 74;
  const SPRING_K = 0.035;
  const CENTER_K = 0.012;
  const DAMPING = 0.82;
  const MAX_SPEED = 14;
  const N = list.length;

  for (let step = 0; step < STEPS; step++) {
    // ① 节点间斥力（O(n²)，节点 ≤500 时可接受）
    for (let i = 0; i < N; i++) {
      const a = list[i];
      for (let j = i + 1; j < N; j++) {
        const b = list[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) {
          // 完全重合时用确定性微扰拆开，避免除零
          dx = pseudoRandom(i * 7 + j) - 0.5;
          dy = pseudoRandom(i + j * 13) - 0.5;
          d2 = dx * dx + dy * dy + 0.01;
        }
        const d = Math.sqrt(d2);
        const f = REPULSION / d2;
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      }
    }
    // ② 有边节点间的弹簧引力
    for (const link of links) {
      const dx = link.b.x - link.a.x;
      const dy = link.b.y - link.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.001;
      const f = (d - SPRING_LEN) * SPRING_K;
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      link.a.vx += fx; link.a.vy += fy;
      link.b.vx -= fx; link.b.vy -= fy;
    }
    // ③ 向中心的弱向心力
    for (const n of list) {
      n.vx += (cx - n.x) * CENTER_K;
      n.vy += (cy - n.y) * CENTER_K;
    }
    // ④ 速度衰减 + 积分 + 边界约束
    for (const n of list) {
      n.vx *= DAMPING;
      n.vy *= DAMPING;
      const speed = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
      if (speed > MAX_SPEED) {
        n.vx = (n.vx / speed) * MAX_SPEED;
        n.vy = (n.vy / speed) * MAX_SPEED;
      }
      n.x = clamp(n.x + n.vx, padding + n.r, W - padding - n.r);
      n.y = clamp(n.y + n.vy, padding + n.r, H - padding - n.r);
    }
  }
}

function setLine(line, link) {
  line.setAttribute('x1', link.a.x);
  line.setAttribute('y1', link.a.y);
  line.setAttribute('x2', link.b.x);
  line.setAttribute('y2', link.b.y);
}

/** 拖动时只更新该节点 circle/text 与相关 line 的坐标，不重算力导向。 */
function updateNodePosition(n, links) {
  n.circle.setAttribute('cx', n.x);
  n.circle.setAttribute('cy', n.y);
  n.text.setAttribute('x', n.x);
  n.text.setAttribute('y', n.y + n.r + 13);
  for (const link of links) {
    if (link.a === n || link.b === n) setLine(link.el, link);
  }
}

/** 节点交互：点击 emit('open-note')（断链节点改为 toast 提示）；拖拽只平移该节点。 */
function attachDrag(n, links, svg, W, H, padding) {
  let dragging = false;
  let moved = false;
  let startX = 0;
  let startY = 0;
  let originX = 0;
  let originY = 0;
  let scaleX = 1;
  let scaleY = 1;

  n.el.addEventListener('pointerdown', (e) => {
    if (e.button != null && e.button !== 0) return;
    const rect = svg.getBoundingClientRect();
    scaleX = rect.width ? W / rect.width : 1;  // 屏幕坐标 → viewBox 坐标
    scaleY = rect.height ? H / rect.height : 1;
    dragging = true;
    moved = false;
    startX = e.clientX;
    startY = e.clientY;
    originX = n.x;
    originY = n.y;
    try { n.el.setPointerCapture(e.pointerId); } catch { /* 某些 WebView 不支持 */ }
    e.preventDefault();
  });

  n.el.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
    n.x = clamp(originX + dx * scaleX, padding + n.r, W - padding - n.r);
    n.y = clamp(originY + dy * scaleY, padding + n.r, H - padding - n.r);
    updateNodePosition(n, links);
  });

  const finish = (e) => {
    if (!dragging) return;
    dragging = false;
    try { n.el.releasePointerCapture(e.pointerId); } catch { /* 忽略 */ }
    if (!moved) {
      if (n.broken) toast(`断链：${n.target}`);
      else emit('open-note', n.path);  // 未拖动则视为点击
    }
  };
  n.el.addEventListener('pointerup', finish);
  n.el.addEventListener('pointercancel', finish);
}

/** 基于种子的确定性伪随机 [0,1)，替代 Math.random 以免每次刷新位置乱跳。 */
function pseudoRandom(seed) {
  const v = Math.sin(seed * 12.9898 + 78.233) * 43758.5453;
  return v - Math.floor(v);
}

function clamp(v, lo, hi) {
  if (lo > hi) return (lo + hi) / 2;
  return Math.max(lo, Math.min(hi, v));
}

function truncate(text, max) {
  const s = String(text == null ? '' : text);
  return s.length > max ? `${s.slice(0, max)}…` : s;
}
