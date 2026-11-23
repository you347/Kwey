/* 知识体检：六项检查 + 评分 + 安全修复。
 *
 * 「体检」的价值不在打分，而在给出**可执行的下一步**：
 * 每一类问题都能一键处理（空笔记进回收站、断链降级为文本、批量补标签），
 * 或者点开笔记手动处理。
 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, $all, escapeHtml, toast } from './utils.js';
import { registerCommand } from './palette.js';
import { askConfirm, askText } from './dialog.js';

export const COMMAND_ID = 'diagnose.open';
const STYLE_ID = 'diagnose-style';

const ds = { open: false, data: null, loading: false, prevFocus: null, expanded: {} };
let dom = null;

export function initDiagnose() {
  registerCommand({
    id: COMMAND_ID,
    title: '知识体检（孤立 / 断链 / 重复）',
    run: () => { openDiagnose().catch(reportError); },
  });
  $('#btn-diagnose')?.addEventListener('click', () => { openDiagnose().catch(reportError); });
}

export async function openDiagnose() {
  ensureDom();
  ds.open = true;
  ds.prevFocus = document.activeElement;
  dom.overlay.classList.remove('hidden');
  await runScan();
}

function close() {
  ds.open = false;
  dom.overlay.classList.add('hidden');
  try { ds.prevFocus && ds.prevFocus.focus({ preventScroll: true }); } catch { /* 已卸载 */ }
}

async function runScan() {
  if (ds.loading) return;
  ds.loading = true;
  dom.body.innerHTML = '<div class="ds-empty">正在扫描索引…</div>';
  try {
    const data = await api.get('/api/diagnose', { deep: '1' });
    ds.data = data;
    render();
  } catch (err) {
    dom.body.innerHTML = `<div class="ds-empty">扫描失败：${escapeHtml(err.message)}</div>`;
  } finally {
    ds.loading = false;
  }
}

/* ------------------------------------------------------------------ 渲染 */
function render() {
  const data = ds.data;
  if (!data) return;
  const ring = scoreRing(data.score, data.grade);
  const chips = ['orphans', 'broken', 'duplicates', 'empty', 'stale', 'untagged']
    .map((key) => {
      const issue = data.issues.find((i) => i.key === key) || { count: 0, label: key };
      const cls = issue.count ? `ds-chip ds-chip--${issue.severity}` : 'ds-chip ds-chip--ok';
      return `<span class="${cls}">${escapeHtml(issue.label)} <b>${issue.count}</b></span>`;
    }).join('');

  dom.body.innerHTML = `
    <div class="ds-hero">
      ${ring}
      <div class="ds-hero__text">
        <h3>${gradeText(data.grade)}</h3>
        <p>共 ${data.total} 篇笔记 · ${data.tag_total} 个标签
          ${data.deep ? '' : ' · 未做重复检测（快速模式）'}</p>
        <div class="ds-chips">${chips}</div>
      </div>
    </div>
    <div class="ds-issues">${data.issues.map(issueHtml).join('')}</div>`;

  for (const btn of $all('[data-fix]', dom.body)) {
    btn.addEventListener('click', () => doFix(btn.dataset.fix, btn).catch(reportError));
  }
  for (const btn of $all('[data-toggle]', dom.body)) {
    btn.addEventListener('click', () => {
      const key = btn.dataset.toggle;
      ds.expanded[key] = !ds.expanded[key];
      render();
    });
  }
  for (const row of $all('[data-open]', dom.body)) {
    row.addEventListener('click', () => {
      close();
      emit('open-note', row.dataset.open);
    });
  }
}

function gradeText(grade) {
  return { excellent: '库很健康', good: '基本健康', fair: '需要整理', poor: '结构在腐烂' }[grade] || '体检结果';
}

