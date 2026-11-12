"""数据库只读探查工具 (仅在用户挂载数据库数据源时启用)."""

import json
from pathlib import Path

from app.core.datasource_registry import DatasourceRegistry
from app.core.db_repo_map import DBRepoMap
from app.core.evidence_collector import build_evidence
from app.schemas.domain_schemas import Evidence


async def generate_db_repo_map(
    registry: DatasourceRegistry,
    query: str = "",
    repo_map: str = "",
    max_tokens: int = 1200,
    ds_id: str | None = None,
    cache_dir: str | Path | None = None,
) -> tuple[Evidence | None, str]:
    """基于表级 `schema_hash` 增量缓存 `DBTag` + PageRank 拓扑排序生成折叠树形 DBRepoMap."""
    if not registry.has_db_datasources():
        return None, ""
    # 判断外键物理外键反射 + *_id 命名推断,没有值的判断,后面做

    mapper = DBRepoMap(cache_dir=cache_dir)
    fingerprints = await registry.inspect_schema_fingerprints(ds_id=ds_id)
    if fingerprints:
        tag_datasets: list[dict] = []
        total_stats = {"reused": 0, "updated": 0, "removed": 0}
        for fp in fingerprints:
            cur_ds_id = str(fp["ds_id"])
            cur_ds_name = str(fp.get("name") or cur_ds_id)
            cfg = registry.get(cur_ds_id)
            scope = cfg.connection_uri if cfg else cur_ds_id

            async def _fetch_changed(tables_to_fetch: list[str], target_id: str = cur_ds_id) -> list[dict]:
                metas = await registry.inspect_schema_metadata(
                    ds_id=target_id, table_names=tables_to_fetch
                )
                return list(metas[0].get("tables", [])) if metas else []

            ds_tags_entry, ds_stats = await mapper.sync_incremental_tags(
                ds_id=cur_ds_id,
                ds_name=cur_ds_name,
                table_hashes=dict(fp.get("table_hashes") or {}),
                fetch_tables_fn=_fetch_changed,
                cache_scope=scope,
            )
            tag_datasets.append(ds_tags_entry)
            for k in total_stats:
                total_stats[k] += ds_stats.get(k, 0)

        map_result = mapper.build_map_from_tag_datasets(
            tag_datasets,
            query=query,
            repo_map=repo_map,
            max_tokens=max_tokens,
            cache_stats=total_stats,
        )
        target_ds = fingerprints[0].get("ds_id")
    else:
        datasets = await registry.inspect_schema_metadata(ds_id=ds_id)
        if not datasets:
            return None, ""
        map_result = mapper.build_map(
            datasets,
            query=query,
            repo_map=repo_map,
            max_tokens=max_tokens,
        )
        target_ds = datasets[0].get("ds_id")

    if not map_result.rendered_map:
        return None, ""

    cache_desc = ""
    if map_result.cache_stats:
        cache_desc = (
            f", cache=reused:{map_result.cache_stats.get('reused', 0)}"
            f"/updated:{map_result.cache_stats.get('updated', 0)}"
        )
    ev = build_evidence(
        tool_name="build_db_repo_map",
        tool_args={"ds_id": ds_id or target_ds, "max_tokens": max_tokens},
        raw_output=map_result.rendered_map,
        datasource_id=target_ds,
        summary_override=(
            f"[build_db_repo_map] 数据源 {target_ds} 生成拓扑大纲 "
            f"({len(map_result.included_tables)}/{map_result.total_tables} 表, "
            f"{len(map_result.edges)} 条关联边{cache_desc})"
        ),
    )
    return ev, map_result.rendered_map


async def build_db_repo_map_evidence(
    registry: DatasourceRegistry,
    query: str = "",
    repo_map: str = "",
    max_tokens: int = 1200,
    ds_id: str | None = None,
) -> Evidence | None:
    """反射全库元数据 + PageRank 拓扑排序生成 DBRepoMap，并签发 db_query 证据."""
    ev, _rendered = await generate_db_repo_map(
        registry,
        query=query,
        repo_map=repo_map,
        max_tokens=max_tokens,
        ds_id=ds_id,
    )
    return ev


async def inspect_table_schema(
    registry: DatasourceRegistry,
    ds_id: str | None = None,
    table_names: list[str] | None = None,
) -> Evidence | None:
    """反射指定表或数据源表结构（含主键/外键/索引）并签发 db_query 证据."""
    if not registry.has_db_datasources():
        return None
    schemas = await registry.inspect_schema_metadata(ds_id=ds_id, table_names=table_names)
    if not schemas or not any(ds.get("tables") for ds in schemas):
        return None
    raw = json.dumps(schemas, ensure_ascii=False, indent=2)
    target_ds = schemas[0].get("ds_id")
    tbl_label = ",".join(table_names) if table_names else "all"
    return build_evidence(
        tool_name="inspect_table_schema",
        tool_args={"ds_id": ds_id or target_ds, "tables": tbl_label},
        raw_output=raw,
        datasource_id=target_ds,
        summary_override=f"[inspect_table_schema] 反射数据源 {target_ds} ({tbl_label}) 表结构成功",
    )


async def execute_readonly_sql(
    registry: DatasourceRegistry,
    sql: str,
    ds_id: str | None = None,
    limit: int = 50,
) -> Evidence | None:
    """执行只读 SELECT / SHOW / EXPLAIN 并签发 db_query 证据."""
    if not registry.has_db_datasources():
        return None
    res = await registry.execute_readonly_sql(sql=sql, ds_id=ds_id, limit=limit)
    raw = json.dumps(res, ensure_ascii=False, default=str, indent=2)
    used_ds = res.get("ds_id") or ds_id
    row_count = len(res.get("rows", []))
    return build_evidence(
        tool_name="execute_readonly_sql",
        tool_args={"ds_id": used_ds, "sql": sql},
        raw_output=raw,
        datasource_id=used_ds,
        summary_override=f"[execute_readonly_sql] 数据源 {used_ds} 返回 {row_count} 行记录",
    )
