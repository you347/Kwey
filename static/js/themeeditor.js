/* P2 自定义 CSS 主题编辑器：设置页自绘区块 + 实时预览 + 保存 / 重置。
 * 用户样式写入 <style id="kwey-user-css">，在 themes.css 之后注入，因此优先级最高；
 * 关闭开关或重置时移除该节点，恢复内置亮/暗主题。 */

import { api, reportError } from './api.js';
import { $, toast } from './utils.js';
import { registerSection } from './settings/registry.js';

export const STYLE_ID = 'kwey-user-css';
export const SECTION_ID = 'theme-editor-section';

// 与后端 app/api/routes_theme.py 保持一致：预览阶段也拒绝外链 / 脚本式片段
const FORBIDDEN_RE = /@import\b|url\s*\(\s*['"]?\s*(?:https?:\/\/|\/\/)|expression\s*\(|javascript\s*:/i;

const VAR_HINT = [
  '--bg', '--bg-alt', '--bg-panel', '--bg-hover',
  '--text', '--text-secondary', '--text-dim',
  '--accent', '--accent-hover', '--accent-text', '--accent-faint',
  '--border', '--border-strong', '--border-faint',
  '--radius-sm', '--radius-md', '--radius-lg',
  '--fs-xs', '--fs-sm', '--fs-md', '--fs-lg', '--shadow',
].join(' / ');

let initialized = false;
// 内置样式表里真实存在的 `.class` / `#id`；用于给用户 CSS 做「类名不存在」体检
let knownTokens = null;

/** 注册为「扩展与外观」下的一张设置卡；卡片标题由 registry 渲染，正文即原模板。 */
export function initThemeEditor() {
  if (initialized) return;
  initialized = true;

  registerSection({
    id: 'theme-editor',
    domId: SECTION_ID,
    group: 'extension',
    order: 20,
    title: '自定义 CSS 主题',
    description: '用户样式最后注入，优先级高于内置主题；保存后才持久化',
    mount: (el) => {
      el.innerHTML = `
    <div class="theme-ai-box" style="border:1px solid var(--border);border-radius:var(--radius-md);padding:10px;margin-bottom:10px">
      <div class="settings-muted" style="margin-bottom:6px">用自然语言描述想要的样式，让内置 Agent 帮你写 CSS（生成后仍需点「保存」才生效）</div>
      <div class="settings-row settings-row--inline">
        <input id="theme-ai-request" class="settings-input" type="text" placeholder="例如：把强调色改成暖橙色，圆角更大，暗色主题文字柔和一些"/>
        <button id="btn-theme-generate" class="btn btn--primary">AI 生成</button>
      </div>
      <div id="theme-ai-status" class="settings-muted"></div>
    </div>
    <label class="settings-row">
      <span class="settings-row__label">实时预览（不写盘）</span>
      <input id="theme-enabled" type="checkbox"/>
    </label>
    <textarea id="theme-css" class="settings-input" rows="8" spellcheck="false"
      placeholder="/* 示例：把强调色改红 */&#10;body { --accent: #f00; }"></textarea>
    <p class="settings-muted" id="theme-css-hint">可覆盖变量：${VAR_HINT}。完整清单见 static/css/themes.css；用户样式最后注入，优先级高于内置主题。</p>
    <div class="settings-row settings-row--inline">
      <button id="btn-theme-save" class="btn btn--primary">保存</button>
      <button id="btn-theme-reset" class="btn btn--ghost">重置（删除自定义 CSS）</button>
    </div>
    <p class="settings-muted" id="theme-status">读取中…</p>`;

      $('#theme-enabled').addEventListener('change', () => {
        syncStyle();
        setStatus($('#theme-enabled').checked ? '已开启实时预览（保存后才持久化）' : '已关闭实时预览（样式节点已移除）');
      });
      $('#theme-css').addEventListener('input', () => {
        if (syncStyle()) setStatus(withAudit('预览已更新（尚未保存）'));
      });
      $('#btn-theme-save').addEventListener('click', () => { save().catch(reportError); });
      $('#btn-theme-reset').addEventListener('click', () => { reset().catch(reportError); });
      $('#btn-theme-generate').addEventListener('click', () => { generate().catch(reportError); });
      $('#theme-ai-request').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); generate().catch(reportError); }
      });
    },
    refresh: () => refresh().catch(reportError),
  });

  refresh().catch(reportError);
}

/** 设置页打开时调用：重新读取磁盘状态并回填输入框。 */
export async function refresh() {
  const area = $('#theme-css');
  if (!area) return;
  let data;
  try {
    data = await api.get('/api/theme/custom');
  } catch (err) {
    setStatus(`读取自定义主题失败：${err.message || err}`);
    return;
  }
  area.value = data.css || '';
  $('#theme-enabled').checked = !!data.enabled;
  syncStyle();
  await loadKnownTokens();
  setStatus(withAudit(data.enabled && data.css ? '自定义主题已启用' : '未启用自定义主题'));
}

