"""LLM 客户端：OpenAI 兼容的 Chat Completions（可选外部依赖）。

通过环境变量配置，**未配置 API Key 即视为未启用**，编排器会自动降级
为离线规则模式，因此本系统在无网络、无密钥的环境下依然完整可用。

    AGENT_LLM_BASE_URL   默认 https://api.deepseek.com/v1
                         （智谱 / 通义千问 / Ollama 等任何 OpenAI 兼容网关同理）
    AGENT_LLM_API_KEY    必填；缺省则禁用 LLM
    AGENT_LLM_MODEL      默认 deepseek-chat
    AGENT_LLM_TIMEOUT    默认 30（秒）

复用已有的 httpx 依赖，不引入任何新的第三方框架。
"""
from __future__ import annotations

import os

import httpx

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 30.0


class LLMError(RuntimeError):
    """LLM 调用失败（网络、鉴权、响应格式等）。"""


class ChatLLM:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_env(cls) -> "ChatLLM | None":
        api_key = os.getenv("AGENT_LLM_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            timeout = float(os.getenv("AGENT_LLM_TIMEOUT", str(DEFAULT_TIMEOUT)))
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        return cls(
            base_url=os.getenv("AGENT_LLM_BASE_URL", DEFAULT_BASE_URL),
            api_key=api_key,
            model=os.getenv("AGENT_LLM_MODEL", DEFAULT_MODEL),
            timeout=timeout,
        )

    @property
    def label(self) -> str:
        return self.model

    # ---------------------------------------------------------------- 调用
    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """返回 assistant 的 message 字典（可能含 tool_calls）。"""
        payload: dict = {"model": self.model, "messages": messages,
                         "temperature": 0}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"LLM 返回 HTTP {exc.response.status_code}："
                f"{exc.response.text[:200]}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"LLM 调用失败：{exc}") from exc

        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常：{str(data)[:200]}") from exc
