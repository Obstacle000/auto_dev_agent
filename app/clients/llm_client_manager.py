"""LLM 客户端管理器 (参考 aider Lazy 延迟加载机制)."""

from typing import Any

from app.config import settings


class LLMClientManager:
    """延迟加载 ChatOpenAI 客户端，减少冷启动开销."""

    def __init__(self) -> None:
        self._cached_cls: Any = None

    def has_valid_key(self) -> bool:
        key = (settings.llm_api_key or "").strip()
        return bool(key) and not key.startswith("sk-your-") and key != "sk-placeholder"

    def get_llm(self, temperature: float | None = None) -> Any:
        if self._cached_cls is None:
            from langchain_openai import ChatOpenAI

            self._cached_cls = ChatOpenAI
        return self._cached_cls(
            api_key=settings.llm_api_key or "sk-placeholder",
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            temperature=settings.llm_temperature if temperature is None else temperature,
        )


llm_client_manager = LLMClientManager()
