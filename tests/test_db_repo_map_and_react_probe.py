"""DBRepoMap (PageRank + 外键关联拓扑) 与数据库 ReAct 探针子图单元测试."""

from pathlib import Path
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
import pytest

from app.clients.llm_client_manager import llm_client_manager
from app.core.datasource_registry import DatasourceConfig, DatasourceRegistry
from app.core.db_repo_map import DBRepoMap, compute_table_schema_hash
from app.exceptions import DatasourceUnreachableError
from app.graphs import evidence_subgraph
from app.graphs.evidence_subgraph import db_probe_subgraph, evidence_probe_subgraph


def _build_large_sample_schema() -> list[dict]:
    tables = [
        {
            "name": f"audit_log_{idx:02d}",
            "comment": "通用审计归档分表",
            "primary_keys": ["id"],
            "columns": [
                {"name": "id", "type": "BIGINT", "nullable": False, "comment": "", "is_pk": True},
                {"name": "payload", "type": "TEXT", "nullable": True, "comment": "载荷", "is_pk": False},
                {"name": "created_at", "type": "DATETIME", "nullable": False, "comment": "", "is_pk": False},
            ],
            "foreign_keys": [],
            "indexes": [],
        }
        for idx in range(25)
    ]
    tables.extend(
        [
            {
                "name": "users",
                "comment": "用户主表",
                "primary_keys": ["id"],
                "columns": [
                    {"name": "id", "type": "BIGINT", "nullable": False, "comment": "", "is_pk": True},
                    {"name": "email", "type": "VARCHAR(128)", "nullable": False, "comment": "邮箱", "is_pk": False},
                ],
                "foreign_keys": [],
                "indexes": [{"name": "idx_email", "column_names": ["email"], "unique": True}],
            },
            {
                "name": "orders",
                "comment": "订单主表",
                "primary_keys": ["id"],
                "columns": [
                    {"name": "id", "type": "BIGINT", "nullable": False, "comment": "", "is_pk": True},
                    {"name": "user_id", "type": "BIGINT", "nullable": False, "comment": "下单用户", "is_pk": False},
                    {"name": "total_amount", "type": "DECIMAL(10,2)", "nullable": False, "comment": "", "is_pk": False},
                ],
                "foreign_keys": [
                    {
                        "constrained_columns": ["user_id"],
                        "referred_table": "users",
                        "referred_columns": ["id"],
                    }
                ],
                "indexes": [{"name": "idx_user_id", "column_names": ["user_id"], "unique": False}],
            },
            {
                "name": "refunds",
                "comment": "订单退款表（无物理外键，靠 order_id 命名关联）",
                "primary_keys": ["id"],
                "columns": [
                    {"name": "id", "type": "BIGINT", "nullable": False, "comment": "", "is_pk": True},
                    {"name": "order_id", "type": "BIGINT", "nullable": False, "comment": "订单ID", "is_pk": False},
                    {"name": "refund_reason", "type": "VARCHAR(255)", "nullable": True, "comment": "退款原因", "is_pk": False},
                ],
                "foreign_keys": [],
                "indexes": [{"name": "idx_order_id", "column_names": ["order_id"], "unique": False}],
            },
        ]
    )
    return [{"ds_id": "ds_mysql_1", "name": "shop_db", "ds_type": "mysql", "tables": tables}]


def test_db_repo_map_pagerank_fk_inference_and_token_budget() -> None:
    datasets = _build_large_sample_schema()
    result = DBRepoMap().build_map(
        datasets,
        query="排查订单退款失败，检查 refunds 表的 refund_reason 与 orders 关联状态",
        repo_map="app/services/refund_service.py:\n  def process_refund",
        max_tokens=450,
    )

    assert result.total_tables == 28
    assert 2 <= len(result.included_tables) < 28
    # PageRank + 个性化种子应把命中的核心业务表排到前排，而不是只拿前 20 张 audit_log_*
    assert "refunds" in result.included_tables[:4]
    assert "orders" in result.included_tables[:5]
    assert "[DB_REPO_MAP]" in result.rendered_map
    assert "orders.user_id -> users.id (fk)" in result.rendered_map
    assert "refunds.order_id -> orders.id (inferred)" in result.rendered_map
    assert "⋮ ..." in result.rendered_map


