/* 渲染层：schema → DOM。分组建 DOM（真实从属：分组 → 卡片 → 行），切换分组只切可见性。
 * 幂等：initView 只跑一次；重建动态区块用 innerHTML 清理，事件全部委托在 #settings-panel 上。
 * 搜索：跨分组按卡片文本过滤，命中卡片保留并标注所属分组；清空恢复当前分组。
 */

import { GROUPS, groupById } from './schema.js';
import { buildRow, paintRange } from './controls.js';
import { sectionsFor, onSectionsChanged } from './registry.js';

const view = {
  ctx: null,
  page: null,
  nav: null,
  panel: null,
  title: null,
  desc: null,
  search: null,
  group: 'general',
  built: false,
};

const ICON = (name) => `<svg class="icon" aria-hidden="true"><use href="#${name}"></use></svg>`;

export function initView(ctx) {
  if (view.built) return;
  view.ctx = ctx;
  view.page = document.getElementById('settings-page');
  view.nav = document.getElementById('settings-nav');
  view.panel = document.getElementById('settings-panel');
  if (!view.page || !view.nav || !view.panel) return;
  view.built = true;

  buildNav();
  buildPanel();
  bindEvents();

  onSectionsChanged((group) => {
    renderSections(group);
    if (view.search.value) applyFilter(view.search.value);
  });

  applyFilter('');
}

/* ------------------------------------------------------------------ 骨架 */

