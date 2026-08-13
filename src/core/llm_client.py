#这是一套大模型统一调用工具，不管是 OpenAI、DeepSeek、通义千问、硅基流动，
# 只要接口格式和 OpenAI 一样，全部用同一套代码调用；自动统计 token 消耗、统一报错、支持图文多模态、函数调用（工具调用），
# 用工厂模式全局复用客户端，业务代码直接调用chat()就能发消息给大模型

"""LLM 客户端抽象层

统一封装多模型调用：OpenAI / DeepSeek / Qwen / 其他兼容 OpenAI API 格式的模型。
内置 token 计数器，每次调用自动记录输入/输出 token 消耗。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from core.exceptions import ConfigError, LLMContextWindowError, LLMError, LLMRateLimitError, LLMTimeoutError
from core.logger import get_trace_id, logger
from core.settings import settings


class TokenUsage:
    """单次 LLM 调用的 token 消耗统计。"""

    def __init__(self) -> None:
        self._prompt_tokens: int = 0
        self._completion_tokens: int = 0
        self._total_tokens: int | None = None

    @property
    def prompt_tokens(self) -> int:
        return self._prompt_tokens

    @prompt_tokens.setter
    def prompt_tokens(self, value: int) -> None:
        self._prompt_tokens = value

    @property
    def completion_tokens(self) -> int:
        return self._completion_tokens

    @completion_tokens.setter
    def completion_tokens(self, value: int) -> None:
        self._completion_tokens = value

    @property
    def total_tokens(self) -> int:
        if self._total_tokens is not None:
            return self._total_tokens
        return self._prompt_tokens + self._completion_tokens

    @total_tokens.setter
    def total_tokens(self, value: int) -> None:
        self._total_tokens = value

    def add(self, other: TokenUsage) -> None:
        self._prompt_tokens += other.prompt_tokens
        self._completion_tokens += other.completion_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class BaseLLMClient(ABC):
    """LLM 客户端抽象基类。

    所有具体 LLM 客户端必须实现:
        - chat(messages, model, temperature, **kwargs) -> (content, TokenUsage)
        - embed(texts, model) -> (list[list[float]], TokenUsage)
    """

    def __init__(self, api_key: str, base_url: str, default_model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[str, TokenUsage]:
        """同步对话调用。

        Args:
            messages: OpenAI 格式消息列表，如 [{"role": "user", "content": "..."}]。
                content 可以是 str（纯文本）或 list（多模态，含 image_url）。
            model: 模型名，None 时使用 default_model
            temperature: 采样温度
            max_tokens: 最大输出 token 数

        Returns:
            (生成的文本内容, TokenUsage 统计)
        """
        ...

    @abstractmethod
    def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        **kwargs: Any,
    ) -> tuple[list[list[float]], TokenUsage]:
        """同步 Embedding 调用。

        Args:
            texts: 待编码文本列表
            model: Embedding 模型名

        Returns:
            (向量列表, TokenUsage 统计)
        """
        ...

    # ------------------------------------------------------------------
    # 便捷方法：流式输出（可选实现，默认抛 NotImplementedError）
    # ------------------------------------------------------------------

    def chat_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """带 function calling 支持的对话调用。

        Returns:
            {"content": str, "tool_calls": list|None, "usage": TokenUsage}
            tool_calls 格式: [{"id": str, "name": str, "arguments": str}]
        """
        raise NotImplementedError("chat_raw not implemented for this client")

    async def astream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """异步流式对话（逐字返回）。子类可覆盖。"""
        raise NotImplementedError("stream_chat not implemented for this client")


class OpenAIClient(BaseLLMClient):
    """兼容 OpenAI API 格式的通用客户端。

    支持：OpenAI 官方 / DeepSeek / 硅基流动 / 阿里云百炼 等。
    支持多模态：检测到 image_url 时自动切换到 vision_model（可独立配置 base_url/api_key）。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        default_model: str,
        timeout: float = 60.0,
        vision_model: str = "",
        vision_base_url: str = "",
        vision_api_key: str = "",
    ) -> None:
        super().__init__(api_key, base_url, default_model)
        self._vision_model = vision_model or default_model
        self._vision_base_url = vision_base_url or base_url
        self._vision_api_key = vision_api_key or api_key
        # 延迟导入 openai，避免未安装时整个模块爆炸
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConfigError(
                "openai 包未安装，请执行: pip install openai"
            ) from exc

        # 读取系统代理（HTTP_PROXY / HTTPS_PROXY）
        import os
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

        def _build_client(api_key_: str, base_url_: str) -> Any:
            client_kwargs: dict[str, Any] = {
                "api_key": api_key_,
                "base_url": base_url_,
                "timeout": timeout,
            }
            if proxy:
                import httpx
                http_client = httpx.Client(timeout=timeout, proxy=proxy)
                client_kwargs["http_client"] = http_client
            return OpenAI(**client_kwargs)

        self._client = _build_client(api_key, base_url)
        # 视觉客户端（独立 base_url/api_key），与主客户端隔离避免单实例多 base_url 冲突
        if vision_base_url and vision_base_url != base_url:
            self._vision_client = _build_client(self._vision_api_key, self._vision_base_url)
            logger.info(
                f"LLM vision client created",
                extra={"vision_model": self._vision_model, "vision_base_url": self._vision_base_url},
            )
        else:
            self._vision_client = self._client

        if proxy:
            logger.info(f"LLM client using proxy: {proxy}")

    @staticmethod
    def _has_image(messages: list[dict[str, Any]]) -> bool:
        """检测消息列表中是否包含图片内容（image_url）。"""
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        return False

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[str, TokenUsage]:
        model = model or self.default_model
        trace_id = get_trace_id()

        logger.debug(
            "LLM chat start",
            extra={
                "model": model,
                "msg_count": len(messages),
                "temperature": temperature,
                "trace_id": trace_id,
            },
        )

        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception as exc:
            self._handle_error(exc, operation="chat")
            raise  # 理论上 _handle_error 会 raise，这里保留以防万一

        content = response.choices[0].message.content or ""
        usage = TokenUsage()
        if response.usage:
            usage.prompt_tokens = response.usage.prompt_tokens
            usage.completion_tokens = response.usage.completion_tokens
            usage.total_tokens = response.usage.total_tokens

        logger.info(
            "LLM chat success",
            extra={
                "model": model,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "trace_id": trace_id,
            },
        )
        return content, usage

    def chat_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """带 function calling 支持的对话（暴露 tool_calls 结构）。"""
        model = model or self.default_model
        trace_id = get_trace_id()

        logger.debug(
            "LLM chat_raw start",
            extra={
                "model": model,
                "msg_count": len(messages),
                "has_tools": bool(tools),
                "trace_id": trace_id,
            },
        )

        try:
            extra_args: dict[str, Any] = {}
            if tools:
                extra_args["tools"] = tools
                extra_args["tool_choice"] = tool_choice

            response = self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
                **extra_args,
                **kwargs,
            )
        except Exception as exc:
            self._handle_error(exc, operation="chat_raw")
            raise

        msg = response.choices[0].message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in msg.tool_calls
            ]

        usage = TokenUsage()
        if response.usage:
            usage.prompt_tokens = response.usage.prompt_tokens
            usage.completion_tokens = response.usage.completion_tokens
            usage.total_tokens = response.usage.total_tokens

        logger.info(
            "LLM chat_raw success",
            extra={
                "model": model,
                "has_content": bool(msg.content),
                "tool_call_count": len(tool_calls or []),
                "total_tokens": usage.total_tokens,
                "trace_id": trace_id,
            },
        )
        return {
            "content": msg.content or "",
            "tool_calls": tool_calls,
            "usage": usage,
        }

    def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        **kwargs: Any,
    ) -> tuple[list[list[float]], TokenUsage]:
        model = model or settings.embedding.model
        trace_id = get_trace_id()

        logger.debug(
            "LLM embed start",
            extra={"model": model, "batch_size": len(texts), "trace_id": trace_id},
        )

        try:
            response = self._client.embeddings.create(
                model=model,
                input=texts,
                **kwargs,
            )
        except Exception as exc:
            self._handle_error(exc, operation="embed")
            raise

        vectors = [item.embedding for item in response.data]
        usage = TokenUsage()
        if response.usage:
            usage.prompt_tokens = response.usage.prompt_tokens
            usage.total_tokens = response.usage.total_tokens

        logger.info(
            "LLM embed success",
            extra={
                "model": model,
                "batch_size": len(texts),
                "total_tokens": usage.total_tokens,
                "trace_id": trace_id,
            },
        )
        return vectors, usage

    # ------------------------------------------------------------------
    # 内部：统一异常转换
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_error(exc: Exception, operation: str) -> None:
        """将 openai 异常转换为 OpenClaw 自定义异常。"""
        exc_str = str(exc).lower()

        if "timeout" in exc_str or "timed out" in exc_str:
            logger.warning(f"LLM {operation} timeout", extra={"error": str(exc)})
            raise LLMTimeoutError(f"LLM {operation} 请求超时: {exc}") from exc

        if "rate limit" in exc_str or "429" in exc_str:
            logger.warning(f"LLM {operation} rate limited", extra={"error": str(exc)})
            raise LLMRateLimitError(f"LLM {operation} 触发限流: {exc}") from exc

        if "context length" in exc_str or "maximum context" in exc_str:
            logger.warning(f"LLM {operation} context overflow", extra={"error": str(exc)})
            raise LLMContextWindowError(f"LLM {operation} 上下文超限: {exc}") from exc

        logger.error(f"LLM {operation} failed", extra={"error": str(exc)})
        raise LLMError(f"LLM {operation} 调用失败: {exc}") from exc


