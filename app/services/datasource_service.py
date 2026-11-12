"""用户外部数据源接入、结构校验与连通性测试服务."""

import uuid
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.core.crypto import decrypt_uri, encrypt_uri, mask_connection_uri
from app.core.datasource_registry import validate_readonly_sql
from app.exceptions import (
    DatasourceNotFoundError,
    DatasourceUnreachableError,
    InvalidParamError,
    ProjectNotFoundError,
)
from app.models.orm_models import UserDatasourceORM
from app.repositories.es.runtime_log_es_repo import RuntimeLogESRepository
from app.repositories.mysql.datasource_repo import DatasourceRepository
from app.repositories.mysql.project_repo import ProjectRepository
from app.schemas.api_schemas import (
    DatasourceCreateRequest,
    DatasourceResponse,
    DatasourceTestResponse,
)


class DatasourceService:
    """管理用户动态接入的数据库、ES 日志与本地日志数据源."""

    def __init__(
        self,
        datasource_repo: DatasourceRepository,
        project_repo: ProjectRepository,
        runtime_log_es_repo: RuntimeLogESRepository | None = None,
    ) -> None:
        self.datasource_repo = datasource_repo
        self.project_repo = project_repo
        self.runtime_log_es_repo = runtime_log_es_repo

    @staticmethod
    def _to_response(row: UserDatasourceORM) -> DatasourceResponse:
        raw_uri = decrypt_uri(row.encrypted_uri, settings.secret_key)
        return DatasourceResponse(
            ds_id=row.id,
            project_id=row.project_id,
            name=row.name,
            ds_type=row.ds_type,  # type: ignore[arg-type]
            es_index_name=row.es_index_name,
            masked_endpoint=mask_connection_uri(raw_uri),
            enabled=row.is_enabled,
        )

    async def register_datasource(
        self, req: DatasourceCreateRequest
    ) -> DatasourceResponse:
        project = await self.project_repo.get_by_id(req.project_id)
        if project is None:
            raise ProjectNotFoundError()

        if req.ds_type == "elasticsearch":
            if not req.es_index_name or not req.es_index_name.strip():
                raise InvalidParamError("接入 Elasticsearch 日志源时必须传入 `es_index_name`")
            if self.runtime_log_es_repo is not None:
                ok, reason = await self.runtime_log_es_repo.validate_index_schema(
                    req.es_index_name.strip()
                )
                if not ok:
                    raise DatasourceUnreachableError(reason)

        encrypted = encrypt_uri(req.connection_uri, settings.secret_key)
        row = UserDatasourceORM(
            id=f"ds_{uuid.uuid4().hex[:10]}",
            project_id=req.project_id,
            name=req.name,
            ds_type=req.ds_type,
            es_index_name=req.es_index_name.strip() if req.es_index_name else None,
            encrypted_uri=encrypted,
            metadata_json={"readonly_verified": req.readonly_verified},
            is_enabled=True,
        )
        saved = await self.datasource_repo.create(row)
        return self._to_response(saved)

    async def list_datasources(
        self, project_id: str | None = None
    ) -> list[DatasourceResponse]:
        rows = await self.datasource_repo.list_by_project(project_id)
        return [self._to_response(r) for r in rows]

    async def test_connectivity(self, ds_id: str) -> DatasourceTestResponse:
        row = await self.datasource_repo.get_by_id(ds_id)
        if row is None:
            raise DatasourceNotFoundError()

        raw_uri = decrypt_uri(row.encrypted_uri, settings.secret_key)
        if row.ds_type in ("mysql", "postgres"):
            validate_readonly_sql("SELECT 1")
            engine = create_async_engine(raw_uri, pool_pre_ping=True)
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                return DatasourceTestResponse(ds_id=ds_id, reachable=True, message="只读数据库连通成功")
            except Exception as exc:
                raise DatasourceUnreachableError(f"数据库连接测试失败: {exc}") from exc
            finally:
                await engine.dispose()

        if row.ds_type == "elasticsearch":
            if self.runtime_log_es_repo is None:
                raise DatasourceUnreachableError("平台 ES 客户端未启用")
            ok, msg = await self.runtime_log_es_repo.validate_index_schema(
                row.es_index_name or ""
            )
            if not ok:
                raise DatasourceUnreachableError(msg)
            return DatasourceTestResponse(ds_id=ds_id, reachable=True, message=msg)

        path = Path(raw_uri)
        if not path.exists():
            raise DatasourceUnreachableError(f"日志路径不存在: {mask_connection_uri(raw_uri)}")
        return DatasourceTestResponse(ds_id=ds_id, reachable=True, message="日志文件可读")
