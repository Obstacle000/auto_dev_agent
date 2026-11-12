"""数据库表结构拓扑图 (`DBRepoMap`) 模块（模仿 `app/core/repo_map.py`）.

核心机制:
1. `DBTag` 统一抽取表注释、主键、列、索引、物理外键 (`fk_ref`) 与类型兼容的推断关联 (`inferred_ref`)；
2. `diskcache` 按数据源与表级 `schema_hash` 持久化 `DBTag`，仅增量重抽新增或变更的表；
3. NetworkX `MultiDiGraph` + Personalized PageRank 结合提问词与代码 `repo_map` 对全库表排序；
4. Binary Search 逼近 `max_tokens` 预算，渲染 `RepoMap` 风格折叠树形大纲（高分核心表展开列，桥接表 `⋮...` 折叠列）。
"""

from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Awaitable, Callable

from diskcache import Cache
import networkx as nx

DB_CACHE_VERSION = 1

DBTag = namedtuple("DBTag", "table_name col_name col_type kind detail target_table".split())

_GENERIC_COLUMNS = {"id", "created_at", "updated_at", "deleted_at", "is_deleted", "status", "remark"}
_INT_TYPE_TOKENS = ("int", "serial", "long", "short", "number")
_STR_TYPE_TOKENS = ("char", "text", "string", "uuid", "clob")


@dataclass(frozen=True)
class DBRelationEdge:
    """表间关联有向边（物理外键或类型兼容的命名推断关联）."""

    src_table: str
    src_columns: tuple[str, ...]
    dst_table: str
    dst_columns: tuple[str, ...]
    relation_kind: str  # "fk" | "inferred"
    weight: float


@dataclass
class DBRepoMapResult:
    """`DBRepoMap` 渲染产物与拓扑统计信息."""

    rendered_map: str
    total_tables: int
    included_tables: list[str] = field(default_factory=list)
    expanded_tables: list[str] = field(default_factory=list)
    edges: list[DBRelationEdge] = field(default_factory=list)
    ranked_scores: dict[str, float] = field(default_factory=dict)
    cache_stats: dict[str, int] = field(default_factory=dict)


def _extract_tokens(text: str) -> set[str]:
    """从自然语言与代码标识中切分小写检索词（兼容 CamelCase 与 snake_case）."""
    if not text:
        return set()
    raw_words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}", text)
    tokens: set[str] = set()
    for word in raw_words:
        lower = word.lower()
        tokens.add(lower)
        camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", word).lower()
        for part in camel_split.split("_"):
            if len(part) >= 2:
                tokens.add(part)
    return tokens


def _match_table_by_stem(stem: str, table_lookup: dict[str, str]) -> str | None:
    """按词干及常见单复数变形匹配库中真实表名."""
    norm = stem.lower().strip()
    if not norm:
        return None
    candidates = [norm, f"{norm}s", f"{norm}es"]
    if norm.endswith("y") and len(norm) > 2:
        candidates.append(f"{norm[:-1]}ies")
    if norm.endswith("s") and len(norm) > 2:
        candidates.append(norm[:-1])
    for cand in candidates:
        if cand in table_lookup:
            return table_lookup[cand]
    return None


def _type_family(col_type: str) -> str:
    lower = (col_type or "").lower()
    if not lower:
        return "unknown"
    if any(tok in lower for tok in _INT_TYPE_TOKENS):
        return "int"
    if any(tok in lower for tok in _STR_TYPE_TOKENS):
        return "str"
    return "other"


def _are_types_compatible(src_type: str, dst_type: str) -> bool:
    f1, f2 = _type_family(src_type), _type_family(dst_type)
    if "unknown" in (f1, f2):
        return True
    return f1 == f2


