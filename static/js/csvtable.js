/* CSV/TSV 表格视图：单元格编辑、增删行列；改动用同一套纯文本内容保存。
 * 序列化规则与 app/viewers/table.py 保持一致（RFC4180 QUOTE_MINIMAL），
 * 且每次打开/切回表格都做「解析→重写→逐字比对」，无法无损往返时退回源码。 */

import { $ } from './utils.js';

const MAX_ROWS = 5000;
const MAX_COLS = 200;
const MAX_CHARS = 2000000;

let host = null;
let switchEl = null;
let model = null;        // {delimiter, rows, colCount, newline, trailing, ext}
let onChange = null;
let onRequestMode = null;
let mode = 'source';

function countChar(text, ch) {
  let n = 0;
  for (const c of text) if (c === ch) n += 1;
  return n;
}

function detectNewline(text) {
  const crlf = (text.match(/\r\n/g) || []).length;
  const lf = (text.match(/\n/g) || []).length - crlf;
  const cr = (text.match(/\r/g) || []).length - crlf;
  if (crlf && crlf >= lf && crlf >= cr) return '\r\n';
  if (cr && cr > lf) return '\r';
  return '\n';
}

/** 与 Python csv.reader 对齐的最小解析器；引号未闭合时抛错。 */
function parseCsv(text, delimiter) {
  const rows = [];
  let row = [];
  let field = '';
  let inQuotes = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 1; } else inQuotes = false;
      } else field += ch;
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === delimiter) {
      row.push(field); field = '';
    } else if (ch === '\r' || ch === '\n') {
      if (ch === '\r' && text[i + 1] === '\n') i += 1;
      row.push(field); rows.push(row); row = []; field = '';
    } else {
      field += ch;
    }
  }
  if (inQuotes) throw new Error('unterminated quote');
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function quoteCell(value, delimiter) {
  const s = String(value == null ? '' : value);
  const need = s.includes(delimiter) || s.includes('"') || s.includes('\n') || s.includes('\r');
  return need ? `"${s.replace(/"/g, '""')}"` : s;
}

function serializeRows(rows, delimiter, newline, trailing) {
  if (!rows.length) return '';
  let text = rows.map((row) => (
    row.length === 1 && row[0] === ''
      ? '""'  // Python csv.writer 对「单空字段行」写 ""，避免退化成空行
      : row.map((cell) => quoteCell(cell, delimiter)).join(delimiter)
  )).join(newline);
  if (trailing) text += newline;
  return text;
}

/** 探测分隔符并做无损往返校验；失败返回 null。 */
function detectModel(text, ext) {
  if (!text || text.length > MAX_CHARS) return null;
  const newline = detectNewline(text);
  const trailing = text.endsWith(newline);
  const candidates = ext === 'tsv' ? ['\t', ',', ';', '|'] : [',', ';', '\t', '|'];
  let best = null;
  for (const delimiter of candidates) {
    let rows;
    try { rows = parseCsv(text, delimiter); } catch { continue; }
    if (!rows.length) continue;
    const width = rows[0].length;
    // 单列文件无法可靠判断分隔符，与后端一致按不安全处理
    if (width < 2 || rows.some((row) => row.length !== width)) continue;
    if (rows.length > MAX_ROWS || width > MAX_COLS) continue;
    if (serializeRows(rows, delimiter, newline, trailing) !== text) continue;
    const score = [width, countChar(text, delimiter)];
    if (!best
      || score[0] > best.score[0]
      || (score[0] === best.score[0] && score[1] > best.score[1])) {
      best = { score, delimiter, rows, colCount: width, newline, trailing, ext };
    }
  }
  return best;
}

function colName(index) {
  let n = index + 1;
  let name = '';
  while (n > 0) {
    const rem = (n - 1) % 26;
    name = String.fromCharCode(65 + rem) + name;
    n = Math.floor((n - 1) / 26);
  }
  return name;
}

function delimiterLabel(delimiter) {
  if (delimiter === '\t') return '制表符';
  if (delimiter === ',') return '逗号';
  if (delimiter === ';') return '分号';
  if (delimiter === '|') return '竖线';
  return delimiter;
}

function buttonEl(text, title, handler) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn--mini';
  btn.textContent = text;
  if (title) btn.title = title;
  btn.addEventListener('click', handler);
  return btn;
}

function focusCell(row, col) {
  const target = host?.querySelector(`.csv-table__cell[data-r="${row}"][data-c="${col}"]`);
  if (target) target.focus();
  else {
    const cells = host?.querySelectorAll('.csv-table__cell') || [];
    if (cells.length) cells[cells.length - 1].focus();
  }
}

function emit() {
  if (!model || !onChange) return;
  onChange(serializeRows(model.rows, model.delimiter, model.newline, model.trailing));
}

function removeRow(index) {
  if (!model) return;
  model.rows.splice(index, 1);
  render();
  emit();
}

function removeCol(index) {
  if (!model || model.colCount <= 1) return;
  model.rows.forEach((row) => row.splice(index, 1));
  model.colCount -= 1;
  render();
  emit();
}

function addRow() {
  if (!model || model.rows.length >= MAX_ROWS) return;
  model.rows.push(new Array(model.colCount).fill(''));
  render();
  const last = model.rows.length - 1;
  window.requestAnimationFrame(() => focusCell(last, 0));
  emit();
}

function addCol() {
  if (!model || model.colCount >= MAX_COLS) return;
  model.colCount += 1;
  model.rows.forEach((row) => row.push(''));
  render();
  emit();
}

