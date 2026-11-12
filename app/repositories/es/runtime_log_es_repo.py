"""用户可选 ES 运行时日志索引只读检索与接入结构校验仓储."""

from elasticsearch import AsyncElasticsearch

REQUIRED_LOG_FIELDS = {"timestamp", "trace_id", "level", "message", "stacktrace"}

RUNTIME_LOG_ES_MAPPINGS = {
    "dynamic": False,
    "properties": {
        "timestamp": {"type": "keyword"},
        "trace_id": {"type": "keyword"},
        "level": {"type": "keyword"},
        "service": {"type": "keyword"},
        "api_path": {"type": "keyword"},
        "user_id": {"type": "keyword"},
        "file_line": {"type": "keyword"},
        "message": {"type": "text"},
        "stacktrace": {"type": "text"},
    },
}


class RuntimeLogESRepository:
    """查询用户自定义 `es_index_name` 的只读日志仓储."""

    def __init__(self, es_client: AsyncElasticsearch | None) -> None:
        self.es_client = es_client

    async def validate_index_schema(self, index_name: str) -> tuple[bool, str]:
        """校验用户自定义 ES 日志索引是否存在并包含规范必需字段."""
        if self.es_client is None:
            return False, "Elasticsearch 客户端未启用或不可用"
        exists = await self.es_client.indices.exists(index=index_name)
        if not exists:
            return False, f"ES 日志索引 `{index_name}` 不存在"
        mapping_resp = await self.es_client.indices.get_mapping(index=index_name)
        idx_meta = mapping_resp.get(index_name) or next(iter(mapping_resp.values()), {})
        props = idx_meta.get("mappings", {}).get("properties", {})
        missing = sorted(REQUIRED_LOG_FIELDS - set(props.keys()))
        if missing:
            return False, f"ES 日志索引 `{index_name}` 缺少规范必需字段: {', '.join(missing)}"
        return True, "索引存在且字段结构校验通过"

    async def ensure_index(self, index_name: str) -> None:
        """供独立测试种子环境创建标准日志索引结构."""
        if self.es_client is None:
            return
        exists = await self.es_client.indices.exists(index=index_name)
        if not exists:
            await self.es_client.indices.create(
                index=index_name,
                mappings=RUNTIME_LOG_ES_MAPPINGS,
            )

    async def bulk_write_logs(self, index_name: str, logs: list[dict]) -> None:
        """供独立测试种子环境写入运行日志."""
        if self.es_client is None or not logs:
            return
        await self.ensure_index(index_name)
        for seq, item in enumerate(logs, start=1):
            trace_id = item.get("trace_id", "trace")
            await self.es_client.index(
                index=index_name,
                id=f"{trace_id}_{seq}",
                document=item,
            )

    async def search_error_logs(
        self,
        index_name: str,
        *,
        keyword: str = "",
        level: str = "ERROR",
        size: int = 10,
    ) -> list[dict]:
        """按级别与关键词检索用户 ES 日志索引."""
        if self.es_client is None:
            return []
        filters: list[dict] = []
        if level:
            filters.append({"term": {"level": level}})
        must: list[dict] = []
        if keyword.strip():
            must.append(
                {
                    "multi_match": {
                        "query": keyword,
                        "fields": ["message", "stacktrace", "api_path"],
                    }
                }
            )
        query = {"bool": {"must": must or [{"match_all": {}}], "filter": filters}}
        resp = await self.es_client.search(
            index=index_name,
            size=size,
            query=query,
            sort=[{"timestamp": {"order": "desc"}}],
        )
        return [hit.get("_source", {}) for hit in resp.get("hits", {}).get("hits", [])]

    async def get_trace_logs(
        self,
        index_name: str,
        trace_id: str,
        size: int = 30,
    ) -> list[dict]:
        """根据 trace_id 拉取单条调用链完整日志."""
        if self.es_client is None:
            return []
        resp = await self.es_client.search(
            index=index_name,
            size=size,
            query={"term": {"trace_id": trace_id}},
            sort=[{"timestamp": {"order": "asc"}}],
        )
        return [hit.get("_source", {}) for hit in resp.get("hits", {}).get("hits", [])]