function scoreRing(score, grade) {
  const r = 34;
  const c = 2 * Math.PI * r;
  const filled = (score / 100) * c;
  const color = { excellent: 'var(--ok, #37b26c)', good: 'var(--accent)', fair: '#d9a441', poor: 'var(--danger, #e5484d)' }[grade] || 'var(--accent)';
  return `<svg class="ds-ring" viewBox="0 0 88 88" role="img" aria-label="健康分 ${score}">
    <circle cx="44" cy="44" r="${r}" class="ds-ring__bg"/>
    <circle cx="44" cy="44" r="${r}" class="ds-ring__fg"
      style="stroke:${color};stroke-dasharray:${filled.toFixed(1)} ${(c - filled).toFixed(1)}"/>
    <text x="44" y="49" text-anchor="middle" class="ds-ring__num">${score}</text>
  </svg>`;
}

function issueHtml(issue) {
  const expanded = !!ds.expanded[issue.key];
  const items = expanded ? issue.items : issue.items.slice(0, 6);
  const canFix = issue.fixable && issue.count > 0;
  const fixLabel = { empty: '全部移入回收站', broken: '降级为纯文本', duplicates: '查看重复', untagged: '批量补标签' }[issue.key] || '处理';
  const rows = items.map((item) => `
    <div class="ds-row" data-open="${escapeHtml(item.path)}" title="${escapeHtml(item.path)}">
      <span class="ds-row__title">${escapeHtml(item.title || item.path)}</span>
      <span class="ds-row__detail">${escapeHtml(item.detail || '')}</span>
    </div>`).join('') || '<div class="ds-row ds-row--empty">没有这类问题</div>';
  return `<section class="ds-issue">
    <header class="ds-issue__head">
      <span class="ds-dot ds-dot--${issue.severity}"></span>
      <span class="ds-issue__label">${escapeHtml(issue.label)}</span>
      <span class="ds-issue__count">${issue.count}</span>
      <span class="ds-issue__ratio">${(issue.ratio * 100).toFixed(0)}%</span>
      ${canFix ? `<button class="btn btn--mini" data-fix="${issue.key}" type="button">${fixLabel}</button>` : ''}
      ${issue.items.length > 6 ? `<button class="btn btn--ghost btn--mini" data-toggle="${issue.key}" type="button">${expanded ? '收起' : `全部 ${issue.count}`}</button>` : ''}
    </header>
    <div class="ds-rows">${rows}</div>
  </section>`;
}

