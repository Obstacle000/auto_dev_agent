"""任务并发执行锁与断点缓存 Redis 仓储."""

from redis.asyncio import Redis


class TaskCacheRepository:
    """管理同一 `task_id` 的分布式运行锁，防止并发重复执行."""

    _local_locks: set[str] = set()

    def __init__(self, redis_client: Redis | None) -> None:
        self.redis_client = redis_client

    @staticmethod
    def _lock_key(task_id: str) -> str:
        return f"auto_dev:lock:task:{task_id}"

    async def acquire_task_lock(self, task_id: str, ttl_seconds: int = 600) -> bool:
        key = self._lock_key(task_id)
        if self.redis_client is not None:
            try:
                acquired = await self.redis_client.set(key, "1", ex=ttl_seconds, nx=True)
                return bool(acquired)
            except Exception:
                pass
        if key in self._local_locks:
            return False
        self._local_locks.add(key)
        return True

    async def release_task_lock(self, task_id: str) -> None:
        key = self._lock_key(task_id)
        self._local_locks.discard(key)
        if self.redis_client is not None:
            try:
                await self.redis_client.delete(key)
            except Exception:
                pass
