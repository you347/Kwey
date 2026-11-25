/* 值班 Agent（A）前端：职责 / 待审 / 运行记录。
 *
 * 入口：ribbon 的「值班」按钮与命令面板 duty.open。
 * 结构：复用 inbox overlay 模式；三个页签；pending 徽标只在有实质提案时出现；
 *       紧急暂停按钮常驻面板头部。
 *
 * 后端接口尚未实现（docs/INNOVATION-PLAN.md §4.9），因此所有 HTTP 调用都集中在
 * 下面的 dutyApi 适配层，并按 §4.9 的路径与字段形状编写。对接真实后端时，
 * 只需要改适配层与 normalize* 函数，UI 不需要动。
 *
 * 安全约定：提案正文、diff、来源、错误信息等模型/服务端文本一律通过
 * textContent 写入 DOM；禁止用 innerHTML 渲染这些文本。
 */

import { api } from './api.js';
import { emit } from './state.js';
import { registerCommand } from './palette.js';
import { $, $all, toast } from './utils.js';
import { askText, askConfirm } from './dialog.js';

export const DUTY_COMMAND_ID = 'duty.open';
const DUTY_OVERLAY_ID = 'overlay-duty';
const STYLE_ID = 'duty-style';
const PROPOSAL_PAGE_SIZE = 50;
const RUN_PAGE_SIZE = 30;
const BADGE_POLL_MS = 45000;
const MAX_DIFF_ROWS = 400;

/* =====================================================================
 * 一、适配层：唯一与后端通信的位置
 *
 *  GET  /api/duty                        → definitions / runtime_state / pending_count
 *  PUT  /api/duty/{id}                   → enabled / scope / schedule / limits / config_version
 *  POST /api/duty/{id}/run               → { config_version } → { run_id, status }
 *  GET  /api/duty/runs?limit&cursor      → 脱敏 run 概要
 *  GET  /api/duty/proposals?status&cursor→ 提案摘要
 *  GET  /api/duty/proposals/{id}         → version / diffs / sources / manifest 摘要
 *  POST /api/duty/proposals/{id}/decision→ version / proposal_hash / decision / reason / approval_grant
 *  POST /api/duty/pause                  → { all } 或 { duty_id }
 *
 * 所有响应沿用 {code, data, message} 信封（api.js 已解包 data）。
 * ===================================================================== */
const BASE = '/api/duty';

function str(v) {
  return v == null ? '' : String(v);
}

