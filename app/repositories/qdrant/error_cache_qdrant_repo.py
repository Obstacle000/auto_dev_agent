"""报错缓存 Qdrant 向量集合仓储 (对应 docs/04_DATABASE.md 3.10 节)."""

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.config import settings
from app.schemas.domain_schemas import ErrorCacheHit, ErrorCacheRecord, build_qdrant_point_id


class ErrorCacheQdrantRepository:
    """管理 `auto_dev_error_cache_vec` 集合并强制 `namespace` 过滤检索."""

    def __init__(
        self,
        qdrant_client: AsyncQdrantClient | None,
        dim: int | None = None,
        collection_name: str | None = None,
    ) -> None:
        self.qdrant_client = qdrant_client
        self.dim = dim or settings.embedding_dim
        self.collection_name = collection_name or settings.qdrant_error_cache_collection

    async def ensure_collection(self) -> None:
        if self.qdrant_client is None:
            return
        exists = await self.qdrant_client.collection_exists(self.collection_name)
        if not exists:
            await self.qdrant_client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )
            await self.qdrant_client.create_payload_index(
                collection_name=self.collection_name,
                field_name="namespace",
                field_schema=PayloadSchemaType.KEYWORD,
            )
            await self.qdrant_client.create_payload_index(
                collection_name=self.collection_name,
                field_name="project_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )

    async def upsert_runbook(
        self, record: ErrorCacheRecord, vector: list[float]
    ) -> None:
        if self.qdrant_client is None:
            return
        point_id = build_qdrant_point_id(record.namespace, record.cache_id)
        payload = {
            "cache_id": record.cache_id,
            "namespace": record.namespace,
            "project_id": record.project_id,
            "error_signature": record.error_signature,
            "error_keywords": record.error_keywords,
            "symptoms": record.symptoms,
            "root_cause": record.root_cause,
            "resolution": record.resolution,
        }
        await self.qdrant_client.upsert(
            collection_name=self.collection_name,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )

    async def search_vector(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int = 5,
    ) -> list[ErrorCacheHit]:
        """强制携带 `namespace` FieldCondition 执行 COSINE 向量检索."""
        if self.qdrant_client is None:
            return []
        result = await self.qdrant_client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            limit=top_k,
            query_filter=Filter(
                must=[
                    FieldCondition(
                        key="namespace",
                        match=MatchValue(value=namespace),
                    )
                ]
            ),
            with_payload=True,
        )
        hits: list[ErrorCacheHit] = []
        for point in result.points:
            payload = point.payload or {}
            hits.append(
                ErrorCacheHit(
                    cache_id=str(payload.get("cache_id", "")),
                    error_signature=str(payload.get("error_signature", "")),
                    root_cause=str(payload.get("root_cause", "")),
                    resolution=str(payload.get("resolution", "")),
                    vector_score=float(point.score),
                )
            )
        return hits
