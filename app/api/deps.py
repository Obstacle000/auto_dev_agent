"""依赖注入装配模块 (参考 my_swe/app/api/deps.py：Session / Client / Repo / Service)."""

from collections.abc import AsyncIterator
from typing import Annotated, Any

from elasticsearch import AsyncElasticsearch
from fastapi import Depends
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import mysql_client_manager
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.clients.redis_client_manager import redis_client_manager
from app.graphs.main_graph import build_main_graph, main_dev_graph
from app.repositories.es.error_cache_es_repo import ErrorCacheESRepository
from app.repositories.es.runtime_log_es_repo import RuntimeLogESRepository
from app.repositories.mysql.datasource_repo import DatasourceRepository
from app.repositories.mysql.error_cache_repo import ErrorCacheRepository
from app.repositories.mysql.project_repo import ProjectRepository
from app.repositories.mysql.task_repo import TaskRepository
from app.repositories.qdrant.error_cache_qdrant_repo import ErrorCacheQdrantRepository
from app.repositories.redis.task_cache_repo import TaskCacheRepository
from app.services.datasource_service import DatasourceService
from app.services.error_cache_service import ErrorCacheService
from app.services.project_service import ProjectService
from app.services.task_service import TaskService


async def get_session() -> AsyncIterator[AsyncSession]:
    """请求级 MySQL AsyncSession."""
    if mysql_client_manager.session_factory is None:
        mysql_client_manager.init()
    assert mysql_client_manager.session_factory is not None
    async with mysql_client_manager.session_factory() as session:
        yield session


def get_es_client() -> AsyncElasticsearch | None:
    if es_client_manager.client is None:
        es_client_manager.init()
    return es_client_manager.client


def get_qdrant_client() -> AsyncQdrantClient | None:
    if qdrant_client_manager.client is None:
        qdrant_client_manager.init()
    return qdrant_client_manager.client


def get_redis_client() -> Redis | None:
    if redis_client_manager.client is None:
        redis_client_manager.init()
    return redis_client_manager.client


def get_embedding_client() -> Any:
    embedding_client_manager.init()
    if not embedding_client_manager.has_valid_key():
        return None
    return embedding_client_manager.client


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ESClientDep = Annotated[AsyncElasticsearch | None, Depends(get_es_client)]
QdrantClientDep = Annotated[AsyncQdrantClient | None, Depends(get_qdrant_client)]
RedisClientDep = Annotated[Redis | None, Depends(get_redis_client)]
EmbeddingClientDep = Annotated[Any, Depends(get_embedding_client)]


def get_project_repository(session: SessionDep) -> ProjectRepository:
    return ProjectRepository(session)


def get_datasource_repository(session: SessionDep) -> DatasourceRepository:
    return DatasourceRepository(session)


def get_task_repository(session: SessionDep) -> TaskRepository:
    return TaskRepository(session)


def get_error_cache_repository(session: SessionDep) -> ErrorCacheRepository:
    return ErrorCacheRepository(session)


def get_error_cache_es_repository(es_client: ESClientDep) -> ErrorCacheESRepository:
    return ErrorCacheESRepository(es_client)


def get_runtime_log_es_repository(es_client: ESClientDep) -> RuntimeLogESRepository | None:
    if es_client is None:
        return None
    return RuntimeLogESRepository(es_client)


def get_error_cache_qdrant_repository(
    qdrant_client: QdrantClientDep,
) -> ErrorCacheQdrantRepository:
    return ErrorCacheQdrantRepository(qdrant_client, embedding_client_manager.config.dim)


def get_task_cache_repository(redis_client: RedisClientDep) -> TaskCacheRepository:
    return TaskCacheRepository(redis_client)


ProjectRepoDep = Annotated[ProjectRepository, Depends(get_project_repository)]
DatasourceRepoDep = Annotated[DatasourceRepository, Depends(get_datasource_repository)]
TaskRepoDep = Annotated[TaskRepository, Depends(get_task_repository)]
ErrorCacheRepoDep = Annotated[ErrorCacheRepository, Depends(get_error_cache_repository)]
ErrorCacheESRepoDep = Annotated[ErrorCacheESRepository, Depends(get_error_cache_es_repository)]
RuntimeLogESRepoDep = Annotated[RuntimeLogESRepository | None, Depends(get_runtime_log_es_repository)]
ErrorCacheQdrantRepoDep = Annotated[ErrorCacheQdrantRepository, Depends(get_error_cache_qdrant_repository)]
TaskCacheRepoDep = Annotated[TaskCacheRepository, Depends(get_task_cache_repository)]


def get_project_service(project_repo: ProjectRepoDep) -> ProjectService:
    return ProjectService(project_repo=project_repo)


def get_datasource_service(
    datasource_repo: DatasourceRepoDep,
    project_repo: ProjectRepoDep,
    runtime_log_es_repo: RuntimeLogESRepoDep,
) -> DatasourceService:
    return DatasourceService(
        datasource_repo=datasource_repo,
        project_repo=project_repo,
        runtime_log_es_repo=runtime_log_es_repo,
    )


def get_error_cache_service(
    error_cache_repo: ErrorCacheRepoDep,
    error_cache_es_repo: ErrorCacheESRepoDep,
    error_cache_qdrant_repo: ErrorCacheQdrantRepoDep,
    embedding_client: EmbeddingClientDep,
) -> ErrorCacheService:
    return ErrorCacheService(
        error_cache_repo=error_cache_repo,
        error_cache_es_repo=error_cache_es_repo,
        error_cache_qdrant_repo=error_cache_qdrant_repo,
        embedding_client=embedding_client,
    )


def get_task_service(
    project_repo: ProjectRepoDep,
    datasource_repo: DatasourceRepoDep,
    task_repo: TaskRepoDep,
    error_cache_repo: ErrorCacheRepoDep,
    error_cache_es_repo: ErrorCacheESRepoDep,
    error_cache_qdrant_repo: ErrorCacheQdrantRepoDep,
    runtime_log_es_repo: RuntimeLogESRepoDep,
    task_cache_repo: TaskCacheRepoDep,
    embedding_client: EmbeddingClientDep,
) -> TaskService:
    active_graph = (
        build_main_graph(checkpointer=redis_client_manager.checkpointer)
        if redis_client_manager.checkpointer is not None
        else main_dev_graph
    )
    return TaskService(
        project_repo=project_repo,
        datasource_repo=datasource_repo,
        task_repo=task_repo,
        error_cache_repo=error_cache_repo,
        error_cache_es_repo=error_cache_es_repo,
        error_cache_qdrant_repo=error_cache_qdrant_repo,
        runtime_log_es_repo=runtime_log_es_repo,
        task_cache_repo=task_cache_repo,
        embedding_client=embedding_client,
        graph=active_graph,
    )


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
DatasourceServiceDep = Annotated[DatasourceService, Depends(get_datasource_service)]
ErrorCacheServiceDep = Annotated[ErrorCacheService, Depends(get_error_cache_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