function num(v) {
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** 时间戳保留数值类型（数值 epoch 不能被 str() 转成字符串，否则 fmtTime 无法识别）。 */
function timeVal(v) {
  return typeof v === 'number' && Number.isFinite(v) ? v : str(v);
}

/** 列表信封兼容数组 / {items} / {proposals} / {runs} 四种常见形态。 */
function normalizeList(raw, key) {
  let items = [];
  if (Array.isArray(raw)) items = raw;
  else if (raw && Array.isArray(raw.items)) items = raw.items;
  else if (raw && key && Array.isArray(raw[key])) items = raw[key];
  return {
    items,
    nextCursor: raw && !Array.isArray(raw) ? (raw.next_cursor ?? raw.cursor ?? null) : null,
    total: raw && !Array.isArray(raw) && raw.total != null ? num(raw.total) : null,
  };
}

function normalizeDuty(d) {
  const schedule = d && d.schedule;
  return {
    id: str(d && d.id),
    recipe: str(d && d.recipe) || 'unknown',
    enabled: !!(d && d.enabled),
    scope: typeof (d && d.scope) === 'string' ? d.scope : str(d && d.scope && d.scope.path),
    schedule: schedule && typeof schedule === 'object'
      ? schedule
      : (typeof schedule === 'string' ? { kind: schedule } : null),
    limits: d && d.limits && typeof d.limits === 'object' ? d.limits : {},
    configVersion: d && d.config_version != null ? d.config_version : (d && d.configVersion != null ? d.configVersion : 0),
  };
}

function normalizeRuntime(r) {
  const raw = r && typeof r === 'object' ? r : {};
  return {
    paused: !!(raw.paused || raw.state === 'paused' || raw.status === 'paused'),
    queueDepth: num(raw.queue_depth ?? (raw.queue && raw.queue.depth) ?? raw.queued),
    activeRunId: str((raw.active_run && raw.active_run.run_id) ?? raw.active_run_id ?? raw.running_run_id),
    generation: raw.vault_generation ?? raw.generation ?? null,
    model: str(raw.model ?? raw.model_name ?? (raw.provider && raw.provider.model)),
    aiReady: raw.ai_ready ?? raw.model_ready ?? null,
    note: str(raw.note),
  };
}

function normalizeOverview(raw) {
  const definitions = Array.isArray(raw && raw.definitions) ? raw.definitions : [];
  return {
    definitions: definitions.map(normalizeDuty),
    runtime: normalizeRuntime(raw && (raw.runtime_state || raw.runtime)),
    pendingCount: raw && raw.pending_count != null ? (num(raw.pending_count) || 0) : null,
  };
}

function normalizeProposalSummary(p) {
  const ops = p && Array.isArray(p.ops) ? p.ops : null;
  const manifest = p && Array.isArray(p.manifest) ? p.manifest : null;
  return {
    id: str(p && (p.proposal_id ?? p.id)),
    runId: str(p && p.run_id),
    dutyId: str(p && p.duty_id),
    recipe: str(p && p.recipe),
    title: str(p && (p.title ?? p.summary)),
    reason: str(p && p.reason),
    version: p && p.version != null ? p.version : null,
    status: str(p && p.status) || 'pending',
    opsCount: num(p && (p.ops_count ?? (ops ? ops.length : null))),
    filesCount: num(p && (p.files_count ?? (manifest ? manifest.length : null))),
    linksAffected: num(p && (p.links_affected ?? p.affected_links ?? p.links_count)),
    createdAt: str(p && p.created_at),
    expiresAt: str(p && p.expires_at),
    hash: str(p && p.proposal_hash),
  };
}

function normalizeDiff(d) {
  return {
    op: str(d && (d.op ?? d.type)),
    action: str(d && d.action),
    path: str(d && d.path),
    target: str(d && (d.target ?? d.new_path ?? d.to)),
    before: d && typeof d.before === 'string' ? d.before : null,
    after: d && typeof d.after === 'string' ? d.after : null,
    linksAffected: num(d && (d.links_affected ?? d.affected_links ?? d.rewritten_links)),
    note: str(d && d.note),
  };
}

function normalizeSource(s) {
  if (typeof s === 'string') return { path: s, title: s, snippet: '', kind: '' };
  return {
    path: str(s && s.path),
    title: str(s && s.title) || str(s && s.path),
    snippet: str(s && (s.snippet ?? s.excerpt)),
    kind: str(s && (s.kind ?? s.role)),
  };
}

function normalizeManifestEntry(m) {
  return {
    path: str(m && m.path),
    exists: m && typeof m.exists === 'boolean' ? m.exists : null,
    role: str(m && m.role),
    sha256: str(m && (m.raw_sha256 ?? m.sha256)),
    mtime: m && m.mtime != null ? m.mtime : null,
  };
}

function normalizeProposalDetail(raw) {
  const base = normalizeProposalSummary(raw);
  return {
    ...base,
    diffs: Array.isArray(raw && raw.diffs) ? raw.diffs.map(normalizeDiff) : [],
    sources: Array.isArray(raw && raw.sources) ? raw.sources.map(normalizeSource) : [],
    manifest: Array.isArray(raw && raw.manifest) ? raw.manifest.map(normalizeManifestEntry) : [],
    ops: Array.isArray(raw && raw.ops) ? raw.ops : [],
    versionChanged: !!(raw && (raw.version_changed || raw.stale)) || base.status === 'stale',
    staleReason: str(raw && (raw.stale_reason ?? raw.version_note)),
    baseVersion: raw && raw.base_version != null ? raw.base_version : null,
    currentVersion: raw && raw.current_version != null ? raw.current_version : null,
    policy: raw && raw.policy && typeof raw.policy === 'object' ? raw.policy : {},
    freshness: raw && raw.freshness && typeof raw.freshness === 'object' ? raw.freshness : {},
  };
}

function normalizeRun(r) {
  return {
    id: str(r && (r.run_id ?? r.id)),
    dutyId: str(r && r.duty_id),
    recipe: str(r && r.recipe),
    status: str(r && r.status) || 'unknown',
    startedAt: timeVal(r && (r.started ?? r.started_at)),
    endedAt: timeVal(r && (r.ended ?? r.ended_at)),
    errorCode: str(r && r.error_code),
    error: str(r && r.error),
    modelUsage: r && r.model_usage != null ? r.model_usage : null,
    opsCount: num(r && r.ops_count),
    proposalId: str(r && r.proposal_id),
    trigger: str(r && r.trigger),
  };
}

function normalizeApplyItem(it) {
  return {
    path: str(it && (it.path ?? it.result_path)),
    op: str(it && it.op),
    status: str(it && it.status) || 'failed',
    error: str(it && it.error),
  };
}

function normalizeDecision(raw) {
  const failed = Array.isArray(raw && raw.failed_items) ? raw.failed_items : (Array.isArray(raw && raw.failed) ? raw.failed : []);
  const uncertain = Array.isArray(raw && raw.uncertain_items) ? raw.uncertain_items : (Array.isArray(raw && raw.uncertain) ? raw.uncertain : []);
  return {
    status: str(raw && raw.status),
    applied: num(raw && raw.applied) || 0,
    total: num(raw && (raw.total ?? raw.ops_count)),
    failedCount: num(raw && raw.failed_count) ?? failed.length,
    uncertainCount: num(raw && raw.uncertain_count) ?? uncertain.length,
    message: str(raw && raw.message),
    items: failed.map(normalizeApplyItem).concat(uncertain.map(normalizeApplyItem)),
  };
}

/** 桌面端一次性审批凭据桥（§4.9）：预览阶段只探测是否存在，不主动索取。 */
function grantBridge() {
  const direct = window.__kweyDutyApproval;
  if (direct && typeof direct.requestGrant === 'function') {
    return (ctx) => direct.requestGrant(ctx);
  }
  const pw = window.pywebview && window.pywebview.api;
  if (pw && typeof pw.duty_approval_grant === 'function') {
    return (ctx) => pw.duty_approval_grant(ctx);
  }
  return null;
}

export const dutyApi = {
  async overview() {
    return normalizeOverview(await api.get(BASE));
  },

  async updateDuty(id, patch) {
    await api.put(`${BASE}/${encodeURIComponent(id)}`, patch);
  },

  async runDuty(id, configVersion) {
    const data = await api.post(`${BASE}/${encodeURIComponent(id)}/run`, { config_version: configVersion });
    return { runId: str(data && data.run_id), status: str(data && data.status) };
  },

  async listRuns({ limit = RUN_PAGE_SIZE, cursor = null } = {}) {
    const raw = await api.get(`${BASE}/runs`, { limit, cursor });
    const list = normalizeList(raw, 'runs');
    return { items: list.items.map(normalizeRun), nextCursor: list.nextCursor, total: list.total };
  },

  async listProposals({ status = null, limit = PROPOSAL_PAGE_SIZE, cursor = null } = {}) {
    const raw = await api.get(`${BASE}/proposals`, { status, limit, cursor });
    const list = normalizeList(raw, 'proposals');
    return { items: list.items.map(normalizeProposalSummary), nextCursor: list.nextCursor, total: list.total };
  },

  async getProposal(id) {
    return normalizeProposalDetail(await api.get(`${BASE}/proposals/${encodeURIComponent(id)}`));
  },

  /**
   * approve 仅对传入的 version / proposal_hash 生效。
   * 若后端最终把 decision 字段定为 approve/reject 布尔，只需改这里。
   */
  async decide(id, { version, proposalHash, decision, reason = '', approvalGrant = null } = {}) {
    const body = {
      version,
      proposal_hash: proposalHash,
      decision,
      reason,
    };
    if (decision === 'approve' && approvalGrant) body.approval_grant = approvalGrant;
    return normalizeDecision(await api.post(`${BASE}/proposals/${encodeURIComponent(id)}/decision`, body));
  },

  async pause({ all = false, dutyId = null } = {}) {
    const body = all ? { all: true } : { duty_id: dutyId };
    await api.post(`${BASE}/pause`, body);
  },

  async requestApprovalGrant({ proposalId, version, proposalHash }) {
    const bridge = grantBridge();
    if (!bridge) return null;
    const res = await bridge({
      proposal_id: proposalId,
      version,
      proposal_hash: proposalHash,
    });
    if (typeof res === 'string') return res || null;
    return str(res && (res.grant ?? res.approval_grant)) || null;
  },
};

/* =====================================================================
 * 二、展示常量与状态
 * ===================================================================== */
const TABS = [
  { id: 'duties', label: '职责' },
  { id: 'proposals', label: '待审' },
  { id: 'runs', label: '运行记录' },
];

const RECIPE_META = {
  inbox_duty: { label: '收件箱值班', desc: '把捕获内容按白名单目录与标签归置（仅补标签 / 移动）' },
  daily_brief: { label: '每日知识简报', desc: '汇总最近一天的变化、关联与明确待办' },
  weekly_health: { label: '每周健康巡检', desc: '列出最重要的体检问题与证据' },
};

const PROPOSAL_STATUS = {
  pending: ['待审', 'duty-chip--pending'],
  applying: ['应用中', 'duty-chip--pending'],
  stale: ['基准已过期', 'duty-chip--warn'],
  partial: ['部分应用', 'duty-chip--warn'],
  uncertain: ['结果不确定', 'duty-chip--bad'],
  interrupted: ['已中断', 'duty-chip--bad'],
  applied: ['已应用', 'duty-chip--ok'],
  rejected: ['已拒绝', 'duty-chip--muted'],
  expired: ['已过期', 'duty-chip--muted'],
};

const RUN_STATUS = {
  queued: ['排队中', 'duty-chip--pending'],
  running: ['运行中', 'duty-chip--pending'],
  no_change: ['无变化', 'duty-chip--muted'],
  awaiting_review: ['待审', 'duty-chip--ok'],
  failed: ['失败', 'duty-chip--bad'],
  budget_exceeded: ['超出预算', 'duty-chip--warn'],
  skipped: ['已跳过', 'duty-chip--muted'],
};

const ACTION_LABEL = {
  create: '新建',
  modify: '修改',
  move: '移动',
  delete: '删除',
  append: '追加',
  tag: '标签',
  link: '双链',
};

const ROLE_LABEL = {
  input: '输入',
  write_target: '写目标',
  link_impact: '链接影响',
};

const dt = {
  open: false,
  tab: 'duties',
  prevFocus: null,
  overview: null,
  pendingCount: null,
  paused: false,
  unavailable: false,
  busy: false,
  pausing: false,
  proposals: [],
  proposalsStatus: 'pending',
  proposalsLoaded: false,
  proposalsCursor: null,
  listVersions: new Map(),
  runs: [],
  runsLoaded: false,
  runsCursor: null,
  /** run.proposalId → 提案当前状态；用于避免把历史 awaiting_review 当成当前待审。 */
  runProposalStatus: new Map(),
  detail: null,
  detailFromId: null,
  result: null,
  conflict: null,
  actionError: null,
  decisionBusy: false,
  loading: { duties: false, proposals: false, runs: false, detail: false },
  errors: {},
};

let dom = null;
let initialized = false;
let pollTimer = null;
let detailSeq = 0;

/* =====================================================================
 * 三、通用 DOM 辅助（所有动态文本走 textContent）
 * ===================================================================== */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function chip(text, cls = 'duty-chip--muted') {
  return el('span', `duty-chip ${cls}`, text);
}

function statusChip(status, map) {
  const entry = map[status];
  return chip(entry ? entry[0] : (status || '未知'), entry ? entry[1] : 'duty-chip--muted');
}

function fact(label, value) {
  const span = el('span', 'duty-fact');
  span.appendChild(el('b', null, label));
  span.appendChild(document.createTextNode(value == null || value === '' ? '—' : String(value)));
  return span;
}

function bannerBlock(text, kind = '') {
  const box = el('div', `duty-banner${kind ? ` duty-banner--${kind}` : ''}`);
  box.appendChild(el('span', 'duty-banner__text', text));
  return box;
}

function emptyState(title, message, actionLabel, onAction) {
  const box = el('div', 'duty-empty');
  box.appendChild(el('h3', null, title));
  if (message) box.appendChild(el('p', null, message));
  if (actionLabel && onAction) {
    const btn = el('button', 'btn btn--mini btn--primary', actionLabel);
    btn.type = 'button';
    btn.addEventListener('click', onAction);
    box.appendChild(btn);
  }
  return box;
}

function appendLoading(text = '读取中…') {
  const box = el('div', 'duty-state');
  box.setAttribute('role', 'status');
  box.appendChild(el('span', 'duty-state__spinner'));
  box.appendChild(el('span', null, text));
  dom.body.appendChild(box);
}

function apiErrState(err) {
  return {
    code: err && typeof err.code === 'number' ? err.code : 0,
    message: (err && err.message) || String(err),
  };
}

function appendErrorState(err) {
  if (err.code === 404) {
    dom.body.appendChild(emptyState(
      '后端尚未就绪',
      '值班 Agent 的服务端接口（INNOVATION-PLAN §4.9）尚未实现。当前面板已按草案完成前端结构；对接后会自动显示职责、提案与运行记录。',
      '重试',
      () => loadTab({ force: true }),
    ));
    return;
  }
  if (err.code === 0) {
    dom.body.appendChild(emptyState(
      '无法连接本地服务',
      `网络请求失败：${err.message}。请确认 Kwey 服务仍在运行，然后重试。`,
      '重试',
      () => loadTab({ force: true }),
    ));
    return;
  }
  if (err.code === 401 || err.code === 403) {
    dom.body.appendChild(emptyState(
      '没有权限',
      `服务拒绝了请求（${err.code}）。值班接口需要本地 token；可在设置页重新签发后重试。`,
      '重试',
      () => loadTab({ force: true }),
    ));
    return;
  }
  dom.body.appendChild(emptyState('加载失败', err.message || '未知错误', '重试', () => loadTab({ force: true })));
}

function appendInlineError(err) {
  const text = err.code === 0
    ? `刷新失败：无法连接本地服务（${err.message}）。当前显示的是上次读取的数据。`
    : `刷新失败（${err.code}）：${err.message}`;
  dom.body.appendChild(bannerBlock(text, 'bad'));
}

function recipeLabel(recipe) {
  return (RECIPE_META[recipe] && RECIPE_META[recipe].label) || recipe || '未知职责';
}

function fmtTime(v) {
  if (v == null || v === '') return '';
  let t = NaN;
  if (typeof v === 'number') {
    t = v < 1e12 ? v * 1000 : v;
  } else {
    const s = String(v).trim();
    // 纯数字字符串：按 epoch 秒 / 毫秒识别（normalize 可能把数值时间戳转成字符串）
    if (/^\d+(?:\.\d+)?$/.test(s)) {
      const n = Number(s);
      if (Number.isFinite(n) && n >= 1e9) t = n < 1e12 ? n * 1000 : n;
    }
    if (!Number.isFinite(t)) t = Date.parse(s);
  }
  if (!Number.isFinite(t)) return String(v);
  const d = new Date(t);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function isExpired(iso) {
  if (!iso) return false;
  const t = Date.parse(iso);
  return Number.isFinite(t) && t < Date.now();
}

function scheduleText(schedule) {
  if (!schedule) return '仅手动';
  if (typeof schedule === 'string') return schedule;
  const kind = str(schedule.kind ?? schedule.mode);
  if (!kind || kind === 'manual') return '仅手动';
  if (kind === 'daily') return `每日 ${schedule.at || schedule.time || '—'}`;
  if (kind === 'weekly') {
    const day = schedule.weekday ?? schedule.day ?? '—';
    return `每周 ${day} ${schedule.at || schedule.time || ''}`.trim();
  }
  return kind;
}

function limitsText(limits) {
  if (!limits || typeof limits !== 'object') return '默认';
  const parts = [];
  if (limits.max_ops != null) parts.push(`≤${limits.max_ops} 项/轮`);
  if (limits.max_files != null) parts.push(`≤${limits.max_files} 文件`);
  if (limits.max_runs_per_hour != null) parts.push(`${limits.max_runs_per_hour} 次/时`);
  if (limits.max_model_calls != null) parts.push(`≤${limits.max_model_calls} 次模型`);
  return parts.length ? parts.join(' · ') : '默认';
}

function usageText(usage) {
  if (usage == null) return '';
  if (typeof usage !== 'object') return str(usage);
  const parts = [];
  if (usage.calls != null) parts.push(`模型 ${usage.calls} 次`);
  if (usage.input_chars != null) parts.push(`输入 ${usage.input_chars} 字符`);
  if (usage.usage_known === false || usage.cost == null) parts.push('费用 unknown');
  return parts.join(' · ');
}

/* =====================================================================
 * 四、diff 计算与安全渲染
 * ===================================================================== */
function splitLines(text) {
  return String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n');
}

/**
 * 前后缀裁剪 + 变更块展示；不做逐字符 LCS（大文件下代价高且无必要）。
 * rows: {type: 'ctx'|'add'|'del', text}
 */
function lineDiff(before, after) {
  const b = before == null ? [] : splitLines(before);
  const a = after == null ? [] : splitLines(after);
  let start = 0;
  while (start < b.length && start < a.length && b[start] === a[start]) start += 1;
  let endB = b.length - 1;
  let endA = a.length - 1;
  while (endB >= start && endA >= start && b[endB] === a[endA]) {
    endB -= 1;
    endA -= 1;
  }

  const removed = Math.max(0, endB - start + 1);
  const added = Math.max(0, endA - start + 1);
  const rows = [];
  const context = 2;

  for (let i = Math.max(0, start - context); i < start; i += 1) rows.push({ type: 'ctx', text: b[i] });

  let emitted = 0;
  for (let i = start; i <= endB && emitted < MAX_DIFF_ROWS; i += 1) {
    rows.push({ type: 'del', text: b[i] });
    emitted += 1;
  }
  for (let i = start; i <= endA && emitted < MAX_DIFF_ROWS; i += 1) {
    rows.push({ type: 'add', text: a[i] });
    emitted += 1;
  }

  for (let i = endB + 1; i < Math.min(b.length, endB + 1 + context); i += 1) {
    rows.push({ type: 'ctx', text: b[i] });
  }

  return { rows, added, removed, truncated: removed + added > MAX_DIFF_ROWS };
}

function diffTable(stats) {
  const box = el('div', 'duty-diff');
  const sign = { add: '+', del: '-', ctx: ' ' };
  for (const row of stats.rows) {
    const line = el('div', `duty-diff__row duty-diff__row--${row.type}`);
    line.appendChild(el('span', 'duty-diff__sign', sign[row.type] || ' '));
    line.appendChild(el('span', 'duty-diff__text', row.text === '' ? ' ' : row.text));
    box.appendChild(line);
  }
  return box;
}

/* =====================================================================
 * 五、初始化与开关
 * ===================================================================== */
export function initDuty() {
  const first = !initialized;
  if (first) {
    initialized = true;
    registerCommand({
      id: DUTY_COMMAND_ID,
      title: '打开值班（职责 / 待审 / 运行记录）',
      run: () => openDuty(),
    });
    $('#btn-duty')?.addEventListener('click', () => openDuty());
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') refreshBadge().catch(() => {});
    });
  } else {
    resetData();
  }
  ensureRibbonBadge();
  refreshBadge().catch(() => {});
  startPolling();
}

