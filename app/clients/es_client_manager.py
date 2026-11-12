"""Elasticsearch 异步客户端管理器."""

from elasticsearch import AsyncElasticsearch

from app.config import settings


class ESClientManager:
    """管理报错缓存与运行时日志查询共用的 Elasticsearch 异步客户端."""

    def __init__(self) -> None:
        self.client: AsyncElasticsearch | None = None

    def init(self, url: str | None = None) -> None:
        if not settings.es_enabled:
            self.client = None
            return
        if self.client is None:
            self.client = AsyncElasticsearch(hosts=[url or settings.es_url])

    async def is_available(self) -> bool:
        if self.client is None:
            return False
        try:
            return bool(await self.client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None


es_client_manager = ESClientManager()
