"""Qdrant 异步向量库客户端管理器."""

from qdrant_client import AsyncQdrantClient

from app.config import settings


class QdrantClientManager:
    """管理报错缓存向量库 Qdrant 客户端生命周期."""

    def __init__(self) -> None:
        self.client: AsyncQdrantClient | None = None

    def init(self, url: str | None = None) -> None:
        if not settings.qdrant_enabled:
            self.client = None
            return
        if self.client is None:
            self.client = AsyncQdrantClient(url=url or settings.qdrant_url)

    async def is_available(self) -> bool:
        if self.client is None:
            return False
        try:
            await self.client.get_collections()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None


qdrant_client_manager = QdrantClientManager()
