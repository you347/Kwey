/* 行控件构造器：每种 type 一个工厂，返回 Element / DocumentFragment。
 * 只负责「长什么样 + 派发什么」：不读 API、不写 settings —— 提交一律交给 index.js 的 action。
 * 事件全部委托到 #settings-panel（见 render.js），因此重建 DOM 不会重复绑定。
 */

const ICON = (name, cls = '') =>
  `<svg class="icon ${cls}" aria-hidden="true"><use href="#${name}"></use></svg>`;

/** 控件通用属性：id / data-setting / data-fallback / data-action 等。 */
function decorate(el, row) {
  if (row.id) el.id = row.id;
  if (row.setting) el.dataset.setting = row.setting;
  if (row.fallback !== undefined && row.fallback !== null) el.dataset.fallback = String(row.fallback);
  if (row.action) el.dataset.action = row.action;
  if (row.label) {
    el.dataset.label = row.label;
    el.setAttribute('aria-label', row.label);
  }
  return el;
}

function inputEl(row, type = 'text') {
  const el = document.createElement('input');
  el.type = type;
  el.className = 'settings-input';
  decorate(el, row);
  if (row.placeholder) el.placeholder = row.placeholder;
  if (row.readonly) el.readOnly = true;
  if (row.spellcheck === false) el.spellcheck = false;
  return el;
}

/** 行骨架：左标签 + 右控件列；full 时只有控件列（占满整行）。 */
function rowShell(row, ctl) {
  const el = document.createElement('div');
  el.className = 'settings-row';
  el.dataset.type = row.type;
  if (row.full) el.classList.add('settings-row--full');
  if (!row.full) {
    const label = document.createElement('div');
    label.className = 'settings-row__label';
    label.textContent = row.label || '';
    if (row.label) label.title = row.label;
    el.appendChild(label);
  }
  const box = document.createElement('div');
  box.className = 'settings-row__ctl';
  if (typeof ctl === 'string') box.innerHTML = ctl;
  else if (ctl) box.appendChild(ctl);
  el.appendChild(box);
  return el;
}

/** 行 + 可选说明行（hint 在行内跨两列，随属下边框）。 */
function withHint(row, el) {
  if (!row.hint) return el;
  const hint = document.createElement('p');
  hint.className = 'settings-row__hint';
  if (row.hint.id) hint.id = row.hint.id;
  if (row.hint.text) hint.textContent = row.hint.text;
  el.appendChild(hint);
  el.classList.add('has-hint');
  return el;
}

/* ---------------------------------------------------------------- 控件工厂 */

function switchRow(row) {
  const el = inputEl(row, 'checkbox');
  el.className = 'settings-switch';
  el.setAttribute('role', 'switch');
  if (row.checked !== undefined) {
    el.checked = !!row.checked;
    el.setAttribute('aria-checked', String(!!row.checked));
  } else {
    el.setAttribute('aria-checked', 'false');
  }
  return withHint(row, rowShell(row, el));
}

function selectRow(row) {
  const el = document.createElement('select');
  el.className = 'settings-input';
  decorate(el, row);
  (row.options || []).forEach(([value, text]) => {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = text;
    el.appendChild(opt);
  });
  return withHint(row, rowShell(row, el));
}

/** 滑块：拖动只更新视觉（--fill + 数值标签），change 才提交。 */
function sliderRow(row) {
  const box = document.createElement('div');
  box.className = 'settings-slider';

  const range = inputEl(row, 'range');
  range.min = String(row.min ?? 0);
  range.max = String(row.max ?? 100);
  range.step = String(row.step ?? 1);
  if (row.unit) range.dataset.unit = row.unit;
  if (row.liveVar) range.dataset.liveVar = row.liveVar;   // 拖动时直接改 <html> 变量（即时预览）
  if (row.valueId) {
    range.dataset.valueId = row.valueId;
    range.setAttribute('aria-describedby', row.valueId);
  }

  const value = document.createElement('span');
  value.className = 'range-value';
  if (row.valueId) value.id = row.valueId;

  box.append(range, value);
  range.addEventListener('input', () => paintRange(range));
  return withHint(row, rowShell(row, box));
}

function textRow(row) {
  const el = inputEl(row, row.inputType || 'text');
  if (row.inputType === 'number') {
    el.step = row.step || 'any';
    if (row.min !== undefined) el.min = String(row.min);
  }
  el.addEventListener('keydown', (e) => { if (e.key === 'Enter') el.blur(); });
  return withHint(row, rowShell(row, el));
}

