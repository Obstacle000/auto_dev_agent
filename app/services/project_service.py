"""项目工作区业务管理服务 (严禁任何测试逻辑或硬编码)."""

import uuid
from pathlib import Path

from app.exceptions import InvalidParamError
from app.models.orm_models import ProjectORM
from app.repositories.mysql.project_repo import ProjectRepository
from app.schemas.api_schemas import ProjectCreateRequest, ProjectResponse
from app.schemas.domain_schemas import derive_project_namespace


class ProjectService:
    """目标代码仓库项目注册与查询服务."""

    def __init__(self, project_repo: ProjectRepository) -> None:
        self.project_repo = project_repo

    @staticmethod
    def _to_response(row: ProjectORM) -> ProjectResponse:
        return ProjectResponse(
            project_id=row.id,
            name=row.name,
            namespace=row.namespace,
            repo_path=row.repo_path,
            default_branch=row.default_branch,
            test_cmd=row.test_cmd,
            lint_cmd=row.lint_cmd,
            sandbox_image=row.sandbox_image,
        )

    async def register_project(self, req: ProjectCreateRequest) -> ProjectResponse:
        repo_dir = Path(req.repo_path).resolve()
        if not repo_dir.is_dir():
            raise InvalidParamError(f"目标 Git 仓库目录不存在: {req.repo_path}")

        if await self.project_repo.get_by_name(req.name) is not None:
            raise InvalidParamError(f"项目名称 `{req.name}` 已存在")

        namespace = derive_project_namespace(str(repo_dir), req.namespace)
        if await self.project_repo.get_by_namespace(namespace) is not None:
            raise InvalidParamError(f"项目命名空间 `{namespace}` 已被占用")

        project = ProjectORM(
            id=f"proj_{uuid.uuid4().hex[:10]}",
            name=req.name,
            namespace=namespace,
            repo_path=str(repo_dir),
            default_branch=req.default_branch,
            test_cmd=req.test_cmd,
            lint_cmd=req.lint_cmd,
            sandbox_image=req.sandbox_image,
        )
        saved = await self.project_repo.create(project)
        return self._to_response(saved)

    async def list_projects(self) -> list[ProjectResponse]:
        rows = await self.project_repo.list_all()
        return [self._to_response(r) for r in rows]
