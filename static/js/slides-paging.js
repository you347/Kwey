/* P2 幻灯片分页（纯函数、零依赖）：把 Markdown 正文切成演示页数组。
 * 规则按优先级尝试：独立成行的 --- → 二级标题 ## → 一级标题 #，
 * 取第一个能切出 ≥2 页的规则；都不满足则整篇一页。
 * 本文件不触碰任何 DOM / 浏览器 API，便于 node 直接自检。
 */

/** 水平分割线 / frontmatter 边界：整行只有三个及以上短横线。 */
const RULE_LINE = /^---+\s*$/;
/** 二级标题行（不吞掉三级标题）。 */
const H2_LINE = /^##[ \t]+\S/;
/** 一级标题行。 */
const H1_LINE = /^#[ \t]+\S/;
/** 围栏代码块开闭（``` 或 ~~~，允许缩进 3 格）。 */
const FENCE_LINE = /^\s{0,3}(`{3,}|~{3,})/;

/**
 * 剥掉文件开头的 frontmatter 块（开头 --- 到下一个 --- 之间，含两端）。
 * 没有闭合行时按普通正文处理，避免把整篇吃掉。
 */
function stripFrontmatter(markdown) {
  const lines = markdown.split(/\r?\n/);
  if (!lines.length || !RULE_LINE.test(lines[0])) return markdown;
  for (let i = 1; i < lines.length; i += 1) {
    if (RULE_LINE.test(lines[i])) return lines.slice(i + 1).join('\n');
  }
  return markdown;
}

/** 逐行找出可作分页起点的行号；围栏代码块内部一律跳过（代码里的 # 与 --- 不算分页）。 */
function collectCuts(lines, test) {
  const cuts = [];
  let fence = '';        // 当前所在围栏的标记字符（` 或 ~），空串表示不在代码块内
  lines.forEach((line, i) => {
    const fenceHit = line.match(FENCE_LINE);
    if (fenceHit) {
      if (!fence) fence = fenceHit[1][0];
      else if (fenceHit[1][0] === fence) fence = '';
      return;              // 围栏行本身既不是标题也不是分割线
    }
    if (fence) return;
    if (test(line)) cuts.push(i);
  });
  return cuts;
}

/** 按切点行号把行数组切片成多页；skipCutLine=true 时丢弃切点那一行（分割线）。 */
function paginate(lines, cuts, skipCutLine) {
  const pages = [];
  let start = 0;
  for (const cut of cuts) {
    if (cut <= start) continue;   // 首行切点或连续切点：跳过，避免空页
    pages.push(lines.slice(start, cut).join('\n'));
    start = skipCutLine ? cut + 1 : cut;
  }
  pages.push(lines.slice(start).join('\n'));
  return pages.map((page) => page.trim()).filter((page) => page.length > 0);
}

/**
 * 把 Markdown 正文切成演示页。
 * @param {string} markdown 笔记正文
 * @returns {string[]} 每页的 Markdown 原文（不含 frontmatter、不含分页用的 ---）
 */
export function splitSlides(markdown) {
  const text = stripFrontmatter(String(markdown == null ? '' : markdown));
  const lines = text.split(/\r?\n/);
  const rules = [
    { test: (line) => RULE_LINE.test(line), skipCutLine: true },   // ---
    { test: (line) => H2_LINE.test(line), skipCutLine: false },    // ##
    { test: (line) => H1_LINE.test(line), skipCutLine: false },     // #
  ];
  for (const rule of rules) {
    const pages = paginate(lines, collectCuts(lines, rule.test), rule.skipCutLine);
    if (pages.length >= 2) return pages;
  }
  const only = text.trim();
  return only ? [only] : [];
}