export function openDuty() {
  ensureDom();
  if (dt.open) {
    dom.tabs.find((b) => b.dataset.tab === dt.tab)?.focus();
    loadTab({ force: true });
    return;
  }
  dt.open = true;
  dt.prevFocus = document.activeElement;
  dom.overlay.classList.remove('hidden');
  render();
  dom.tabs.find((b) => b.dataset.tab === dt.tab)?.focus();
  loadTab({ force: true });
  refreshBadge().catch(() => {});
  startPolling();
}

export function closeDuty() {
  if (!dt.open) return;
  dt.open = false;
  detailSeq += 1;
  dt.detail = null;
  dt.result = null;
  dt.conflict = null;
  dt.actionError = null;
  if (dom) dom.overlay.classList.add('hidden');
  try {
    if (dt.prevFocus && typeof dt.prevFocus.focus === 'function') dt.prevFocus.focus({ preventScroll: true });
  } catch { /* 目标已卸载 */ }
}

function resetData() {
  detailSeq += 1;
  dt.overview = null;
  dt.pendingCount = null;
  dt.paused = false;
  dt.unavailable = false;
  dt.proposals = [];
  dt.proposalsLoaded = false;
  dt.proposalsCursor = null;
  dt.listVersions = new Map();
  dt.runs = [];
  dt.runsLoaded = false;
  dt.runsCursor = null;
  dt.runProposalStatus = new Map();
  dt.detail = null;
  dt.detailFromId = null;
  dt.result = null;
  dt.conflict = null;
  dt.actionError = null;
  dt.errors = {};
  dt.loading = { duties: false, proposals: false, runs: false, detail: false };
  updateBadges();
  if (dt.open) render();
}

/* =====================================================================
 * 六、徽标与轮询
 * ===================================================================== */
function ensureRibbonBadge() {
  const btn = $('#btn-duty');
  if (!btn) return null;
  btn.classList.add('ribbon__btn--badged');
  let badge = btn.querySelector('.ribbon__badge');
  if (!badge) {
    badge = el('span', 'ribbon__badge hidden');
    badge.setAttribute('aria-hidden', 'true');
    btn.appendChild(badge);
  }
  return badge;
}

function updateBadges() {
  const n = dt.pendingCount;
  const show = typeof n === 'number' && n > 0;
  const text = show ? (n > 99 ? '99+' : String(n)) : '';

  const badge = ensureRibbonBadge();
  if (badge) {
    badge.classList.toggle('hidden', !show);
    badge.textContent = text;
  }

  const btn = $('#btn-duty');
  if (btn) {
    const label = show ? `值班：${n} 个待审提案` : '值班：职责 / 待审 / 运行记录';
    btn.setAttribute('aria-label', label);
    btn.dataset.tip = show ? `值班 · ${n} 个待审` : '值班';
  }

  if (dom && dom.tabCount) {
    dom.tabCount.classList.toggle('hidden', !show);
    dom.tabCount.textContent = text;
  }
}

function setPendingCount(n) {
  dt.pendingCount = n == null ? null : Math.max(0, Math.floor(Number(n) || 0));
  updateBadges();
}

/** 「实质提案」：pending 且至少有一项 ops/文件；未知计数时按后端归入待审为准。 */
function isSubstantive(p) {
  if (!p || p.status !== 'pending') return false;
  if (p.opsCount == null && p.filesCount == null) return true;
  return (p.opsCount || 0) > 0 || (p.filesCount || 0) > 0;
}

async function refreshBadge() {
  try {
    const ov = await dutyApi.overview();
    dt.overview = ov;
    dt.unavailable = false;
    dt.paused = !!ov.runtime.paused;
    if (ov.pendingCount != null) {
      setPendingCount(ov.pendingCount);
    } else {
      await syncCountFromProposals();
    }
  } catch (err) {
    if (err && err.code === 404) {
      dt.unavailable = true;
      setPendingCount(0);
      stopPolling();
      if (dt.open) render();
    }
  }
}

async function syncCountFromProposals() {
  const res = await dutyApi.listProposals({ status: 'pending', limit: PROPOSAL_PAGE_SIZE });
  const count = res.total != null ? res.total : res.items.filter(isSubstantive).length;
  setPendingCount(count);
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') refreshBadge().catch(() => {});
  }, BADGE_POLL_MS);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

/* =====================================================================
 * 七、页签与主渲染
 * ===================================================================== */
function render() {
  if (!dom) return;
  updateTabAttrs();
  updateBadges();
  renderBanner();
  updateHeadStatus();
  renderBody();
  renderFoot();
  updatePauseButton();
}

function updateTabAttrs() {
  for (const btn of dom.tabs) {
    const active = btn.dataset.tab === dt.tab;
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
    btn.tabIndex = active ? 0 : -1;
    btn.classList.toggle('duty-tab--active', active);
  }
  dom.body.setAttribute('aria-labelledby', `duty-tab-${dt.tab}`);
}

function updateHeadStatus() {
  let text = '';
  if (dt.detail) {
    text = `提案 v${dt.detail.version == null ? '—' : dt.detail.version} · ${proposalStatusLabel(dt.detail.status)}`;
  } else if (dt.tab === 'duties' && dt.overview) {
    text = `${dt.overview.definitions.length} 个职责 · ${dt.pendingCount == null ? '—' : dt.pendingCount} 个待审`;
  } else if (dt.tab === 'proposals') {
    text = dt.proposalsLoaded ? `${dt.proposals.length} 条` : '';
  } else if (dt.tab === 'runs') {
    text = dt.runsLoaded ? `${dt.runs.length} 条` : '';
  }
  dom.headStatus.textContent = text;
}

function proposalStatusLabel(status) {
  const entry = PROPOSAL_STATUS[status];
  return entry ? entry[0] : (status || '未知');
}

function updatePauseButton() {
  if (!dom) return;
  const disabled = dt.paused || dt.pausing || dt.unavailable;
  dom.pauseBtn.disabled = disabled;
  dom.pauseBtn.textContent = dt.pausing ? '暂停中…' : (dt.paused ? '已暂停' : '紧急暂停');
  dom.pauseBtn.title = dt.paused
    ? '所有职责已暂停；恢复方式以后端运行时为准'
    : '紧急暂停：立即禁止后续调度与应用（网络调用可能到超时才返回）';
}

function renderBanner() {
  dom.banner.textContent = '';
  if (dt.paused) {
    dom.banner.classList.remove('hidden');
    dom.banner.className = 'duty-banner duty-banner--warn';
    dom.banner.appendChild(el('span', 'duty-banner__text',
      '已紧急暂停：所有职责停止调度与写入。已入队任务不会自动应用；恢复方式以后端运行时为准。'));
    return;
  }
  dom.banner.classList.add('hidden');
}

function renderBody() {
  if (!dom) return;
  dom.body.textContent = '';

  if (dt.loading.detail && !dt.detail) {
    appendLoading('读取提案详情…');
    return;
  }
  if (dt.errors.detail && !dt.detail) {
    appendErrorState(dt.errors.detail);
    return;
  }
  if (dt.detail) {
    renderDetail();
    return;
  }

  if (dt.tab === 'duties') {
    if (!dt.overview) {
      if (dt.errors.duties) appendErrorState(dt.errors.duties);
      else appendLoading();
      return;
    }
    if (dt.errors.duties) appendInlineError(dt.errors.duties);
    renderDuties();
    return;
  }

  if (dt.tab === 'proposals') {
    if (!dt.proposalsLoaded) {
      if (dt.errors.proposals) appendErrorState(dt.errors.proposals);
      else appendLoading();
      return;
    }
    if (dt.errors.proposals) appendInlineError(dt.errors.proposals);
    renderProposals();
    return;
  }

  if (!dt.runsLoaded) {
    if (dt.errors.runs) appendErrorState(dt.errors.runs);
    else appendLoading();
    return;
  }
  if (dt.errors.runs) appendInlineError(dt.errors.runs);
  renderRuns();
}

function renderFoot() {
  if (!dom) return;
  if (dt.detail) {
    dom.foot.textContent = 'Esc 返回列表 · Tab 在可操作项间移动 · 批准只对当前版本生效';
    return;
  }
  if (dt.tab === 'duties') {
    dom.foot.textContent = '职责只在应用运行期间检查；所有写入都需要人工审批。';
  } else if (dt.tab === 'proposals') {
    dom.foot.textContent = '↑↓ 选择 · Enter 查看 · Esc 关闭 · 无变化的运行不会形成提案';
  } else {
    dom.foot.textContent = '运行记录为脱敏概要；不展示密钥或完整 prompt。';
  }
}

/* =====================================================================
 * 八、职责页
 * ===================================================================== */
function renderDuties() {
  const ov = dt.overview;
  dom.body.appendChild(runtimeStrip(ov.runtime));
  dom.body.appendChild(el('p', 'duty-hint',
    '职责范围是宿主读取与生成笔记的硬约束，不是给模型的建议。职责仅在 Kwey 运行期间检查；首版所有写入都需要人工批准。'));

  if (!ov.definitions.length) {
    dom.body.appendChild(emptyState(
      '尚无职责定义',
      '服务端会提供三个内置职责（收件箱值班 / 每日知识简报 / 每周健康巡检），默认关闭。未配置生成模型时仍可做规则扫描，但不会伪称 AI 已运行。',
    ));
    return;
  }

  const list = el('div', 'duty-cards');
  for (const def of ov.definitions) list.appendChild(dutyCard(def));
  dom.body.appendChild(list);
}

