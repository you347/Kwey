"""P2 自定义主题 API：用户 CSS 存 ``<vault>/.kwey/custom.css``。

- ``GET  /api/theme/custom``：返回 ``{css, enabled}``；未写过时 css 为空。
- ``PUT  /api/theme/custom``：body ``{css, enabled}``，原子写 CSS 并把开关写入
  ``AppSettings.custom_css_enabled``（settings 字段由集成卡补到 models/config）。
- ``DELETE /api/theme/custom``：删除 CSS 并禁用。

安全约定：
- 路径固定为 vault 根下 ``.kwey/custom.css``，请求体不接受任何路径字段，杜绝穿越。
- ``.kwey/`` 是索引忽略的系统目录，不是用户笔记；此处用专用原子写，绝不经过
  VaultService 写用户笔记的路径校验（VaultService 本就拒绝保留目录）。
- 基本 CSS 过滤：禁 ``@import`` / ``url(http`` / ``expression(`` / ``javascript:``，
  命中返回 400，阻止外链与脚本式注入。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.runtime import resource_root

router = APIRouter(prefix="/api/theme", tags=["theme"])

CSS_DIRNAME = ".kwey"
CSS_FILENAME = "custom.css"
ENABLED_SETTING_KEY = "custom_css_enabled"
MAX_CSS_CHARS = 200_000
# 主题生成是一次性长任务：思考型模型可能先跑几十秒推理，30s 必超时
GENERATE_TIMEOUT = 180.0

# 外链 / 脚本式 CSS 片段；大小写不敏感，容忍 url( 'http 这类空白与引号
_FORBIDDEN_RE = re.compile(
    r"@import\b"
    r"|url\s*\(\s*['\"]?\s*https?://"
    r"|url\s*\(\s*['\"]?\s*//"
    r"|expression\s*\("
    r"|javascript\s*:",
    re.IGNORECASE,
)


class CustomThemeRequest(BaseModel):
    """自定义主题写入请求：css 为完整样式文本，enabled 控制是否注入。"""

    css: str = ""
    enabled: bool = False


class ThemeGenerateRequest(BaseModel):
    """Agent 生成 CSS 请求：request 为自然语言需求，可选基于当前 CSS 修改。"""

    request: str
    include_current: bool = True


# 主题可覆盖的 CSS 变量（与 themes.css 保持一致，供 Agent 生成时参考）
THEME_VARS = [
    "--accent", "--accent-hover", "--accent-faint", "--accent-text",
    "--bg", "--bg-alt", "--bg-panel", "--bg-hover",
    "--text", "--text-dim", "--text-secondary",
    "--border", "--border-faint", "--border-strong",
    "--radius-sm", "--radius-md", "--radius-lg",
    "--fs-xs", "--fs-sm", "--fs-md", "--fs-lg",
    "--shadow", "--shadow-soft", "--tag", "--tag-bg", "--mark",
    "--success", "--broken", "--code-bg", "--scrollbar",
]

# 真实 DOM 选择器速查：与 static/css 下的实际类名逐条对齐。
# 没有这份清单时模型会臆造 .agent-reply / .assistant 之类不存在的类名，
# 生成的 CSS 语法合法但匹配不到任何元素（表现为「已保存却不生效」）。
SELECTOR_MAP = (
    "骨架：:root / .titlebar / .sidebar / .ribbon / .panel / .panel-tabs / "
    ".panel-view / .resizer / .rightbar / .statusbar / .tabbar / .tab / .tab--active\n"
    "文件树与列表：.filetree / .tree-row / .tree-row--active / .panel-list\n"
    "编辑器：.editor-container / .editor-pane / .editor-main / .editor-toolbar / "
    ".cm-s-kwey（CodeMirror 源码模式）/ .editor-textarea（降级文本框）/ "
    ".preview-pane / .markdown-body\n"
    "内置聊天（面板容器 #panel-chat）：消息行 .chat-msg，修饰类 .chat-msg--user / "
    ".chat-msg--assistant / .chat-msg--error；气泡 .chat-msg__bubble，助手 Markdown "
    "渲染后追加 .chat-markdown；"
    "工具步骤 .chat-step / .chat-step--ok / .chat-step--err / .chat-step--wait；"
    "输入区 .chat-input / 文本框 #chat-input / 按钮行 .chat-input__actions / "
    "发送按钮 #btn-chat-send（**文字按钮，无图标 svg**）/ 停止 #btn-chat-stop / "
    "清空 #btn-chat-clear\n"
    "弹层与提示：.dialog / .overlay / .toast / .toast--error / .toast--success"
)

_GENERATE_SYSTEM = (
    "你是 Kwey 笔记软件的 CSS 主题助手。用户用自然语言描述想要的视觉风格，"
    "你只输出 CSS 规则，不要任何解释、不要 Markdown 代码围栏。\n"
    "规则：\n"
    "1. 优先在 :root 中覆盖这些变量：" + " ".join(THEME_VARS) + "；\n"
    "2. 若需求涉及具体组件（聊天区、编辑器、侧栏、状态栏等），只能使用下面清单里的"
    "选择器原样组合，禁止臆造不存在的类名（例如 .agent-reply / .assistant / .message "
    "都不存在）；\n"
    "3. 清单里没有的选择器一律不要写：改不了就用变量或直接放弃该项；\n"
    "4. 定位单个按钮 / 输入框时用 #id，不要写 button、.btn 这类会波及全局的标签选择器；\n"
    "5. 要改「形状」（爱心、圆形、五角星等）时用 CSS 绘制（伪元素 + border-radius / "
    "transform / clip-path），不要把 emoji 字符当成图形；按钮原文字用 font-size: 0 隐藏，"
    "不要再加 overflow: hidden（会把画出来的形状裁掉）；\n"
    "   心形要点：两块圆角矩形各占一半宽度，分别贴住左 / 右半边，绕**底边外侧角**旋转"
    "（transform-origin: 0 100% 与 100% 100%）；两块都居中再 rotate(±45deg) 会得蝴蝶结；\n"
    "6. 自定义变量必须有规则真正引用（:root 里定义了 --x 却没有任何规则用 var(--x)，"
    "等于没写，不会产生任何效果）；\n"
    "7. 内置规则有用 ID 选择器（如 #panel-chat .chat-markdown code）的，"
    "覆盖时加 !important；\n"
    "8. 严禁 @import、url(http、expression(、javascript:，不要引入外部字体或图片；\n"
    "9. 只输出最终 CSS 文本，不要输出思考过程。\n\n"
    "【真实 DOM 选择器速查】\n" + SELECTOR_MAP + "\n\n"
    "【示例 1】用户说「右侧聊天区 agent 回复字体改成蓝色」，正确输出：\n"
    ".chat-msg--assistant .chat-msg__bubble { color: #1e88ff; }\n\n"
    "【示例 2】用户说「发送按钮改成红色爱心」，正确输出：\n"
    "#btn-chat-send { position: relative; width: 24px; height: 22px; padding: 0;\n"
    "  background: transparent; border: 0; font-size: 0; cursor: pointer; }\n"
    "#btn-chat-send::before, #btn-chat-send::after { content: \"\"; position: absolute;\n"
    "  top: 2px; width: 11px; height: 17px; background: #e5484d;\n"
    "  border-radius: 11px 11px 0 0; }\n"
    "#btn-chat-send::before { left: 11px; transform: rotate(-45deg);\n"
    "  transform-origin: 0 100%; }\n"
    "#btn-chat-send::after { left: 0; transform: rotate(45deg);\n"
    "  transform-origin: 100% 100%; }\n"
)


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _thinking_off_body(provider: Any) -> Dict[str, Any]:
    """DashScope（qwen3 系）默认开思考：一次主题生成会先产数万字推理（实测 208s），
    30s 超时必然失败。只在阿里云官方兼容端点关闭思考；其它网关不附加未知字段，
    免得被 400 拒绝（调用方另有「去掉扩展字段重试一次」的兜底）。"""
    host = (urlparse(str(provider.config.get("endpoint") or "")).hostname or "").lower()
    if host.endswith("aliyuncs.com"):
        return {"enable_thinking": False}
    return {}


_CSS_DIR = resource_root() / "static" / "css"
_TOKEN_RE = re.compile(r"([.#])([A-Za-z_][\w-]*)")
_HEX_RE = re.compile(r"^(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_known_cache: Dict[str, Any] | None = None


def known_selectors() -> Dict[str, Any]:
    """扫内置样式表，汇总出现过的类名与 ID（进程内缓存）。

    用途：设置页据此提示「用户 CSS 里写了样式表根本没有的类名」——这类选择器
    语法合法、页面却永远匹配不到，是「保存了却不生效」的典型原因。
    """
    global _known_cache
    if _known_cache is not None:
        return _known_cache
    classes: set[str] = set()
    ids: set[str] = set()
    for path in sorted(_CSS_DIR.glob("*.css")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for kind, name in _TOKEN_RE.findall(text):
            if kind == ".":
                classes.add(name)
            elif not _HEX_RE.match(name):  # 排除 #fff 这类颜色值误判成 ID
                ids.add(name)
    _known_cache = {"classes": sorted(classes), "ids": sorted(ids)}
    return _known_cache


@router.get("/known-selectors")
async def get_known_selectors():
    """内置样式表的类名 / ID 清单（只读，供前端校验自定义 CSS）。"""
    return {"code": 0, "data": known_selectors(), "message": "ok"}


def _css_path() -> Path:
    """返回唯一允许的写入目标；路径固定，不接受调用方输入。"""
    root = Path(deps.require_vault().paths.ensure_root()).resolve()
    path = root / CSS_DIRNAME / CSS_FILENAME
    # 双重校验：即使将来有人改动常量，也不会写出 .kwey 目录
    if path.parent != (root / CSS_DIRNAME) or path.name != CSS_FILENAME:
        raise _bad("自定义主题路径非法")
    if path.is_symlink():
        raise _bad("自定义主题文件是符号链接，拒绝写入")
    return path


def _atomic_write(path: Path, text: str) -> None:
    """UTF-8 原子写：临时文件 + replace，失败不留下半截 CSS。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read_css(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise _bad(f"读取自定义主题失败: {exc}") from exc


def _state(css: str) -> Dict[str, Any]:
    """开关取 settings；旧库未写该字段时，有 CSS 内容即视为启用。"""
    config = deps.get_config()
    raw = config.get(ENABLED_SETTING_KEY) if config is not None else None
    enabled = bool(css.strip()) if raw is None else bool(raw)
    return {"css": css, "enabled": enabled}


def _validate_css(css: str) -> None:
    """拦截外链 / 脚本式片段；返回 400 而不落盘。"""
    if not isinstance(css, str):
        raise _bad("css 必须是字符串")
    if len(css) > MAX_CSS_CHARS:
        raise _bad(f"css 过长（上限 {MAX_CSS_CHARS} 字符）")
    if _FORBIDDEN_RE.search(css):
        raise _bad(
            "自定义 CSS 含被禁止的外链或脚本片段"
            "（@import / url(http / expression( / javascript:）"
        )


@router.get("/custom")
async def get_custom_theme():
    """读取当前自定义 CSS 与启用状态（只读）。"""
    return {"code": 0, "data": _state(_read_css(_css_path())), "message": "ok"}


@router.put("/custom")
async def put_custom_theme(req: CustomThemeRequest):
    """过滤后原子写 CSS，并持久化 enabled 开关。"""
    _validate_css(req.css)
    path = _css_path()
    _atomic_write(path, req.css)
    config = deps.require_config()
    config.set({ENABLED_SETTING_KEY: bool(req.enabled)})
    return {"code": 0, "data": _state(req.css), "message": "ok"}


@router.delete("/custom")
async def delete_custom_theme():
    """删除 CSS 文件并关闭自定义主题（文件不存在也返回成功）。"""
    path = _css_path()
    path.unlink(missing_ok=True)
    config = deps.require_config()
    config.set({ENABLED_SETTING_KEY: False})
    return {"code": 0, "data": {"css": "", "enabled": False}, "message": "ok"}


@router.post("/generate")
async def generate_theme(req: ThemeGenerateRequest):
    """用已配置的 Provider 按自然语言需求生成 CSS（只返回，不落盘）。"""
    from app.agent.provider import AgentProvider, ProviderError

    want = (req.request or "").strip()
    if not want:
        raise _bad("请描述想要的样式，例如『把强调色改成暖橙色，圆角大一点』")
    if len(want) > 2000:
        raise _bad("需求描述过长（上限 2000 字符）")

    vault = deps.require_vault()
    provider = AgentProvider(vault.paths.root)
    if not provider.configured:
        raise _bad("未配置 Agent Provider：请先在设置页填写 endpoint 与 API Key，再使用 AI 生成样式")
    if str(provider.config.get("protocol") or "openai") != "openai":
        raise _bad("AI 生成样式需要 openai 协议的 Provider（当前为 http）")

    current = _read_css(_css_path()) if req.include_current else ""
    user_content = (
        f"【当前 CSS】\n{current or '(空)'}\n\n【用户需求】\n{want}\n\n"
        "请输出完整、可直接使用的 CSS（若基于当前 CSS 修改，保留未涉及的规则）。"
    )
    messages = [
        {"role": "system", "content": _GENERATE_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    extra = _thinking_off_body(provider)
    try:
        result = provider.chat(messages, extra_body=extra, timeout=GENERATE_TIMEOUT)
    except ProviderError as exc:
        # 网关不认识 enable_thinking 会回 400/422；去掉扩展字段重试一次，别让用户背锅
        if extra and ("HTTP 400" in str(exc) or "HTTP 422" in str(exc)):
            try:
                result = provider.chat(messages, timeout=GENERATE_TIMEOUT)
            except ProviderError as retry_exc:
                raise _bad(f"AI 生成失败：{retry_exc}") from None
        else:
            raise _bad(f"AI 生成失败：{exc}") from None

    css = str(result.get("content") or "").strip()
    # 剥离模型可能带出的 Markdown 围栏
    if css.startswith("```"):
        css = re.sub(r"^```(?:css)?\s*", "", css)
        css = re.sub(r"\s*```$", "", css)
    # 复用同一套安全过滤：AI 输出同样不许外链/脚本
    _validate_css(css)
    return {"code": 0, "data": {"css": css, "request": want}, "message": "ok"}
