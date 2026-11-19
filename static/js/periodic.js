/* 周期笔记（C2 / P2）：ISO 周笔记 + 月度笔记。

命令面板入口 periodic.week / periodic.month 在本模块加载时自注册，
集成卡只需在 app.js 的 import 区加一行 `import './periodic.js';` 即可接线。
笔记生成、模板占位符替换全部由后端 /api/periodic/* 完成（统一写路径），
前端只负责：调用 → 新建时刷新文件树 → emit('open-note') 打开。无浮层 DOM。
 */

import { api, reportError } from './api.js';
import { emit } from './state.js';
import { toast } from './utils.js';
import { registerCommand } from './palette.js';

/** 打开周期笔记：kind=week|month；不存在时后端自动创建并套模板（幂等）。 */
async function openPeriodic(kind, label) {
  try {
    const data = await api.get(`/api/periodic/${kind}`);
    if (data && !data.exists) {
      emit('tree-refresh');  // 新建后刷新文件树，与 daily/模板套用行为一致
      toast(`已创建${label}：${data.path}`, 'success');
    }
    emit('open-note', data.path);
    return data;
  } catch (err) {
    reportError(err);
    return null;
  }
}

/** 打开/创建本周笔记（ISO 周，周一起；跨年按 ISO 周名归属）。 */
export async function openWeeklyNote() {
  return openPeriodic('week', '本周笔记');
}

/** 打开/创建本月笔记。 */
export async function openMonthlyNote() {
  return openPeriodic('month', '本月笔记');
}

// 自注册：模块加载即写入命令注册表（与 app.js 的 palette.registerCommand 同源同表）
registerCommand({ id: 'periodic.week', title: '打开/创建本周笔记', run: () => openWeeklyNote() });
registerCommand({ id: 'periodic.month', title: '打开/创建本月笔记', run: () => openMonthlyNote() });