function runtimeStrip(runtime) {
  const box = el('div', 'duty-runtime');
  box.appendChild(fact('调度', runtime.paused ? '已暂停' : '可调度'));
  box.appendChild(fact('队列', `${runtime.queueDepth == null ? 0 : runtime.queueDepth} 个待运行`));
  const model = runtime.aiReady === false ? '未配置（规则版）' : (runtime.model || '未配置（规则版）');
  box.appendChild(fact('模型', model));
  if (runtime.activeRunId) box.appendChild(fact('当前运行', runtime.activeRunId));
  if (runtime.generation != null) box.appendChild(fact('库生成号', String(runtime.generation)));
  if (runtime.note) box.appendChild(fact('说明', runtime.note));
  return box;
}

function dutyCard(def) {
  const card = el('div', 'duty-card');

  const head = el('div', 'duty-card__head');
  head.appendChild(el('span', 'duty-card__title', recipeLabel(def.recipe)));
  head.appendChild(chip(def.enabled ? '已启用' : '未启用', def.enabled ? 'duty-chip--ok' : 'duty-chip--muted'));
  head.appendChild(el('span', 'duty-card__flex'));

  const runBtn = el('button', 'btn btn--mini', '手动试跑');
  runBtn.type = 'button';
  runBtn.dataset.dutyAction = 'run';
  runBtn.dataset.id = def.id;
  runBtn.title = '异步入队：不等待模型、不写笔记，结果只会进入提案队列';
  runBtn.disabled = dt.unavailable;
  head.appendChild(runBtn);

  const toggle = el('button', `btn btn--mini${def.enabled ? '' : ' btn--primary'}`, def.enabled ? '停用' : '启用');
  toggle.type = 'button';
  toggle.dataset.dutyAction = 'toggle';
  toggle.dataset.id = def.id;
  toggle.disabled = dt.unavailable;
  head.appendChild(toggle);

  card.appendChild(head);

  const meta = RECIPE_META[def.recipe];
  if (meta && meta.desc) card.appendChild(el('div', 'duty-card__desc', meta.desc));

  const facts = el('div', 'duty-facts');
  facts.appendChild(fact('范围', def.scope || '未设置（职责不可运行）'));
  facts.appendChild(fact('时间', scheduleText(def.schedule)));
  facts.appendChild(fact('预算', limitsText(def.limits)));
  facts.appendChild(fact('配置版本', def.configVersion == null ? '—' : `v${def.configVersion}`));
  card.appendChild(facts);

  return card;
}

/* =====================================================================
 * 九、待审列表
 * ===================================================================== */
function renderProposals() {
  const filters = el('div', 'duty-filters');
  filters.setAttribute('role', 'group');
  filters.setAttribute('aria-label', '提案状态筛选');
  const options = [
    { id: 'pending', label: '待审' },
    { id: 'stale', label: '基准过期' },
    { id: 'partial', label: '部分应用' },
    { id: 'all', label: '全部' },
  ];
  for (const opt of options) {
    const btn = el('button', 'btn btn--mini duty-filter', opt.label);
    btn.type = 'button';
    btn.dataset.proposalFilter = opt.id;
    btn.setAttribute('aria-pressed', opt.id === dt.proposalsStatus ? 'true' : 'false');
    filters.appendChild(btn);
  }
  dom.body.appendChild(filters);

  if (!dt.proposals.length) {
    const text = dt.proposalsStatus === 'pending'
      ? '没有待审提案。职责只有在产生实际改动时才会形成提案；无变化不会通知。'
      : '当前筛选下没有提案。';
    dom.body.appendChild(emptyState('队列为空', text));
    return;
  }

  const list = el('div', 'duty-list');
  for (const p of dt.proposals) list.appendChild(proposalRow(p));
  dom.body.appendChild(list);

  if (dt.proposalsCursor) {
    const more = el('button', 'btn btn--mini', '加载更多');
    more.type = 'button';
    more.id = 'duty-proposals-more';
    more.style.marginTop = '4px';
    dom.body.appendChild(more);
  }
}

function proposalRow(p) {
  const row = el('button', 'duty-row');
  row.type = 'button';
  row.dataset.id = p.id;
  row.setAttribute('aria-label', `提案 ${p.id}，版本 ${p.version == null ? '未知' : p.version}，${proposalStatusLabel(p.status)}`);

  const head = el('span', 'duty-row__head');
  head.appendChild(el('span', 'duty-row__title', p.title || p.reason || '(未命名提案)'));
  head.appendChild(statusChip(p.status, PROPOSAL_STATUS));
  row.appendChild(head);

  if (p.reason && p.title) row.appendChild(el('span', 'duty-row__reason', p.reason));

  const meta = el('span', 'duty-row__meta');
  const bits = [];
  bits.push(recipeLabel(p.recipe));
  if (p.version != null) bits.push(`v${p.version}`);
  if (p.filesCount != null) bits.push(`${p.filesCount} 个文件`);
  if (p.opsCount != null) bits.push(`${p.opsCount} 项操作`);
  if (p.linksAffected != null) bits.push(`影响链接 ${p.linksAffected}`);
  if (p.createdAt) bits.push(`创建 ${fmtTime(p.createdAt)}`);
  meta.textContent = bits.join(' · ');
  row.appendChild(meta);

  if (p.expiresAt) {
    const expired = isExpired(p.expiresAt) && p.status === 'pending';
    const exp = el('span', `duty-row__expiry${expired ? ' duty-row__expiry--warn' : ''}`,
      `${expired ? '已超过保留期' : '过期于'} ${fmtTime(p.expiresAt)}`);
    row.appendChild(exp);
  }

  return row;
}

async function loadProposals(status = dt.proposalsStatus, cursor = null, { append = false } = {}) {
  if (!append) {
    dt.loading.proposals = true;
    dt.errors.proposals = null;
    if (!dt.detail) render();
  }
  try {
    const res = await dutyApi.listProposals({
      status: status === 'all' ? null : status,
      limit: PROPOSAL_PAGE_SIZE,
      cursor,
    });
    if (append) dt.proposals = dt.proposals.concat(res.items);
    else dt.proposals = res.items;
    dt.proposalsLoaded = true;
    dt.proposalsCursor = res.nextCursor;
    dt.listVersions = new Map(dt.proposals.map((p) => [p.id, p.version]));
    if (status === 'pending') {
      const count = res.total != null && res.total > res.items.length
        ? res.total
        : dt.proposals.filter(isSubstantive).length;
      setPendingCount(count);
    }
  } catch (err) {
    dt.errors.proposals = apiErrState(err);
    if (err && err.code === 404) {
      dt.unavailable = true;
      stopPolling();
    }
  } finally {
    dt.loading.proposals = false;
    if (!dt.detail) render();
  }
}

/* =====================================================================
 * 十、提案详情（安全 diff）
 * ===================================================================== */
async function openDetail(id) {
  dt.detailFromId = id;
  dt.actionError = null;
  await fetchDetail(id, { reset: true });
}

async function fetchDetail(id, { reset = true } = {}) {
  const seq = ++detailSeq;
  if (reset) {
    dt.detail = null;
    dt.result = null;
    dt.conflict = null;
    dt.errors.detail = null;
  }
  dt.loading.detail = true;
  render();
  try {
    const detail = await dutyApi.getProposal(id);
    if (seq !== detailSeq) return;
    const listed = dt.listVersions.get(id);
    if (listed != null && detail.version != null && String(listed) !== String(detail.version)) {
      detail.versionChanged = true;
      if (!detail.staleReason) {
        detail.staleReason = `列表中的版本为 v${listed}，详情已变为 v${detail.version}`;
      }
    }
    dt.detail = detail;
    dt.errors.detail = null;
  } catch (err) {
    if (seq !== detailSeq) return;
    if (err && (err.code === 409 || err.code === 410)) dt.conflict = apiErrState(err);
    else dt.errors.detail = apiErrState(err);
    if (err && err.code === 404) {
      dt.unavailable = true;
      stopPolling();
    }
  } finally {
    if (seq === detailSeq) {
      dt.loading.detail = false;
      render();
      focusDetailBack();
    }
  }
}

function focusDetailBack() {
  const back = dom && dom.body.querySelector('#duty-detail-back');
  if (back) back.focus({ preventScroll: true });
}

function closeDetail() {
  detailSeq += 1;
  const id = dt.detailFromId;
  dt.detail = null;
  dt.loading.detail = false;
  dt.errors.detail = null;
  dt.result = null;
  dt.conflict = null;
  dt.actionError = null;
  render();
  const row = $all('.duty-row', dom.body).find((r) => r.dataset.id === id);
  if (row) row.focus({ preventScroll: false });
  else dom.tabs.find((b) => b.dataset.tab === 'proposals')?.focus();
}