function buildNav() {
  const search = document.createElement('div');
  search.className = 'settings-nav__search';
  search.innerHTML = `${ICON('i-search')}<input id="settings-search" type="search" placeholder="搜索设置项…"
    aria-label="搜索设置项" autocomplete="off" spellcheck="false"/>`;

  const list = document.createElement('nav');
  list.className = 'settings-nav__list';
  list.setAttribute('role', 'tablist');
  list.setAttribute('aria-label', '设置分组');

  GROUPS.forEach((group, i) => {
    if (group.id === 'about') {
      const rule = document.createElement('div');
      rule.className = 'settings-nav__rule';
      list.appendChild(rule);
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'settings-nav__item';
    btn.dataset.group = group.id;
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-selected', 'false');
    btn.setAttribute('aria-controls', 'settings-panel');
    btn.tabIndex = i === 0 ? 0 : -1;
    btn.innerHTML = `${ICON(group.icon)}<span>${group.title}</span>`;
    list.appendChild(btn);
  });

  const foot = document.createElement('div');
  foot.className = 'settings-nav__foot';
  foot.textContent = 'Kwey · 本地优先';
  view.nav.append(search, list, foot);
  view.search = search.querySelector('#settings-search');
}

function buildPanel() {
  view.panel.innerHTML = `
    <header class="settings-page__header">
      <div class="settings-page__heading">
        <h2 id="settings-group-title">常规</h2>
        <p id="settings-group-desc" class="settings-page__desc"></p>
      </div>
      <button type="button" id="btn-settings-close" class="btn btn--ghost" data-action="settings.close" aria-label="关闭设置">
        ${ICON('i-close')}<span>关闭</span>
      </button>
    </header>
    <div class="settings-groups"></div>`;

  view.title = view.panel.querySelector('#settings-group-title');
  view.desc = view.panel.querySelector('#settings-group-desc');
  const host = view.panel.querySelector('.settings-groups');
  GROUPS.forEach((group) => host.appendChild(buildGroup(group)));
}

function buildGroup(group) {
  const sec = document.createElement('section');
  sec.className = 'settings-group';
  sec.dataset.group = group.id;
  sec.hidden = true;

  const cards = document.createElement('div');
  cards.className = 'settings-group__cards';
  (group.cards || []).forEach((card) => cards.appendChild(buildCard(card, group)));

  const dynamic = document.createElement('div');
  dynamic.className = 'settings-group__dynamic';
  dynamic.dataset.dynamic = group.id;

  sec.append(cards, dynamic);
  return sec;
}

function buildCard(card, group) {
  const el = document.createElement('section');
  el.className = 'settings-card';
  if (card.id) el.id = card.id;

  const head = document.createElement('header');
  head.className = 'settings-card__head';
  const title = document.createElement('h3');
  title.textContent = card.title || '';
  title.dataset.groupTitle = group.title;
  head.appendChild(title);
  if (card.desc) {
    const desc = document.createElement('p');
    desc.className = 'settings-card__desc';
    desc.textContent = card.desc;
    head.appendChild(desc);
  }

  const body = document.createElement('div');
  body.className = 'settings-card__body';
  (card.rows || []).forEach((row) => body.appendChild(buildRow(row, view.ctx)));
  if (card.note) {
    const note = document.createElement('p');
    note.className = 'settings-card__note';
    note.textContent = card.note;
    body.appendChild(note);
  }

  el.append(head, body);
  return el;
}

/* ------------------------------------------------------------ 动态区块 */

/** 挂载某分组的注册区块（registry 变更后也走这里，天然幂等）。 */
export function renderSections(group) {
  const host = view.panel.querySelector(`.settings-group__dynamic[data-dynamic="${group}"]`);
  if (!host) return;
  host.innerHTML = '';
  sectionsFor(group).forEach((sec) => {
    const card = document.createElement('section');
    card.className = 'settings-card';
    card.id = sec.domId || `settings-card-${sec.id}`;

    const head = document.createElement('header');
    head.className = 'settings-card__head';
    const title = document.createElement('h3');
    title.textContent = sec.title || sec.id;
    title.dataset.groupTitle = groupById(group).title;
    head.appendChild(title);
    if (sec.description) {
      const desc = document.createElement('p');
      desc.className = 'settings-card__desc';
      desc.textContent = sec.description;
      head.appendChild(desc);
    }

    const body = document.createElement('div');
    body.className = 'settings-card__body';

    card.append(head, body);
    host.appendChild(card); // 先入文档再 mount：动态模块内部会用 $('#id') 做全局查找

    try {
      sec.mount?.(body);
    } catch (err) {
      const msg = document.createElement('p');
      msg.className = 'settings-card__note';
      msg.textContent = `模块挂载失败：${err && err.message ? err.message : err}`;
      body.appendChild(msg);
      console.error('[kwey] 设置区块挂载失败', sec.id, err);
    }
  });
}

/** 首次渲染全部动态区块（init 时调用一次）。 */
export function renderAll() {
  GROUPS.forEach((g) => renderSections(g.id));
  applyFilter(view.search ? view.search.value : '');
}

/* -------------------------------------------------------------- 分组切换 */

export function setGroup(id) {
  view.group = groupById(id).id;
  if (view.search) view.search.value = '';
  applyFilter('');
  view.panel.scrollTop = 0;
  const active = view.nav.querySelector(`.settings-nav__item[data-group="${view.group}"]`);
  if (active) {
    view.nav.querySelectorAll('.settings-nav__item').forEach((b) => {
      b.tabIndex = b === active ? 0 : -1;
    });
  }
}

/* ------------------------------------------------------------------ 搜索 */

function applyFilter(rawQuery) {
  const query = String(rawQuery || '').trim();
  const q = query.toLowerCase();
  const searching = !!q;
  view.page.classList.toggle('is-searching', searching);

  let total = 0;
  GROUPS.forEach((group) => {
    const sec = view.panel.querySelector(`.settings-group[data-group="${group.id}"]`);
    const cards = sec.querySelectorAll('.settings-card:not(.settings-card--empty)');
    let visible = 0;
    cards.forEach((card) => {
      const hit = searching && card.textContent.toLowerCase().includes(q);
      card.hidden = searching ? !hit : false;
      if (hit) visible += 1;
    });
    syncEmptyCard(sec, cards.length);
    sec.hidden = searching ? visible === 0 : group.id !== view.group;
    total += visible;
    const active = group.id === view.group;
    view.nav.querySelector(`.settings-nav__item[data-group="${group.id}"]`)
      ?.setAttribute('aria-selected', String(!searching && active));
  });

  const group = groupById(view.group);
  view.title.textContent = searching ? '搜索结果' : group.title;
  view.desc.textContent = searching
    ? (total ? `“${query}” · ${total} 个匹配设置项` : `“${query}” · 没有匹配的设置项`)
    : group.desc;
}

/** 分组没有任何卡片时给一张兜底说明卡（保证不出现空白分组）。 */
function syncEmptyCard(sec, realCount) {
  let empty = sec.querySelector('.settings-card--empty');
  if (!empty && realCount === 0) {
    empty = document.createElement('section');
    empty.className = 'settings-card settings-card--empty';
    empty.textContent = '该分组暂无可配置项：打开 vault 后会加载对应模块。';
    sec.querySelector('.settings-group__cards')?.appendChild(empty);
  }
  if (empty) empty.hidden = realCount > 0 || !!view.search.value;
}

/* ------------------------------------------------------------------ 回填 */

/** 用 state.settings 回填所有声明了 setting 的控件。 */
export function syncControls() {
  const settings = view.ctx.getSettings() || {};
  view.panel.querySelectorAll('[data-setting]').forEach((el) => {
    const raw = readPath(settings, el.dataset.setting);
    const fallback = el.dataset.fallback;
    if (el.type === 'checkbox') {
      el.checked = !!raw;
      el.setAttribute('aria-checked', String(!!raw));
      return;
    }
    if (el.type === 'range') {
      el.value = String(raw ?? fallback ?? el.min);
      paintRange(el);
      return;
    }
    el.value = raw === undefined || raw === null || raw === '' ? (fallback ?? '') : String(raw);
  });
  view.panel.querySelectorAll('input[type="checkbox"][role="switch"]').forEach((el) => {
    el.setAttribute('aria-checked', String(el.checked));
  });
}

function readPath(obj, path) {
  if (!obj || !path) return undefined;
  return String(path).split('.').reduce((acc, key) => (acc == null ? undefined : acc[key]), obj);
}

/* ------------------------------------------------------------------ 事件 */

function bindEvents() {
  view.search.addEventListener('input', () => applyFilter(view.search.value));
  view.search.addEventListener('keydown', onSearchKey);

  view.nav.addEventListener('click', (e) => {
    const btn = e.target.closest('.settings-nav__item');
    if (!btn) return;
    setGroup(btn.dataset.group);
    btn.focus();
  });
  view.nav.addEventListener('keydown', onNavKey);

  view.panel.addEventListener('change', (e) => {
    const el = e.target;
    if (el.type === 'checkbox') el.setAttribute('aria-checked', String(el.checked));
    const action = el.dataset && el.dataset.action;
    if (!action) return;
    view.ctx.handlers?.[action]?.(el.type === 'checkbox' ? el.checked : el.value, el);
  });
  view.panel.addEventListener('input', (e) => {
    if (e.target.type !== 'range') return;
    paintRange(e.target);
    const cssVar = e.target.dataset.liveVar;
    if (cssVar) {
      document.documentElement.style.setProperty(cssVar, `${e.target.value}${e.target.dataset.unit || ''}`);
    }
  });
  view.panel.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn || btn.disabled) return;
    view.ctx.handlers?.[btn.dataset.action]?.(btn.dataset.value || '', btn);
  });
}

