/* 设置页 schema —— 纯数据：分组 → 卡片 → 行。
 * 约束：不 import 任何 DOM / API 模块，不产生副作用；行为一律用字符串 action 交给 index.js。
 * id 沿用旧设置页 DOM 契约（agentlink / vectorsettings / autopush / hotkeys 直接 $('#id') 绑定），改名即破坏。
 *
 * 行字段速查：
 *   type       switch | select | slider | text | path | secret | action | link | custom
 *   id         控件 DOM id（契约，不可改）
 *   label      行标签（左列，超长省略 + title）
 *   setting    绑定的 settings 字段（仅本模块负责回填的字段才声明，避免与外部模块抢写）
 *   fallback   空值兜底
 *   action     index.js HANDLERS 的键
 *   hint       {id?, text?} 行下方说明 / 状态行（id 供外部模块写状态）
 *   full       整行控件（无左列标签）
 *   note       卡片级脚注（纯说明文字，非控件）
 */

/** 与 app/server.py 的 FastAPI(version=...) 对齐；仅用于「关于」卡展示。 */
export const APP_VERSION = '0.2.0';

export const GROUPS = [
  {
    id: 'general',
    title: '常规',
    icon: 'i-sliders',
    desc: '外观、自动保存与快捷键',
    cards: [
      {
        title: '外观',
        rows: [
          {
            type: 'select', id: 'settings-theme', label: '主题',
            setting: 'theme', fallback: 'light', action: 'theme.change',
            options: [['light', '亮色'], ['dark', '暗色']],
          },
          {
            type: 'slider', id: 'settings-autosave', label: '自动保存延迟',
            setting: 'autosave_delay_ms', fallback: 800, action: 'autosave.change',
            min: 300, max: 2400, step: 100, unit: 'ms', valueId: 'settings-autosave-value',
          },
          {
            type: 'link', id: 'link-to-hotkeys', label: '快捷键设置',
            meta: '命令面板与全局按键', action: 'goto.hotkeys',
          },
        ],
      },
      {
        title: '字体',
        desc: '按区域独立调节；拖动即时预览，松手保存',
        note: '全局字号影响按钮、面板与标签；左侧栏、编辑预览、智能体聊天可各自覆盖。',
        rows: [
          {
            type: 'slider', id: 'font-ui', label: '全局字号',
            setting: 'fonts.ui', fallback: 13, action: 'fonts.change',
            min: 11, max: 18, step: 1, unit: 'px', valueId: 'font-ui-value', liveVar: '--font-ui',
          },
          {
            type: 'slider', id: 'font-sidebar', label: '左侧栏字号',
            setting: 'fonts.sidebar', fallback: 13, action: 'fonts.change',
            min: 11, max: 18, step: 1, unit: 'px', valueId: 'font-sidebar-value', liveVar: '--font-sidebar',
          },
          {
            type: 'slider', id: 'font-editor', label: '编辑与预览字号',
            setting: 'fonts.editor', fallback: 15, action: 'fonts.change',
            min: 12, max: 22, step: 1, unit: 'px', valueId: 'font-editor-value', liveVar: '--font-editor',
          },
          {
            type: 'slider', id: 'font-chat', label: '聊天字号',
            setting: 'fonts.chat', fallback: 13, action: 'fonts.change',
            min: 11, max: 18, step: 1, unit: 'px', valueId: 'font-chat-value', liveVar: '--font-chat',
          },
          {
            type: 'action',
            buttons: [{ id: 'btn-fonts-reset', text: '恢复默认', variant: 'ghost', action: 'fonts.reset' }],
          },
        ],
      },
      {
        title: '快捷键',
        desc: '点击组合键后按下新按键；与其它命令冲突时会提示',
        note: '录制中按 Esc 取消；请使用带 Ctrl / Alt 的组合，避免影响正常输入。',
        rows: [
          { type: 'custom', id: 'hotkeys-list', full: true, className: 'hotkeys-list' },
        ],
      },
    ],
  },
  {
    id: 'files',
    title: '文件与目录',
    icon: 'i-folder',
    desc: '附件、模板与每日笔记的落盘位置',
    cards: [
      {
        title: '目录约定',
        desc: '相对 vault 根目录，留空则回到默认值',
        rows: [
          {
            type: 'text', id: 'settings-attachment-dir', label: '附件目录',
            setting: 'attachment_dir', fallback: 'attachments', placeholder: 'attachments',
            action: 'dir.change',
          },
          {
            type: 'text', id: 'settings-template-dir', label: '模板目录',
            setting: 'template_dir', fallback: 'templates', placeholder: 'templates',
            action: 'dir.change',
          },
          {
            type: 'text', id: 'settings-daily-dir', label: '每日笔记目录',
            setting: 'daily_dir', fallback: 'daily', placeholder: 'daily',
            action: 'dir.change',
          },
        ],
      },
      {
        title: '当前 Vault',
        desc: '磁盘 Markdown 文件夹即唯一事实源，索引可整库重建',
        rows: [
          {
            type: 'custom', label: 'Vault 路径', mount: 'vault-row',
            hint: { id: 'settings-vault-info', text: '正在读取 vault 信息…' },
          },
        ],
      },
      {
        title: '导入已有笔记库',
        id: 'import-section',
        desc: '从 Obsidian / Logseq / 任意 Markdown 文件夹迁入，保留目录结构与附件',
        note: '先扫描预览（只读，不写盘），确认后再导入；源库不会被修改。',
        rows: [
          {
            type: 'path', id: 'import-source', label: '源目录',
            placeholder: 'D:/我的知识库', pickerId: 'import-pick', pickerText: '选择',
            hint: { id: 'import-hint', text: '选择 vault 根目录（含 .obsidian 的那一层）' },
          },
          {
            type: 'select', id: 'import-conflict', label: '同名冲突',
            options: [
              ['rename', '自动改名（保留两者）'],
              ['skip', '跳过已有'],
              ['overwrite', '覆盖已有'],
            ],
          },
          { type: 'switch', id: 'import-attachments', label: '同时导入附件', checked: true },
          {
            type: 'action',
            hint: { id: 'import-status', text: '' },
            buttons: [
              { id: 'import-scan', text: '扫描预览', variant: 'primary' },
              { id: 'import-apply', text: '开始导入' },
            ],
          },
        ],
      },
    ],
  },
  {
    id: 'agent',
    title: 'Agent Link',
    icon: 'i-link',
    desc: 'MCP 接入、写盘快照与访问令牌',
    cards: [
      {
        title: 'MCP Server',
        desc: '任意支持 MCP 的客户端都可以 stdio 方式接入',
        note: 'REST / SSE 端点需请求头 X-Kwey-Token；整理预览与应用为本地 UI 端点，必须显式确认才落盘。',
        rows: [
          { type: 'custom', full: true, mount: 'mcp-cmd' },
        ],
      },
      {
        title: 'git 快照',
        desc: '每次写盘自动提交，可在快照时间线回滚',
        rows: [
          {
            type: 'switch', id: 'settings-snapshots-enabled', label: '启用写盘快照',
            hint: { id: 'agent-snapshot-status', text: '快照状态读取中…' },
          },
        ],
      },
      {
        title: '访问令牌',
        desc: '供 Agent 以 HTTP 方式读写 vault，权限三级：只读 / 读写 / 管理',
        note: 'token 仅保存在本机浏览器；撤销后使用它的 Agent 会立即失去访问权限。',
        rows: [
          { type: 'custom', label: '当前 Token', mount: 'token-issue' },
          { type: 'custom', id: 'agent-token-list', full: true, className: 'agent-token-list' },
        ],
      },
    ],
  },
  {
    id: 'provider',
    title: '模型接入',
    icon: 'i-spark',
    desc: 'OpenAI 兼容端点，内置「助手」聊天使用该配置',
    cards: [
      {
        title: 'Provider',
        desc: '配置写入 .kwey/provider.json',
        rows: [
          {
            type: 'text', id: 'agent-provider-endpoint', label: 'Endpoint',
            placeholder: 'https://api.example.com/v1',
          },
          { type: 'text', id: 'agent-provider-model', label: '模型', placeholder: 'gpt-4o-mini' },
          {
            type: 'select', id: 'agent-provider-protocol', label: '协议',
            options: [['openai', 'openai'], ['http', 'http']],
          },
          {
            type: 'secret', id: 'agent-provider-key', label: 'API Key',
            placeholder: '粘贴 API Key（支持长 key，可换行查看完整内容）',
            toggleId: 'btn-agent-key-toggle', saveId: 'btn-agent-provider-save', saveText: '保存配置',
            hint: { text: '保存后前端只保留掩码，不再回显完整密钥。' },
          },
        ],
      },
    ],
  },
  {
    id: 'search',
    title: '检索与向量',
    icon: 'i-search',
    desc: 'FTS5 全文 + 向量 + 链接图，经 RRF 融合排序',
    cards: [
      {
        title: '语义检索',
        id: 'vector-section',
        desc: '本地 ONNX 模型，离线可用；不再依赖 chromadb',
        note: '自动模式优先用本地模型（bge-small-zh，24MB，首次点「下载模型」）；未就绪时若有已配置的 Provider，可改用 API embedding；最后才用哈希词向量兜底。向量库在 .kwey/vector.db，可随时重建。',
        rows: [
          {
            type: 'switch', id: 'vector-enabled', label: '启用向量',
            hint: { id: 'vector-status', text: '状态读取中…' },
          },
          {
            type: 'select', id: 'vector-backend', label: '向量后端',
            options: [
              ['auto', '自动（优先 sqlite-vec）'],
              ['sqlite-vec', 'sqlite-vec（推荐）'],
              ['chroma', 'Chroma（老配置）'],
              ['null', '关闭后端'],
            ],
          },
          {
            type: 'select', id: 'vector-embedder', label: 'Embedding 来源',
            options: [
              ['auto', '自动（本地 → API → 哈希）'],
              ['local', '仅本地 ONNX 模型'],
              ['api', '仅 API embedding'],
              ['hashing', '仅哈希词向量（零依赖）'],
            ],
          },
          {
            type: 'text', id: 'vector-model', label: '本地模型',
            placeholder: 'bge-small-zh-v1.5',
            hint: { id: 'vector-model-status', text: '模型状态读取中…' },
          },
          {
            type: 'text', id: 'vector-api-model', label: 'API 模型',
            placeholder: '留空 = 不用 API（如 text-embedding-v3）',
          },
          { type: 'text', id: 'vector-weight', label: '向量权重', inputType: 'number', placeholder: '1.0' },
          { type: 'text', id: 'vector-graph-weight', label: '图权重', inputType: 'number', placeholder: '0.5' },
          { type: 'text', id: 'vector-rrf-k', label: 'RRF k', inputType: 'number', placeholder: '60' },
          {
            type: 'action',
            buttons: [
              { id: 'btn-vector-download', text: '下载模型', title: '下载 24MB 量化模型到用户目录' },
              { id: 'btn-vector-save', text: '保存配置', variant: 'primary' },
              { id: 'btn-vector-rebuild', text: '重建索引', title: '后台重建，进度见上方状态行' },
            ],
          },
          {
            type: 'action', label: '语义试查',
            buttons: [{ id: 'btn-vector-test', text: '试查一句话' }],
            hint: { id: 'vector-test-result', text: '用一句话验证「换个说法也能搜到」' },
          },
        ],
      },
    ],
  },
  {
    id: 'sync',
    title: '同步',
    icon: 'i-clock',
    desc: '每日定时把 vault 推送到 Git 远端',
    cards: [
      {
        title: '每日自动推送',
        id: 'autopush-section',
        desc: '应用未运行时下次启动自动补推；push 前先 pull --rebase',
        note: '冲突时暂停等待手动处理；地址写入仓库 .git/config，凭据交给系统 git credential helper，此处不采集 token。',
        rows: [
          {
            type: 'text', id: 'autopush-remote-url', label: '仓库地址',
            placeholder: 'git@gitee.com:you/vault.git',
            hint: { id: 'autopush-bind-hint', text: '正在读取绑定状态…' },
          },
          {
            type: 'action',
            buttons: [{ id: 'autopush-bind', text: '绑定仓库', variant: 'primary' }],
          },
          { type: 'switch', id: 'autopush-enabled', label: '启用每日推送' },
          {
            type: 'select', id: 'autopush-remote', label: '远端',
            options: [['origin', 'origin']],
          },
          { type: 'text', id: 'autopush-branch', label: '分支', placeholder: '留空 = 当前分支' },
          { type: 'text', id: 'autopush-time', label: '推送时间', inputType: 'time', placeholder: '22:00' },
          {
            type: 'text', id: 'autopush-commit-template', label: '提交信息模板',
            placeholder: 'chore: 自动同步 {date}',
          },
          {
            type: 'text', id: 'autopush-include-dirs', label: '包含目录',
            placeholder: '逗号分隔，留空=整个 vault',
          },
          {
            type: 'action',
            hint: { id: 'autopush-status', text: '状态读取中…' },
            buttons: [
              { id: 'autopush-save', text: '保存配置', variant: 'primary' },
              { id: 'autopush-run', text: '立即推送' },
            ],
          },
        ],
      },
    ],
  },
  {
    id: 'extension',
    title: '扩展与外观',
    icon: 'i-folder-open',
    desc: '插件市场与自定义 CSS 主题',
    cards: [],
  },
  {
    id: 'about',
    title: '关于',
    icon: 'i-file',
    desc: '版本信息与项目文档',
    cards: [
      {
        title: 'Kwey',
        desc: '本地优先、Agent 原生的个人知识库',
        note: '磁盘 Markdown 是唯一事实源；索引损坏时删除 .kwey/index.db 重开即自动重建。',
        rows: [
          { type: 'custom', full: true, mount: 'about-meta' },
          {
            type: 'action', label: '版本更新',
            buttons: [{ id: 'btn-update-check', text: '检查更新' }],
            hint: { id: 'update-status', text: `当前 v${APP_VERSION}` },
          },
          {
            type: 'link', label: '项目文档', meta: 'docs/ARCHITECTURE.md',
            action: 'about.copy-doc',
          },
        ],
      },
    ],
  },
];

/** 按 id 取分组（找不到时回落到第一组）。 */
export function groupById(id) {
  return GROUPS.find((g) => g.id === id) || GROUPS[0];
}
