"""报错经验缓存三端同步与 RRF (k=60) 检索服务."""

from typing import Any

from app.config import now_iso
from app.repositories.es.error_cache_es_repo import ErrorCacheESRepository
from app.repositories.mysql.error_cache_repo import ErrorCacheRepository
from app.repositories.qdrant.error_cache_qdrant_repo import ErrorCacheQdrantRepository
from app.schemas.api_schemas import ErrorCacheItem, ErrorCacheSearchResponse
from app.schemas.domain_schemas import ErrorCacheRecord
from app.tools.cache_tools import run_hybrid_error_cache_search


class ErrorCacheService:
    """负责 MySQL -> ES -> Qdrant 三端经验归档与按命名空间隔离的混合检索."""

    def __init__(
        self,
        error_cache_repo: ErrorCacheRepository,
        error_cache_es_repo: ErrorCacheESRepository | None = None,
        error_cache_qdrant_repo: ErrorCacheQdrantRepository | None = None,
        embedding_client: Any = None,
    ) -> None:
        self.error_cache_repo = error_cache_repo
        self.error_cache_es_repo = error_cache_es_repo
        self.error_cache_qdrant_repo = error_cache_qdrant_repo
        self.embedding_client = embedding_client

    async def save_and_sync(self, record: ErrorCacheRecord) -> ErrorCacheRecord:
        saved = await self.error_cache_repo.upsert(record)
        synced = ErrorCacheRecord(
            cache_id=saved.cache_id,
            namespace=saved.namespace,
            project_id=saved.project_id,
            source_task_id=saved.source_task_id,
            error_signature=saved.error_signature,
            error_keywords=saved.error_keywords,
            symptoms=saved.symptoms,
            root_cause=saved.root_cause,
            resolution=saved.resolution,
            created_at=saved.created_at.isoformat() if saved.created_at else now_iso(),
        )
        if self.error_cache_es_repo is not None:
            await self.error_cache_es_repo.upsert_runbook(synced)
        if self.error_cache_qdrant_repo is not None and self.embedding_client is not None:
            text_to_embed = f"{synced.error_signature}\n{synced.symptoms}\n{synced.root_cause}"
            vec = await self.embedding_client.aembed_query(text_to_embed)
            await self.error_cache_qdrant_repo.upsert_runbook(synced, vec)
        return synced

    async def reindex_all_from_mysql(self, namespace: str | None = None) -> int:
        rows = await self.error_cache_repo.list_all(namespace=namespace)
        if self.error_cache_es_repo is not None:
            await self.error_cache_es_repo.ensure_index()
        if self.error_cache_qdrant_repo is not None:
            await self.error_cache_qdrant_repo.ensure_collection()

        count = 0
        for row in rows:
            rec = ErrorCacheRecord(
                cache_id=row.cache_id,
                namespace=row.namespace,
                project_id=row.project_id,
                source_task_id=row.source_task_id,
                error_signature=row.error_signature,
                error_keywords=row.error_keywords,
                symptoms=row.symptoms,
                root_cause=row.root_cause,
                resolution=row.resolution,
                created_at=row.created_at.isoformat() if row.created_at else now_iso(),
            )
            if self.error_cache_es_repo is not None:
                await self.error_cache_es_repo.upsert_runbook(rec)
            if self.error_cache_qdrant_repo is not None and self.embedding_client is not None:
                vec = await self.embedding_client.aembed_query(
                    f"{rec.error_signature}\n{rec.symptoms}\n{rec.root_cause}"
                )
                await self.error_cache_qdrant_repo.upsert_runbook(rec, vec)
            count += 1
        return count

    async def hybrid_search(
        self,
        *,
        namespace: str,
        query: str,
        top_k: int = 3,
    ) -> ErrorCacheSearchResponse:
        raw = await run_hybrid_error_cache_search(
            namespace=namespace,
            query=query,
            es_repo=self.error_cache_es_repo,
            qdrant_repo=self.error_cache_qdrant_repo,
            embedding_client=self.embedding_client,
            top_k=top_k,
        )
        return ErrorCacheSearchResponse(**raw)

    async def list_cache_items(
        self,
        *,
        namespace: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        rows, total = await self.error_cache_repo.list_paginated(
            namespace=namespace, page=page, page_size=page_size
        )
        items = [
            ErrorCacheItem(
                cache_id=r.cache_id,
                namespace=r.namespace,
                project_id=r.project_id,
                source_task_id=r.source_task_id,
                error_signature=r.error_signature,
                error_keywords=r.error_keywords,
                symptoms=r.symptoms,
                root_cause=r.root_cause,
                resolution=r.resolution,
                created_at=r.created_at.isoformat() if r.created_at else "",
            ).model_dump()
            for r in rows
        ]
        return {"total": total, "page": page, "page_size": page_size, "items": items}
