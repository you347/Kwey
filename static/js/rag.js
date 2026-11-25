/* F4 RAG 问答（C5）：build_context 组证据 → Provider 答案 → 带可点击引用。
 * 浮层 #overlay-rag 的 DOM 由集成卡加入 index.html（见报告补丁）；元素缺失时安全降级。
 * 无 Provider 时后端返回摘录式回答，前端照常显示并标注来源。
 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { $, escapeHtml, toast } from './utils.js';
import { registerCommand } from './palette.js';

let initialized = false;
let history = [];   // [{question, answer, sources, used_provider, hallucinated}]
let asking = false;

const EMPTY_HINT = '<div class="panel-empty">输入问题，从你的知识库找答案；引用可点击直达笔记。</div>';

// P2-D：来源列表明确标注失效与替代，不把 expired/superseded 混作当前事实。
const FRESH_LABEL = {
  not_yet_effective_declared: '尚未生效',
  expired_declared: '已过期',
  superseded_declared: '已被替代',
  invalid_metadata: '元数据有误',
};

function sourceBadge(s) {
  const state = s.freshness && s.freshness.state;
  const parts = [];
  if (FRESH_LABEL[state]) parts.push(FRESH_LABEL[state]);
  if (s.replacement_for) parts.push('替代证据');
  if (!parts.length) return '';
  const hint = s.freshness && s.freshness.superseded_by
    ? ` title="${escapeHtml(`替代：${s.freshness.superseded_by}`)}"`
    : '';
  return ` <span class="rag-fresh"${hint}>${escapeHtml(parts.join(' · '))}</span>`;
}

export function initRag() {
  if (initialized) return;
  initialized = true;

  $('#btn-rag-close')?.addEventListener('click', closeRag);
  $('#btn-rag-clear')?.addEventListener('click', clearHistory);
  $('#btn-rag-ask')?.addEventListener('click', () => { ask().catch(reportError); });
  $('#overlay-rag')?.addEventListener('mousedown', (e) => {
    if (e.target === $('#overlay-rag')) closeRag();
  });
  $('#rag-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask().catch(reportError); }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#overlay-rag')?.classList.contains('hidden')) closeRag();
  });

  registerCommand({ id: 'rag.open', title: '问我库（RAG 问答）', run: openRag });
}

export function openRag() {
  if (!$('#overlay-rag')) {
    toast('RAG 问答面板尚未就绪（缺少 DOM）', 'error');
    return;
  }
  $('#overlay-rag').classList.remove('hidden');
  renderHistory();
  $('#rag-input')?.focus();
}

export function closeRag() {
  $('#overlay-rag')?.classList.add('hidden');
}

function clearHistory() {
  if (asking) { toast('回答进行中，请稍候', 'error'); return; }
  history = [];
  renderHistory();
}

/** 把答案里的 [[path]] / [[path|显示名]] 渲染为可点击引用；幻觉引用（后端已剥离）不渲染。 */
function renderAnswer(answer, hallucinated = []) {
  const html = escapeHtml(answer).replace(
    /\[\[([^\]\|]+?)(?:\|([^\]]+))?\]\]/g,
    (_all, path, label) => {
      const p = path.trim();
      if (hallucinated.includes(p)) return escapeHtml(label || p);
      const text = label || p.split('/').pop().replace(/\.md$/i, '');
      return `<button class="rag-cite" data-path="${escapeHtml(p)}" title="${escapeHtml(p)}">${escapeHtml(text)}</button>`;
    },
  );
  return html.replace(/\n/g, '<br>');
}

function renderHistory() {
  const box = $('#rag-history');
  if (!box) return;
  if (!history.length) {
    box.innerHTML = EMPTY_HINT;
    return;
  }
  box.innerHTML = history.map((h, idx) => `
    <div class="rag-turn" data-idx="${idx}">
      <div class="rag-turn__q">${escapeHtml(h.question)}</div>
      <div class="rag-turn__a">
        <div class="rag-turn__badge ${h.used_provider ? 'rag-turn__badge--ai' : ''}">${h.used_provider ? 'AI 生成' : '检索摘录'}</div>
        <div class="rag-turn__text">${renderAnswer(h.answer, h.hallucinated || [])}</div>
        ${(h.hallucinated || []).length ? `<div class="rag-turn__warn">已剔除 ${h.hallucinated.length} 条无依据引用</div>` : ''}
        ${h.sources?.length ? `<details class="rag-turn__sources"><summary>${h.sources.length} 条来源</summary><ul>${h.sources.map((s) => `<li><button class="rag-cite" data-path="${escapeHtml(s.path)}">${escapeHtml(s.title || s.path)}</button>${sourceBadge(s)}</li>`).join('')}</ul></details>` : ''}
      </div>
    </div>`).join('');

  // 引用点击 → 打开对应笔记
  box.querySelectorAll('.rag-cite').forEach((btn) => {
    btn.addEventListener('click', () => {
      emit('open-note', btn.dataset.path);
      closeRag();
    });
  });
  box.scrollTop = box.scrollHeight;
}

async function ask() {
  if (asking) return;
  const input = $('#rag-input');
  const question = (input?.value || '').trim();
  if (!question) { toast('请输入问题', 'error'); return; }

  asking = true;
  const btn = $('#btn-rag-ask');
  if (btn) btn.disabled = true;
  if (input) input.value = '';
  history.push({ question, answer: '正在检索与生成…', sources: [], used_provider: false, hallucinated: [] });
  renderHistory();

  try {
    const data = await api.post('/api/rag/ask', { question });
    history[history.length - 1] = data;
  } catch (err) {
    history[history.length - 1] = {
      question,
      answer: `查询失败：${err.message || err}`,
      sources: [], used_provider: false, hallucinated: [],
    };
    reportError(err);
  } finally {
    asking = false;
    if (btn) btn.disabled = false;
    renderHistory();
  }
}
