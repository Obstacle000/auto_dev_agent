"""LangGraph 运行期上下文 `AutoDevContext` (对应 docs/05_LANGGRAPH.md 4.4 节)."""

from typing import Any, TypedDict

from app.core.datasource_registry import DatasourceRegistry
from app.repositories.es.error_cache_es_repo import ErrorCacheESRepository
from app.repositories.es.runtime_log_es_repo import RuntimeLogESRepository
from app.repositories.mysql.error_cache_repo import ErrorCacheRepository
from app.repositories.mysql.task_repo import TaskRepository
from app.repositories.qdrant.error_cache_qdrant_repo import ErrorCacheQdrantRepository


class AutoDevContext(TypedDict):
    """仅承载不可序列化对象和动态连接配置，不写入 Checkpointer."""

    task_id: str
    project_id: str
    namespace: str
    repo_root: str
    datasource_registry: DatasourceRegistry
    active_datasource_ids: list[str]
    es_log_index: str | None
    log_path: str | None
    task_repo: TaskRepository
    error_cache_repo: ErrorCacheRepository
    error_cache_es_repo: ErrorCacheESRepository
    error_cache_qdrant_repo: ErrorCacheQdrantRepository
    runtime_log_es_repo: RuntimeLogESRepository | None
    embedding_client: Any
    sandbox_image: str
    test_cmd: str
    lint_cmd: str | None
