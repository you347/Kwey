/* 写入溯源（B1）：工具栏徽标、来源详情、人工审阅与「未审」扫描。
 *
 * 只消费既有 /api/provenance/*；审阅提交 path + expected_payload_hash，
 * 服务端版本不符时返回 409，前端明确提示「版本已变化」而不是静默重试。 */

import { api } from './api.js';
import { state, emit } from './state.js';
import { $, escapeHtml, toast } from './utils.js';

const STATE_META = {
  unknown: {
    label: '来源未知',
    hint: '没有本机写入基线，可能是旧库笔记或由外部程序创建 / 修改。',
  },
  agent_unreviewed: {
    label: 'Agent 改写 · 未审',
    hint: '这篇包含 Agent 派生内容，当前版本尚未人工确认。',
  },
  agent_reviewed_current: {
    label: '已审 · 当前版本',
    hint: '人工已确认当前内容版本；后续正文或业务属性变化会再次变为未审。',
  },
  human_or_system_current: {
    label: '人工/系统保存',
    hint: '最后写入经 Kwey 写路径，未检测到 Agent 派生标记。',
  },
  external_changed: {
    label: '外部改动',
    hint: '磁盘内容在 Kwey 之外发生变化；审阅前请先重新载入或在本机保存一次。',
  },
};

const KIND_LABEL = {
  human: '人工',
  agent: 'Agent',
  system: '系统',
  unknown: '未知',
};

const SCAN_TTL_MS = 30_000;
const SCAN_LIMIT = 500;
const SCAN_CONCURRENCY = 6;

let initialized = false;
let current = null;      // { path, info }
let lastError = '';
const scanCache = new Map();  // path -> { state, at }

/* ------------------------------------------------------------------ 初始化 */
export function initProvenance() {
  if (initialized) return;
  initialized = true;

  const badge = $('#provenance-badge');
  const pop = $('#provenance-pop');
  if (badge) badge.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
  if (pop) {
    pop.addEventListener('click', (e) => e.stopPropagation());
    const closeBtn = $('#provenance-close');
    if (closeBtn) closeBtn.addEventListener('click', () => close());
  }
  document.addEventListener('pointerdown', (e) => {
    if (!isOpen()) return;
    if (e.target.closest('#provenance-pop') || e.target.closest('#provenance-badge')) return;
    close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isOpen()) {
      close();
      const badgeEl = $('#provenance-badge');
      if (badgeEl) badgeEl.focus();
    }
  });
  window.addEventListener('resize', () => close());
}

function isOpen() {
  const pop = $('#provenance-pop');
  return !!pop && !pop.classList.contains('hidden');
}

function close() {
  const pop = $('#provenance-pop');
  const badge = $('#provenance-badge');
  if (pop) pop.classList.add('hidden');
  if (badge) badge.setAttribute('aria-expanded', 'false');
}