function renderDetail() {
  const d = dt.detail;
  const wrap = el('div', 'duty-detail');

  const bar = el('div', 'duty-detail__bar');
  const back = el('button', 'btn btn--mini', '← 返回待审');
  back.type = 'button';
  back.id = 'duty-detail-back';
  bar.appendChild(back);
  bar.appendChild(el('span', 'duty-detail__id', `提案 ${d.id || '—'}`));
  bar.appendChild(statusChip(d.status, PROPOSAL_STATUS));
  bar.appendChild(el('span', 'duty-detail__spacer'));
  const reload = el('button', 'btn btn--mini', '重新载入');
  reload.type = 'button';
  reload.id = 'duty-detail-reload';
  reload.title = '重新读取该提案的最新版本（不自动合并冲突）';
  bar.appendChild(reload);
  wrap.appendChild(bar);

  if (dt.result && dt.result.proposalId === d.id) wrap.appendChild(resultBlock(dt.result));
  if (dt.actionError) wrap.appendChild(bannerBlock(`操作未完成（${dt.actionError.code || '错误'}）：${dt.actionError.message}`, 'bad'));
  if (dt.conflict) wrap.appendChild(bannerBlock(`提案已变化（${dt.conflict.code}）：${dt.conflict.message} 请重新载入并重新审核，不能静默合并。`, 'warn'));

  if (d.status === 'stale' || d.versionChanged) {
    const reason = d.staleReason || '源笔记或配置在预览后发生了变化。';
    wrap.appendChild(bannerBlock(`基准版本已过期：${reason} 不能直接批准；请让职责重新生成提案。`, 'warn'));
  }
  if (d.status === 'partial' || d.status === 'uncertain') {
    wrap.appendChild(bannerBlock(
      d.status === 'partial'
        ? '该提案此前部分应用：已落盘的项不会回滚；未完成项需要人工对照实际文件处理。'
        : '该提案结果不确定：可能有文件已落盘但未记账。请先核对实际文件，不要自动重跑。',
      'bad',
    ));
  }
  if (d.status === 'expired') {
    wrap.appendChild(bannerBlock('该提案已过期，不能再批准。过期不等于已擦除磁盘备份。', 'warn'));
  } else if (d.status === 'pending' && isExpired(d.expiresAt)) {
    wrap.appendChild(bannerBlock('该提案已超过保留期（expires_at）。服务端应将其标记为过期；请重新载入，不要批准。', 'warn'));
  }

  wrap.appendChild(el('h3', 'duty-detail__title', d.title || d.reason || '(未命名提案)'));

  const links = d.linksAffected != null
    ? d.linksAffected
    : d.diffs.reduce((sum, item) => sum + (item.linksAffected || 0), 0);

  const meta = el('div', 'duty-detail__meta');
  meta.appendChild(fact('职责', recipeLabel(d.recipe) !== '未知职责' ? recipeLabel(d.recipe) : (d.dutyId || '—')));
  if (d.runId) meta.appendChild(fact('运行', d.runId));
  meta.appendChild(fact('版本', d.version == null ? '—' : `v${d.version}`));
  meta.appendChild(fact('文件', d.filesCount == null ? `${d.manifest.length}` : String(d.filesCount)));
  meta.appendChild(fact('受影响链接', `${links} 条`));
  if (d.createdAt) meta.appendChild(fact('创建', fmtTime(d.createdAt)));
  if (d.expiresAt) meta.appendChild(fact('过期', fmtTime(d.expiresAt)));
  if (d.baseVersion != null || d.currentVersion != null) {
    meta.appendChild(fact('基准/当前', `${d.baseVersion == null ? '—' : `v${d.baseVersion}`} / ${d.currentVersion == null ? '—' : `v${d.currentVersion}`}`));
  }
  if (d.hash) meta.appendChild(fact('提案哈希', d.hash.slice(0, 16)));
  wrap.appendChild(meta);

  wrap.appendChild(sourcesSection(d));
  const fresh = freshnessSection(d);
  if (fresh) wrap.appendChild(fresh);
  wrap.appendChild(diffsSection(d));
  wrap.appendChild(manifestSection(d));

  const fineprint = el('p', 'duty-detail__fineprint',
    '待审提案默认 7 天后过期；终态提案的 before/after 与来源摘录会再保留 7 天后清除。批准只对详情所示版本生效，且不自动宣称事实正确。');
  wrap.appendChild(fineprint);

  wrap.appendChild(actionsSection(d));

  dom.body.appendChild(wrap);
}

function sourcesSection(d) {
  const section = el('section', 'duty-section');
  section.appendChild(el('h4', 'duty-section__title', `来源（${d.sources.length}）`));
  if (!d.sources.length) {
    section.appendChild(el('p', 'duty-hint', '该提案没有提供来源；不要在没有证据的情况下批准。'));
    return section;
  }
  const list = el('div', 'duty-sources');
  for (const src of d.sources) {
    const btn = el('button', 'duty-source');
    btn.type = 'button';
    if (src.path) {
      btn.dataset.sourcePath = src.path;
      btn.title = `打开笔记：${src.path}`;
    } else {
      btn.disabled = true;
    }
    const head = el('span', 'duty-source__head');
    head.appendChild(el('span', 'duty-source__title', src.title || src.path || '(无路径来源)'));
    if (src.kind) head.appendChild(chip(src.kind, 'duty-chip--muted'));
    btn.appendChild(head);
    if (src.snippet) btn.appendChild(el('span', 'duty-source__snippet', src.snippet));
    list.appendChild(btn);
  }
  section.appendChild(list);
  return section;
}

const FRESH_RISK_LABEL = {
  not_yet_effective_declared: '尚未生效',
  expired_declared: '已过期',
  superseded_declared: '已被替代',
  invalid_metadata: '元数据有误',
};

/** P2-D：提案预览的保鲜提示；只提示冲突，不自动撤销旧事实。 */
function freshnessSection(d) {
  const risks = d.freshness && Array.isArray(d.freshness.risks) ? d.freshness.risks : [];
  if (!risks.length) return null;
  const section = el('section', 'duty-section');
  section.appendChild(el('h4', 'duty-section__title', `保鲜提示（${risks.length}）`));
  for (const risk of risks) {
    const label = FRESH_RISK_LABEL[risk.state] || risk.state || '未知状态';
    const target = risk.superseded_by ? ` → 替代：${risk.superseded_by}` : '';
    section.appendChild(el('p', 'duty-hint', `${risk.path || '(无路径)'}：${label}${target}`));
  }
  section.appendChild(el('p', 'duty-hint', '仅提示可能冲突，需人工判断；不会自动用失效来源更新当前规则，也不会自动撤销旧事实。'));
  return section;
}

function diffsSection(d) {
  const section = el('section', 'duty-section');
  section.appendChild(el('h4', 'duty-section__title', `变更（${d.diffs.length}）`));
  if (!d.diffs.length) {
    section.appendChild(el('p', 'duty-hint', '该提案没有 diff。没有实质差异时不会进入待审队列。'));
    return section;
  }
  for (const diff of d.diffs) section.appendChild(diffCard(diff));
  return section;
}

function diffCard(diff) {
  const card = el('div', 'duty-diff-card');
  const action = diff.action || diff.op;
  const head = el('div', 'duty-diff-card__head');
  head.appendChild(chip(ACTION_LABEL[action] || action || '变更',
    action === 'create' ? 'duty-chip--ok' : (action === 'delete' ? 'duty-chip--bad' : 'duty-chip--pending')));
  head.appendChild(el('code', 'duty-diff-card__path', diff.path || '—'));

  if (action === 'move') {
    head.appendChild(el('span', 'duty-diff-card__arrow', '→'));
    head.appendChild(el('code', 'duty-diff-card__path', diff.target || '（未知目标）'));
  }
  if (diff.linksAffected != null) {
    head.appendChild(el('span', 'duty-diff-card__links', `影响链接 ${diff.linksAffected} 条`));
  }
  card.appendChild(head);

  const isMove = action === 'move';
  if (isMove) {
    const pathRow = el('div', 'duty-diff-card__move');
    pathRow.appendChild(el('span', 'duty-diff-card__move-label', '移动前'));
    pathRow.appendChild(el('code', null, diff.path || '—'));
    pathRow.appendChild(el('span', 'duty-diff-card__move-label', '移动后'));
    pathRow.appendChild(el('code', null, diff.target || '（未知目标）'));
    card.appendChild(pathRow);
    card.appendChild(el('p', 'duty-diff-card__note', diff.note || '正文不变；反链会按链接解析规则重写。'));
    return card;
  }

  if (diff.before == null && diff.after == null) {
    card.appendChild(el('p', 'duty-diff-card__note', diff.note || '没有正文差异（仅元数据或路径变化）。'));
    return card;
  }

  const stats = lineDiff(diff.before, diff.after);
  const statLine = el('div', 'duty-diff-card__stat');
  statLine.appendChild(el('span', 'duty-diff-card__add', `+${stats.added}`));
  statLine.appendChild(el('span', 'duty-diff-card__del', `−${stats.removed}`));
  if (diff.before == null) statLine.appendChild(el('span', 'duty-diff-card__tag', '新文件'));
  if (diff.after == null) statLine.appendChild(el('span', 'duty-diff-card__tag', '删除内容'));
  if (stats.truncated) statLine.appendChild(el('span', 'duty-diff-card__tag', `已截断（仅显示前 ${MAX_DIFF_ROWS} 行变更）`));
  card.appendChild(statLine);

  if (stats.added || stats.removed) card.appendChild(diffTable(stats));
  else card.appendChild(el('p', 'duty-diff-card__note', '无正文变化。'));

  if (diff.note) card.appendChild(el('p', 'duty-diff-card__note', diff.note));
  return card;
}

function manifestSection(d) {
  const section = el('section', 'duty-section');
  if (!d.manifest.length) {
    section.appendChild(el('h4', 'duty-section__title', '受影响文件'));
    section.appendChild(el('p', 'duty-hint', d.filesCount ? `服务端报告影响 ${d.filesCount} 个文件，但未返回 manifest 明细。` : '没有受影响文件清单。'));
    return section;
  }

  const details = document.createElement('details');
  details.className = 'duty-manifest';
  const summary = el('summary', null, `受影响文件（${d.manifest.length}）`);
  details.appendChild(summary);

  const list = el('div', 'duty-manifest__list');
  for (const entry of d.manifest) {
    const row = el('div', 'duty-manifest__row');
    row.appendChild(el('code', 'duty-manifest__path', entry.path || '—'));
    row.appendChild(chip(ROLE_LABEL[entry.role] || entry.role || '未知', 'duty-chip--muted'));
    if (entry.exists === false) row.appendChild(chip('目标不存在', 'duty-chip--pending'));
    else if (entry.exists === true) row.appendChild(chip('已存在', 'duty-chip--muted'));
    if (entry.sha256) row.appendChild(el('span', 'duty-manifest__hash', entry.sha256.slice(0, 12)));
    list.appendChild(row);
  }
  details.appendChild(list);
  section.appendChild(details);
  return section;
}

function actionsSection(d) {
  const section = el('section', 'duty-section duty-section--actions');
  section.appendChild(el('h4', 'duty-section__title', '决策'));

  const bridgeReady = !!grantBridge();
  const decided = dt.result
    && dt.result.proposalId === d.id
    && ['applied', 'partial', 'uncertain'].includes(dt.result.status);
  const canDecide = (d.status === 'pending' || d.status === 'stale') && !decided;
  const canApprove = d.status === 'pending' && !d.versionChanged && !dt.paused && bridgeReady && !decided;

  const actions = el('div', 'duty-detail__actions');

  const rejectBtn = el('button', 'btn btn--mini', '拒绝提案');
  rejectBtn.type = 'button';
  rejectBtn.id = 'duty-detail-reject';
  rejectBtn.disabled = !canDecide || dt.decisionBusy;
  rejectBtn.title = canDecide ? '拒绝只改变队列状态，不修改任何笔记' : '当前状态不能拒绝';
  actions.appendChild(rejectBtn);

  const approveBtn = el('button', 'btn btn--mini btn--primary', dt.decisionBusy ? '处理中…' : '批准并应用');
  approveBtn.type = 'button';
  approveBtn.id = 'duty-detail-approve';
  approveBtn.disabled = !canApprove || dt.decisionBusy;
  if (!bridgeReady) approveBtn.title = '浏览器模式不能批准：需要桌面端一次性审批凭据';
  else if (d.versionChanged || d.status === 'stale') approveBtn.title = '基准版本已变化，不能批准';
  else if (dt.paused) approveBtn.title = '已暂停，不能批准';
  actions.appendChild(approveBtn);

  section.appendChild(actions);

  let hintText = '批准会重新校验版本与权限，并逐项应用；失败项不会自动重跑。';
  if (!bridgeReady) hintText = '批准需要桌面端原生确认与一次性凭据；当前为浏览器/只读模式，只能预览或拒绝。';
  else if (d.versionChanged || d.status === 'stale') hintText = '基准版本已变化，必须先让职责重新生成提案。';
  else if (dt.paused) hintText = '值班已暂停，恢复前不能批准。';
  section.appendChild(el('p', 'duty-hint', hintText));

  return section;
}

