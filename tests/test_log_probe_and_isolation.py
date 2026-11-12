"""日志探针 ES 直读 / 本地降级策略与 ES/Qdrant/DB 隔离测试."""

from pathlib import Path
from types import SimpleNamespace
import pytest

from app.core.datasource_registry import DatasourceRegistry
from app.graphs.evidence_subgraph import evidence_probe_subgraph
from app.repositories.es.error_cache_es_repo import ErrorCacheESRepository
from app.repositories.es.runtime_log_es_repo import RuntimeLogESRepository
from app.repositories.qdrant.error_cache_qdrant_repo import ErrorCacheQdrantRepository
from app.tools.log_tools import probe_runtime_logs


class _FakeESIndices:
    def __init__(self, mappings_by_index: dict[str, dict]) -> None:
        self.mappings_by_index = mappings_by_index

    async def exists(self, index: str) -> bool:
        return index in self.mappings_by_index

    async def get_mapping(self, index: str) -> dict:
        return {index: {"mappings": {"properties": self.mappings_by_index[index]}}}

    async def create(self, index: str, mappings: dict) -> None:
        self.mappings_by_index[index] = mappings.get("properties", {})


class FakeESClient:
    def __init__(self, mappings_by_index: dict[str, dict] | None = None) -> None:
        self.indices = _FakeESIndices(mappings_by_index or {})
        self.last_search: dict | None = None
        self.indexed_docs: list[dict] = []

    async def index(self, index: str, id: str, document: dict) -> None:
        self.indexed_docs.append({"index": index, "id": id, "document": document})

    async def search(self, **kwargs) -> dict:
        self.last_search = kwargs
        return {
            "hits": {
                "hits": [
                    {
                        "_score": 7.5,
                        "_source": {
                            "cache_id": "err_1234567890",
                            "timestamp": "2026-11-11T10:00:00+08:00",
                            "trace_id": "tr_demo_1",
                            "level": "ERROR",
                            "message": "TypeError in payment",
                            "stacktrace": "Traceback ... TypeError",
                            "error_signature": "TypeError",
                            "root_cause": "discount is None",
                            "resolution": "use discount or 0",
                        },
                    }
                ]
            }
        }


class FakeQdrantClient:
    def __init__(self) -> None:
        self.last_query: dict | None = None

    async def query_points(self, **kwargs):
        self.last_query = kwargs
        return SimpleNamespace(
            points=[
                SimpleNamespace(
                    score=0.91,
                    payload={
                        "cache_id": "err_1234567890",
                        "error_signature": "TypeError",
                        "root_cause": "discount is None",
                        "resolution": "use discount or 0",
                    },
                )
            ]
        )


@pytest.mark.anyio
async def test_log_probe_es_direct_and_local_fallback(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "app.log").write_text(
        "INFO ok\nERROR TypeError: local fallback failure\nTraceback line 12\n",
        encoding="utf-8",
    )

    fake_es = FakeESClient(
        mappings_by_index={
            "custom_order_logs": {
                "timestamp": {"type": "keyword"},
                "trace_id": {"type": "keyword"},
                "level": {"type": "keyword"},
                "message": {"type": "text"},
                "stacktrace": {"type": "text"},
            }
        }
    )
    es_repo = RuntimeLogESRepository(fake_es)  # type: ignore[arg-type]

    # 1. 配置了 ES 索引时：直读 ES，不扫描本地文件
    ev_es, gaps_es = await probe_runtime_logs(
        repo_root=str(tmp_path),
        keyword="TypeError",
        runtime_log_es_repo=es_repo,
        es_log_index="custom_order_logs",
        datasource_id="ds_es_1",
    )
    assert not gaps_es
    assert len(ev_es) == 1
    assert ev_es[0].tool_name == "search_error_logs"
    assert fake_es.last_search is not None
    assert fake_es.last_search["index"] == "custom_order_logs"

    # 2. 未配置 ES 时：降级扫描本地 *.log 文件
    ev_local, gaps_local = await probe_runtime_logs(
        repo_root=str(tmp_path),
        keyword="TypeError",
        runtime_log_es_repo=None,
        es_log_index=None,
    )
    assert not gaps_local
    assert len(ev_local) == 1
    assert ev_local[0].tool_name == "scan_local_logs"
    assert "local fallback failure" in ev_local[0].raw_snippet

    # 3. 既无 ES 也无本地日志文件：记录 gap，不报错
    empty_repo = tmp_path / "empty_proj"
    empty_repo.mkdir()
    ev_none, gaps_none = await probe_runtime_logs(
        repo_root=str(empty_repo),
        keyword="TypeError",
    )
    assert ev_none == []
    assert gaps_none


@pytest.mark.anyio
async def test_es_custom_index_validation_and_namespace_isolation() -> None:
    fake_es = FakeESClient(
        mappings_by_index={
            "bad_logs": {"timestamp": {"type": "keyword"}, "message": {"type": "text"}},
            "good_logs": {
                "timestamp": {"type": "keyword"},
                "trace_id": {"type": "keyword"},
                "level": {"type": "keyword"},
                "message": {"type": "text"},
                "stacktrace": {"type": "text"},
            },
        }
    )
    log_repo = RuntimeLogESRepository(fake_es)  # type: ignore[arg-type]
    ok_bad, msg_bad = await log_repo.validate_index_schema("bad_logs")
    assert not ok_bad
    assert "stacktrace" in msg_bad

    ok_good, _ = await log_repo.validate_index_schema("good_logs")
    assert ok_good

    # 验证 ES 与 Qdrant 报错缓存检索均强制携带 namespace 过滤
    cache_es = ErrorCacheESRepository(fake_es)  # type: ignore[arg-type]
    await cache_es.search_bm25("proj_a_1234", "TypeError")
    assert fake_es.last_search["query"]["bool"]["filter"] == [{"term": {"namespace": "proj_a_1234"}}]

    fake_qdrant = FakeQdrantClient()
    cache_vec = ErrorCacheQdrantRepository(fake_qdrant, dim=4)  # type: ignore[arg-type]
    await cache_vec.search_vector("proj_a_1234", [0.1, 0.2, 0.3, 0.4])
    flt = fake_qdrant.last_query["query_filter"]
    assert flt.must[0].key == "namespace"
    assert flt.must[0].match.value == "proj_a_1234"


@pytest.mark.anyio
async def test_evidence_subgraph_skips_db_probe_when_no_datasource(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def settle(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "app.log").write_text("ERROR settle failed with TypeError\n", encoding="utf-8")

    ctx = {
        "task_id": "t_no_db",
        "project_id": "p1",
        "namespace": "demo_1234",
        "repo_root": str(tmp_path),
        "datasource_registry": DatasourceRegistry(),
        "active_datasource_ids": [],
        "es_log_index": None,
        "log_path": None,
        "task_repo": None,
        "error_cache_repo": None,
        "error_cache_es_repo": None,
        "error_cache_qdrant_repo": None,
        "runtime_log_es_repo": None,
        "embedding_client": None,
        "sandbox_image": "python:3.12-slim",
        "test_cmd": "pytest -q",
        "lint_cmd": None,
    }
    res = await evidence_probe_subgraph.ainvoke(
        {
            "user_query": "settle 报错 TypeError",
            "repo_map": "calc.py: settle",
            "has_db_datasource": False,
            "has_log_datasource": False,
            "evidences": [],
            "gaps": [],
        },
        context=ctx,
    )
    ev_types = {ev.evidence_type for ev in res["evidences"]}
    assert "source_code" in ev_types
    assert "log_error" in ev_types
    assert "db_query" not in ev_types
