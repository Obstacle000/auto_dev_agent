"""报错缓存查询与混合检索调试路由 (`/api/v1/error-cache`)."""

from fastapi import APIRouter, Query

from app.api.deps import ErrorCacheServiceDep
from app.schemas.api_schemas import ErrorCacheSearchRequest, ErrorCacheSearchResponse

router = APIRouter(prefix="/api/v1/error-cache", tags=["报错缓存"])


@router.get("")
async def list_error_cache(
    svc: ErrorCacheServiceDep,
    namespace: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    return await svc.list_cache_items(namespace=namespace, page=page, page_size=page_size)


@router.post("/search", response_model=ErrorCacheSearchResponse)
async def search_error_cache_hybrid(
    req: ErrorCacheSearchRequest,
    svc: ErrorCacheServiceDep,
) -> ErrorCacheSearchResponse:
    return await svc.hybrid_search(
        namespace=req.namespace,
        query=req.query,
        top_k=req.top_k,
    )
