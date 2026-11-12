"""Pytest 共享配置."""

import pytest

from app.core.repo_map import repo_map_manager


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _cleanup_repo_map_manager():
    """测试结束后释放持久化 RepoMap SQLite 缓存句柄，避免 Windows 临时目录清理报错."""
    yield
    repo_map_manager.close_all()
