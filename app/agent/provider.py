"""Agent Provider 适配器：读取 ``<vault>/.kwey/provider.json``，调用外部 LLM 生成结构化 ops。

零新第三方依赖：HTTP 走标准库 ``urllib.request``。
- ``openai`` 协议：POST ``<endpoint>/chat/completions``（OpenAI Chat Completions 兼容格式）。
- ``http`` 协议：POST ``<endpoint>``，直接发 ``{instruction, context}``。

返回的 ops 为 ``list[dict]``，交给 :mod:`app.agent.ops` 做校验 / 预演 / 应用。

安全：``ProviderError`` 消息绝不回显 ``api_key``；配置损坏时静默回退默认值。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.parser import frontmatter as fm_mod
from app.paths import PathError, is_ignored, normalize_rel, validate_relative

# 缺省配置（字段均可选，缺省给合理默认）
DEFAULTS: Dict[str, Any] = {
    "endpoint": "",              # 空表示未配置任何 Provider
    "api_key": "",
    "model": "gpt-4o-mini",
    "protocol": "openai",        # openai | http
    "timeout": 30.0,
    "max_context_chars": 12000,
}

# OpenAI 协议要求的 system 提示：约束模型只输出 JSON
SYSTEM_PROMPT = '只返回 JSON: {"ops":[...]}'

# 代码围栏：```json ... ``` / ``` ... ```
_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE
)


class ProviderError(Exception):
    """Provider 未配置或调用失败（消息清晰，不暴露 api_key）。"""


def _strip_fence(text: str) -> str:
    """剥离 ```json ... ``` 代码围栏；无围栏时原样去空白返回。"""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped


class AgentProvider:
    """vault 级 Provider 门面。``root`` 为 vault 绝对路径。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.config: Dict[str, Any] = self._load_config()

    # ------------------------------------------------------------------ config
    def _load_config(self) -> Dict[str, Any]:
        """读 ``.kwey/provider.json``，字段可选，缺失 / 损坏均回退默认值。"""
        cfg = dict(DEFAULTS)
        path = self.root / ".kwey" / "provider.json"
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key in DEFAULTS:
                        if key in raw and raw[key] is not None:
                            cfg[key] = raw[key]
            except (json.JSONDecodeError, OSError, ValueError):
                # 损坏配置回退默认值，不把文件内容 / 密钥写进异常
                pass
        # 协议收敛到合法枚举
        if cfg.get("protocol") not in ("openai", "http"):
            cfg["protocol"] = "openai"
        # 数值字段做类型收敛，避免配置里写字符串导致 urlopen 报错
        try:
            cfg["timeout"] = float(cfg["timeout"])
        except (TypeError, ValueError):
            cfg["timeout"] = DEFAULTS["timeout"]
        try:
            cfg["max_context_chars"] = int(cfg["max_context_chars"])
        except (TypeError, ValueError):
            cfg["max_context_chars"] = DEFAULTS["max_context_chars"]
        return cfg

    @property
    def configured(self) -> bool:
        """是否已配置可用 endpoint。"""
        return bool(str(self.config.get("endpoint") or "").strip())

    # ------------------------------------------------------------------ context
    def build_context(
        self,
        vault: Any = None,
        paths: Optional[List[str]] = None,
        max_chars: int = 12000,
    ) -> str:
        """构建上下文字符串：读笔记标题 + 正文，按字符数预算截断。

        Args:
            vault: 可选。传入带 ``paths.root`` 的对象（如 VaultService）时以其为根，
                便于与 ``deps.require_vault()`` 对接；为 None 时用 ``self.root``。
            paths: 指定笔记的 vault 相对路径列表（非法路径跳过）；
                缺省扫描当前库前若干篇 md（排除系统保留目录）。
            max_chars: 上下文最大字符数（token 预算的字符数近似）。
        """
        root = self._resolve_root(vault)
        if paths:
            safe: List[str] = []
            for p in paths:
                try:
                    safe.append(validate_relative(p))
                except PathError:
                    continue  # 拒绝目录穿越 / 保留目录，避免读到 vault 外文件
            rels = safe
        else:
            rels = self._list_notes(root)

        budget = max(0, int(max_chars))
        chunks: List[str] = []
        used = 0
        for rel in rels:
            if not rel or used >= budget:
                break
            doc = self._read_doc(root, rel)
            if doc is None:
                continue
            title, body = doc
            block = f"# {title} ({rel})\n{body}\n"
            remaining = budget - used
            if len(block) > remaining:
                block = block[:remaining]
            chunks.append(block)
            used += len(block)
        return "".join(chunks)

    def _resolve_root(self, vault: Any) -> Path:
        if vault is None:
            return self.root
        # VaultService / 任何带 paths.root 的对象
        vp = getattr(getattr(vault, "paths", None), "root", None)
        if vp:
            return Path(vp)
        if isinstance(vault, (str, Path)):
            return Path(vault)
        return self.root

    @staticmethod
    def _list_notes(root: Path) -> List[str]:
        """扫描 vault 内前若干篇 md（排除系统保留目录），稳定排序。"""
        if not root.is_dir():
            return []
        rels: List[str] = []
        for p in sorted(root.rglob("*"), key=lambda x: x.as_posix().lower()):
            if not p.is_file() or p.suffix.lower() not in (".md", ".markdown"):
                continue
            try:
                rel = normalize_rel(p.relative_to(root).as_posix())
            except ValueError:
                continue
            if is_ignored(rel):
                continue
            rels.append(rel)
            if len(rels) >= 50:  # 「前若干篇」上限，避免超大库扫描过久
                break
        return rels

    @staticmethod
    def _read_doc(root: Path, rel: str) -> Optional[Tuple[str, str]]:
        """读单篇 → ``(title, body)``；不存在 / 坏文件返回 None。"""
        try:
            abs_path = root.joinpath(*rel.split("/"))
            if not abs_path.is_file():
                return None
            raw = abs_path.read_text(encoding="utf-8-sig")
        except OSError:
            return None
        fm, body = fm_mod.parse_frontmatter(raw)
        title = (
            str(fm.get("title") or "").strip()
            or rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        )
        return title, body

    # ------------------------------------------------------------------ run
    def run(self, instruction: str, paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """调用 Provider 生成 ops；未配置 / 网络失败 / 解析失败抛 ``ProviderError``。"""
        if not self.configured:
            raise ProviderError(
                "未配置 Agent Provider：请在 <vault>/.kwey/provider.json 中设置 "
                "endpoint 等字段，或改用请求体直接传入 ops 进行预演 / 应用"
            )
        context = self.build_context(
            paths=paths, max_chars=int(self.config["max_context_chars"])
        )
        protocol = self.config["protocol"]
        if protocol == "openai":
            payload, url = self._openai_payload(instruction, context)
        else:
            payload, url = self._http_payload(instruction, context)
        raw_text = self._post(url, payload, use_bearer=(protocol == "openai"))
        return self._parse_ops(raw_text)

    def _openai_payload(
        self, instruction: str, context: str
    ) -> Tuple[Dict[str, Any], str]:
        """拼 OpenAI Chat Completions 请求；endpoint 已含 /chat/completions 则不重复拼。"""
        endpoint = str(self.config["endpoint"]).rstrip("/")
        url = (
            endpoint
            if endpoint.endswith("/chat/completions")
            else endpoint + "/chat/completions"
        )
        user_content = f"{context}\n\n{instruction}" if context else instruction
        body: Dict[str, Any] = {
            "model": self.config["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        return body, url

    def _http_payload(
        self, instruction: str, context: str
    ) -> Tuple[Dict[str, Any], str]:
        return (
            {"instruction": instruction, "context": context},
            str(self.config["endpoint"]),
        )

    def _post(
        self,
        url: str,
        payload: Dict[str, Any],
        use_bearer: bool,
        timeout: Optional[float] = None,
    ) -> str:
        """用 urllib 发 POST，返回响应体文本；异常统一转 ``ProviderError``。

        ``timeout`` 为空时取配置值；一次性长任务（如主题生成）可单独放宽，
        不必改全局配置。
        """
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if use_bearer:
            key = str(self.config.get("api_key") or "")
            if key:
                headers["Authorization"] = f"Bearer {key}"
        limit = float(self.config["timeout"] if timeout is None else timeout)
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=limit) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # 透出响应体关键信息（如阿里云 "Workspace endpoint access denied"），
            # 否则用户只看到 403 无法判断是 key / 空间 / 模型哪一步出错。
            detail = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")[:400]
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    err = parsed.get("error") or parsed
                    if isinstance(err, dict):
                        detail = str(err.get("message") or err.get("code") or "")[:200]
                if not detail:
                    detail = raw[:160]
            except (ValueError, OSError, AttributeError):
                pass
            hint = ""
            if "workspace" in detail.lower() and "denied" in detail.lower():
                hint = "（专属工作空间 endpoint 与 API Key 归属不一致；改用公网端点 dashscope.aliyuncs.com/compatible-mode/v1 或换成 key 所属空间的 endpoint）"
            raise ProviderError(
                f"Provider 返回 HTTP {exc.code}{('：' + detail) if detail else ''}，请检查 endpoint / model / API Key{hint}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # 超时与真断网要分开报：思考型模型动辄 200s，笼统说「网络不通」
            # 会让用户去查 endpoint，实际只是 timeout 太小。
            reason = getattr(exc, "reason", exc)
            timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
            if not timed_out and "timed out" in str(reason).lower():
                timed_out = True
            if timed_out:
                raise ProviderError(
                    f"Provider 请求超时（{limit:.0f}s 无响应）：可能是模型长思考或 endpoint 慢，"
                    "可换更快的模型，或调大 provider.json 的 timeout"
                ) from None
            raise ProviderError(
                "Provider 网络请求失败，请检查 endpoint 与网络连通性"
            ) from None

    @staticmethod
    def _parse_ops(raw_text: str) -> List[Dict[str, Any]]:
        """解析响应：取 choices[0].message.content 或整体 {"ops":[...]}，剥离代码围栏。"""
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            raise ProviderError("Provider 返回的不是合法 JSON") from None

        content: Any = data
        if isinstance(data, dict) and "choices" in data:
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise ProviderError(
                    "Provider（openai 协议）响应缺少 choices[0].message.content"
                ) from None
            if isinstance(content, str):
                content = _strip_fence(content)
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    raise ProviderError(
                        "Provider 返回的内容（剥离代码围栏后）不是合法 JSON"
                    ) from None

        if isinstance(content, dict):
            ops = content.get("ops")
        elif isinstance(content, list):
            ops = content
        else:
            ops = None
        if not isinstance(ops, list):
            raise ProviderError("Provider 响应中未找到 ops 数组")
        return [op for op in ops if isinstance(op, dict)]

    # ------------------------------------------------------------------ chat（卡B 内核）
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """OpenAI function-calling 聊天补全，返回规范化 assistant 消息。

        返回 ``{"content": str|None, "tool_calls": [{"id","name","arguments":dict}]}``。
        仅支持 openai 协议（http 协议是 ops 专用）；未配置 / 网络 / 解析失败抛
        ``ProviderError``（消息绝不回显 api_key）。``run()`` ops 路径不受影响。

        ``extra_body``：追加厂商扩展字段（如 DashScope 的 ``enable_thinking``），
        不覆盖 ``model`` / ``messages`` 等核心字段。``timeout``：本次调用的超时秒数。
        """
        if not self.configured:
            raise ProviderError(
                "未配置 Agent Provider：请在设置中填写 endpoint 等字段后再使用内置智能体"
            )
        if self.config["protocol"] != "openai":
            raise ProviderError("内置智能体聊天需要 openai 协议的 Provider（当前为 http）")
        endpoint = str(self.config["endpoint"]).rstrip("/")
        url = (
            endpoint
            if endpoint.endswith("/chat/completions")
            else endpoint + "/chat/completions"
        )
        body: Dict[str, Any] = {
            "model": self.config["model"],
            "messages": messages,
            "tool_choice": "auto",
        }
        if tools:
            body["tools"] = tools
        if extra_body:
            for key, value in dict(extra_body).items():
                body.setdefault(key, value)  # 扩展字段不覆盖核心字段
        raw_text = self._post(url, body, use_bearer=True, timeout=timeout)
        return self._parse_chat(raw_text)

    @staticmethod
    def _parse_chat(raw_text: str) -> Dict[str, Any]:
        """解析 Chat Completions 响应 → 规范化 assistant 消息（tool_calls.arguments 转 dict）。"""
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            raise ProviderError("Provider 返回的不是合法 JSON") from None
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(
                "Provider（openai 协议）响应缺少 choices[0].message"
            ) from None
        if not isinstance(msg, dict):
            raise ProviderError("Provider 响应的 message 不是对象")

        content = msg.get("content")
        if content is not None and not isinstance(content, str):
            content = str(content)

        tool_calls: List[Dict[str, Any]] = []
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str) and raw_args.strip():
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raise ProviderError(f"工具 {name} 的 arguments 不是合法 JSON") from None
            else:
                args = {}
            if not isinstance(args, dict):
                args = {}
            tool_calls.append({"id": tc.get("id") or "", "name": name, "arguments": args})
        return {"content": content, "tool_calls": tool_calls}


# --------------------------------------------------------------------- 共用辅助
def thinking_off_body(provider: "AgentProvider") -> Dict[str, Any]:
    """DashScope（qwen3 系）默认开思考，一次结构化生成会先产数万字推理。

    只在阿里云官方兼容端点关闭思考；其它网关不附加未知字段，免得被 400 拒绝。
    """
    from urllib.parse import urlparse

    host = (urlparse(str(provider.config.get("endpoint") or "")).hostname or "").lower()
    return {"enable_thinking": False} if host.endswith("aliyuncs.com") else {}


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠出第一个 JSON 对象（容忍 ``` 围栏与前后废话）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start: end + 1])
        except (ValueError, TypeError):
            return None
    return data if isinstance(data, dict) else None


def chat_json(
    provider: "AgentProvider",
    messages: List[Dict[str, Any]],
    *,
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """chat + 关闭思考 + 400/422 自动去扩展字段重试 + 解析 JSON 对象。

    失败抛 ProviderError（消息可直接展示给用户）。
    """
    extra = thinking_off_body(provider)
    try:
        result = provider.chat(messages, extra_body=extra, timeout=timeout)
    except ProviderError as exc:
        if extra and ("HTTP 400" in str(exc) or "HTTP 422" in str(exc)):
            result = provider.chat(messages, timeout=timeout)
        else:
            raise
    data = extract_json_object(str(result.get("content") or ""))
    if data is None:
        raise ProviderError("模型返回的不是合法 JSON，请换个说法再试")
    return data