/** vault 绝对路径：输入框 + 系统文件夹选择（pywebview 桥可用时才显形）。 */
function pathRow(row) {
  const box = document.createElement('div');
  box.className = 'settings-path';
  const input = inputEl(row);
  box.appendChild(input);

  if (row.pickerId) {
    const pick = document.createElement('button');
    pick.type = 'button';
    pick.id = row.pickerId;
    pick.className = 'btn btn--ghost btn--mini hidden';
    pick.textContent = row.pickerText || '选择文件夹';
    box.appendChild(pick);
  }
  return withHint(row, rowShell(row, box));
}

/** 长密钥：textarea 三行 + 显隐 + 独立保存（160px 单行输入框装不下长 key）。 */
function secretRow(row) {
  const box = document.createElement('div');
  box.className = 'settings-secret';

  const area = document.createElement('textarea');
  area.className = 'settings-input settings-secret__area';
  decorate(area, row);
  area.rows = 3;
  area.spellcheck = false;
  area.wrap = 'soft';
  if (row.placeholder) area.placeholder = row.placeholder;

  const bar = document.createElement('div');
  bar.className = 'settings-secret__bar';
  if (row.toggleId) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.id = row.toggleId;
    toggle.className = 'btn btn--ghost btn--mini';
    toggle.textContent = '显示';
    bar.appendChild(toggle);
  }
  if (row.saveId) {
    const save = document.createElement('button');
    save.type = 'button';
    save.id = row.saveId;
    save.className = 'btn btn--primary btn--mini';
    save.textContent = row.saveText || '保存';
    if (row.action) save.dataset.action = row.action;
    bar.appendChild(save);
  }

  box.append(area, bar);
  return withHint(row, rowShell(row, box));
}

function actionRow(row) {
  const box = document.createElement('div');
  box.className = 'settings-actions';
  (row.buttons || []).forEach((btn) => {
    const el = document.createElement('button');
    el.type = 'button';
    el.id = btn.id;
    el.className = `btn${btn.variant === 'primary' ? ' btn--primary' : ''}${btn.variant === 'ghost' ? ' btn--ghost' : ''}`;
    el.textContent = btn.text;
    if (btn.action) el.dataset.action = btn.action;
    if (btn.title) el.title = btn.title;
    box.appendChild(el);
  });
  return withHint(row, rowShell(row, box));
}

/** 跳转行：整行可点，右侧 meta 灰字 + chevron。 */
function linkRow(row) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = 'settings-link';
  if (row.id) el.id = row.id;
  if (row.action) el.dataset.action = row.action;
  el.innerHTML = `
    <span class="settings-link__text">
      <span class="settings-link__label">${escapeText(row.label || '')}</span>
      ${row.meta ? `<span class="settings-link__meta">${escapeText(row.meta)}</span>` : ''}
    </span>
    ${ICON('i-chevron', 'settings-link__chev')}`;
  return el;
}

/** 逃生舱：交给 index.js 的 mounts 回调填充（token 行 / 命令块 / 快捷键列表等）。 */
function customRow(row, ctx) {
  if (row.full) {
    const el = document.createElement('div');
    el.className = row.className || 'settings-custom';
    if (row.id) el.id = row.id;
    ctx.mounts?.[row.mount]?.(el);
    return withHint(row, el);
  }
  const el = rowShell(row, null);
  const box = el.querySelector('.settings-row__ctl');
  if (row.mount && box) ctx.mounts?.[row.mount]?.(box);
  return withHint(row, el);
}

const FACTORIES = {
  switch: switchRow,
  select: selectRow,
  slider: sliderRow,
  text: textRow,
  path: pathRow,
  secret: secretRow,
  action: actionRow,
  link: linkRow,
  custom: customRow,
};

/** 唯一入口：row 声明 → DOM（Element 或 Fragment）。 */
export function buildRow(row, ctx) {
  const factory = FACTORIES[row.type];
  if (!factory) {
    const el = document.createElement('p');
    el.className = 'settings-row__hint';
    el.textContent = `未知控件类型：${row.type}`;
    return el;
  }
  return factory(row, ctx);
}

/** 重绘滑块填充与数值标签（回填 / 拖动共用）。 */
export function paintRange(range) {
  const min = Number(range.min || 0);
  const max = Number(range.max || 100);
  const value = Number(range.value || 0);
  const pct = max > min ? ((value - min) / (max - min)) * 100 : 0;
  range.style.setProperty('--fill', `${pct}%`);
  if (!range.dataset.valueId) return;
  const label = document.getElementById(range.dataset.valueId);
  if (label) label.textContent = `${value}${range.dataset.unit || ''}`;
}

/** 文本转义（只用在本模块拼字符串的地方）。 */
function escapeText(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
