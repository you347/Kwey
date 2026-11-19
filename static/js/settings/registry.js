/* 动态区块注册表：给插件市场 / 自定义 CSS 这类运行期模块用的设置卡插槽。
 * registerSection 可重复调用（同 id 覆盖，用于热更新）；注册后立即通知渲染层挂载，
 * 因此调用方必须在注册**之后**再拿 el 内的节点引用（mount 里已经赋值）。
 */

const sections = [];
let listener = null;

/**
 * @param {object} def
 * @param {string} def.id        唯一标识（重复注册视为覆盖）
 * @param {string} def.group     归属分组 id（见 schema.js GROUPS）
 * @param {string} def.domId     卡片 DOM id（旧契约，如 plugins-section）
 * @param {number} def.order     组内排序，小的在前
 * @param {string} def.title     卡片标题
 * @param {string} def.description 卡片说明
 * @param {(el: HTMLElement) => void} def.mount 挂载：往 el（卡片正文）里写内容
 * @param {() => any} def.refresh 每次打开设置时刷新数据
 */
export function registerSection(def) {
  if (!def || !def.id || !def.group) throw new Error('registerSection 需要 id 与 group');
  const next = { order: 100, ...def };
  const idx = sections.findIndex((s) => s.id === def.id);
  if (idx >= 0) sections[idx] = { ...sections[idx], ...next };
  else sections.push(next);
  sections.sort((a, b) => (a.order || 0) - (b.order || 0));
  if (listener) listener(def.group);
  return sections.find((s) => s.id === def.id);
}

export function sectionsFor(group) {
  return sections.filter((s) => s.group === group);
}

/** 刷新已注册模块的数据（打开设置 / vault 切换时调用）；单模块失败不影响其它。 */
export function refreshSections(group) {
  const list = group ? sectionsFor(group) : sections.slice();
  list.forEach((sec) => {
    try {
      const ret = sec.refresh?.();
      if (ret && typeof ret.catch === 'function') ret.catch(() => {});
    } catch (err) {
      console.error('[kwey] 设置区块刷新失败', sec.id, err);
    }
  });
}

/** 渲染层订阅注册变化（同一时刻只有一个渲染层）。 */
export function onSectionsChanged(fn) {
  listener = fn;
}