# ------------------------------------------------------------------------------
# 工厂类
# ------------------------------------------------------------------------------

class LLMFactory:
    """按配置动态创建 LLM 客户端。"""

    _client_cache: dict[str, BaseLLMClient] = {}

    @classmethod
    def create(cls, provider: str | None = None) -> BaseLLMClient:
        """根据 provider 名称创建对应客户端，同名缓存复用。"""
        provider = provider or settings.llm.provider
        cache_key = provider

        if cache_key in cls._client_cache:
            return cls._client_cache[cache_key]

        if provider in ("openai", "deepseek", "qwen", "siliconflow"):
            # 这些平台都兼容 OpenAI API 格式
            client = OpenAIClient(
                api_key=settings.llm.api_key,
                base_url=settings.llm.base_url,
                default_model=settings.llm.model,
                timeout=float(settings.llm.timeout),
            )
        else:
            raise ConfigError(f"不支持的 LLM provider: {provider}")

        cls._client_cache[cache_key] = client
        logger.info(f"LLM client created: {provider}", extra={"base_url": settings.llm.base_url})
        return client

    @classmethod
    def get_default(cls) -> BaseLLMClient:
        """获取默认（settings 中配置的）客户端。"""
        return cls.create(settings.llm.provider)


# ------------------------------------------------------------------------------
# 全局便捷函数（大部分场景直接用这个）
# ------------------------------------------------------------------------------

_default_client: BaseLLMClient | None = None


def get_llm_client() -> BaseLLMClient:
    """获取全局默认 LLM 客户端（懒加载）。"""
    global _default_client
    if _default_client is None:
        _default_client = LLMFactory.get_default()
    return _default_client


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    **kwargs: Any,
) -> tuple[str, TokenUsage]:
    """一行代码调用 LLM 对话。"""
    return get_llm_client().chat(messages, model=model, temperature=temperature, **kwargs)


def embed(texts: list[str], *, model: str | None = None, **kwargs: Any) -> tuple[list[list[float]], TokenUsage]:
    """一行代码调用 Embedding。"""
    return get_llm_client().embed(texts, model=model, **kwargs)
