/* 通用右键菜单：视口钳制定位、键盘导航（↑↓/Enter/Esc）、外部点击/滚动/失焦自动关闭。
 * 条目契约：{label, icon?, hint?, danger?, run} 或 {sep:true}。 */

let menuEl = null;
let entries = [];
let activeIdx = -1;
let prevFocus = null;

export function showContextMenu(x, y, items) {
  closeContextMenu();
  entries = items;
  prevFocus = document.activeElement;

  menuEl = document.createElement('div');
  menuEl.className = 'ctx-menu';
  menuEl.setAttribute('role', 'menu');
  menuEl.tabIndex = -1;

  entries.forEach((entry, i) => {
    if (entry.sep) {
      const sep = document.createElement('div');
      sep.className = 'ctx-menu__sep';
      menuEl.appendChild(sep);
      return;
    }
    const el = document.createElement('div');
    el.className = `ctx-menu__item${entry.danger ? ' ctx-menu__item--danger' : ''}`;
    el.setAttribute('role', 'menuitem');
    el.dataset.idx = String(i);
    if (entry.icon) {
      const ic = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      ic.setAttribute('class', 'icon');
      ic.setAttribute('aria-hidden', 'true');
      const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
      use.setAttribute('href', `#${entry.icon}`);
      ic.appendChild(use);
      el.appendChild(ic);
    }
    const label = document.createElement('span');
    label.className = 'ctx-menu__label';
    label.textContent = entry.label;
    el.appendChild(label);
    if (entry.hint) {
      const hint = document.createElement('span');
      hint.className = 'ctx-menu__hint';
      hint.textContent = entry.hint;
      el.appendChild(hint);
    }
    el.addEventListener('click', () => {
      closeContextMenu();
      if (entry.run) entry.run();
    });
    el.addEventListener('mousemove', () => setActive(i));
    menuEl.appendChild(el);
  });

  document.body.appendChild(menuEl);
  const rect = menuEl.getBoundingClientRect();
  menuEl.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`;
  menuEl.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`;

  document.addEventListener('mousedown', onOutside, true);
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', closeContextMenu);
  window.addEventListener('blur', closeContextMenu);
  menuEl.focus();
}

export function closeContextMenu() {
  if (!menuEl) return;
  document.removeEventListener('mousedown', onOutside, true);
  document.removeEventListener('keydown', onKey, true);
  document.removeEventListener('scroll', onScroll, true);
  window.removeEventListener('resize', closeContextMenu);
  window.removeEventListener('blur', closeContextMenu);
  menuEl.remove();
  menuEl = null;
  entries = [];
  activeIdx = -1;
  if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
  prevFocus = null;
}

function actionableIdxs() {
  return entries.map((e, i) => (e.sep ? -1 : i)).filter((i) => i >= 0);
}

function setActive(i) {
  activeIdx = i;
  if (!menuEl) return;
  menuEl.querySelectorAll('.ctx-menu__item').forEach((el) => {
    el.classList.toggle('ctx-menu__item--active', Number(el.dataset.idx) === i);
  });
}

function move(delta) {
  const idxs = actionableIdxs();
  if (!idxs.length) return;
  const pos = idxs.indexOf(activeIdx);
  const next = pos === -1 ? (delta > 0 ? 0 : idxs.length - 1) : (pos + delta + idxs.length) % idxs.length;
  setActive(idxs[next]);
}

function onOutside(e) {
  if (menuEl && !menuEl.contains(e.target)) closeContextMenu();
}

function onScroll() { closeContextMenu(); }

function onKey(e) {
  if (!menuEl) return;
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeContextMenu(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); e.stopPropagation(); move(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); move(-1); }
  else if (e.key === 'Home') { e.preventDefault(); const idxs = actionableIdxs(); if (idxs.length) setActive(idxs[0]); }
  else if (e.key === 'End') { e.preventDefault(); const idxs = actionableIdxs(); if (idxs.length) setActive(idxs[idxs.length - 1]); }
  else if (e.key === 'Enter') {
    e.preventDefault();
    e.stopPropagation();
    const entry = entries[activeIdx];
    if (entry && !entry.sep) {
      closeContextMenu();
      if (entry.run) entry.run();
    }
  }
}