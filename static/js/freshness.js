/* 知识保鲜（P2-D）：工具栏徽标 + 失效/替代详情 + 一键关闭降权。
 *
 * 只消费 /api/freshness/note 与既有 /api/settings；字段修改仍走 VaultService。
 * 状态语义与后端 app/freshness.py 对齐：unknown 表示未声明，不等同 expired。
 */

import { api } from './api.js';
import { state, emit } from './state.js';
import { $, escapeHtml, toast } from './utils.js';

const STATE_META = {
  current_declared: { label: '当前声明', hint: '声明在当前时点有效；仅代表声明，不等于事实已被验证。' },
  not_yet_effective_declared: { label: '尚未生效', hint: '当前时点早于 valid_from，不能当作现行规则。' },
  expired_declared: { label: '已过期', hint: '已到达 valid_until；历史记录仍保留，不会被自动删除。' },
  superseded_declared: { label: '已被替代', hint: '存在明确替代关系；旧事实保留，检索会降权并优先补入替代证据。' },
  unknown: { label: '无有效期声明', hint: '没有生命周期字段；unknown 不等于已过期。' },
  invalid_metadata: { label: '元数据有误', hint: '日期非法、起止颠倒或替代目标缺失/成环；请人工修正，不会悄悄降级。' },
};

let initialized = false;
let current = null;   // { path, info }
let lastError = '';

export function initFreshness() {
  if (initialized) return;
  initialized = true;

  const badge = $('#freshness-badge');
  if (badge) badge.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
  const pop = $('#freshness-pop');
  if (pop) {
    pop.addEventListener('click', (e) => e.stopPropagation());
    $('#freshness-close')?.addEventListener('click', () => close());
  }
  document.addEventListener('pointerdown', (e) => {
    if (!isOpen()) return;
    if (e.target.closest('#freshness-pop') || e.target.closest('#freshness-badge')) return;
    close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isOpen()) close();
  });
}

function isOpen() {
  const pop = $('#freshness-pop');
  return !!pop && !pop.classList.contains('hidden');
}

function close() {
  $('#freshness-pop')?.classList.add('hidden');
  $('#freshness-badge')?.setAttribute('aria-expanded', 'false');
}

function toggle() {
  if (!current) return;
  if (isOpen()) { close(); return; }
  render();
  const pop = $('#freshness-pop');
  const badge = $('#freshness-badge');
  if (!pop || !badge) return;
  pop.classList.remove('hidden');
  badge.setAttribute('aria-expanded', 'true');
  placePopover(pop, badge);
}

function placePopover(pop, badge) {
  const rect = badge.getBoundingClientRect();
  const box = pop.getBoundingClientRect();
  const pad = 8;
  let left = Math.max(pad, Math.min(rect.left, window.innerWidth - box.width - pad));
  let top = rect.bottom + 6;
  if (top + box.height > window.innerHeight - pad) top = rect.top - box.height - 6;
  top = Math.max(pad, Math.min(top, window.innerHeight - box.height - pad));
  pop.style.left = `${Math.round(left)}px`;
  pop.style.top = `${Math.round(top)}px`;
}

/* ------------------------------------------------------------------ 读取 */
export async function refresh(path = state.activePath) {
  if (!initialized) initFreshness();
  if (!path) { clear(); return; }
  if (current && current.path !== path) close();
  try {
    const info = await api.get('/api/freshness/note', { path });
    if (path !== state.activePath) return; // 切走后丢弃迟到响应
    current = { path, info };
    lastError = '';
    renderBadge();
    if (isOpen()) render();
  } catch {
    if (path !== state.activePath) return;
    current = null;
    close();
    renderBadge();
  }
}

export function clear() {
  current = null;
  lastError = '';
  close();
  renderBadge();
}

function renderBadge() {
  const badge = $('#freshness-badge');
  const label = $('#freshness-badge-label');
  if (!badge || !label) return;
  const info = current && current.info;
  if (!info || info.state === 'unknown') {
    badge.classList.add('hidden');
    return;
  }
  const meta = STATE_META[info.state] || STATE_META.unknown;
  badge.classList.remove('hidden');
  badge.dataset.state = info.state;
  label.textContent = meta.label;
  badge.title = info.superseded_by
    ? `替代路径：${info.superseded_by}（点击查看详情）`
    : `${meta.hint}（点击查看详情）`;
}

/* ------------------------------------------------------------------ 详情 */
function render() {
  const body = $('#freshness-body');
  if (!body || !current) return;
  const { path, info } = current;
  const meta = STATE_META[info.state] || STATE_META.unknown;

  const rows = [
    ['状态', escapeHtml(meta.label)],
    ['评估时点', info.as_of ? escapeHtml(formatTime(info.as_of)) : '—'],
    ['生效起点', info.valid_from ? escapeHtml(formatTime(info.valid_from)) : '—'],
    ['到期边界', info.valid_until ? escapeHtml(formatTime(info.valid_until)) : '—'],
    ['替代路径', info.superseded_by ? escapeHtml(info.superseded_by) : '—'],
    ['适用范围', (info.fact_scope || []).length ? escapeHtml(info.fact_scope.join('、')) : '—'],
    ['验证时间', info.verified_at ? escapeHtml(formatTime(info.verified_at)) : '—'],
  ];

  const hints = [{ text: meta.hint, cls: '' }];
  for (const problem of info.problems || []) {
    hints.push({ text: problem, cls: 'prov-hint--warn' });
  }
  if (lastError) hints.push({ text: lastError, cls: 'prov-hint--error' });

  const downweight = info.downweight !== false;
  const actions = [];
  if (info.replacement) {
    actions.push(`<button id="fresh-open-replacement" class="btn btn--mini" type="button">打开替代笔记</button>`);
  }
  actions.push(
    `<button id="fresh-toggle" class="btn btn--mini ${downweight ? 'btn--ghost' : 'btn--primary'}" type="button">${downweight ? '关闭检索降权' : '恢复检索降权'}</button>`,
  );

  body.innerHTML = `
    ${rows.map(([k, v]) => `
      <div class="prov-row">
        <span class="prov-row__k">${k}</span>
        <span class="prov-row__v">${v}</span>
      </div>`).join('')}
    ${hints.map((h) => `<div class="prov-hint ${h.cls}">${escapeHtml(h.text)}</div>`).join('')}
    <div class="prov-actions">${actions.join('')}</div>`;

  $('#fresh-open-replacement')?.addEventListener('click', () => {
    emit('open-note', info.replacement);
    close();
  });
  $('#fresh-toggle')?.addEventListener('click', () => toggleDownweight(!downweight));
}

async function toggleDownweight(next) {
  const btn = $('#fresh-toggle');
  if (btn) btn.disabled = true;
  try {
    await api.put('/api/settings', { freshness: { downweight: next } });
    if (current) current.info = { ...current.info, downweight: next };
    state.settings = state.settings || {};
    state.settings.freshness = { ...(state.settings.freshness || {}), downweight: next };
    toast(next ? '已恢复检索降权' : '已关闭检索降权（仅影响排序）', 'success', 2200);
    render();
  } catch (err) {
    lastError = `切换失败：${err.message || err}`;
    render();
  } finally {
    if (btn) btn.disabled = false;
  }
}

function formatTime(value) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString('zh-CN', { hour12: false });
}