@pytest.mark.anyio
async def test_db_repo_map_incremental_schema_hash_cache(tmp_path: Path) -> None:
    datasets = _build_large_sample_schema()
    tables_by_name = {t["name"]: t for t in datasets[0]["tables"]}
    hashes_v1 = {name: compute_table_schema_hash(meta) for name, meta in tables_by_name.items()}
    fetched_batches: list[list[str]] = []

    async def _fetch(names: list[str]) -> list[dict]:
        fetched_batches.append(list(names))
        return [tables_by_name[n] for n in names if n in tables_by_name]

    cache_dir = tmp_path / "db_cache"
    mapper = DBRepoMap(cache_dir=cache_dir)
    ds_entry_1, stats_1 = await mapper.sync_incremental_tags(
        ds_id="ds_mysql_1",
        ds_name="shop_db",
        table_hashes=hashes_v1,
        fetch_tables_fn=_fetch,
        cache_scope="mysql://shop",
    )
    assert stats_1 == {"reused": 0, "updated": 28, "removed": 0}
    assert len(fetched_batches) == 1
    assert len(fetched_batches[0]) == 28

    # 第 2 次：修改 refunds（新增一列）、删除 audit_log_24，其余 26 张表不变 -> 仅增量拉取 refunds
    tables_by_name["refunds"] = {
        **tables_by_name["refunds"],
        "columns": [
            *tables_by_name["refunds"]["columns"],
            {"name": "trace_id", "type": "VARCHAR(64)", "nullable": True, "comment": "链路ID", "is_pk": False},
        ],
    }
    tables_by_name.pop("audit_log_24")
    hashes_v2 = {name: compute_table_schema_hash(meta) for name, meta in tables_by_name.items()}

    mapper_reloaded = DBRepoMap(cache_dir=cache_dir)
    ds_entry_2, stats_2 = await mapper_reloaded.sync_incremental_tags(
        ds_id="ds_mysql_1",
        ds_name="shop_db",
        table_hashes=hashes_v2,
        fetch_tables_fn=_fetch,
        cache_scope="mysql://shop",
    )
    assert stats_2 == {"reused": 26, "updated": 1, "removed": 1}
    assert fetched_batches[1] == ["refunds"]

    res = mapper_reloaded.build_map_from_tag_datasets(
        [ds_entry_2],
        query="refunds trace_id",
        max_tokens=500,
        cache_stats=stats_2,
    )
    assert "cache=reused:26/updated:1/removed:1" in res.rendered_map
    assert "trace_id: VARCHAR(64)" in res.rendered_map


class _FakeDBRegistry(DatasourceRegistry):
    def __init__(self, datasets: list[dict]) -> None:
        super().__init__()
        self.register(
            DatasourceConfig(
                ds_id="ds_mysql_1",
                name="shop_db",
                ds_type="mysql",
                connection_uri="mysql+asyncmy://readonly:pass@localhost:3306/shop",
            )
        )
        self._datasets = datasets
        self.executed_sqls: list[str] = []

    async def inspect_schema_metadata(
        self,
        ds_id: str | None = None,
        table_names: list[str] | None = None,
    ) -> list[dict]:
        if not table_names:
            return self._datasets
        wanted = set(table_names)
        return [
            {
                **ds,
                "tables": [t for t in ds.get("tables", []) if t["name"] in wanted],
            }
            for ds in self._datasets
        ]

    async def execute_readonly_sql(
        self, sql: str, ds_id: str | None = None, limit: int = 50
    ) -> dict:
        self.executed_sqls.append(sql)
        if "bad_col" in sql:
            raise DatasourceUnreachableError("Unknown column 'bad_col' in 'field list'")
        return {
            "ds_id": "ds_mysql_1",
            "columns": ["order_id", "refund_reason"],
            "rows": [{"order_id": 1001, "refund_reason": "timeout"}],
        }


def _make_db_ctx(tmp_path: Path, registry: DatasourceRegistry) -> dict:
    return {
        "task_id": "tsk_db_react_1",
        "project_id": "prj_1",
        "namespace": "shop_ns",
        "repo_root": str(tmp_path),
        "sandbox_work_root": str(tmp_path / ".sandboxes"),
        "active_datasource_ids": ["ds_mysql_1"],
        "es_log_index": None,
        "log_path": None,
        "test_command": "pytest",
        "task_repo": None,
        "task_cache_repo": None,
        "error_cache_es_repo": None,
        "error_cache_qdrant_repo": None,
        "runtime_log_es_repo": None,
        "embedding_client": None,
        "datasource_registry": registry,
        "sandbox_runner": None,
        "repo_map_manager": None,
    }


