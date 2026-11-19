/* ============ Kwey 开屏：交互式拉链 + 登录/注册 ============
   设计意图：拉链 = 解压知识库。品牌字标入场后拉头停在起点"呼吸"引导，
   用户按住拉头沿右上→左下对角线拖动，链齿按进度实时分离；
   拖到末端（≥97%）触发揭幕，中途松手整条回弹。
   动效只操作 transform/opacity；prefers-reduced-motion 下整段跳过。
   ================================================================ */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, bindFolderPicker } from './utils.js';

const ZIP_MS = 1700;        // （保留常量：回弹动画基准）
const TOOTH_MS = 280;       // 单颗齿分离过渡时长
const SLIDE_MS = 620;       // 两半遮罩滑出时长
const BRAND_HOLD = 350;     // 字标停留后才允许交互
const TOOTH_PITCH = 30;     // 齿距 px
const OPEN_AT = 0.97;       // 拉到该进度即揭幕
const START_AT = 0.035;     // 拉头初始进度：保证在视口内可抓取
const DRAG_GAIN = 1 / 0.78; // 拖动增益：拖屏宽 78% 的位移即可拉满
const LS_KEY = 'kwey-last-vault';

const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ---------------------------------------------------------------- 拉链构建 */

/** 沿"右上→左下"对角线布置齿排与拉头，返回几何信息。 */
function buildZip() {
  const W = innerWidth;
  const H = innerHeight;
  const len = Math.hypot(W, H) + 90;
  const zip = $('#splash-zip');
  const glow = $('#splash-glow');

  // 容器原点放在右上角，旋转后 +x 轴指向左下角
  const angle = Math.atan2(H, -W); // 屏幕坐标系（y 向下）
  zip.style.left = `${W}px`;
  zip.style.top = '0px';
  zip.style.transform = `rotate(${angle}rad)`;
  zip.style.marginTop = '-38px';   // 中线贴合裂缝

  glow.style.width = `${len}px`;
  glow.style.transform = `rotate(${angle}rad)`;

  const svg = zip.querySelector('svg');
  const defs = `
    <defs>
      <linearGradient id="zip-metal" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#e8edf4"/>
        <stop offset="0.45" stop-color="#9aa6b6"/>
        <stop offset="1" stop-color="#5c6878"/>
      </linearGradient>
    </defs>`;
  const teethTop = [];
  const teethBottom = [];
  const count = Math.ceil(len / TOOTH_PITCH) + 2;
  for (let i = 0; i < count; i += 1) {
    const x = i * TOOTH_PITCH;
    teethTop.push(`<rect data-i="${i}" x="${x}" y="20" width="13" height="9" rx="2.2" fill="url(#zip-metal)"/>`);
    teethBottom.push(`<rect data-i="${i}" x="${x + TOOTH_PITCH / 2}" y="38" width="13" height="9" rx="2.2" fill="url(#zip-metal)"/>`);
  }
  // 拉头 + 加大命中区（透明 rect，桌面/触屏都易抓取）
  const slider = `
    <g id="zip-slider" transform="translate(-34, 23)">
      <rect x="-24" y="-26" width="96" height="84" fill="transparent" style="pointer-events:all;cursor:grab"/>
      <path d="M0 8 L14 0 L34 6 L34 24 L14 30 L0 22 Z" fill="url(#zip-metal)"/>
      <rect x="30" y="12" width="4" height="8" rx="1.5" fill="#3d4654"/>
      <path d="M36 15 h14 a3 3 0 0 1 3 3 v10 a3 3 0 0 1 -3 3 h-8 a3 3 0 0 1 -3 -3 Z"
            fill="none" stroke="#8b97a8" stroke-width="3.4" stroke-linejoin="round"/>
    </g>`;
  svg.innerHTML = `${defs}
    <g id="teeth-top">${teethTop.join('')}</g>
    <g id="teeth-bottom">${teethBottom.join('')}</g>
    ${slider}`;

  return {
    len, count, angle,
    sliderEl: svg.querySelector('#zip-slider'),
    hitEl: svg.querySelector('#zip-slider rect'),
    topTeeth: [...svg.querySelectorAll('#teeth-top rect')],
    bottomTeeth: [...svg.querySelectorAll('#teeth-bottom rect')],
    glowEl: glow,
    tipEl: $('#splash-tip'),
  };
}

/* ---------------------------------------------------------------- 交互编排 */