function resultBlock(res) {
  const kind = res.status === 'applied'
    ? 'ok'
    : (res.status === 'partial' ? 'warn' : (res.status === 'rejected' ? 'muted' : 'bad'));
  const box = bannerBlock(decisionText(res), kind);
  if (res.items && res.items.length) {
    const list = el('ul', 'duty-result__list');
    for (const item of res.items) {
      const statusText = item.status === 'uncertain' ? '结果不确定' : '失败';
      list.appendChild(el('li', null,
        `${item.path || '(未知路径)'} · ${statusText}${item.error ? `：${item.error}` : ''}`));
    }
    box.appendChild(list);
  }
  return box;
}

function decisionText(res) {
  if (res.status === 'applied') return `已应用 ${res.applied || res.total || 0} 项变更。`;
  if (res.status === 'partial') {
    return `部分成功：已应用 ${res.applied || 0} 项，失败 ${res.failedCount || 0} 项，不确定 ${res.uncertainCount || 0} 项。未完成项不会自动重跑。`;
  }
  if (res.status === 'uncertain') {
    return '结果不确定：可能有文件已落盘但未记账。请对照实际文件后决定是否重新提案（不会自动重跑 append）。';
  }
  if (res.status === 'rejected') return '已拒绝：没有修改任何笔记。';
  if (res.status === 'stale') return '基准版本已变化，已停止应用；请重新生成提案。';
  if (res.status === 'submitted') return '决策已提交，但服务端未返回应用结果；请刷新并核对实际状态。';
  if (res.message) return res.message;
  return `状态：${res.status || '未知'}`;
}

/* =====================================================================
 * 十一、运行记录
 * ===================================================================== */
function renderRuns() {
  if (!dt.runs.length) {
    dom.body.appendChild(emptyState(
      '还没有运行记录',
      '职责启用后会在应用运行期间检查，也可以在「职责」页手动试跑。未配置模型时仍可生成规则版结果；无变化会记为 no_change，不打扰你。',
      '去职责页试跑',
      () => selectTab('duties'),
    ));
    return;
  }

  const list = el('div', 'duty-runs');
  for (const run of dt.runs) list.appendChild(runRow(run));
  dom.body.appendChild(list);

  if (dt.runsCursor) {
    const more = el('button', 'btn btn--mini', '加载更多');
    more.type = 'button';
    more.id = 'duty-runs-more';
    more.style.marginTop = '4px';
    dom.body.appendChild(more);
  }
}

function runStatusChip(run) {
  // awaiting_review 是运行当时的中间态；提案已有终态时必须按终态展示。
  const proposalStatus = run.proposalId ? dt.runProposalStatus.get(run.proposalId) : null;
  if (run.status === 'awaiting_review' && proposalStatus && proposalStatus !== 'pending') {
    const meta = PROPOSAL_STATUS[proposalStatus];
    if (meta) return chip(meta[0], meta[1]);
  }
  return statusChip(run.status, RUN_STATUS);
}

function runRow(run) {
  const row = el('div', 'duty-run');

  const head = el('div', 'duty-run__head');
  head.appendChild(runStatusChip(run));
  head.appendChild(el('span', 'duty-run__title', recipeLabel(run.recipe)));
  if (run.startedAt) head.appendChild(el('span', 'duty-run__time', fmtTime(run.startedAt)));
  head.appendChild(el('span', 'duty-run__flex'));

  if (run.proposalId) {
    const btn = el('button', 'btn btn--mini', '查看提案');
    btn.type = 'button';
    btn.dataset.runProposal = run.proposalId;
    head.appendChild(btn);
  }
  row.appendChild(head);

  const meta = el('div', 'duty-run__meta');
  const bits = [];
  if (run.id) bits.push(`run ${run.id}`);
  if (run.trigger) bits.push(`触发 ${run.trigger}`);
  if (run.opsCount != null) bits.push(`${run.opsCount} 项操作`);
  if (run.endedAt) bits.push(`结束 ${fmtTime(run.endedAt)}`);
  const usage = usageText(run.modelUsage);
  if (usage) bits.push(usage);
  if (run.errorCode) bits.push(`错误码 ${run.errorCode}`);
  meta.textContent = bits.join(' · ');
  row.appendChild(meta);

  if (run.error) row.appendChild(el('div', 'duty-run__error', run.error));

  return row;
}

async function fetchRunProposalStatuses(items = []) {
  const ids = new Set(items.map((r) => r.proposalId).filter(Boolean));
  const map = new Map();
  if (!ids.size) return map;
  const list = normalizeList(await api.get(`${BASE}/proposals`, { limit: 200 }), 'proposals');
  for (const p of list.items.map(normalizeProposalSummary)) {
    if (p.id && ids.has(p.id)) map.set(p.id, p.status);
  }
  return map;
}

async function loadRuns(cursor = null, { append = false } = {}) {
  if (!append) {
    dt.loading.runs = true;
    dt.errors.runs = null;
    if (!dt.detail) render();
  }
  try {
    const res = await dutyApi.listRuns({ limit: RUN_PAGE_SIZE, cursor });
    if (append) dt.runs = dt.runs.concat(res.items);
    else dt.runs = res.items;
    dt.runsLoaded = true;
    dt.runsCursor = res.nextCursor;
    try {
      const statuses = await fetchRunProposalStatuses(res.items);
      for (const [id, status] of statuses) dt.runProposalStatus.set(id, status);
    } catch { /* 提案状态拉取失败时回退为运行状态展示 */ }
  } catch (err) {
    dt.errors.runs = apiErrState(err);
    if (err && err.code === 404) {
      dt.unavailable = true;
      stopPolling();
    }
  } finally {
    dt.loading.runs = false;
    if (!dt.detail) render();
  }
}

/* =====================================================================
 * 十二、数据加载与操作
 * ===================================================================== */
function loadTab({ force = false } = {}) {
  if (dt.tab === 'duties') {
    if (!dt.overview || force) return loadDuties();
    return Promise.resolve();
  }
  if (dt.tab === 'proposals') {
    if (dt.detail) return Promise.resolve();
    if (!dt.proposalsLoaded || force) return loadProposals(dt.proposalsStatus);
    return Promise.resolve();
  }
  if (!dt.runsLoaded || force) return loadRuns();
  return Promise.resolve();
}

async function loadDuties() {
  dt.loading.duties = true;
  dt.errors.duties = null;
  render();
  try {
    const ov = await dutyApi.overview();
    dt.overview = ov;
    dt.unavailable = false;
    dt.paused = !!ov.runtime.paused;
    if (ov.pendingCount != null) setPendingCount(ov.pendingCount);
    else await syncCountFromProposals();
  } catch (err) {
    dt.errors.duties = apiErrState(err);
    if (err && err.code === 404) {
      dt.unavailable = true;
      stopPolling();
    }
  } finally {
    dt.loading.duties = false;
    render();
  }
}

function selectTab(id) {
  if (!TABS.some((t) => t.id === id)) return;
  if (dt.tab === id && !dt.detail) return;
  if (dt.detail) {
    detailSeq += 1;
    dt.detail = null;
    dt.detailFromId = null;
    dt.result = null;
    dt.conflict = null;
    dt.actionError = null;
  }
  dt.tab = id;
  render();
  loadTab({ force: dt.tab === 'proposals' && dt.proposalsLoaded });
}

async function toggleDuty(id) {
  const def = (dt.overview && dt.overview.definitions || []).find((d) => d.id === id);
  if (!def || dt.busy) return;
  const next = !def.enabled;
  dt.busy = true;
  try {
    await dutyApi.updateDuty(id, {
      enabled: next,
      scope: def.scope,
      schedule: def.schedule,
      limits: def.limits,
      config_version: def.configVersion,
    });
    toast(next ? `已启用${recipeLabel(def.recipe)}` : `已停用${recipeLabel(def.recipe)}`, 'success');
    await loadDuties();
  } catch (err) {
    toast(`设置失败：${err.message}`, 'error');
  } finally {
    dt.busy = false;
    if (dt.open) render();
  }
}

async function runDuty(id) {
  const def = (dt.overview && dt.overview.definitions || []).find((d) => d.id === id);
  if (!def || dt.busy) return;
  dt.busy = true;
  try {
    const res = await dutyApi.runDuty(id, def.configVersion);
    toast(`已加入运行队列${res.runId ? `（${res.runId}）` : ''}；不会立即写入笔记`, 'success');
    selectTab('runs');
  } catch (err) {
    toast(`试跑失败：${err.message}`, 'error');
  } finally {
    dt.busy = false;
    if (dt.open) render();
  }
}

async function pauseAll() {
  if (dt.paused || dt.pausing) return;
  const ok = await askConfirm({
    title: '紧急暂停值班',
    message: '暂停后所有职责立即停止调度与写入；已入队任务不会自动应用，网络中的模型调用可能到超时才返回。恢复方式以后端运行时为准。',
    confirmText: '立即暂停',
    danger: true,
  });
  if (!ok) return;
  dt.pausing = true;
  render();
  try {
    await dutyApi.pause({ all: true });
    dt.paused = true;
    toast('已紧急暂停：后续调度与应用被禁止', 'success');
    refreshBadge().catch(() => {});
  } catch (err) {
    toast(`暂停失败：${err.message}`, 'error');
  } finally {
    dt.pausing = false;
    if (dt.open) render();
  }
}

async function approveProposal() {
  const d = dt.detail;
  if (!d || dt.decisionBusy) return;
  const ok = await askConfirm({
    title: '批准并应用',
    message: `将应用提案 ${d.id}（v${d.version == null ? '—' : d.version}），影响 ${d.filesCount == null ? d.manifest.length : d.filesCount} 个文件。批准只对这一版本生效；服务端会在写入前重新校验版本，若磁盘已变化会拒绝并标记过期。`,
    confirmText: '批准',
  });
  if (!ok) return;

  dt.decisionBusy = true;
  dt.actionError = null;
  dt.conflict = null;
  render();
  try {
    const grant = await dutyApi.requestApprovalGrant({
      proposalId: d.id,
      version: d.version,
      proposalHash: d.hash,
    });
    if (!grant) {
      dt.actionError = { code: 403, message: '未获得桌面端一次性审批凭据；浏览器模式不能批准。' };
      return;
    }
    const res = await dutyApi.decide(d.id, {
      version: d.version,
      proposalHash: d.hash,
      decision: 'approve',
      approvalGrant: grant,
    });
    applyDecisionResult(res, 'approve');
  } catch (err) {
    handleDecisionError(err);
  } finally {
    dt.decisionBusy = false;
    if (dt.open) render();
  }
}