function toggle() {
  if (!current) return;
  if (isOpen()) { close(); return; }
  lastError = '';
  render();
  const pop = $('#provenance-pop');
  const badge = $('#provenance-badge');
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

/* ------------------------------------------------------------------ 读当前笔记 */
export async function refresh(path = state.activePath) {
  if (!initialized) initProvenance();
  if (!path) { clear(); return; }
  if (current && current.path !== path) close();
  try {
    const info = await api.get('/api/provenance/note', { path });
    if (path !== state.activePath) return;  // 切走后的迟到响应丢弃
    scanCache.delete(path);
    current = { path, info };
    lastError = '';
    if (isOpen()) render();
    renderBadge();
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

/** 写盘 / 外部事件后让「未审」扫描缓存失效（当前笔记状态由 refresh 负责）。 */
export function invalidate(path) {
  if (path) scanCache.delete(path);
  else scanCache.clear();
}

function renderBadge() {
  const badge = $('#provenance-badge');
  const label = $('#provenance-badge-label');
  if (!badge || !label) return;
  if (!current || !current.info) {
    badge.classList.add('hidden');
    return;
  }
  const meta = STATE_META[current.info.state] || STATE_META.unknown;
  badge.classList.remove('hidden');
  badge.dataset.state = current.info.state || 'unknown';
  label.textContent = meta.label;
  badge.setAttribute('aria-label', `写入来源：${meta.label}（点击查看详情）`);
  badge.title = '';
}

/* ------------------------------------------------------------------ 详情与审阅 */
function render() {
  const body = $('#provenance-body');
  if (!body || !current) return;
  const { info } = current;
  const baseline = info.baseline || null;
  const declared = info.declared || {};
  const meta = STATE_META[info.state] || STATE_META.unknown;
  const verified = !!baseline;

  const writer = verified ? baseline.last_writer : declared.last_writer;
  const kind = verified ? baseline.writer_kind : declared.writer_kind;
  const runId = verified ? baseline.run_id : declared.run_id;
  const writtenAt = verified ? baseline.recorded_at : '';
  const reviewedAt = verified ? baseline.reviewed_at : declared.reviewed_at;

  const rows = [
    ['状态', meta.label],
    ['最后写入者', writer ? `${escapeHtml(writer)}${kind ? `（${escapeHtml(KIND_LABEL[kind] || kind)}）` : ''}` : '—'],
    ['来源 run', runId ? escapeHtml(runId) : '—'],
    ['写入时间', writtenAt ? escapeHtml(formatTime(writtenAt)) : '—'],
  ];
  if (reviewedAt) rows.push(['审阅时间', escapeHtml(formatTime(reviewedAt))]);

  const hints = [];
  if (info.claim_unverified) {
    hints.push({ text: 'frontmatter 中的 kwey_ 声明没有本机基线核对，只能视为「未校验声明」。', cls: 'prov-hint--warn' });
  }
  hints.push({ text: meta.hint, cls: '' });

  if (info.state === 'agent_unreviewed') {
    if (state.dirty) {
      hints.push({ text: '当前有未保存修改；请先保存，再审阅磁盘上的当前版本。', cls: 'prov-hint--warn' });
    }
  } else if (info.state === 'external_changed') {
    hints.push({ text: '重新载入或在本机保存一次后，再对当前版本做审阅。', cls: 'prov-hint--warn' });
  } else if (info.state === 'unknown' && !info.claim_unverified) {
    hints.push({ text: '本机没有写入记录，无法审阅。', cls: '' });
  }
  if (lastError) hints.push({ text: lastError, cls: 'prov-hint--error' });

  const canReview = info.state === 'agent_unreviewed' && !state.dirty;
  const action = info.state === 'agent_unreviewed'
    ? `<button id="provenance-review" class="btn btn--mini btn--primary" type="button"${canReview ? '' : ' disabled'}>标记为已审当前版本</button>`
    : '';

  body.innerHTML = `
    ${rows.map(([k, v]) => `
      <div class="prov-row">
        <span class="prov-row__k">${k}</span>
        <span class="prov-row__v">${v}</span>
      </div>`).join('')}
    ${hints.map((h) => `<div class="prov-hint ${h.cls}">${escapeHtml(h.text)}</div>`).join('')}
    ${action ? `<div class="prov-actions">${action}</div>` : ''}`;

  const btn = $('#provenance-review');
  if (btn) btn.addEventListener('click', () => review());
}

async function review() {
  if (!current) return;
  const { path, info } = current;
  const btn = $('#provenance-review');
  if (btn) btn.disabled = true;
  lastError = '';
  try {
    const updated = await api.post('/api/provenance/review', {
      path,
      expected_payload_hash: info.current_payload_hash,
    });
    current = { path, info: updated };
    lastError = '';
    // 审阅只改管理字段，但服务端已写盘；同步 frontmatter/mtime，避免下次保存把审阅字段覆盖掉。
    try {
      const doc = await api.get('/api/note', { path });
      if (state.doc && state.doc.path === path) {
        state.doc.frontmatter = doc.frontmatter;
        state.doc.mtime = doc.mtime;
      }
    } catch { /* 同步失败不影响审阅结果展示 */ }
    scanCache.delete(path);
    emit('provenance-reviewed', { path });
    emit('provenance-updated', { path });
    render();
    renderBadge();
    toast('已标记为已审当前版本', 'success', 1800);
  } catch (err) {
    if (err.code === 409) {
      const message = '版本已变化：当前内容在本机审阅前被修改，请刷新后重新确认。';
      toast(message, 'error', 4200);
      await refresh(path);  // 先刷新到最新版本，再把冲突提示留在详情里
      lastError = message;
    } else {
      lastError = `审阅失败：${err.message}`;
      toast(lastError, 'error', 3000);
    }
    render();
  }
}

function formatTime(value) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString('zh-CN', { hour12: false });
}

/* ------------------------------------------------------------------ 未审扫描 */
/** 批量读取给定笔记的派生状态；只用于「未审」筛选，按 30s TTL 缓存。 */
export async function scanUnreviewed(paths, { force = false, limit = SCAN_LIMIT } = {}) {
  const all = Array.from(new Set((paths || []).filter((p) => /\.md$/i.test(p))));
  const selected = all.slice(0, limit);
  const now = Date.now();
  const unreviewed = new Set();
  const queue = [];

  for (const path of selected) {
    const hit = scanCache.get(path);
    if (!force && hit && now - hit.at < SCAN_TTL_MS) {
      if (hit.state === 'agent_unreviewed') unreviewed.add(path);
    } else {
      queue.push(path);
    }
  }

  let index = 0;
  const worker = async () => {
    while (index < queue.length) {
      const path = queue[index];
      index += 1;
      try {
        const info = await api.get('/api/provenance/note', { path });
        scanCache.set(path, { state: info.state, at: Date.now() });
        if (info.state === 'agent_unreviewed') unreviewed.add(path);
      } catch {
        scanCache.set(path, { state: 'unknown', at: Date.now() });
      }
    }
  };
  const workers = Array.from(
    { length: Math.min(SCAN_CONCURRENCY, Math.max(1, queue.length)) },
    worker,
  );
  await Promise.all(workers);

  return {
    paths: unreviewed,
    scanned: selected.length,
    total: all.length,
    truncated: all.length > selected.length,
  };
}
