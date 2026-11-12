"""用户数据源接入与校验接口路由 (`/api/v1/datasources`)."""

from fastapi import APIRouter, Query

from app.api.deps import DatasourceServiceDep
from app.schemas.api_schemas import (
    DatasourceCreateRequest,
    DatasourceResponse,
    DatasourceTestResponse,
)

router = APIRouter(prefix="/api/v1/datasources", tags=["数据源"])


@router.post("", response_model=DatasourceResponse)
async def register_datasource(
    req: DatasourceCreateRequest,
    svc: DatasourceServiceDep,
) -> DatasourceResponse:
    return await svc.register_datasource(req)


@router.post("/{ds_id}/test", response_model=DatasourceTestResponse)
async def test_datasource_connectivity(
    ds_id: str,
    svc: DatasourceServiceDep,
) -> DatasourceTestResponse:
    return await svc.test_connectivity(ds_id)


@router.get("", response_model=list[DatasourceResponse])
async def list_datasources(
    svc: DatasourceServiceDep,
    project_id: str | None = Query(default=None),
) -> list[DatasourceResponse]:
    return await svc.list_datasources(project_id=project_id)
