"""独立测试种子初始化脚本 (通过 `python -m app.seed.seed_runner` 显式运行)."""

import argparse
import asyncio
import uuid
from pathlib import Path

from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import mysql_client_manager
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.config import now_iso
from app.models import Base
from app.models.orm_models import ProjectORM
from app.repositories.es.error_cache_es_repo import ErrorCacheESRepository
from app.repositories.es.runtime_log_es_repo import RuntimeLogESRepository
from app.repositories.mysql.error_cache_repo import ErrorCacheRepository
from app.repositories.mysql.project_repo import ProjectRepository
from app.repositories.qdrant.error_cache_qdrant_repo import ErrorCacheQdrantRepository
from app.schemas.domain_schemas import (
    ErrorCacheRecord,
    build_cache_id,
    derive_project_namespace,
)
from app.services.error_cache_service import ErrorCacheService


def build_sample_seed_logs() -> list[dict]:
    """生成符合特定 ES 日志索引结构的测试样例日志."""
    return [
        {
            "timestamp": now_iso(),
            "trace_id": "tr_seed_001",
            "level": "ERROR",
            "service": "sample-service",
            "api_path": "/api/orders/settle",
            "user_id": "10086",
            "file_line": "app/services/settlement.py:42",
            "message": "TypeError: unsupported operand type(s) for -: 'decimal.Decimal' and 'NoneType'",
            "stacktrace": "Traceback (most recent call last):\n  File \"app/services/settlement.py\", line 42, in settle\n    total = amount - discount\nTypeError: unsupported operand type(s)",
        }
    ]


async def run_seed(repo_path: str, es_log_index: str | None = None) -> None:
    """初始化演示项目与示例报错缓存，绝不反向污染核心 services."""
    mysql_client_manager.init()
    es_client_manager.init()
    qdrant_client_manager.init()
    embedding_client_manager.init()

    try:
        assert mysql_client_manager.engine is not None
        assert mysql_client_manager.session_factory is not None
        async with mysql_client_manager.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        resolved_repo = str(Path(repo_path).resolve())
        namespace = derive_project_namespace(resolved_repo)

        async with mysql_client_manager.session_factory() as session:
            project_repo = ProjectRepository(session)
            project = await project_repo.get_by_namespace(namespace)
            if project is None:
                project = await project_repo.create(
                    ProjectORM(
                        id=f"proj_{uuid.uuid4().hex[:10]}",
                        name=f"seed_{namespace}",
                        namespace=namespace,
                        repo_path=resolved_repo,
                    )
                )

            sig = "TypeError: unsupported operand type(s) for -"
            root_cause = "按折扣率计算的优惠券未填充 discount_amount 默认值"
            cache_id = build_cache_id(namespace, sig, root_cause)
            es_cache_repo = ErrorCacheESRepository(es_client_manager.client)
            qdrant_cache_repo = ErrorCacheQdrantRepository(
                qdrant_client_manager.client, embedding_client_manager.config.dim
            )
            emb = embedding_client_manager.client if embedding_client_manager.has_valid_key() else None

            cache_svc = ErrorCacheService(
                error_cache_repo=ErrorCacheRepository(session),
                error_cache_es_repo=es_cache_repo if await es_client_manager.is_available() else None,
                error_cache_qdrant_repo=qdrant_cache_repo if await qdrant_client_manager.is_available() else None,
                embedding_client=emb,
            )
            await cache_svc.save_and_sync(
                ErrorCacheRecord(
                    cache_id=cache_id,
                    namespace=namespace,
                    project_id=project.id,
                    error_signature=sig,
                    error_keywords="TypeError,NoneType,discount_amount",
                    symptoms="折扣券结算时报错 unsupported operand type(s) for -",
                    root_cause=root_cause,
                    resolution="当 discount_amount 为 None 时按 discount_rate 计算或默认置 0",
                    created_at=now_iso(),
                )
            )

        if es_log_index and await es_client_manager.is_available():
            log_es_repo = RuntimeLogESRepository(es_client_manager.client)
            await log_es_repo.bulk_write_logs(es_log_index, build_sample_seed_logs())
    finally:
        await qdrant_client_manager.close()
        await es_client_manager.close()
        await mysql_client_manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-Dev Agent 独立测试种子脚本")
    parser.add_argument("--repo-path", required=True, help="测试用目标仓库目录")
    parser.add_argument("--es-log-index", default=None, help="可选写入样例日志的 ES 索引名")
    args = parser.parse_args()
    asyncio.run(run_seed(repo_path=args.repo_path, es_log_index=args.es_log_index))


if __name__ == "__main__":
    main()
