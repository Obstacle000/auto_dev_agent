"""平台 MySQL 7 张核心表 ORM 映射 (严格对应 docs/04_DATABASE.md)."""

from datetime import datetime
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ProjectORM(Base):
    """目标项目配置表 (`projects`)."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    namespace: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    repo_path: Mapped[str] = mapped_column(String(512), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(64), nullable=False, default="main")
    test_cmd: Mapped[str] = mapped_column(String(255), nullable=False, default="pytest -q")
    lint_cmd: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sandbox_image: Mapped[str] = mapped_column(
        String(128), nullable=False, default="python:3.12-slim"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserDatasourceORM(Base):
    """用户数据源接入表 (`user_datasources`)."""

    __tablename__ = "user_datasources"
    __table_args__ = (
        Index("idx_ds_project_type", "project_id", "ds_type", "is_enabled"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    ds_type: Mapped[str] = mapped_column(String(32), nullable=False)
    es_index_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    encrypted_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DevTaskORM(Base):
    """研发任务主表 (`dev_tasks`)."""

    __tablename__ = "dev_tasks"
    __table_args__ = (Index("idx_task_project_status", "project_id", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    used_datasource_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    edit_plan: Mapped[list | None] = mapped_column(JSON, nullable=True)
    base_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_branch: Mapped[str | None] = mapped_column(String(128), nullable=True)
    final_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TaskMessageORM(Base):
    """任务交互消息表 (`task_messages`)."""

    __tablename__ = "task_messages"
    __table_args__ = (Index("idx_msg_task_turn", "task_id", "turn_index"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dev_tasks.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class TaskEvidenceORM(Base):
    """修改依据证据表 (`task_evidences`)."""

    __tablename__ = "task_evidences"
    __table_args__ = (Index("idx_ev_task_type", "task_id", "evidence_type"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dev_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    datasource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    query_args: Mapped[str] = mapped_column(String(1024), nullable=False)
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    raw_snippet: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class PatchRecordORM(Base):
    """沙箱补丁与审批记录表 (`patch_records`)."""

    __tablename__ = "patch_records"
    __table_args__ = (Index("idx_patch_task_round", "task_id", "round_no"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dev_tasks.id", ondelete="CASCADE"), nullable=False
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    edited_files: Mapped[list] = mapped_column(JSON, nullable=False)
    patch_diff: Mapped[str] = mapped_column(Text, nullable=False)
    test_passed: Mapped[bool] = mapped_column(nullable=False, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    test_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    human_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ErrorCacheRunbookORM(Base):
    """历史报错缓存主表 (`error_cache_runbooks`)."""

    __tablename__ = "error_cache_runbooks"
    __table_args__ = (Index("idx_cache_ns_sig", "namespace", "error_signature"),)

    cache_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    source_task_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("dev_tasks.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    error_signature: Mapped[str] = mapped_column(String(255), nullable=False)
    error_keywords: Mapped[str] = mapped_column(String(512), nullable=False)
    symptoms: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
