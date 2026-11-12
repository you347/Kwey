/* 附件：粘贴/拖入图片 → 上传到 attachments/ → 光标处插入相对链接（P0-12）。 */

import { api, reportError } from './api.js';
import { insertText } from './editor.js';
import { toast } from './utils.js';

/** 绑定到编辑器容器（粘贴 + 拖放）。 */
export function bindAttachments(el) {
  el.addEventListener('paste', async (e) => {
    const items = Array.from((e.clipboardData && e.clipboardData.items) || []);
    const imageItem = items.find((i) => i.type && i.type.startsWith('image/'));
    if (!imageItem) return;
    e.preventDefault();
    const file = imageItem.getAsFile();
    if (!file) return;
    const name = file.name && file.name !== 'image.png' ? file.name : `粘贴图片_${Date.now()}.png`;
    await uploadAndInsert(file, name);
  });

  el.addEventListener('dragover', (e) => { e.preventDefault(); });

  el.addEventListener('drop', async (e) => {
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || [])
      .filter((f) => f.type && f.type.startsWith('image/'));
    if (!files.length) return;
    e.preventDefault();
    for (const f of files) await uploadAndInsert(f, f.name);
  });
}

async function uploadAndInsert(file, displayName) {
  try {
    const renamed = new File([file], displayName, { type: file.type || 'image/png' });
    const data = await api.upload(renamed);
    insertText(`![](${data.path})\n`);
    toast(`附件已保存: ${data.path}`, 'success');
  } catch (err) {
    reportError(err);
  }
}
