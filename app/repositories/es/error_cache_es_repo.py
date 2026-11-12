"""报错缓存 ES BM25 索引仓储 (对应 docs/04_DATABASE.md 3.9 节)."""

from elasticsearch import AsyncElasticsearch

from app.config import now_iso, settings
from app.schemas.domain_schemas import ErrorCacheHit, ErrorCacheRecord, build_es_cache_doc_id

ERROR_CACHE_ES_MAPPINGS = {
    "dynamic": False,
    "properties": {
        "cache_id": {"type": "keyword"},
        "namespace": {"type": "keyword"},
        "project_id": {"type": "keyword"},
        "error_signature": {"type": "text"},
        "error_keywords": {"type": "text"},
        "symptoms": {"type": "text"},
        "root_cause": {"type": "text"},
        "resolution": {"type": "text"},
        "created_at": {"type": "date"},
    },
}


class ErrorCacheESRepository:
    """管理 `auto_dev_error_cache_bm25` 单索引并按 `namespace` 强制隔离检索."""

    def __init__(
        self,
        es_client: AsyncElasticsearch | None,
        index_name: str | None = None,
    ) -> None:
        self.es_client = es_client
        self.index_name = index_name or settings.es_error_cache_index

    async def ensure_index(self) -> None:
        if self.es_client is None:
            return
        exists = await self.es_client.indices.exists(index=self.index_name)
        if not exists:
            await self.es_client.indices.create(
                index=self.index_name,
                mappings=ERROR_CACHE_ES_MAPPINGS,
            )

    async def upsert_runbook(self, record: ErrorCacheRecord) -> None:
        if self.es_client is None:
            return
        doc_id = build_es_cache_doc_id(record.namespace, record.cache_id)
        doc = {
            "cache_id": record.cache_id,
            "namespace": record.namespace,
            "project_id": record.project_id,
            "error_signature": record.error_signature,
            "error_keywords": record.error_keywords,
            "symptoms": record.symptoms,
            "root_cause": record.root_cause,
            "resolution": record.resolution,
            "created_at": record.created_at or now_iso(),
        }
        await self.es_client.index(index=self.index_name, id=doc_id, document=doc)

    async def search_bm25(
        self,
        namespace: str,
        query: str,
        top_k: int = 5,
    ) -> list[ErrorCacheHit]:
        """按命名空间严格过滤执行 BM25 加权检索."""
        if self.es_client is None:
            return []
        resp = await self.es_client.search(
            index=self.index_name,
            size=top_k,
            query={
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": [
                                    "error_keywords^3",
                                    "error_signature^2",
                                    "symptoms",
                                ],
                            }
                        }
                    ],
                    "filter": [{"term": {"namespace": namespace}}],
                }
            },
        )
        hits: list[ErrorCacheHit] = []
        for item in resp.get("hits", {}).get("hits", []):
            src = item.get("_source", {})
            hits.append(
                ErrorCacheHit(
                    cache_id=str(src.get("cache_id", "")),
                    error_signature=str(src.get("error_signature", "")),
                    root_cause=str(src.get("root_cause", "")),
                    resolution=str(src.get("resolution", "")),
                    bm25_score=float(item.get("_score") or 0.0),
                )
            )
        return hits
