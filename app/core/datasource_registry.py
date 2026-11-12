"""用户动态数据源注册中心 (参考 my_swe/app/core/datasource_registry.py)."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.exceptions import DatasourceUnreachableError, UnsafeSQLBlockedError
from app.schemas.domain_schemas import DatasourceKind

_FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|MERGE|CALL|EXEC)\b",
    re.IGNORECASE,
)


def validate_readonly_sql(sql: str) -> str:
    """严格校验 SQL 只读安全性，仅允许单条 SELECT / SHOW / EXPLAIN."""
    cleaned = (sql or "").strip()
    if not cleaned:
        raise UnsafeSQLBlockedError("SQL 语句不能为空")
    without_comments = re.sub(r"--.*?$|/\*.*?\*/", " ", cleaned, flags=re.MULTILINE | re.DOTALL).strip()
    statements = [s.strip() for s in without_comments.split(";") if s.strip()]
    if len(statements) != 1:
        raise UnsafeSQLBlockedError("禁止执行多语句 SQL")
    stmt = statements[0]
    if not re.match(r"^(SELECT|SHOW|EXPLAIN)\b", stmt, re.IGNORECASE):
        raise UnsafeSQLBlockedError("只读探查仅允许 SELECT / SHOW / EXPLAIN 语句")
    if _FORBIDDEN_SQL_PATTERN.search(stmt):
        raise UnsafeSQLBlockedError("检测到写操作或 DDL 关键字，已阻断执行")
    return stmt


@dataclass
class DatasourceConfig:
    ds_id: str
    name: str
    ds_type: DatasourceKind
    connection_uri: str
    es_index_name: str | None = None
    schema_whitelist: list[str] = field(default_factory=list)


class DatasourceRegistry:
    """按任务隔离管理用户动态接入的数据源与只读网关."""

    def __init__(self) -> None:
        self._datasources: dict[str, DatasourceConfig] = {}

    def register(self, cfg: DatasourceConfig) -> None:
        self._datasources[cfg.ds_id] = cfg

    def get(self, ds_id: str) -> DatasourceConfig | None:
        return self._datasources.get(ds_id)

    def has_db_datasources(self) -> bool:
        return any(
            cfg.ds_type in ("mysql", "postgres") for cfg in self._datasources.values()
        )

    def get_db_datasources(self) -> list[DatasourceConfig]:
        return [
            cfg
            for cfg in self._datasources.values()
            if cfg.ds_type in ("mysql", "postgres")
        ]

    def get_es_datasource(self) -> DatasourceConfig | None:
        for cfg in self._datasources.values():
            if cfg.ds_type == "elasticsearch" and cfg.es_index_name:
                return cfg
        return None

    async def inspect_schema_metadata(
        self,
        ds_id: str | None = None,
        table_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """完整反射数据库表结构、主键、外键、索引与表/列注释，用于 DBRepoMap 与表详情探查."""
        targets = (
            [self._datasources[ds_id]]
            if ds_id and ds_id in self._datasources
            else self.get_db_datasources()
        )
        requested_set = {t.strip() for t in (table_names or []) if t and t.strip()}
        results: list[dict[str, Any]] = []
        for cfg in targets:
            if cfg.ds_type not in ("mysql", "postgres"):
                continue
            engine = create_async_engine(cfg.connection_uri, pool_pre_ping=True)
            try:
                async with engine.connect() as conn:
                    def _reflect(sync_conn) -> list[dict[str, Any]]:
                        insp = sa_inspect(sync_conn)
                        all_tables = insp.get_table_names()
                        if cfg.schema_whitelist:
                            allowed = set(cfg.schema_whitelist)
                            all_tables = [t for t in all_tables if t in allowed]
                        if requested_set:
                            all_tables = [t for t in all_tables if t in requested_set]

                        table_metas: list[dict[str, Any]] = []
                        for tbl in all_tables:
                            try:
                                tbl_comment = (insp.get_table_comment(tbl) or {}).get("text") or ""
                            except Exception:
                                tbl_comment = ""
                            try:
                                pks = list((insp.get_pk_constraint(tbl) or {}).get("constrained_columns", []) or [])
                            except Exception:
                                pks = []
                            pk_set = set(pks)

                            cols_meta: list[dict[str, Any]] = []
                            for col in insp.get_columns(tbl):
                                cols_meta.append(
                                    {
                                        "name": col["name"],
                                        "type": str(col["type"]),
                                        "nullable": bool(col.get("nullable", True)),
                                        "comment": col.get("comment") or "",
                                        "is_pk": col["name"] in pk_set,
                                    }
                                )

                            try:
                                raw_fks = insp.get_foreign_keys(tbl) or []
                            except Exception:
                                raw_fks = []
                            fks_meta = [
                                {
                                    "constrained_columns": list(fk.get("constrained_columns", []) or []),
                                    "referred_table": fk.get("referred_table") or "",
                                    "referred_columns": list(fk.get("referred_columns", []) or []),
                                }
                                for fk in raw_fks
                                if fk.get("referred_table")
                            ]

                            try:
                                raw_idxs = insp.get_indexes(tbl) or []
                            except Exception:
                                raw_idxs = []
                            idxs_meta = [
                                {
                                    "name": idx.get("name") or "",
                                    "column_names": list(idx.get("column_names", []) or []),
                                    "unique": bool(idx.get("unique", False)),
                                }
                                for idx in raw_idxs
                            ]

                            table_metas.append(
                                {
                                    "name": tbl,
                                    "comment": tbl_comment,
                                    "primary_keys": pks,
                                    "columns": cols_meta,
                                    "foreign_keys": fks_meta,
                                    "indexes": idxs_meta,
                                }
                            )
                        return table_metas

                    tables_detail = await conn.run_sync(_reflect)
                    results.append(
                        {
                            "ds_id": cfg.ds_id,
                            "name": cfg.name,
                            "ds_type": cfg.ds_type,
                            "tables": tables_detail,
                        }
                    )
            except Exception as exc:
                raise DatasourceUnreachableError(f"连接数据源 {cfg.name} 失败: {exc}") from exc
            finally:
                await engine.dispose()
        return results

    async def inspect_schema_fingerprints(
        self,
        ds_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """批量计算全库每张表的结构指纹 (`schema_hash`)，用于 DBRepoMap 按表增量更新."""
        targets = (
            [self._datasources[ds_id]]
            if ds_id and ds_id in self._datasources
            else self.get_db_datasources()
        )
        results: list[dict[str, Any]] = []
        for cfg in targets:
            if cfg.ds_type not in ("mysql", "postgres"):
                continue
            table_hashes: dict[str, str] = {}
            used_fast_catalog = False
            engine = create_async_engine(cfg.connection_uri, pool_pre_ping=True)
            try:
                async with engine.connect() as conn:
                    table_payloads: dict[str, dict[str, Any]] = {}
                    if cfg.ds_type == "mysql":
                        tbl_rows = (
                            await conn.execute(
                                text(
                                    "SELECT TABLE_NAME, IFNULL(TABLE_COMMENT, '') "
                                    "FROM information_schema.TABLES "
                                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"
                                )
                            )
                        ).fetchall()
                        col_rows = (
                            await conn.execute(
                                text(
                                    "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
                                    "IFNULL(COLUMN_COMMENT, ''), COLUMN_KEY "
                                    "FROM information_schema.COLUMNS "
                                    "WHERE TABLE_SCHEMA = DATABASE() "
                                    "ORDER BY TABLE_NAME, ORDINAL_POSITION"
                                )
                            )
                        ).fetchall()
                        fk_rows = (
                            await conn.execute(
                                text(
                                    "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                                    "FROM information_schema.KEY_COLUMN_USAGE "
                                    "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL "
                                    "ORDER BY TABLE_NAME, ORDINAL_POSITION"
                                )
                            )
                        ).fetchall()
                    else:
                        tbl_rows = (
                            await conn.execute(
                                text(
                                    "SELECT c.relname, COALESCE(obj_description(c.oid, 'pg_class'), '') "
                                    "FROM pg_class c "
                                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                                    "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')"
                                )
                            )
                        ).fetchall()
                        col_rows = (
                            await conn.execute(
                                text(
                                    "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
                                    "CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END, "
                                    "COALESCE(col_description(c.oid, a.attnum), ''), '' "
                                    "FROM pg_attribute a "
                                    "JOIN pg_class c ON c.oid = a.attrelid "
                                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                                    "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') "
                                    "AND a.attnum > 0 AND NOT a.attisdropped "
                                    "ORDER BY c.relname, a.attnum"
                                )
                            )
                        ).fetchall()
                        fk_rows = (
                            await conn.execute(
                                text(
                                    "SELECT cl.relname, att.attname, fcl.relname, fatt.attname "
                                    "FROM pg_constraint con "
                                    "JOIN pg_class cl ON cl.oid = con.conrelid "
                                    "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                                    "JOIN pg_class fcl ON fcl.oid = con.confrelid "
                                    "JOIN LATERAL unnest(con.conkey, con.confkey) AS k(attnum, fattnum) ON true "
                                    "JOIN pg_attribute att ON att.attrelid = cl.oid AND att.attnum = k.attnum "
                                    "JOIN pg_attribute fatt ON fatt.attrelid = fcl.oid AND fatt.attnum = k.fattnum "
                                    "WHERE n.nspname = current_schema() AND con.contype = 'f' "
                                    "ORDER BY cl.relname, att.attnum"
                                )
                            )
                        ).fetchall()

                    allowed = set(cfg.schema_whitelist) if cfg.schema_whitelist else None
                    for t_name, t_comment in tbl_rows:
                        if allowed and t_name not in allowed:
                            continue
                        table_payloads[str(t_name)] = {
                            "comment": str(t_comment or ""),
                            "columns": [],
                            "fks": [],
                        }
                    for t_name, c_name, c_type, c_null, c_comment, c_key in col_rows:
                        bucket = table_payloads.get(str(t_name))
                        if bucket is not None:
                            bucket["columns"].append(
                                [str(c_name), str(c_type), str(c_null), str(c_comment or ""), str(c_key or "")]
                            )
                    for t_name, c_name, ref_tbl, ref_col in fk_rows:
                        bucket = table_payloads.get(str(t_name))
                        if bucket is not None:
                            bucket["fks"].append([str(c_name), str(ref_tbl), str(ref_col)])

                    for t_name, payload in table_payloads.items():
                        seed = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                        table_hashes[t_name] = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
                    used_fast_catalog = True
            except Exception:
                used_fast_catalog = False
            finally:
                await engine.dispose()

            if not used_fast_catalog:
                fallback_meta = await self.inspect_schema_metadata(ds_id=cfg.ds_id)
                for ds_item in fallback_meta:
                    for tbl in ds_item.get("tables", []):
                        seed = json.dumps(tbl, ensure_ascii=False, sort_keys=True)
                        table_hashes[tbl["name"]] = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

            results.append(
                {
                    "ds_id": cfg.ds_id,
                    "name": cfg.name,
                    "ds_type": cfg.ds_type,
                    "table_hashes": table_hashes,
                }
            )
        return results

    async def inspect_tables(self, ds_id: str | None = None) -> list[dict[str, Any]]:
        """兼容精简接口：反射数据库前 20 张表的表名与字段名."""
        meta_list = await self.inspect_schema_metadata(ds_id=ds_id)
        results: list[dict[str, Any]] = []
        for ds in meta_list:
            schema_info = {
                tbl["name"]: [col["name"] for col in tbl.get("columns", [])]
                for tbl in ds.get("tables", [])[:20]
            }
            results.append({"ds_id": ds["ds_id"], "name": ds["name"], "tables": schema_info})
        return results

    async def execute_readonly_sql(
        self, sql: str, ds_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """执行经过安全校验的只读 SQL 并限制返回行数."""
        safe_sql = validate_readonly_sql(sql)
        db_list = self.get_db_datasources()
        if ds_id:
            cfg = self._datasources.get(ds_id)
        else:
            cfg = db_list[0] if db_list else None
        if cfg is None or cfg.ds_type not in ("mysql", "postgres"):
            return {"ds_id": None, "rows": [], "note": "未配置数据库数据源"}

        engine = create_async_engine(cfg.connection_uri, pool_pre_ping=True)
        try:
            async with engine.connect() as conn:
                res = await conn.execute(text(safe_sql))
                keys = list(res.keys())
                rows = [dict(zip(keys, row)) for row in res.fetchmany(limit)]
                return {"ds_id": cfg.ds_id, "columns": keys, "rows": rows}
        except UnsafeSQLBlockedError:
            raise
        except Exception as exc:
            raise DatasourceUnreachableError(f"只读 SQL 执行失败: {exc}") from exc
        finally:
            await engine.dispose()