async function rejectProposal() {
  const d = dt.detail;
  if (!d || dt.decisionBusy) return;
  const reason = await askText({
    title: '拒绝提案',
    label: '拒绝原因（可留空；只存入控制数据，不写入笔记）',
    placeholder: '例如：范围不合规范',
    confirmText: '拒绝',
  });
  if (reason === null) return;

  dt.decisionBusy = true;
  dt.actionError = null;
  dt.conflict = null;
  render();
  try {
    const res = await dutyApi.decide(d.id, {
      version: d.version,
      proposalHash: d.hash,
      decision: 'reject',
      reason,
    });
    applyDecisionResult(res, 'reject');
  } catch (err) {
    handleDecisionError(err);
  } finally {
    dt.decisionBusy = false;
    if (dt.open) render();
  }
}

function applyDecisionResult(res, kind) {
  const proposalId = dt.detail ? dt.detail.id : dt.detailFromId;
  const status = res.status || (kind === 'reject' ? 'rejected' : 'submitted');
  dt.result = {
    proposalId,
    kind,
    status,
    applied: res.applied,
    total: res.total,
    failedCount: res.failedCount,
    uncertainCount: res.uncertainCount,
    message: res.message,
    items: res.items,
  };

  // 决策后同步运行记录中的提案状态，避免已拒绝/已应用的运行仍显示「待审」。
  if (proposalId && PROPOSAL_STATUS[status]) dt.runProposalStatus.set(proposalId, status);

  if (res.status === 'applied') toast('已应用；已通过统一写路径落盘', 'success');
  else if (res.status === 'partial') toast('部分成功：请查看未完成项', 'error');
  else if (res.status === 'uncertain') toast('结果不确定：请对照实际文件', 'error');
  else if (kind === 'reject') toast('已拒绝，未修改任何笔记', 'success');
  else toast('决策已提交', 'success');

  if (res.status === 'applied' || res.status === 'partial') emit('tree-refresh');
  refreshBadge().catch(() => {});

  if (!proposalId) return;
  loadProposals(dt.proposalsStatus).catch(() => {});
  dutyApi.getProposal(proposalId)
    .then((fresh) => {
      if (dt.detail && dt.detail.id === proposalId) {
        dt.detail = fresh;
        if (dt.open) render();
      }
    })
    .catch(() => { /* 终态后可能被清理；保留当前详情与结果横幅 */ });
}

function handleDecisionError(err) {
  const code = err && typeof err.code === 'number' ? err.code : 0;
  let message = (err && err.message) || String(err);
  if (code === 409) message = '提案或源笔记在预览后发生变化（409）。请重新载入提案并重新审核，不能把冲突静默合并。';
  else if (code === 410) message = '提案已过期（410），不能批准。';
  else if (code === 401 || code === 403) message = `没有批准权限（${code}）。批准需要本地会话与桌面端一次性凭据。`;
  else if (code === 0) message = '网络不可用：无法连接本地服务，请确认服务仍在运行。';
  dt.actionError = { code, message };
  toast(message, 'error');
}

/* =====================================================================
 * 十三、交互绑定
 * ===================================================================== */
function ensureDom() {
  if (dom) return;
  injectStyle();

  const overlay = document.createElement('div');
  overlay.id = DUTY_OVERLAY_ID;
  overlay.className = 'overlay hidden';
  overlay.innerHTML = `
    <div id="duty-panel" class="overlay__panel duty-panel" role="dialog" aria-modal="true" aria-labelledby="duty-title">
      <div class="duty-head">
        <svg class="icon duty-head__icon" aria-hidden="true"><use href="#i-duty"/></svg>
        <span id="duty-title" class="duty-title">值班</span>
        <span id="duty-head-status" class="duty-head__status" role="status" aria-live="polite"></span>
        <span class="duty-head__actions">
          <button id="btn-duty-pause" class="btn btn--danger btn--mini" type="button">紧急暂停</button>
          <button id="btn-duty-refresh" class="btn btn--mini" type="button">刷新</button>
          <button id="btn-duty-close" class="btn btn--ghost btn--mini" type="button">关闭</button>
        </span>
      </div>
      <div class="duty-tabs" role="tablist" aria-label="值班视图">
        <button class="duty-tab" role="tab" data-tab="duties" id="duty-tab-duties" aria-controls="duty-body" aria-selected="true" tabindex="0">职责</button>
        <button class="duty-tab" role="tab" data-tab="proposals" id="duty-tab-proposals" aria-controls="duty-body" aria-selected="false" tabindex="-1">待审 <span id="duty-tab-count" class="duty-tab__badge hidden"></span></button>
        <button class="duty-tab" role="tab" data-tab="runs" id="duty-tab-runs" aria-controls="duty-body" aria-selected="false" tabindex="-1">运行记录</button>
      </div>
      <div id="duty-banner" class="duty-banner hidden" role="status"></div>
      <div id="duty-body" class="duty-body" role="tabpanel" aria-labelledby="duty-tab-duties" tabindex="-1"></div>
      <div id="duty-foot" class="duty-foot"></div>
    </div>`;
  document.body.appendChild(overlay);

  const panel = overlay.firstElementChild;
  dom = {
    overlay,
    panel,
    body: panel.querySelector('#duty-body'),
    banner: panel.querySelector('#duty-banner'),
    headStatus: panel.querySelector('#duty-head-status'),
    foot: panel.querySelector('#duty-foot'),
    tabCount: panel.querySelector('#duty-tab-count'),
    pauseBtn: panel.querySelector('#btn-duty-pause'),
    refreshBtn: panel.querySelector('#btn-duty-refresh'),
    closeBtn: panel.querySelector('#btn-duty-close'),
    tabs: Array.from(panel.querySelectorAll('.duty-tab')),
  };

  dom.closeBtn.addEventListener('click', closeDuty);
  dom.refreshBtn.addEventListener('click', () => {
    refreshBadge().catch(() => {});
    if (dt.detail) fetchDetail(dt.detail.id, { reset: false }).catch(() => {});
    else loadTab({ force: true });
  });
  dom.pauseBtn.addEventListener('click', () => { pauseAll(); });

  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) closeDuty(); });
  overlay.addEventListener('keydown', onOverlayKey);

  for (const btn of dom.tabs) {
    btn.addEventListener('click', () => selectTab(btn.dataset.tab));
    btn.addEventListener('keydown', onTabKey);
  }

  dom.body.addEventListener('click', onBodyClick);
  dom.body.addEventListener('keydown', onBodyKey);
}

function onOverlayKey(e) {
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    if (dt.detail) closeDetail();
    else closeDuty();
    return;
  }
  if (e.key !== 'Tab') return;
  const focusables = Array.from(dom.panel.querySelectorAll(
    'button:not([disabled]), [tabindex="0"], input, select, textarea, summary',
  )).filter((node) => node.offsetParent !== null);
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function onTabKey(e) {
  const index = dom.tabs.indexOf(e.currentTarget);
  let next = null;
  if (e.key === 'ArrowRight') next = dom.tabs[(index + 1) % dom.tabs.length];
  else if (e.key === 'ArrowLeft') next = dom.tabs[(index - 1 + dom.tabs.length) % dom.tabs.length];
  else if (e.key === 'Home') next = dom.tabs[0];
  else if (e.key === 'End') next = dom.tabs[dom.tabs.length - 1];
  if (!next) return;
  e.preventDefault();
  selectTab(next.dataset.tab);
  next.focus();
}

function onBodyKey(e) {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Home' && e.key !== 'End') return;
  const row = e.target.closest('.duty-row');
  if (!row) return;
  const rows = $all('.duty-row', dom.body);
  if (!rows.length) return;
  e.preventDefault();
  const index = rows.indexOf(row);
  let nextIndex = index;
  if (e.key === 'ArrowDown') nextIndex = Math.min(rows.length - 1, index + 1);
  else if (e.key === 'ArrowUp') nextIndex = Math.max(0, index - 1);
  else if (e.key === 'Home') nextIndex = 0;
  else nextIndex = rows.length - 1;
  rows[nextIndex].focus();
  rows[nextIndex].scrollIntoView({ block: 'nearest' });
}

function onBodyClick(e) {
  const actionBtn = e.target.closest('[data-duty-action]');
  if (actionBtn) {
    const id = actionBtn.dataset.id;
    if (actionBtn.dataset.dutyAction === 'toggle') toggleDuty(id);
    else if (actionBtn.dataset.dutyAction === 'run') runDuty(id);
    return;
  }

  const filterBtn = e.target.closest('[data-proposal-filter]');
  if (filterBtn) {
    const status = filterBtn.dataset.proposalFilter;
    if (status === dt.proposalsStatus) return;
    dt.proposalsStatus = status;
    dt.proposalsLoaded = false;
    dt.proposals = [];
    dt.proposalsCursor = null;
    loadProposals(status);
    return;
  }

  if (e.target.closest('#duty-proposals-more')) {
    loadProposals(dt.proposalsStatus, dt.proposalsCursor, { append: true });
    return;
  }
  if (e.target.closest('#duty-runs-more')) {
    loadRuns(dt.runsCursor, { append: true });
    return;
  }

  const sourceBtn = e.target.closest('[data-source-path]');
  if (sourceBtn) {
    const path = sourceBtn.dataset.sourcePath;
    if (!path) return;
    closeDuty();
    emit('open-note', path);
    return;
  }

  const runProposal = e.target.closest('[data-run-proposal]');
  if (runProposal) {
    dt.tab = 'proposals';
    if (!dt.proposalsLoaded) loadProposals(dt.proposalsStatus);
    render();
    openDetail(runProposal.dataset.runProposal);
    return;
  }

  if (e.target.closest('#duty-detail-back')) {
    closeDetail();
    return;
  }
  if (e.target.closest('#duty-detail-reload')) {
    if (dt.detail) fetchDetail(dt.detail.id, { reset: false });
    return;
  }
  if (e.target.closest('#duty-detail-approve')) {
    approveProposal();
    return;
  }
  if (e.target.closest('#duty-detail-reject')) {
    rejectProposal();
    return;
  }

  const row = e.target.closest('.duty-row');
  if (row && row.dataset.id) openDetail(row.dataset.id);
}

/* =====================================================================
 * 十四、样式（与 inbox 一样运行时注入，避免改共享 CSS 文件）
 * ===================================================================== */
function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
#${DUTY_OVERLAY_ID} { padding: 4vh 12px; }
.duty-panel {
  width: min(1000px, 96vw);
  height: min(84vh, 880px);
  max-height: none;
  padding: 0;
  overflow: hidden;
}
.duty-head {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  border-bottom: 1px solid var(--border); flex: none; flex-wrap: wrap;
}
.duty-head__icon { width: 16px; height: 16px; stroke: var(--accent); fill: none; stroke-width: 1.6;
  stroke-linecap: round; stroke-linejoin: round; }
