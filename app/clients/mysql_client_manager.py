"""平台 MySQL 异步连接池管理器."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


class MySQLClientManager:
    """管理平台元数据主库 SQLAlchemy 异步引擎与会话工厂."""

    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    def init(self, dsn: str | None = None) -> None:
        if self.engine is not None:
            return
        target_dsn = dsn or settings.mysql_dsn
        kwargs: dict = {"echo": False, "pool_pre_ping": True}
        if not target_dsn.startswith("sqlite"):
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 20
        self.engine = create_async_engine(target_dsn, **kwargs)
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None


mysql_client_manager = MySQLClientManager()
