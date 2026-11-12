"""项目配置 MySQL 仓储 (`projects`)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm_models import ProjectORM


class ProjectRepository:
    """`projects` 表 CRUD 封装."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, project_id: str) -> ProjectORM | None:
        return await self.session.get(ProjectORM, project_id)

    async def get_by_name(self, name: str) -> ProjectORM | None:
        stmt = select(ProjectORM).where(ProjectORM.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_namespace(self, namespace: str) -> ProjectORM | None:
        stmt = select(ProjectORM).where(ProjectORM.namespace == namespace)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[ProjectORM]:
        stmt = select(ProjectORM).order_by(ProjectORM.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def create(self, project: ProjectORM) -> ProjectORM:
        self.session.add(project)
        await self.session.commit()
        await self.session.refresh(project)
        return project