def _estimate_tokens(text: str) -> int:
    """轻量 Token 数估算（约 4 字符 ≈ 1 Token）."""
    return max(1, len(text) // 4) if text else 0


def compute_table_schema_hash(tbl: dict[str, Any]) -> str:
    """对单张表的元数据计算确定性 SHA1 指纹，用于缓存增量比对."""
    seed = json.dumps(tbl, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def table_meta_to_tags(tbl: dict[str, Any]) -> list[DBTag]:
    """将单张表元数据转换为标准化 `DBTag` 标签列表."""
    t_name = str(tbl["name"])
    tags: list[DBTag] = [
        DBTag(
            table_name=t_name,
            col_name="",
            col_type="",
            kind="table",
            detail=str(tbl.get("comment") or "").strip(),
            target_table="",
        )
    ]

    pk_cols = list(tbl.get("primary_keys") or [])
    pk_set = set(pk_cols)
    for col in tbl.get("columns", []) or []:
        c_name = str(col.get("name", ""))
        c_type = str(col.get("type", ""))
        nullable = bool(col.get("nullable", True))
        c_comment = str(col.get("comment") or "").strip()
        is_pk = bool(col.get("is_pk")) or (c_name in pk_set)
        if is_pk:
            tags.append(DBTag(t_name, c_name, c_type, "pk", "", ""))
        null_flag = "NULL" if nullable else "NOT NULL"
        tags.append(DBTag(t_name, c_name, c_type, "col", f"{null_flag}|{c_comment}", ""))

    for idx in tbl.get("indexes", []) or []:
        idx_cols = list(idx.get("column_names", []) or [])
        if not idx_cols:
            continue
        idx_kind = "UNIQUE" if idx.get("unique") else "INDEX"
        tags.append(
            DBTag(
                t_name,
                ",".join(idx_cols),
                idx_kind,
                "index",
                str(idx.get("name") or ""),
                "",
            )
        )

    for fk in tbl.get("foreign_keys", []) or []:
        ref_tbl = str(fk.get("referred_table") or "")
        if not ref_tbl:
            continue
        src_cols = tuple(fk.get("constrained_columns", []) or ())
        dst_cols = tuple(fk.get("referred_columns", []) or ())
        tags.append(
            DBTag(
                t_name,
                ",".join(src_cols),
                "",
                "fk_ref",
                ",".join(dst_cols),
                ref_tbl,
            )
        )
    return tags


class DBRepoMap:
    """基于 `DBTag`、表级 `schema_hash` 增量缓存、PageRank 与折叠树的大纲生成器."""

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        self._disk_cache: Cache | None = None
        self._mem_cache: dict[str, dict[str, Any]] = {}
        if cache_dir is not None:
            try:
                path = Path(cache_dir)
                path.mkdir(parents=True, exist_ok=True)
                self._disk_cache = Cache(str(path))
            except Exception:
                self._disk_cache = None

    def _load_bucket(self, key: str) -> dict[str, dict[str, Any]]:
        if self._disk_cache is not None:
            try:
                data = self._disk_cache.get(key)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return dict(self._mem_cache.get(key, {}))

    def _save_bucket(self, key: str, data: dict[str, dict[str, Any]]) -> None:
        self._mem_cache[key] = data
        if self._disk_cache is not None:
            try:
                self._disk_cache[key] = data
            except Exception:
                pass

    async def sync_incremental_tags(
        self,
        *,
        ds_id: str,
        ds_name: str,
        table_hashes: dict[str, str],
        fetch_tables_fn: Callable[[list[str]], Awaitable[list[dict[str, Any]]]],
        cache_scope: str = "",
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """对比 live `table_hashes` 与持久化缓存，仅对变更或新增表增量拉取并重算 `DBTag`."""
        scope_sig = hashlib.sha1(cache_scope.encode("utf-8")).hexdigest()[:8] if cache_scope else "default"
        bucket_key = f"db_tags:v{DB_CACHE_VERSION}:{ds_id}:{scope_sig}"
        cached = self._load_bucket(bucket_key)

        reused = 0
        removed = 0
        changed_tables: list[str] = []
        next_bucket: dict[str, dict[str, Any]] = {}

        for tbl_name in list(cached.keys()):
            if tbl_name not in table_hashes:
                removed += 1

        for tbl_name, live_hash in table_hashes.items():
            entry = cached.get(tbl_name)
            if entry and entry.get("hash") == live_hash and isinstance(entry.get("tags"), list):
                next_bucket[tbl_name] = entry
                reused += 1
            else:
                changed_tables.append(tbl_name)

        updated = 0
        if changed_tables:
            fetched_metas = await fetch_tables_fn(changed_tables)
            for tbl_meta in fetched_metas:
                t_name = str(tbl_meta["name"])
                t_tags = table_meta_to_tags(tbl_meta)
                next_bucket[t_name] = {
                    "hash": table_hashes.get(t_name) or compute_table_schema_hash(tbl_meta),
                    "tags": t_tags,
                }
                updated += 1

        self._save_bucket(bucket_key, next_bucket)
        tags_by_table = {
            tbl_name: list(next_bucket[tbl_name]["tags"])
            for tbl_name in table_hashes
            if tbl_name in next_bucket
        }
        stats = {"reused": reused, "updated": updated, "removed": removed}
        return (
            {
                "ds_id": ds_id,
                "name": ds_name,
                "tags_by_table": tags_by_table,
            },
            stats,
        )

    def extract_join_edges_from_tags(
        self, tags_by_table: dict[str, list[DBTag]]
    ) -> list[DBRelationEdge]:
        """从 `DBTag` 中提取物理外键 (`fk`) 并补充类型兼容的 `*_id` 推断关联 (`inferred`)."""
        table_lookup = {t.lower(): t for t in tags_by_table}
        pk_type_by_table: dict[str, tuple[str, str]] = {}
        for t_name, tags in tags_by_table.items():
            for tag in tags:
                if tag.kind == "pk":
                    pk_type_by_table[t_name] = (tag.col_name, tag.col_type)
                    break

        edges: list[DBRelationEdge] = []
        seen_pairs: set[tuple[str, tuple[str, ...], str]] = set()

        for t_name, tags in tags_by_table.items():
            for tag in tags:
                if tag.kind != "fk_ref" or not tag.target_table:
                    continue
                canonical_dst = table_lookup.get(tag.target_table.lower(), tag.target_table)
                src_cols = tuple(c for c in tag.col_name.split(",") if c)
                dst_cols = tuple(c for c in tag.detail.split(",") if c)
                key = (t_name, src_cols, canonical_dst)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    edges.append(
                        DBRelationEdge(
                            src_table=t_name,
                            src_columns=src_cols,
                            dst_table=canonical_dst,
                            dst_columns=dst_cols,
                            relation_kind="fk",
                            weight=10.0,
                        )
                    )

        for t_name, tags in tags_by_table.items():
            for tag in tags:
                if tag.kind != "col":
                    continue
                col_name = tag.col_name
                lower_col = col_name.lower()
                if not lower_col.endswith("_id") or len(lower_col) <= 3:
                    continue
                stem = lower_col[:-3]
                matched_dst = _match_table_by_stem(stem, table_lookup)
                if matched_dst is None and "_" in stem:
                    matched_dst = _match_table_by_stem(stem.rsplit("_", 1)[-1], table_lookup)
                if not matched_dst or matched_dst == t_name:
                    continue

                pk_col, pk_type = pk_type_by_table.get(matched_dst, ("id", ""))
                if not _are_types_compatible(tag.col_type, pk_type):
                    continue

                key = (t_name, (col_name,), matched_dst)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                edges.append(
                    DBRelationEdge(
                        src_table=t_name,
                        src_columns=(col_name,),
                        dst_table=matched_dst,
                        dst_columns=(pk_col,),
                        relation_kind="inferred",
                        weight=3.0,
                    )
                )
        return edges

    def _compute_personalization(
        self,
        tags_by_table: dict[str, list[DBTag]],
        *,
        query: str,
        repo_map: str,
        mentioned_tables: list[str] | None,
    ) -> dict[str, float]:
        """根据提问、代码 `repo_map` 与 `DBTag` 计算各表 Personalized PageRank 种子权重."""
        query_tokens = _extract_tokens(query)
        code_tokens = _extract_tokens(repo_map)
        explicit_set = {t.lower() for t in (mentioned_tables or []) if t}
        table_lookup = {t.lower(): t for t in tags_by_table}

        query_table_hits: set[str] = set()
        for tok in query_tokens:
            matched = _match_table_by_stem(tok, table_lookup)
            if matched:
                query_table_hits.add(matched)

        scores: dict[str, float] = {}
        for t_name, tags in tags_by_table.items():
            lower_name = t_name.lower()
            score = 1.0

            if lower_name in explicit_set:
                score += 120.0
            if t_name in query_table_hits or lower_name in query_tokens:
                score += 100.0
            elif any(part in query_tokens for part in lower_name.split("_") if len(part) >= 3):
                score += 35.0
            if lower_name in code_tokens:
                score += 15.0

            for tag in tags:
                if tag.kind == "table" and tag.detail:
                    if query_tokens and (_extract_tokens(tag.detail) & query_tokens):
                        score += 20.0
                elif tag.kind == "col":
                    col_lower = tag.col_name.lower()
                    if not col_lower or col_lower in _GENERIC_COLUMNS:
                        continue
                    if col_lower in query_tokens:
                        score += 30.0
                    elif col_lower in code_tokens:
                        score += 4.0

            scores[t_name] = score

        total = sum(scores.values())
        if total <= 0:
            return {t: 1.0 / max(1, len(tags_by_table)) for t in tags_by_table}
        return {t: val / total for t, val in scores.items()}

    def rank_tables_by_tags(
        self,
        tags_by_table: dict[str, list[DBTag]],
        edges: list[DBRelationEdge],
        *,
        query: str = "",
        repo_map: str = "",
        mentioned_tables: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        """在表关联图上执行 Personalized PageRank，返回按重要度降序排列的 `(table_name, score)`."""
        if not tags_by_table:
            return []

        graph = nx.MultiDiGraph()
        for t_name in tags_by_table:
            graph.add_node(t_name)

        for edge in edges:
            if edge.src_table in graph and edge.dst_table in graph:
                graph.add_edge(edge.src_table, edge.dst_table, weight=edge.weight)
                graph.add_edge(edge.dst_table, edge.src_table, weight=edge.weight * 0.5)

        personalization = self._compute_personalization(
            tags_by_table,
            query=query,
            repo_map=repo_map,
            mentioned_tables=mentioned_tables,
        )

        if graph.number_of_edges() > 0:
            try:
                ranked = nx.pagerank(
                    graph,
                    weight="weight",
                    personalization=personalization,
                    dangling=personalization,
                )
            except ZeroDivisionError:
                ranked = personalization
        else:
            ranked = personalization

        degree_map = {t_name: graph.degree(t_name) for t_name in graph.nodes}
        sorted_names = sorted(
            tags_by_table.keys(),
            key=lambda t_name: (
                ranked.get(t_name, 0.0),
                personalization.get(t_name, 0.0),
                degree_map.get(t_name, 0),
                t_name,
            ),
            reverse=True,
        )
        return [(t_name, float(ranked.get(t_name, 0.0))) for t_name in sorted_names]

    def _format_table_tree(
        self,
        t_name: str,
        tags: list[DBTag],
        edges_for_tbl: list[DBRelationEdge],
        *,
        expanded: bool,
        ds_label: str = "",
    ) -> str:
        """渲染 RepoMap 风格折叠树：核心表展开全部列，桥接/次要表折叠普通列 (`⋮...`)."""
        table_comment = ""
        pk_tags: list[DBTag] = []
        col_tags: list[DBTag] = []
        idx_tags: list[DBTag] = []
        for tag in tags:
            if tag.kind == "table":
                table_comment = tag.detail
            elif tag.kind == "pk":
                pk_tags.append(tag)
            elif tag.kind == "col":
                col_tags.append(tag)
            elif tag.kind == "index":
                idx_tags.append(tag)

        header = f"{t_name}:"
        if ds_label:
            header += f" [{ds_label}]"
        if table_comment:
            header += f"  -- {table_comment}"
        lines = [header]

        if pk_tags:
            pk_desc = ", ".join(f"{t.col_name} ({t.col_type})" for t in pk_tags)
            lines.append(f"│ PK: {pk_desc}")

        for edge in edges_for_tbl:
            lines.append(
                f"│ FK: {','.join(edge.src_columns)} -> {edge.dst_table}.{','.join(edge.dst_columns)} ({edge.relation_kind})"
            )

        if idx_tags:
            idx_strs = [f"{t.col_type} ({t.col_name})" for t in idx_tags[:4]]
            lines.append(f"│ IDX: {'; '.join(idx_strs)}")

        pk_names = {t.col_name for t in pk_tags}
        if expanded:
            for col in col_tags:
                null_flag, _, c_comment = col.detail.partition("|")
                flags = []
                if col.col_name in pk_names:
                    flags.append("PK")
                if null_flag == "NOT NULL":
                    flags.append("NOT NULL")
                flag_str = f" [{' '.join(flags)}]" if flags else ""
                comment_str = f"  // {c_comment}" if c_comment else ""
                lines.append(f"│ {col.col_name}: {col.col_type}{flag_str}{comment_str}")
        else:
            omitted = sum(1 for c in col_tags if c.col_name not in pk_names)
            if omitted > 0:
                lines.append(f"⋮ ... ({omitted} cols folded)")

        return "\n".join(lines)

    def build_map_from_tag_datasets(
        self,
        tag_datasets: list[dict[str, Any]],
        *,
        query: str = "",
        repo_map: str = "",
        max_tokens: int = 1200,
        mentioned_tables: list[str] | None = None,
        cache_stats: dict[str, int] | None = None,
    ) -> DBRepoMapResult:
        """基于 `DBTag` 集合执行 PageRank 排序与 Token 二分裁剪，输出折叠树形大纲."""
        all_tags_by_table: dict[str, list[DBTag]] = {}
        table_ds_label: dict[str, str] = {}
        all_edges: list[DBRelationEdge] = []
        ds_names: list[str] = []

        for ds in tag_datasets:
            ds_id = str(ds.get("ds_id") or "")
            ds_name = str(ds.get("name") or ds_id)
            if ds_id:
                ds_names.append(ds_id)
            ds_tags: dict[str, list[DBTag]] = dict(ds.get("tags_by_table") or {})
            if not ds_tags:
                continue
            all_edges.extend(self.extract_join_edges_from_tags(ds_tags))
            for t_name, tags in ds_tags.items():
                all_tags_by_table[t_name] = tags
                table_ds_label[t_name] = ds_name

        if not all_tags_by_table:
            return DBRepoMapResult(
                rendered_map="",
                total_tables=0,
                cache_stats=dict(cache_stats or {}),
            )

        ranked_pairs = self.rank_tables_by_tags(
            all_tags_by_table,
            all_edges,
            query=query,
            repo_map=repo_map,
            mentioned_tables=mentioned_tables,
        )
        ranked_scores = {t_name: score for t_name, score in ranked_pairs}

        edges_by_table: dict[str, list[DBRelationEdge]] = {}
        for edge in all_edges:
            edges_by_table.setdefault(edge.src_table, []).append(edge)

        multi_ds = len(ds_names) > 1

        def _render_slice(count: int) -> tuple[str, list[str]]:
            expand_limit = min(count, max(3, min(6, (count + 2) // 3)))
            selected_tables = [t_name for t_name, _ in ranked_pairs[:count]]
            expanded_names = selected_tables[:expand_limit]
            selected_set = set(selected_tables)

            stats_suffix = ""
            if cache_stats:
                stats_suffix = (
                    f" cache=reused:{cache_stats.get('reused', 0)}"
                    f"/updated:{cache_stats.get('updated', 0)}"
                    f"/removed:{cache_stats.get('removed', 0)}"
                )
            header = (
                f"[DB_REPO_MAP] datasources={','.join(ds_names) or 'default'} "
                f"showing={count}/{len(all_tags_by_table)} tables "
                f"(expanded={len(expanded_names)}, folded={count - len(expanded_names)}){stats_suffix}"
            )
            parts = [header]
            for idx, t_name in enumerate(selected_tables):
                parts.append(
                    self._format_table_tree(
                        t_name,
                        all_tags_by_table[t_name],
                        edges_by_table.get(t_name, []),
                        expanded=idx < expand_limit,
                        ds_label=table_ds_label.get(t_name, "") if multi_ds else "",
                    )
                )

            active_edges = [
                e for e in all_edges if e.src_table in selected_set and e.dst_table in selected_set
            ]
            boundary_edges = [
                e for e in all_edges if (e.src_table in selected_set) ^ (e.dst_table in selected_set)
            ]
            if active_edges:
                edge_lines = [
                    f"  {e.src_table}.{','.join(e.src_columns)} -> {e.dst_table}.{','.join(e.dst_columns)} ({e.relation_kind})"
                    for e in active_edges[:30]
                ]
                parts.append("[JOIN_EDGES]\n" + "\n".join(edge_lines))
            if boundary_edges:
                hint_lines = [
                    f"  {e.src_table}.{','.join(e.src_columns)} -> {e.dst_table}.{','.join(e.dst_columns)} ({e.relation_kind})"
                    for e in boundary_edges[:10]
                ]
                parts.append("[UNEXPANDED_NEIGHBOR_HINTS]\n" + "\n".join(hint_lines))
            return "\n\n".join(parts), expanded_names

        low, high = 1, len(ranked_pairs)
        best_count = 1
        while low <= high:
            mid = (low + high) // 2
            candidate_text, _ = _render_slice(mid)
            if _estimate_tokens(candidate_text) <= max_tokens:
                best_count = mid
                low = mid + 1
            else:
                high = mid - 1

        final_text, expanded_names = _render_slice(best_count)
        included_names = [t_name for t_name, _ in ranked_pairs[:best_count]]
        return DBRepoMapResult(
            rendered_map=final_text,
            total_tables=len(all_tags_by_table),
            included_tables=included_names,
            expanded_tables=expanded_names,
            edges=all_edges,
            ranked_scores=ranked_scores,
            cache_stats=dict(cache_stats or {}),
        )

    def build_map(
        self,
        schema_datasets: list[dict[str, Any]],
        *,
        query: str = "",
        repo_map: str = "",
        max_tokens: int = 1200,
        mentioned_tables: list[str] | None = None,
    ) -> DBRepoMapResult:
        """从完整表元数据列表直接构建折叠树形 `[DB_REPO_MAP]`（内存无缓存路径兼容入口）."""
        tag_datasets: list[dict[str, Any]] = []
        for ds in schema_datasets:
            tags_by_table = {
                tbl["name"]: table_meta_to_tags(tbl)
                for tbl in (ds.get("tables") or [])
                if tbl.get("name")
            }
            tag_datasets.append(
                {
                    "ds_id": ds.get("ds_id"),
                    "name": ds.get("name"),
                    "tags_by_table": tags_by_table,
                }
            )
        return self.build_map_from_tag_datasets(
            tag_datasets,
            query=query,
            repo_map=repo_map,
            max_tokens=max_tokens,
            mentioned_tables=mentioned_tables,
        )
