"""Embedding 向量模型客户端管理器 (支持延迟加载)."""

from dataclasses import dataclass
from typing import Any

from app.config import settings


@dataclass
class EmbeddingConfig:
    dim: int
    model: str
    api_key: str
    base_url: str


class EmbeddingClientManager:
    """按需实例化 OpenAI 兼容向量模型客户端."""

    def __init__(self) -> None:
        self.config = EmbeddingConfig(
            dim=settings.embedding_dim,
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
        self._client: Any = None

    def init(self) -> None:
        self.config = EmbeddingConfig(
            dim=settings.embedding_dim,
            model=settings.embedding_model,
            api_key=settings.embedding_api_key or settings.llm_api_key,
            base_url=settings.embedding_base_url or settings.llm_base_url,
        )

    @property
    def client(self) -> Any:
        if self._client is None:
            from langchain_openai import OpenAIEmbeddings

            kwargs: dict[str, Any] = {
                "model": self.config.model,
                "api_key": self.config.api_key or "sk-placeholder",
            }
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._client = OpenAIEmbeddings(**kwargs)
        return self._client

    def has_valid_key(self) -> bool:
        key = (self.config.api_key or "").strip()
        return bool(key) and not key.startswith("sk-your-") and key != "sk-placeholder"

    async def embed_text(self, text: str) -> list[float]:
        """将文本编码为指定维度的向量."""
        return await self.client.aembed_query(text)


embedding_client_manager = EmbeddingClientManager()
