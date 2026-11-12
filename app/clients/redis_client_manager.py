"""Redis 异步连接池与 LangGraph Checkpointer 管理器."""

from typing import Any
import aiosqlite
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from redis.asyncio import Redis

from app.config import PROJECT_ROOT, settings
from app.schemas.domain_schemas import Evidence


class RedisClientManager:
    """管理平台 Redis 异步客户端与持久化 LangGraph Checkpointer."""

    def __init__(self) -> None:
        self.client: Redis | None = None
        self.checkpointer: Any = None
        self._sqlite_conn: aiosqlite.Connection | None = None

    def init(self, url: str | None = None) -> None:
        if not settings.redis_enabled:
            self.client = None
            return
        if self.client is None:
            self.client = Redis.from_url(url or settings.redis_url, decode_responses=True)

    async def is_available(self) -> bool:
        if self.client is None:
            return False
        try:
            return bool(await self.client.ping())
        except Exception:
            return False

    async def init_checkpointer(self) -> Any:
        """优先启用 AsyncRedisSaver，Redis 未启用或不可达时降级为 AsyncSqliteSaver."""
        if await self.is_available() and self.client is not None:
            saver = AsyncRedisSaver(redis_client=self.client)
            await saver.asetup()
            self.checkpointer = saver
            return self.checkpointer

        db_path = PROJECT_ROOT / ".autodev_checkpoints.db"
        self._sqlite_conn = await aiosqlite.connect(str(db_path))
        saver = AsyncSqliteSaver(
            conn=self._sqlite_conn,
            serde=JsonPlusSerializer(allowed_msgpack_modules=[Evidence]),
        )
        await saver.setup()
        self.checkpointer = saver
        return self.checkpointer

    async def close(self) -> None:
        self.checkpointer = None
        if self._sqlite_conn is not None:
            await self._sqlite_conn.close()
            self._sqlite_conn = None
        if self.client is not None:
            await self.client.aclose()
            self.client = None


redis_client_manager = RedisClientManager()
