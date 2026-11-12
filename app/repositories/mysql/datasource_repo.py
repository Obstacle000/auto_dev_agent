"""用户数据源 MySQL 仓储 (`user_datasources`)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm_models import UserDatasourceORM


class DatasourceRepository:
    """`user_datasources` 表 CRUD 封装."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, ds_id: str) -> UserDatasourceORM | None:
        return await self.session.get(UserDatasourceORM, ds_id)

    async def get_by_ids(
        self, project_id: str, ds_ids: list[str]
    ) -> list[UserDatasourceORM]:
        if not ds_ids:
            return []
        stmt = select(UserDatasourceORM).where(
            UserDatasourceORM.project_id == project_id,
            UserDatasourceORM.id.in_(ds_ids),
            UserDatasourceORM.is_enabled.is_(True),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_project(self, project_id: str | None = None) -> list[UserDatasourceORM]:
        stmt = select(UserDatasourceORM)
        if project_id:
            stmt = stmt.where(UserDatasourceORM.project_id == project_id)
        stmt = stmt.order_by(UserDatasourceORM.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def create(self, datasource: UserDatasourceORM) -> UserDatasourceORM:
        self.session.add(datasource)
        await self.session.commit()
        await self.session.refresh(datasource)
        return datasource