.duty-title { font-weight: 600; font-size: var(--fs-md); }
.duty-head__status { font-size: var(--fs-xs); color: var(--text-dim); }
.duty-head__actions { margin-left: auto; display: flex; gap: 6px; }
.duty-tabs {
  display: flex; gap: 2px; padding: 0 10px; flex: none; overflow-x: auto;
  border-bottom: 1px solid var(--border);
}
.duty-tab {
  border: none; background: transparent; color: var(--text-secondary);
  padding: 8px 12px; cursor: pointer; font-size: var(--fs-sm);
  border-bottom: 2px solid transparent; display: flex; align-items: center; gap: 6px;
  white-space: nowrap;
}
.duty-tab:hover { color: var(--text); }
.duty-tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--accent); }
.duty-tab:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: var(--radius-sm); }
.duty-tab__badge {
  min-width: 16px; height: 16px; padding: 0 4px; border-radius: 999px;
  background: var(--accent); color: var(--accent-text);
  font-size: 10px; line-height: 16px; text-align: center; font-weight: 700;
}
.overlay .ribbon__badge, .ribbon__badge {
  position: absolute; top: 2px; right: 2px; min-width: 14px; height: 14px;
  padding: 0 3px; border-radius: 999px; background: var(--accent); color: var(--accent-text);
  font-size: 9px; line-height: 14px; text-align: center; font-weight: 700; pointer-events: none;
}
.ribbon__btn--badged { position: relative; }
.duty-banner {
  margin: 10px 14px 0; padding: 8px 10px; border: 1px solid var(--border);
  border-left: 3px solid var(--text-dim); border-radius: var(--radius-sm);
  background: var(--bg-panel); color: var(--text); font-size: var(--fs-xs); line-height: 1.6;
  flex: none;
}
.duty-banner--warn { border-left-color: #d9a441; }
.duty-banner--bad { border-left-color: var(--broken); }
.duty-banner--ok { border-left-color: var(--success); }
.duty-banner--muted { border-left-color: var(--border-strong); color: var(--text-secondary); }
.duty-banner__text { display: block; }
.duty-banner ul { margin: 6px 0 0 18px; padding: 0; }
.duty-banner li { margin: 2px 0; }
.duty-body { flex: 1; overflow: auto; padding: 12px 14px; }
.duty-foot {
  flex: none; padding: 8px 14px; border-top: 1px solid var(--border-faint);
  font-size: var(--fs-xs); color: var(--text-dim);
}
.duty-state { display: flex; align-items: center; gap: 8px; padding: 24px 4px; color: var(--text-dim); font-size: var(--fs-sm); }
.duty-state__spinner {
  width: 12px; height: 12px; border: 2px solid var(--border-strong);
  border-top-color: var(--accent); border-radius: 50%; animation: duty-spin .8s linear infinite;
}
@keyframes duty-spin { to { transform: rotate(360deg); } }
.duty-empty {
  padding: 40px 24px; text-align: center; color: var(--text-dim);
  font-size: var(--fs-sm);
}
.duty-empty h3 { color: var(--text); margin: 0 0 8px; font-size: var(--fs-md); }
.duty-empty p { max-width: 560px; margin: 0 auto 14px; line-height: 1.7; }
.duty-hint { font-size: var(--fs-xs); color: var(--text-dim); line-height: 1.65; margin: 6px 0 10px; }
.duty-runtime {
  display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 8px 10px; margin-bottom: 10px;
  border: 1px solid var(--border-faint); border-radius: var(--radius-md);
  background: var(--bg-alt); font-size: var(--fs-xs); color: var(--text-secondary);
}
.duty-fact { display: inline-flex; gap: 5px; align-items: baseline; min-width: 0; }
.duty-fact b { color: var(--text-dim); font-weight: 500; }
.duty-card {
  border: 1px solid var(--border-faint); border-radius: var(--radius-md);
  background: var(--bg-alt); padding: 10px 12px; margin-bottom: 10px;
}
.duty-card__head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.duty-card__title { font-weight: 600; font-size: var(--fs-sm); }
.duty-card__flex { flex: 1; }
.duty-card__desc { font-size: var(--fs-xs); color: var(--text-dim); margin-top: 6px; line-height: 1.6; }
.duty-facts { display: flex; flex-wrap: wrap; gap: 4px 16px; margin-top: 8px; font-size: var(--fs-xs); color: var(--text-secondary); }
.duty-chip {
  display: inline-flex; align-items: center; gap: 4px; padding: 0 8px;
  height: 18px; border: 1px solid var(--border); border-radius: 999px;
  font-size: 10px; color: var(--text-dim); white-space: nowrap;
}
.duty-chip--ok { color: var(--success); border-color: var(--success); }
.duty-chip--pending { color: var(--accent); border-color: var(--accent); }
.duty-chip--warn { color: #d9a441; border-color: #d9a441; }
.duty-chip--bad { color: var(--broken); border-color: var(--broken); }
.duty-chip--muted { opacity: .85; }
.duty-filters { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
.duty-filter[aria-pressed="true"] {
  background: var(--accent-faint); border-color: var(--accent); color: var(--accent);
}
.duty-list { display: flex; flex-direction: column; gap: 6px; }
.duty-row {
  display: block; width: 100%; text-align: left; font: inherit; color: inherit;
  border: 1px solid var(--border-faint); border-left: 2px solid transparent;
  border-radius: var(--radius-md); padding: 9px 11px; background: transparent; cursor: pointer;
}
.duty-row:hover { background: var(--bg-hover); }
.duty-row:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.duty-row__head { display: flex; align-items: center; gap: 8px; }
.duty-row__title { font-size: var(--fs-sm); font-weight: 600; flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.duty-row__reason { display: block; font-size: var(--fs-xs); color: var(--text-secondary);
  margin-top: 4px; line-height: 1.55; }
.duty-row__meta { display: block; font-size: var(--fs-xs); color: var(--text-dim); margin-top: 4px; }
.duty-row__expiry { display: block; font-size: var(--fs-xs); color: var(--text-dim); margin-top: 2px; }
.duty-row__expiry--warn { color: #d9a441; }
.duty-detail { display: flex; flex-direction: column; gap: 10px; }
.duty-detail__bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.duty-detail__id { font-size: var(--fs-xs); color: var(--text-dim); }
.duty-detail__spacer { flex: 1; }
.duty-detail__title { margin: 0; font-size: var(--fs-md); line-height: 1.5; }
.duty-detail__meta { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: var(--fs-xs);
  color: var(--text-secondary); padding: 8px 10px; border: 1px solid var(--border-faint);
  border-radius: var(--radius-md); background: var(--bg-alt); }
.duty-detail__fineprint { margin: 0; font-size: var(--fs-xs); color: var(--text-dim); line-height: 1.7; }
.duty-detail__actions { display: flex; gap: 8px; flex-wrap: wrap; }
.duty-section { display: flex; flex-direction: column; gap: 8px; }
.duty-section__title { margin: 4px 0 0; font-size: var(--fs-sm); }
.duty-sources { display: flex; flex-direction: column; gap: 6px; }
.duty-source {
  display: block; width: 100%; text-align: left; font: inherit; color: inherit;
  border: 1px solid var(--border-faint); border-radius: var(--radius-md);
  background: var(--bg-alt); padding: 8px 10px; cursor: pointer;
}
.duty-source:hover { background: var(--bg-hover); }
.duty-source:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.duty-source__head { display: flex; align-items: center; gap: 8px; }
.duty-source__title { font-size: var(--fs-xs); font-weight: 600; flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.duty-source__snippet { display: block; margin-top: 4px; font-size: var(--fs-xs);
  color: var(--text-dim); line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.duty-diff-card { border: 1px solid var(--border-faint); border-radius: var(--radius-md); overflow: hidden; }
.duty-diff-card__head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 7px 10px; background: var(--bg-alt); font-size: var(--fs-xs); }
.duty-diff-card__path { font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: var(--fs-xs); color: var(--text-secondary); word-break: break-all; }
.duty-diff-card__arrow { color: var(--text-dim); }
.duty-diff-card__links { color: var(--text-dim); margin-left: auto; }
.duty-diff-card__stat { display: flex; gap: 10px; padding: 5px 10px; font-size: var(--fs-xs);
  color: var(--text-dim); border-top: 1px solid var(--border-faint); }
.duty-diff-card__add { color: var(--success); }
.duty-diff-card__del { color: var(--broken); }
.duty-diff-card__tag { color: var(--text-dim); }
.duty-diff-card__note { margin: 0; padding: 6px 10px; font-size: var(--fs-xs);
  color: var(--text-dim); line-height: 1.6; }
.duty-diff-card__move { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px;
  padding: 7px 10px; border-top: 1px solid var(--border-faint); font-size: var(--fs-xs); }
.duty-diff-card__move code { word-break: break-all; }
.duty-diff-card__move-label { color: var(--text-dim); }
.duty-diff {
  max-height: 320px; overflow: auto; background: var(--bg);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12px; line-height: 1.55;
}
.duty-diff__row { display: flex; gap: 8px; padding: 0 8px; }
.duty-diff__sign { flex: none; width: 10px; color: var(--text-dim); user-select: none; }
.duty-diff__text { flex: 1; min-width: 0; white-space: pre-wrap; word-break: break-word; }
.duty-diff__row--add { background: rgba(63, 185, 80, .12); }
.duty-diff__row--del { background: rgba(240, 97, 109, .12); }
.duty-diff__row--ctx { color: var(--text-secondary); }
.duty-manifest { border: 1px solid var(--border-faint); border-radius: var(--radius-md); padding: 6px 10px; }
.duty-manifest summary { cursor: pointer; font-size: var(--fs-xs); color: var(--text-secondary); }
.duty-manifest__list { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }
.duty-manifest__row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: var(--fs-xs); }
.duty-manifest__path { flex: 1; min-width: 140px; word-break: break-all; color: var(--text-secondary); }
.duty-manifest__hash { color: var(--text-dim); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.duty-runs { display: flex; flex-direction: column; gap: 6px; }
.duty-run { border: 1px solid var(--border-faint); border-radius: var(--radius-md);
  padding: 8px 10px; background: var(--bg-alt); }
.duty-run__head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.duty-run__title { font-size: var(--fs-sm); font-weight: 600; }
.duty-run__time { font-size: var(--fs-xs); color: var(--text-dim); }
.duty-run__flex { flex: 1; }
.duty-run__meta { font-size: var(--fs-xs); color: var(--text-dim); margin-top: 4px; line-height: 1.6; }
.duty-run__error { font-size: var(--fs-xs); color: var(--broken); margin-top: 4px; line-height: 1.6; }
@media (max-width: 640px) {
  #${DUTY_OVERLAY_ID} { padding: 0; }
  .duty-panel { width: 100vw; height: 100vh; max-height: none; border: none; border-radius: 0; }
  .duty-head { padding: 8px 10px; }
  .duty-head__status { display: none; }
  .duty-body { padding: 10px; }
  .duty-banner { margin: 8px 10px 0; }
  .duty-detail__meta { flex-direction: column; gap: 2px; }
}
@media (prefers-reduced-motion: reduce) {
  .duty-panel, .duty-tab, .duty-row, .duty-source { animation: none !important; transition: none !important; }
  .duty-state__spinner { animation: none; border-top-color: var(--border-strong); }
}
`;
  document.head.appendChild(style);
}
