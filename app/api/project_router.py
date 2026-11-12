"""项目管理接口路由 (`/api/v1/projects`)."""

from fastapi import APIRouter

from app.api.deps import ProjectServiceDep
from app.schemas.api_schemas import ProjectCreateRequest, ProjectResponse

router = APIRouter(prefix="/api/v1/projects", tags=["项目管理"])


@router.post("", response_model=ProjectResponse)
async def register_project(
    req: ProjectCreateRequest,
    svc: ProjectServiceDep,
) -> ProjectResponse:
    return await svc.register_project(req)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(svc: ProjectServiceDep) -> list[ProjectResponse]:
    return await svc.list_projects()