/** 交互式拉开：按住拉头拖动；齿按进度分离；松手回弹；拉满揭幕。 */
function enableInteractiveZip(zip) {
  const { len, count, angle, sliderEl, hitEl, topTeeth, bottomTeeth, glowEl, tipEl } = zip;
  const dirX = Math.cos(angle);
  const dirY = Math.sin(angle);
  const easing = 'cubic-bezier(0.16, 1, 0.3, 1)';
  let progress = START_AT;
  let dragging = false;
  let originX = 0;
  let originY = 0;
  let dragBase = START_AT;
  let settled = false; // 已揭幕，忽略后续指针事件

  // 拉头定位（START_AT 进度确保在视口内）
  const placeSlider = (p, immediate) => {
    sliderEl.style.transition = immediate ? 'none' : `transform 0.3s ${easing}`;
    sliderEl.style.transform = `translate(${p * len - 34}px, 23px)`;
  };
  placeSlider(START_AT, true);

  // 呼吸引导：拉头微微前探，提示可拖（拖动开始时取消）
  const breathe = sliderEl.animate(
    [
      { transform: `translate(${START_AT * len - 34}px, 23px)` },
      { transform: `translate(${START_AT * len - 22}px, 23px)` },
      { transform: `translate(${START_AT * len - 34}px, 23px)` },
    ],
    { duration: 1600, delay: BRAND_HOLD, iterations: Infinity, easing: 'ease-in-out' },
  );

  // 齿状态切换：CSS transition 负责平滑，每帧只翻转个别齿
  const toothOpen = (el, open, row) => {
    if (open) {
      el.style.opacity = '0.14';
      el.style.transform = row === 'top'
        ? 'translateY(-13px) rotate(-5deg)'
        : 'translateY(13px) rotate(5deg)';
    } else {
      el.style.opacity = '';
      el.style.transform = '';
    }
  };
  const applyTeeth = (p) => {
    const n = Math.round(p * count);
    for (let i = 0; i < count; i += 1) {
      toothOpen(topTeeth[i], i < n, 'top');
      toothOpen(bottomTeeth[i], i < n, 'bottom');
    }
  };
  applyTeeth(START_AT); // 起点处最前几颗齿呈已开状态

  const setProgress = (p) => {
    progress = Math.min(1, Math.max(START_AT * 0.5, p));
    placeSlider(progress, true);
    applyTeeth(progress);
    glowEl.style.opacity = String(0.15 + progress * 0.75);
    if (progress > 0.05 && tipEl) tipEl.classList.add('is-read');
  };

  const onDown = (e) => {
    if (settled || performance.now() < BRAND_HOLD) return;
    dragging = true;
    breathe.cancel();
    hitEl.style.cursor = 'grabbing';
    originX = e.clientX;
    originY = e.clientY;
    dragBase = progress;
    hitEl.setPointerCapture(e.pointerId);
    e.preventDefault();
  };
  const onMove = (e) => {
    if (!dragging || settled) return;
    const proj = (e.clientX - originX) * dirX + (e.clientY - originY) * dirY;
    setProgress(dragBase + (proj / len) * DRAG_GAIN);
    if (progress >= OPEN_AT) settleOpen();
  };
  const onUp = (e) => {
    if (!dragging) return;
    dragging = false;
    try { hitEl.releasePointerCapture(e.pointerId); } catch { /* noop */ }
    if (settled) return;
    // 未拉满 → 回弹：拉头滑回起点，齿复位
    placeSlider(START_AT, false);
    applyTeeth(START_AT);
    glowEl.style.opacity = '';
    progress = START_AT;
    hitEl.style.cursor = 'grab';
  };

  function settleOpen() {
    settled = true;
    dragging = false;
    placeSlider(1, false);
    applyTeeth(1);
    if (tipEl) tipEl.classList.add('is-read');
    // 拉头坠落实场
    sliderEl.animate(
      [
        { transform: `translate(${len - 34}px, 23px)`, opacity: 1 },
        { transform: `translate(${len - 10}px, 62px) rotate(26deg)`, opacity: 0 },
      ],
      { duration: 420, easing, fill: 'forwards' },
    );
    setTimeout(() => finishSplash(splashTarget), 160);
  }

  hitEl.addEventListener('pointerdown', onDown);
  hitEl.addEventListener('pointermove', onMove);
  hitEl.addEventListener('pointerup', onUp);
  hitEl.addEventListener('pointercancel', onUp);
}

/* ---------------------------------------------------------------- 揭幕流程 */

