/* frontmatter 属性区：展示与编辑，保存后走统一写路径写回（P0-10）。 */

import { escapeHtml, $, toast } from './utils.js';

let visible = true;

/**
 * 渲染属性编辑区。
 * @param {object|null} fm frontmatter dict
 * @param {(newFm: object) => Promise<void>} onSave 保存回调（app.js 里合并 PUT /api/note）
 */
export function renderProperties(fm, onSave) {
  const box = $('#properties');
  if (!box) return;
  const entries = Object.entries(fm || {});
  if (!entries.length) { box.innerHTML = ''; return; }

  box.innerHTML = `
    <div class="properties-toggle" id="props-toggle">▾ 属性 (${entries.length})</div>
    <div class="properties-body" id="props-body">
      <table class="properties-table"><tbody></tbody></table>
      <div class="properties-actions">
        <button class="btn btn--mini" id="props-add">＋属性</button>
        <button class="btn btn--mini btn--primary" id="props-save">保存属性</button>
      </div>
    </div>`;

  const tbody = box.querySelector('tbody');
  const buildRows = (data) => {
    tbody.innerHTML = '';
    for (const [key, value] of Object.entries(data)) {
      const tr = document.createElement('tr');
      const tdKey = document.createElement('td');
      tdKey.className = 'properties-key';
      tdKey.textContent = key;
      const tdVal = document.createElement('td');
      const input = document.createElement('input');
      input.type = 'text';
      input.dataset.key = key;
      input.value = value === null || value === undefined ? '' : JSON.stringify(value).replace(/^"|"$/g, '');
      tdVal.appendChild(input);
      tr.appendChild(tdKey);
      tr.appendChild(tdVal);
      tbody.appendChild(tr);
    }
  };
  buildRows(fm);

  // 折叠开关
  const toggle = box.querySelector('#props-toggle');
  const body = box.querySelector('#props-body');
  toggle.addEventListener('click', () => {
    visible = !visible;
    body.classList.toggle('hidden', !visible);
    toggle.textContent = `${visible ? '▾' : '▸'} 属性 (${entries.length})`;
  });

  box.querySelector('#props-add').addEventListener('click', () => {
    const key = window.prompt('新属性名：');
    if (!key) return;
    const data = collect();
    data[key] = '';
    buildRows(data);
  });

  box.querySelector('#props-save').addEventListener('click', async () => {
    try { await onSave(collect()); toast('属性已保存', 'success'); }
    catch (err) { toast(`属性保存失败: ${err.message}`, 'error'); }
  });

  function collect() {
    const result = {};
    for (const input of box.querySelectorAll('input[data-key]')) {
      const raw = input.value.trim();
      let parsed = raw;
      if (raw === 'true') parsed = true;
      else if (raw === 'false') parsed = false;
      else if (raw !== '' && !isNaN(Number(raw)) && /^-?\d+(\.\d+)?$/.test(raw)) parsed = Number(raw);
      result[input.dataset.key] = parsed;
    }
    return result;
  }
}

export function clearProperties() {
  const box = $('#properties');
  if (box) box.innerHTML = '';
}