/* ------------------------------------------------------------------ 修复 */
async function doFix(key, btn) {
  const issue = ds.data.issues.find((i) => i.key === key);
  if (!issue) return;
  const paths = issue.items.map((item) => item.path);

  if (key === 'duplicates') {
    toast('重复笔记建议手动合并：点条目打开对照', 'info', 4000);
    return;
  }
  if (key === 'untagged') {
    const tag = await askText({
      title: '批量补标签',
      label: `给这 ${Math.min(paths.length, 50)} 篇无标签笔记加一个标签`,
      placeholder: '例如 待整理',
      confirmText: '写入',
    });
    if (!tag) return;
    return applyFix({ action: 'tag', paths: paths.slice(0, 50), tag: tag.replace(/^#/, '') }, btn);
  }
  const messages = {
    empty: `将 ${paths.length} 篇空笔记移入回收站？可从回收站还原。`,
    broken: `把 ${paths.length} 篇笔记里的断链降级为纯文本？链接语法会消失，文字保留。`,
  };
  const ok = await askConfirm({ title: '确认修复', message: messages[key] || '确认执行？', confirmText: '执行' });
  if (!ok) return;
  return applyFix({ action: key === 'empty' ? 'trash' : 'strip_broken', paths: paths.slice(0, 200) }, btn);
}

async function applyFix(body, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await api.post('/api/diagnose/fix', body);
    toast(`已处理 ${res.fixed} 篇${res.errors.length ? `（${res.errors.length} 篇失败）` : ''}`, 'success');
    emit('tree-refresh');
    await runScan();
  } catch (err) {
    toast(`修复失败：${err.message}`, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ DOM */
function ensureDom() {
  if (dom) return;
  injectStyle();
  const overlay = document.createElement('div');
  overlay.id = 'overlay-diagnose';
  overlay.className = 'overlay hidden';
  overlay.innerHTML = `
    <div id="diagnose-panel" class="overlay__panel" style="width:min(920px,94vw);height:min(82vh,820px);max-height:none;display:flex;flex-direction:column;padding:0;overflow:hidden">
      <div class="ds-head">
        <svg class="icon" aria-hidden="true"><use href="#i-sliders"/></svg>
        <span class="ds-title">知识体检</span>
        <span class="ds-head__actions">
          <button id="btn-diagnose-rescan" class="btn btn--mini" type="button">重新扫描</button>
          <button id="btn-diagnose-close" class="btn btn--ghost btn--mini" type="button">关闭</button>
        </span>
      </div>
      <div id="diagnose-body" class="ds-body"></div>
      <div class="overlay__hint">点条目打开笔记 · 修复只动被选中的笔记（空笔记进回收站，可还原）· Esc 关闭</div>
    </div>`;
  document.body.appendChild(overlay);
  dom = { overlay, body: $('#diagnose-body', overlay) };
  $('#btn-diagnose-close', overlay).addEventListener('click', close);
  $('#btn-diagnose-rescan', overlay).addEventListener('click', () => { runScan().catch(reportError); });
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { close(); e.stopPropagation(); }
  });
}

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
.ds-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  border-bottom: 1px solid var(--border); flex: none; }
.ds-title { font-weight: 600; font-size: var(--fs-md); }
.ds-head__actions { margin-left: auto; display: flex; gap: 6px; }
.ds-body { flex: 1; overflow: auto; padding: 14px 16px 18px; }
.ds-empty { padding: 60px 20px; text-align: center; color: var(--text-dim); }
.ds-hero { display: flex; gap: 18px; align-items: center; padding: 4px 2px 14px; }
.ds-ring { width: 88px; height: 88px; flex: none; }
.ds-ring__bg { fill: none; stroke: var(--border-faint); stroke-width: 8; }
.ds-ring__fg { fill: none; stroke-width: 8; stroke-linecap: round;
  transform: rotate(-90deg); transform-origin: 44px 44px; transition: stroke-dasharray .5s ease; }
.ds-ring__num { fill: var(--text); font-size: 22px; font-weight: 700; }
.ds-hero__text h3 { margin: 0 0 4px; font-size: var(--fs-md); }
.ds-hero__text p { margin: 0 0 8px; color: var(--text-dim); font-size: var(--fs-xs); }
.ds-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.ds-chip { font-size: var(--fs-xs); padding: 2px 8px; border-radius: 999px;
  background: var(--bg-panel); border: 1px solid var(--border-faint); color: var(--text-dim); }
.ds-chip b { color: var(--text); margin-left: 4px; }
.ds-chip--ok { opacity: .55; }
.ds-chip--error { border-color: var(--danger, #e5484d); }
.ds-chip--warn { border-color: #d9a441; }
.ds-issues { display: flex; flex-direction: column; gap: 10px; }
.ds-issue { border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; background: var(--bg-panel); }
.ds-issue__head { display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border-bottom: 1px solid var(--border-faint); font-size: var(--fs-sm); }
.ds-issue__label { font-weight: 600; }
.ds-issue__count { color: var(--text-dim); font-variant-numeric: tabular-nums; }
.ds-issue__ratio { color: var(--text-dim); font-size: var(--fs-xs); margin-right: auto; }
.ds-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-dim); flex: none; }
.ds-dot--error { background: var(--danger, #e5484d); }
.ds-dot--warn { background: #d9a441; }
.ds-dot--info { background: var(--accent); }
.ds-rows { max-height: 260px; overflow: auto; }
.ds-row { display: flex; gap: 10px; align-items: baseline; padding: 6px 10px;
  border-bottom: 1px solid var(--border-faint); cursor: pointer; font-size: var(--fs-xs); }
.ds-row:last-child { border-bottom: 0; }
.ds-row:hover { background: var(--bg-hover, rgba(127,127,127,.07)); }
.ds-row__title { color: var(--text); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; max-width: 46%; }
.ds-row__detail { color: var(--text-dim); margin-left: auto; text-align: right;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ds-row--empty { color: var(--text-dim); cursor: default; justify-content: center; }
`;
  document.head.appendChild(style);
}
