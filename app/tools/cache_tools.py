"""报错缓存双路检索工具与 RRF (k=60) 倒数秩融合 (仅依赖仓储与 core)."""

import asyncio
import json
from typing import Any

from app.core.evidence_collector import build_evidence
from app.repositories.es.error_cache_es_repo import ErrorCacheESRepository
from app.repositories.qdrant.error_cache_qdrant_repo import ErrorCacheQdrantRepository
from app.schemas.domain_schemas import ErrorCacheHit, Evidence


def rrf_fuse_runbooks(
    bm25_hits: list[ErrorCacheHit],
    vector_hits: list[ErrorCacheHit],
    *,
    k: int = 60,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion (RRF k=60) 融合 ES BM25 与 Qdrant 向量召回结果."""
    scores: dict[str, float] = {}
    records: dict[str, dict[str, Any]] = {}

    for rank, hit in enumerate(bm25_hits, start=1):
        if not hit.cache_id:
            continue
        scores[hit.cache_id] = scores.get(hit.cache_id, 0.0) + 1.0 / (k + rank)
        records[hit.cache_id] = {
            "cache_id": hit.cache_id,
            "error_signature": hit.error_signature,
            "root_cause": hit.root_cause,
            "resolution": hit.resolution,
        }

    for rank, hit in enumerate(vector_hits, start=1):
        if not hit.cache_id:
            continue
        scores[hit.cache_id] = scores.get(hit.cache_id, 0.0) + 1.0 / (k + rank)
        records.setdefault(
            hit.cache_id,
            {
                "cache_id": hit.cache_id,
                "error_signature": hit.error_signature,
                "root_cause": hit.root_cause,
                "resolution": hit.resolution,
            },
        )

    ranked_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)[:top_n]
    fused: list[dict[str, Any]] = []
    for cid in ranked_ids:
        item = dict(records[cid])
        item["rrf_score"] = round(scores[cid], 6)
        fused.append(item)
    return fused


async def run_hybrid_error_cache_search(
    *,
    namespace: str,
    query: str,
    es_repo: ErrorCacheESRepository | None,
    qdrant_repo: ErrorCacheQdrantRepository | None,
    embedding_client: Any,
    top_k: int = 3,
) -> dict[str, Any]:
    """按 `namespace` 强隔离并发执行 ES BM25 与 Qdrant 向量检索并做 RRF 融合."""
    bm25_task = (
        es_repo.search_bm25(namespace=namespace, query=query, top_k=top_k * 2)
        if es_repo is not None
        else asyncio.sleep(0, result=[])
    )

    async def _vec_search() -> list[ErrorCacheHit]:
        if qdrant_repo is None or embedding_client is None:
            return []
        try:
            vec = await embedding_client.aembed_query(query)
        except Exception:
            return []
        return await qdrant_repo.search_vector(
            namespace=namespace, query_vector=vec, top_k=top_k * 2
        )
    # 它的作用是把多个异步任务“打包”，让它们同时（并发）运行，并收集所有结果
    bm25_hits, vector_hits = await asyncio.gather(
        bm25_task, _vec_search(), return_exceptions=True
    )
    if isinstance(bm25_hits, BaseException):
        bm25_hits = []
    if isinstance(vector_hits, BaseException):
        vector_hits = []

    fused_results = rrf_fuse_runbooks(bm25_hits, vector_hits, k=60, top_n=top_k)
    return {
        "bm25_hits": [
            {"cache_id": h.cache_id, "bm25_score": h.bm25_score} for h in bm25_hits
        ],
        "vector_hits": [
            {"cache_id": h.cache_id, "vector_score": h.vector_score} for h in vector_hits
        ],
        "fused_results": fused_results,
    }


async def search_error_cache(
    *,
    namespace: str,
    query: str,
    es_repo: ErrorCacheESRepository | None,
    qdrant_repo: ErrorCacheQdrantRepository | None,
    embedding_client: Any,
    top_k: int = 3,
) -> tuple[list[Evidence], list[str]]:
    """检索历史报错缓存并由 evidence_collector 签发 `error_cache` 证据."""
    result = await run_hybrid_error_cache_search(
        namespace=namespace,
        query=query,
        es_repo=es_repo,
        qdrant_repo=qdrant_repo,
        embedding_client=embedding_client,
        top_k=top_k,
    )
    fused = result.get("fused_results", [])
    if not fused:
        return [], [f"命名空间 `{namespace}` 下暂无匹配的历史报错缓存经验"]

    raw = json.dumps(fused, ensure_ascii=False, indent=2)
    ev = build_evidence(
        tool_name="search_error_cache",
        tool_args={"namespace": namespace, "query": query, "top_k": top_k},
        raw_output=raw,
        summary_override=f"[search_error_cache] 命名空间 `{namespace}` RRF 召回 {len(fused)} 条历史报错缓存",
    )
    return [ev], []
