"""全局配置管理模块 (基于 pydantic-settings)."""

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def now_iso() -> str:
    """生成当前本地时区 ISO-8601 时间戳字符串."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Settings(BaseSettings):
    """平台运行期环境变量配置."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    debug: bool = False

    mysql_dsn: str = "mysql+aiomysql://root:123456@127.0.0.1:3306/auto_dev_db?charset=utf8mb4"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_enabled: bool = True

    es_url: str = "http://127.0.0.1:9200"
    es_enabled: bool = True
    es_error_cache_index: str = "auto_dev_error_cache_bm25"

    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_enabled: bool = True
    qdrant_error_cache_collection: str = "auto_dev_error_cache_vec"

    embedding_dim: int = 1024
    embedding_model: str = "text-embedding-v3"
    embedding_api_key: str = ""
    embedding_base_url: str = ""

    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    repo_map_tokens: int = 1200
    llm_max_context_window: int = 128000

    secret_key: str = "auto-dev-agent-change-me-in-prod"

    sandbox_image: str = "python:3.12-slim"
    sandbox_max_retries: int = 3
    sandbox_require_docker: bool = True
    log_scan_globs: list[str] = Field(default_factory=lambda: ["*.log", "logs/*.log"])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取缓存后的全局配置实例."""
    return Settings()


settings = get_settings()