/** 搜索框里上下键在可见控件间移动，Enter 直达第一个结果。 */
function onSearchKey(e) {
  if (!['ArrowDown', 'ArrowUp', 'Enter'].includes(e.key)) return;
  const items = visibleFocusables();
  if (!items.length) return;
  e.preventDefault();
  if (e.key === 'ArrowUp') {
    items[items.length - 1].focus();
    return;
  }
  items[0].focus();
}

function visibleFocusables() {
  const sel = 'input:not([type="hidden"]), select, textarea, button';
  const scope = view.panel.querySelector('.settings-groups') || view.panel;
  return Array.from(scope.querySelectorAll(sel)).filter((el) => {
    if (el.disabled || el.tabIndex < 0) return false;
    if (el.closest('[hidden]')) return false;
    return el.getClientRects().length > 0;
  });
}

function onNavKey(e) {
  const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End'];
  if (!keys.includes(e.key)) return;
  const tabs = Array.from(view.nav.querySelectorAll('.settings-nav__item'));
  const idx = tabs.indexOf(document.activeElement);
  if (idx < 0) return;
  e.preventDefault();
  let next = idx;
  if (e.key === 'ArrowDown') next = (idx + 1) % tabs.length;
  if (e.key === 'ArrowUp') next = (idx - 1 + tabs.length) % tabs.length;
  if (e.key === 'Home') next = 0;
  if (e.key === 'End') next = tabs.length - 1;
  setGroup(tabs[next].dataset.group);
  tabs[next].focus();
}
