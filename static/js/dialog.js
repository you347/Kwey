/* 应用内对话框：askText / askConfirm，替代原生 prompt/confirm。
 * Promise 化；Esc 取消 / Enter 确认；焦点圈定在对话框内，关闭后归还焦点。
 * 同一时刻只允许一个对话框，后开的会把先开的按「取消」结算。 */

let activeCancel = null;

/**
 * @param {object} opts
 * @param {string} opts.title 标题
 * @param {string} [opts.message] 说明文本（confirm 用）
 * @param {string} [opts.label] 输入框标签（text 用）
 * @param {string} [opts.value] 输入框初值
 * @param {string} [opts.placeholder]
 * @param {string} [opts.confirmText='确定']
 * @param {string} [opts.cancelText='取消']
 * @param {boolean} [opts.danger] 确认按钮用警示色
 * @param {'text'|'confirm'} opts.kind
 * @returns {Promise<string|null|boolean>} text → 输入值|null；confirm → boolean
 */
function openDialog(opts) {
  if (activeCancel) activeCancel();

  return new Promise((resolve) => {
    const prevFocus = document.activeElement;
    const overlay = document.createElement('div');
    overlay.className = 'dialog-overlay';
    const isText = opts.kind === 'text';
    overlay.innerHTML = `
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
        <h3 class="dialog__title" id="dialog-title"></h3>
        <p class="dialog__message hidden"></p>
        <label class="dialog__label hidden" for="dialog-input"></label>
        <input id="dialog-input" class="dialog__input hidden" type="text" autocomplete="off" spellcheck="false"/>
        <div class="dialog__actions">
          <button type="button" class="btn dialog__cancel"></button>
          <button type="button" class="btn dialog__ok"></button>
        </div>
      </div>`;
    const panel = overlay.firstElementChild;
    const titleEl = overlay.querySelector('.dialog__title');
    const msgEl = overlay.querySelector('.dialog__message');
    const labelEl = overlay.querySelector('.dialog__label');
    const inputEl = overlay.querySelector('.dialog__input');
    const cancelBtn = overlay.querySelector('.dialog__cancel');
    const okBtn = overlay.querySelector('.dialog__ok');

    titleEl.textContent = opts.title || '请确认';
    cancelBtn.textContent = opts.cancelText || '取消';
    okBtn.textContent = opts.confirmText || '确定';
    okBtn.classList.add(opts.danger ? 'btn--danger' : 'btn--primary');
    if (isText) {
      labelEl.textContent = opts.label || '';
      labelEl.classList.remove('hidden');
      inputEl.classList.remove('hidden');
      inputEl.value = opts.value || '';
      inputEl.placeholder = opts.placeholder || '';
    } else {
      msgEl.textContent = opts.message || '';
      msgEl.classList.remove('hidden');
    }

    let settled = false;
    const close = (result) => {
      if (settled) return;
      settled = true;
      activeCancel = null;
      document.removeEventListener('keydown', onKey, true);
      overlay.remove();
      if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
      resolve(result);
    };
    const confirm = () => close(isText ? inputEl.value.trim() : true);
    const cancel = () => close(isText ? null : false);
    activeCancel = cancel;

    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel(); return; }
      if (e.key === 'Enter' && e.target !== cancelBtn) { e.preventDefault(); e.stopPropagation(); confirm(); return; }
      if (e.key === 'Tab') {
        // 焦点圈定：输入框 ↔ 取消 ↔ 确认 循环
        const focusables = isText ? [inputEl, cancelBtn, okBtn] : [cancelBtn, okBtn];
        const idx = focusables.indexOf(document.activeElement);
        e.preventDefault();
        const next = e.shiftKey
          ? focusables[(idx - 1 + focusables.length) % focusables.length]
          : focusables[(idx + 1) % focusables.length];
        next.focus();
      }
    };
    document.addEventListener('keydown', onKey, true);
    overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) cancel(); });
    cancelBtn.addEventListener('click', cancel);
    okBtn.addEventListener('click', confirm);

    document.body.appendChild(overlay);
    if (isText) { inputEl.focus(); inputEl.select(); } else okBtn.focus();
  });
}

/** 文本输入对话框；取消返回 null。 */
export function askText(opts) {
  return openDialog({ ...opts, kind: 'text' });
}

/** 确认对话框；确定 true / 取消 false。 */
export function askConfirm(opts) {
  return openDialog({ ...opts, kind: 'confirm' });
}