function finishSplash(openTarget) {
  const splash = $('#splash');
  splash.classList.add('is-open');
  setTimeout(() => {
    if (openTarget === 'login') {
      const login = $('#login');
      login.classList.remove('hidden');
      requestAnimationFrame(() => login.classList.add('in'));
      const input = $('#login-vault-path');
      input.value = localStorage.getItem(LS_KEY) || '';
      setTimeout(() => input.focus(), 450);
    }
  }, 140);
  setTimeout(() => splash.remove(), SLIDE_MS + 160);
}

function initSplash(openTarget) {
  const zip = buildZip();
  enableInteractiveZip(zip);
}

/* ---------------------------------------------------------------- 登录/注册 */

async function submitLogin(form) {
  const input = $('#login-vault-path');
  const path = input.value.trim();
  const btn = $('#login-submit');
  const status = $('#login-status');
  if (!path) { status.textContent = '请选择文件夹，或输入绝对路径'; input.focus(); return; }
  await openVault(path, btn, status);
  input.focus();
}

async function submitSignup(form) {
  const input = $('#signup-path');
  const btn = $('#signup-submit');
  const status = $('#login-status');
  const path = input.value.trim();
  if (!path) { status.textContent = '请选择一个文件夹（可在对话框中新建）'; input.focus(); return; }
  await openVault(path, btn, status, '创建中…', '创建并进入');
  input.focus();
}

async function openVault(path, btn, status, loadingText = '正在拉开…', idleText = '') {
  btn.disabled = true;
  if (loadingText) btn.textContent = loadingText;
  status.textContent = '';
  try {
    const info = await api.post('/api/vault/open', { path });
    localStorage.setItem(LS_KEY, path);
    const login = $('#login');
    login.classList.add('is-done');
    setTimeout(() => login.remove(), 500);
    emit('vault-opened', info);
  } catch (err) {
    status.textContent = err && err.message ? err.message : '打开失败';
    btn.disabled = false;
    if (idleText) btn.textContent = idleText;
  }
}

function switchTab(name) {
  const tabs = document.querySelectorAll('#login-tabs .login__tab');
  tabs.forEach((b) => b.classList.toggle('login__tab--active', b.dataset.tab === name));
  for (const pane of ['signin', 'signup']) {
    const el = $(`#tab-${pane}`);
    const show = pane === name;
    el.classList.toggle('hidden', !show);
    if (show) { // 重新触发入场动画
      el.style.animation = 'none';
      void el.offsetHeight;
      el.style.animation = '';
    }
  }
  $('#login-status').textContent = '';
}

function bindLogin() {
  const form = $('#login-form');
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const active = document.querySelector('#login-tabs .login__tab--active');
    if (active && active.dataset.tab === 'signup') submitSignup(form);
    else submitLogin(form);
  });
  document.querySelectorAll('#login-tabs .login__tab').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  // 系统文件夹选择（仅桌面模式 pywebview 桥可用时显示按钮）
  bindFolderPicker('login-pick', 'login-vault-path');
  bindFolderPicker('signup-pick', 'signup-path');
  // 逃生口：跳过登录层，回到原欢迎屏流程
  $('#login-skip').addEventListener('click', () => {
    const login = $('#login');
    login.classList.add('is-done');
    setTimeout(() => login.remove(), 500);
  });
}

/* ---------------------------------------------------------------- 入口 */

let splashTarget = 'login'; // 'login' | 'app'

async function boot() {
  bindLogin();
  if (reduceMotion) {
    // 降级：跳过整段动画，直接揭示目标界面
    $('#splash').remove();
    await revealWithoutSplash();
    return;
  }
  try {
    const health = await api.get('/api/health');
    splashTarget = health.vault ? 'app' : 'login';
  } catch { splashTarget = 'login'; }
  if (splashTarget === 'app') $('#login').remove();
  initSplash(splashTarget);
}

async function revealWithoutSplash() {
  try {
    const health = await api.get('/api/health');
    if (health.vault) { $('#login').remove(); return; }
  } catch { /* 按 login 处理 */ }
  const login = $('#login');
  login.classList.remove('hidden');
  login.classList.add('in');
  const input = $('#login-vault-path');
  input.value = localStorage.getItem(LS_KEY) || '';
  input.focus();
}

boot().catch((err) => {
  // 兜底：开屏任何异常都不能挡住主界面
  document.getElementById('splash')?.remove();
  document.getElementById('login')?.classList.remove('hidden');
  reportError(err);
});