/** 拉取内置样式表的类名 / ID 清单（一次即可）；失败则不体检，不阻塞主题功能。 */
async function loadKnownTokens() {
  if (knownTokens) return knownTokens;
  try {
    const data = await api.get('/api/theme/known-selectors');
    knownTokens = new Set([
      ...(data.classes || []).map((c) => `.${c}`),
      ...(data.ids || []).map((i) => `#${i}`),
    ]);
  } catch {
    knownTokens = null;
  }
  return knownTokens;
}

/** 校验用户 CSS 里的类名 / ID 是否真实存在于内置样式表：捕捉「语法合法但类名写错」
 *  的静默失效（例如臆造出 .agent-reply）。与元素是否渲染无关，避免空聊天时误报。 */
function withAudit(base) {
  if (!knownTokens) return base;
  const style = document.getElementById(STYLE_ID);
  if (!style || !style.sheet) return base;
  let rules;
  try {
    rules = Array.from(style.sheet.cssRules || []);
  } catch {
    return base;
  }
  const unknown = new Set();
  for (const rule of rules) {
    const sel = rule.selectorText;
    if (!sel) continue;  // @media / @keyframes 等跳过
    for (const [token] of sel.matchAll(/[.#][A-Za-z_][\w-]*/g)) {
      if (!knownTokens.has(token)) unknown.add(token);
    }
  }
  if (!unknown.size) return base;
  const list = Array.from(unknown);
  const detail = list.length === 1
    ? `${list[0]} 在样式表中不存在`
    : `${list.slice(0, 3).join('、')} 等 ${list.length} 个选择器在样式表中不存在`;
  return `${base}（注意：${detail}，可能写错了）`;
}

function setStatus(text) {
  const el = $('#theme-status');
  if (el) el.textContent = text;
}

function removeStyle() {
  const style = document.getElementById(STYLE_ID);
  if (style) style.remove();
}

/** 把 textarea 内容同步到 <style>；返回是否成功应用（非法 CSS 不注入）。 */
function syncStyle() {
  const area = $('#theme-css');
  if (!area) return false;
  const css = area.value;
  if (FORBIDDEN_RE.test(css)) {
    removeStyle();
    setStatus('含被禁止的片段（@import / url(http / expression( / javascript:），预览与保存都会拒绝');
    return false;
  }
  if (!$('#theme-enabled').checked || !css.trim()) {
    removeStyle();
    return true;
  }
  let style = document.getElementById(STYLE_ID);
  if (!style) {
    style = document.createElement('style');
    style.id = STYLE_ID;
    document.head.appendChild(style);  // 位于 themes.css 之后 → 覆盖内置变量
  }
  style.textContent = css;
  return true;
}

async function save() {
  const css = $('#theme-css').value;
  if (FORBIDDEN_RE.test(css)) {
    toast('自定义 CSS 含被禁止的外链或脚本片段，未保存', 'error');
    return;
  }
  const enabled = !!$('#theme-enabled').checked;
  await api.put('/api/theme/custom', { css, enabled });
  toast(enabled ? '自定义 CSS 已保存并应用' : '自定义 CSS 已保存（未启用）', 'success');
  await refresh();
}

async function reset() {
  await api.del('/api/theme/custom');
  $('#theme-css').value = '';
  $('#theme-enabled').checked = false;
  removeStyle();
  setStatus('已重置：自定义 CSS 已删除，恢复内置主题');
  toast('已重置自定义主题', 'success');
}

/** 让内置 Agent 按自然语言需求生成 CSS：填入编辑器并实时预览，用户确认后保存。 */
async function generate() {
  const input = $('#theme-ai-request');
  const want = (input?.value || '').trim();
  if (!want) { toast('请先描述想要的样式', 'error'); return; }
  const status = $('#theme-ai-status');
  const btn = $('#btn-theme-generate');
  if (status) status.textContent = '正在生成样式…（通常几秒，模型慢时可能半分钟）';
  if (btn) btn.disabled = true;
  try {
    const data = await api.post('/api/theme/generate', {
      request: want,
      include_current: true,
    });
    $('#theme-css').value = data.css || '';
    $('#theme-enabled').checked = true;
    if (syncStyle()) {
      if (status) status.textContent = withAudit('已生成并预览（尚未保存，点「保存」才持久化）');
      toast('样式已生成并预览', 'success');
    }
  } catch (err) {
    if (status) status.textContent = `生成失败：${err.message || err}`;
    reportError(err);
  } finally {
    if (btn) btn.disabled = false;
  }
}