@pytest.mark.anyio
async def test_db_probe_subgraph_react_loop_and_sql_error_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph_nodes = set(db_probe_subgraph.get_graph().nodes.keys())
    assert {"db_agent_node", "db_tools_node"}.issubset(graph_nodes)

    fake_registry = _FakeDBRegistry(_build_large_sample_schema())

    class _FakeDBToolLLM:
        def __init__(self) -> None:
            self.bound_tool_names: list[str] = []
            self.step = 0

        def bind_tools(self, tools):
            self.bound_tool_names = [t.name for t in tools]
            return self

        async def ainvoke(self, messages):
            self.step += 1
            if self.step == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "db_call_1",
                            "name": "get_db_repo_map",
                            "args": {"focus_query": "orders refunds"},
                        }
                    ],
                )
            if self.step == 2:
                assert any(
                    isinstance(m, SystemMessage) and "[REPO_MAP]" in str(m.content)
                    for m in messages
                )
                assert any(
                    isinstance(m, SystemMessage) and "[DB_REPO_MAP]" in str(m.content)
                    for m in messages
                )
                assert any(
                    isinstance(m, ToolMessage)
                    and m.name == "get_db_repo_map"
                    and "refunds.order_id -> orders.id" in str(m.content)
                    for m in messages
                )
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "db_call_2",
                            "name": "run_readonly_sql",
                            "args": {"sql": "SELECT bad_col FROM orders LIMIT 5"},
                        }
                    ],
                )
            if self.step == 3:
                # 确认 SQL 报错信息也回流给了模型，模型可据此修正查询语句
                assert any(
                    isinstance(m, ToolMessage)
                    and m.name == "run_readonly_sql"
                    and "Unknown column 'bad_col'" in str(m.content)
                    for m in messages
                )
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "db_call_3",
                            "name": "run_readonly_sql",
                            "args": {
                                "sql": "SELECT o.id AS order_id, r.refund_reason FROM orders o JOIN refunds r ON r.order_id = o.id LIMIT 10"
                            },
                        }
                    ],
                )
            assert any(
                isinstance(m, ToolMessage)
                and m.name == "run_readonly_sql"
                and "timeout" in str(m.content)
                for m in messages
            )
            return AIMessage(content="已完成库表关联核对与只读 SELECT 验证。")

    fake_llm = _FakeDBToolLLM()
    monkeypatch.setattr(llm_client_manager, "has_valid_key", lambda: True)
    monkeypatch.setattr(llm_client_manager, "get_llm", lambda temperature=0.0: fake_llm)
    monkeypatch.setattr(
        evidence_subgraph,
        "_fallback_db_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DB ReAct 成功后不应走兜底")),
    )

    result = await db_probe_subgraph.ainvoke(
        {
            "probe_type": "db_query",
            "intent": "bugfix",
            "target_question": "排查订单退款失败，核对 orders 与 refunds 关联数据",
            "repo_map": "app/services/refund_service.py:\n  def process_refund",
            "db_repo_map": "",
            "worker_messages": [],
            "chat_fnames": [],
            "evidences": [],
            "gaps": [],
        },
        context=_make_db_ctx(tmp_path, fake_registry),
    )

    assert fake_llm.step == 4
    assert set(fake_llm.bound_tool_names) == {
        "get_db_repo_map",
        "inspect_table_detail",
        "run_readonly_sql",
    }
    assert len(fake_registry.executed_sqls) == 2
    assert [ev.tool_name for ev in result["evidences"]] == [
        "build_db_repo_map",
        "execute_readonly_sql",
    ]
    assert all(ev.evidence_type == "db_query" for ev in result["evidences"])
    assert "repo_map" not in result
    assert "[DB_REPO_MAP]" in result["db_repo_map"]
    assert "refunds.order_id -> orders.id" in result["db_repo_map"]


@pytest.mark.anyio
async def test_db_probe_subgraph_no_key_falls_back_to_db_repo_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_registry = _FakeDBRegistry(_build_large_sample_schema())
    monkeypatch.setattr(llm_client_manager, "has_valid_key", lambda: False)

    result = await db_probe_subgraph.ainvoke(
        {
            "probe_type": "db_query",
            "intent": "bugfix",
            "target_question": "查询 refunds 与 orders 的退款原因字段",
            "repo_map": "app/services/refund_service.py:\n  def process_refund",
            "db_repo_map": "",
            "worker_messages": [],
            "chat_fnames": [],
            "evidences": [],
            "gaps": [],
        },
        context=_make_db_ctx(tmp_path, fake_registry),
    )

    assert not result["gaps"]
    assert len(result["evidences"]) == 1
    ev = result["evidences"][0]
    assert ev.tool_name == "build_db_repo_map"
    assert ev.evidence_type == "db_query"
    assert "[DB_REPO_MAP]" in ev.raw_snippet
    assert "refunds.order_id -> orders.id" in ev.raw_snippet
    assert "[DB_REPO_MAP]" in result["db_repo_map"]
    assert "repo_map" not in result

    # 验证父子图并发（code_probe_worker + db_probe_worker 等）汇总后 db_repo_map 不会被其他分支覆盖为空
    (tmp_path / "refund.py").write_text("def handle_refund():\n    pass\n", encoding="utf-8")
    parent_res = await evidence_probe_subgraph.ainvoke(
        {
            "user_query": "查询 refunds 与 orders 的退款原因字段",
            "repo_map": "refund.py:\n  def handle_refund",
            "db_repo_map": "",
            "has_db_datasource": True,
            "has_log_datasource": False,
            "intent": "bugfix",
            "enabled_probes": ["source_code", "db_query"],
            "chat_fnames": [],
            "evidences": [],
            "gaps": [],
        },
        context=_make_db_ctx(tmp_path, fake_registry),
    )
    assert "[DB_REPO_MAP]" in parent_res["db_repo_map"]
    assert parent_res["repo_map"] == "refund.py:\n  def handle_refund"
