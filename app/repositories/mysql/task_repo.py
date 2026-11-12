"""研发任务、消息、证据与补丁记录 MySQL 仓储."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm_models import (
    DevTaskORM,
    PatchRecordORM,
    TaskEvidenceORM,
    TaskMessageORM,
)
from app.schemas.domain_schemas import Evidence


class TaskRepository:
    """`dev_tasks` / `task_messages` / `task_evidences` / `patch_records` 持久化封装."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_task(self, task_id: str) -> DevTaskORM | None:
        return await self.session.get(DevTaskORM, task_id)

    async def create_task(self, task: DevTaskORM) -> DevTaskORM:
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        edit_plan: list[dict] | None = None,
        base_commit_sha: str | None = None,
        target_branch: str | None = None,
        final_commit_sha: str | None = None,
    ) -> DevTaskORM | None:
        task = await self.get_task(task_id)
        if task is None:
            return None
        if status is not None:
            task.status = status
        if edit_plan is not None:
            task.edit_plan = edit_plan
        if base_commit_sha is not None:
            task.base_commit_sha = base_commit_sha
        if target_branch is not None:
            task.target_branch = target_branch
        if final_commit_sha is not None:
            task.final_commit_sha = final_commit_sha
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def append_message(
        self, task_id: str, role: str, content: str, turn_index: int = 1
    ) -> TaskMessageORM:
        msg = TaskMessageORM(
            task_id=task_id,
            turn_index=turn_index,
            role=role,
            content=content,
        )
        self.session.add(msg)
        await self.session.commit()
        await self.session.refresh(msg)
        return msg

    async def list_messages(self, task_id: str) -> list[TaskMessageORM]:
        stmt = (
            select(TaskMessageORM)
            .where(TaskMessageORM.task_id == task_id)
            .order_by(TaskMessageORM.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def upsert_evidences(self, task_id: str, evidences: list[Evidence]) -> None:
        """基于复合主键 `(id, task_id)` 去重落盘证据."""
        for ev in evidences:
            existing = await self.session.get(TaskEvidenceORM, (ev.id, task_id))
            if existing is not None:
                continue
            self.session.add(
                TaskEvidenceORM(
                    id=ev.id,
                    task_id=task_id,
                    evidence_type=ev.evidence_type,
                    datasource_id=ev.datasource_id,
                    tool_name=ev.tool_name,
                    query_args=ev.query_args[:1024],
                    summary=ev.summary[:512],
                    raw_snippet=ev.raw_snippet,
                )
            )
        await self.session.commit()

    async def list_evidences(self, task_id: str) -> list[TaskEvidenceORM]:
        stmt = (
            select(TaskEvidenceORM)
            .where(TaskEvidenceORM.task_id == task_id)
            .order_by(TaskEvidenceORM.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_evidence(self, task_id: str, ev_id: str) -> TaskEvidenceORM | None:
        return await self.session.get(TaskEvidenceORM, (ev_id, task_id))

    async def save_patch_record(self, record: PatchRecordORM) -> PatchRecordORM:
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        return record

    async def list_patch_records(self, task_id: str) -> list[PatchRecordORM]:
        stmt = (
            select(PatchRecordORM)
            .where(PatchRecordORM.task_id == task_id)
            .order_by(PatchRecordORM.round_no.asc(), PatchRecordORM.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def update_latest_patch_approval(
        self,
        task_id: str,
        approval_status: str,
        human_feedback: str | None = None,
    ) -> PatchRecordORM | None:
        stmt = (
            select(PatchRecordORM)
            .where(PatchRecordORM.task_id == task_id)
            .order_by(PatchRecordORM.round_no.desc(), PatchRecordORM.created_at.desc())
            .limit(1)
        )
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        if record is None:
            return None
        record.approval_status = approval_status
        if human_feedback is not None:
            record.human_feedback = human_feedback
        await self.session.commit()
        await self.session.refresh(record)
        return record