function render() {
  if (!host || !model) return;
  host.innerHTML = '';

  const bar = document.createElement('div');
  bar.className = 'csv-table__bar';
  const hint = document.createElement('span');
  hint.className = 'csv-table__hint';
  hint.textContent = `分隔符：${delimiterLabel(model.delimiter)} · ${model.rows.length} 行 × ${model.colCount} 列`;
  bar.appendChild(hint);
  bar.appendChild(buttonEl('＋ 行', '在末尾新增一行', addRow));
  bar.appendChild(buttonEl('＋ 列', '在末尾新增一列', addCol));
  host.appendChild(bar);

  const table = document.createElement('table');
  table.className = 'csv-table__grid';

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  const corner = document.createElement('th');
  corner.className = 'csv-table__corner';
  headRow.appendChild(corner);
  for (let c = 0; c < model.colCount; c += 1) {
    const th = document.createElement('th');
    const label = document.createElement('span');
    label.textContent = colName(c);
    th.appendChild(label);
    th.appendChild(buttonEl('×', `删除第 ${c + 1} 列`, () => removeCol(c)));
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  model.rows.forEach((row, r) => {
    while (row.length < model.colCount) row.push('');
    const tr = document.createElement('tr');
    const rh = document.createElement('th');
    rh.className = 'csv-table__rownum';
    const rlabel = document.createElement('span');
    rlabel.textContent = String(r + 1);
    rh.appendChild(rlabel);
    rh.appendChild(buttonEl('×', `删除第 ${r + 1} 行`, () => removeRow(r)));
    tr.appendChild(rh);
    for (let c = 0; c < model.colCount; c += 1) {
      const td = document.createElement('td');
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'csv-table__cell';
      input.value = row[c] == null ? '' : String(row[c]);
      input.dataset.r = String(r);
      input.dataset.c = String(c);
      input.setAttribute('aria-label', `第 ${r + 1} 行第 ${c + 1} 列`);
      input.addEventListener('input', () => { row[c] = input.value; emit(); });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === 'ArrowDown') {
          e.preventDefault();
          if (r + 1 < model.rows.length) focusCell(r + 1, c);
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          if (r > 0) focusCell(r - 1, c);
        }
      });
      td.appendChild(input);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  host.appendChild(table);

  if (!model.rows.length) {
    const empty = document.createElement('div');
    empty.className = 'csv-table__empty';
    empty.textContent = '表格为空，点击「＋ 行」开始。';
    host.appendChild(empty);
  }
}

export function initCsvTable({ onChange: changeHandler, onRequestMode: modeHandler } = {}) {
  host = $('#csv-table');
  switchEl = $('#plain-mode-switch');
  onChange = changeHandler;
  onRequestMode = modeHandler;
  switchEl?.querySelectorAll('[data-plain-mode]').forEach((btn) => {
    btn.addEventListener('click', () => onRequestMode?.(btn.dataset.plainMode));
  });
}

/** 装载纯文本文档；可表格化时返回 true 并默认进入表格视图。 */
export function open(doc) {
  close();
  const ext = String(doc.format || '').toLowerCase();
  if (ext !== 'csv' && ext !== 'tsv') return false;
  let candidate = null;
  if (doc.table && Array.isArray(doc.table.rows)) {
    const rows = doc.table.rows.map((row) => row.map((cell) => String(cell == null ? '' : cell)));
    const table = {
      delimiter: doc.table.delimiter,
      rows,
      colCount: doc.table.col_count,
      newline: doc.newline || '\n',
      trailing: !!doc.table.trailing_newline,
      ext,
    };
    // 服务端已校验过一次；这里再按前端序列化器复核，防止两端规则漂移
    if (serializeRows(rows, table.delimiter, table.newline, table.trailing) === doc.content) {
      candidate = table;
    }
  }
  if (!candidate) candidate = detectModel(doc.content, ext);
  if (!candidate) return false;
  model = candidate;
  host?.classList.remove('hidden');
  const area = $('#editor-area');
  if (area) area.setAttribute('data-csv-table', 'true');
  setMode('table');
  return true;
}

export function close() {
  model = null;
  mode = 'source';
  if (host) { host.innerHTML = ''; host.classList.add('hidden'); }
  const area = $('#editor-area');
  if (area) {
    area.removeAttribute('data-csv-table');
    area.removeAttribute('data-csv-mode');
  }
  switchEl?.querySelectorAll('[data-plain-mode]').forEach((btn) => btn.classList.remove('btn--active'));
}

/** 源码 → 表格：解析当前编辑器文本；失败返回 false（调用方留在源码并提示）。 */
export function parseInto(text) {
  const ext = model?.ext || 'csv';
  const next = detectModel(text, ext);
  if (!next) return false;
  model = next;
  render();
  return true;
}

export function setMode(next) {
  if (next === 'table' && !model) return;
  mode = next === 'table' ? 'table' : 'source';
  const area = $('#editor-area');
  if (area) area.setAttribute('data-csv-mode', mode);
  switchEl?.querySelectorAll('[data-plain-mode]').forEach((btn) => {
    btn.classList.toggle('btn--active', btn.dataset.plainMode === mode);
  });
  if (mode === 'table') render();
}

export function isActive() { return !!model; }
export function isTableMode() { return !!model && mode === 'table'; }

export function getText() {
  if (!model) return '';
  return serializeRows(model.rows, model.delimiter, model.newline, model.trailing);
}

/** 供调试/测试读取当前模型摘要。 */
export function currentModel() {
  if (!model) return null;
  return {
    delimiter: model.delimiter,
    rows: model.rows.map((row) => [...row]),
    colCount: model.colCount,
    newline: model.newline,
    trailing: model.trailing,
  };
}
