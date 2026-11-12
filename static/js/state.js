/* 前端状态：打开标签栈、激活标签、设置、事件订阅表。 */

export const state = {
  /** 打开的标签 [{path,title}]，顺序即展示顺序 */
  tabs: [],
  /** 当前激活标签的 vault 相对路径 */
  activePath: null,
  /** /api/settings 全量设置 */
  settings: {},
  /** 当前激活笔记文档缓存 {path,title,content,frontmatter,mtime} */
  doc: null,
  /** 编辑器模式 source|preview|split */
  editorMode: 'split',
  /** 是否有未保存改动 */
  dirty: false,
  /** 事件订阅表：event -> Set<fn> */
  handlers: new Map(),
};

/** 订阅内部事件。 */
export function on(event, fn) {
  if (!state.handlers.has(event)) state.handlers.set(event, new Set());
  state.handlers.get(event).add(fn);
}

/** 取消订阅。 */
export function off(event, fn) {
  const set = state.handlers.get(event);
  if (set) set.delete(fn);
}

/** 派发内部事件（所有订阅者，异常互不影响）。 */
export function emit(event, data) {
  const set = state.handlers.get(event);
  if (!set) return;
  for (const fn of Array.from(set)) {
    try { fn(data); } catch (err) { console.error(`[kwey] 事件处理失败: ${event}`, err); }
  }
}
