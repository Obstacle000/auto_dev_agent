"""应用生命周期管理：仅初始化客户端、建表与索引结构检查 (严禁包含测试种子写入)."""

from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import mysql_client_manager
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.clients.redis_client_manager import redis_client_manager
from app.core.repo_map import repo_map_manager
from app.models import Base
from app.repositories.es.error_cache_es_repo import ErrorCacheESRepository
from app.repositories.qdrant.error_cache_qdrant_repo import ErrorCacheQdrantRepository


@asynccontextmanager
async def lifespan(app: FastAPI):
    mysql_client_manager.init()
    redis_client_manager.init()
    await redis_client_manager.init_checkpointer()
    es_client_manager.init()
    qdrant_client_manager.init()
    embedding_client_manager.init()

    assert mysql_client_manager.engine is not None
    async with mysql_client_manager.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if await es_client_manager.is_available():
        await ErrorCacheESRepository(es_client_manager.client).ensure_index()

    if await qdrant_client_manager.is_available():
        await ErrorCacheQdrantRepository(
            qdrant_client_manager.client,
            embedding_client_manager.config.dim,
        ).ensure_collection()

    yield

    repo_map_manager.close_all()
    await qdrant_client_manager.close()
    await es_client_manager.close()
    await redis_client_manager.close()
    await mysql_client_manager.close()
