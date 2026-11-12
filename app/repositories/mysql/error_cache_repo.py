"""报错经验缓存 MySQL 唯一事实源仓储 (`error_cache_runbooks`)."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm_models import ErrorCacheRunbookORM
from app.schemas.domain_schemas import ErrorCacheRecord


class ErrorCacheRepository:
    """`error_cache_runbooks` 读写仓储."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, record: ErrorCacheRecord) -> ErrorCacheRunbookORM:
        existing = await self.session.get(ErrorCacheRunbookORM, record.cache_id)
        if existing is not None:
            existing.error_keywords = record.error_keywords
            existing.symptoms = record.symptoms
            existing.root_cause = record.root_cause
            existing.resolution = record.resolution
            await self.session.commit()
            await self.session.refresh(existing)
            return existing

        row = ErrorCacheRunbookORM(
            cache_id=record.cache_id,
            namespace=record.namespace,
            source_task_id=record.source_task_id,
            project_id=record.project_id,
            error_signature=record.error_signature[:255],
            error_keywords=record.error_keywords[:512],
            symptoms=record.symptoms,
            root_cause=record.root_cause,
            resolution=record.resolution,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def list_paginated(
        self,
        *,
        namespace: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ErrorCacheRunbookORM], int]:
        base = select(ErrorCacheRunbookORM)
        count_stmt = select(func.count()).select_from(ErrorCacheRunbookORM)
        if namespace:
            base = base.where(ErrorCacheRunbookORM.namespace == namespace)
            count_stmt = count_stmt.where(ErrorCacheRunbookORM.namespace == namespace)

        total = int((await self.session.execute(count_stmt)).scalar() or 0)
        stmt = (
            base.order_by(ErrorCacheRunbookORM.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await self.session.execute(stmt)).scalars().all())
        return items, total

    async def list_all(self, namespace: str | None = None) -> list[ErrorCacheRunbookORM]:
        stmt = select(ErrorCacheRunbookORM)
        if namespace:
            stmt = stmt.where(ErrorCacheRunbookORM.namespace == namespace)
        stmt = stmt.order_by(ErrorCacheRunbookORM.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())
