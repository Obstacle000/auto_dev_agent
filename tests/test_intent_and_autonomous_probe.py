"""入口意图分流、自主源码探查循环与只读诊断短路路径测试."""

from pathlib import Path
from langchain_core.messages import AIMessage, ToolMessage
import pytest

from app.clients.llm_client_manager import llm_client_manager
from app.core.chat_chunks import load_chat_files_block
from app.core.datasource_registry import DatasourceRegistry
from app.core.intent_router import heuristic_classify_intent
from app.graphs import evidence_subgraph
from app.graphs.evidence_subgraph import code_probe_subgraph, evidence_probe_subgraph
from app.graphs.main_graph import main_dev_graph


class _FakeTaskRepo:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.evidences = []

    async def update_task(self, task_id: str, **kwargs) -> None:
        self.updates.append({"task_id": task_id, **kwargs})

    async def upsert_evidences(self, task_id: str, evidences) -> None:
        self.evidences.extend(evidences)


def _make_ctx(repo_root: Path, task_repo=None, log_path: str | None = None) -> dict:
    return {
        "task_id": "task_intent_demo",
        "project_id": "proj_1",
        "namespace": "intent_demo_1234",
        "repo_root": str(repo_root),
        "datasource_registry": DatasourceRegistry(),
        "active_datasource_ids": [],
        "es_log_index": None,
        "log_path": log_path,
        "task_repo": task_repo or _FakeTaskRepo(),
        "error_cache_repo": None,
        "error_cache_es_repo": None,
        "error_cache_qdrant_repo": None,
        "runtime_log_es_repo": None,
        "embedding_client": None,
        "sandbox_image": "python:3.12-slim",
        "test_cmd": "pytest -q",
        "lint_cmd": None,
    }


@pytest.mark.anyio
async def test_intent_classification_and_probe_filtering(tmp_path: Path) -> None:
    (tmp_path / "router.py").write_text("def list_orders():\n    return []\n", encoding="utf-8")
    (tmp_path / "app.log").write_text("ERROR failed to load config\n", encoding="utf-8")

    intent_feat, probes_feat = heuristic_classify_intent(
        "新增一个导出订单 CSV 的接口",
        has_db_datasource=True,
        has_log_signal=True,
    )
    assert intent_feat == "feature"
    assert probes_feat == ["source_code", "db_query"]

    intent_qa, probes_qa = heuristic_classify_intent(
        "为什么日志报 TimeoutError，先分析原因不要改代码",
        has_db_datasource=False,
        has_log_signal=True,
    )
    assert intent_qa == "qa"
    assert "log_error" in probes_qa
    assert "source_code" in probes_qa

    intent_bug, probes_bug = heuristic_classify_intent(
        "排查订单结算 500 报错",
        has_db_datasource=False,
        has_log_signal=False,
    )
    assert intent_bug == "bugfix"
    assert probes_bug == ["error_cache", "source_code", "log_error"]

    # feature 任务进入证据子图时应跳过 log_error，只收集 source_code
    res = await evidence_probe_subgraph.ainvoke(
        {
            "user_query": "新增导出订单 CSV 接口",
            "repo_map": "router.py: list_orders",
            "has_db_datasource": False,
            "has_log_datasource": False,
            "evidences": [],
            "gaps": [],
        },
        context=_make_ctx(tmp_path),
    )
    ev_types = {ev.evidence_type for ev in res["evidences"]}
    assert res["intent"] == "feature"
    assert "source_code" in ev_types
    assert "log_error" not in ev_types


@pytest.mark.anyio
async def test_autonomous_code_probe_worker_tool_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orders_dir = tmp_path / "orders"
    orders_dir.mkdir()
    (orders_dir / "router.py").write_text(
        "def get_order(order_id: str):\n    return {'id': order_id}\n",
        encoding="utf-8",
    )

    graph_nodes = set(code_probe_subgraph.get_graph().nodes.keys())
    assert {"code_agent_node", "code_tools_node"}.issubset(graph_nodes)

    class _FakeToolLLM:
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
                            "id": "call_ls_1",
                            "name": "list_repo_directory",
                            "args": {"rel_dir": "orders"},
                        }
                    ],
                )
            if self.step == 2:
                # 确认 code_tools_node 执行完毕并将 ToolMessage 回流给了 code_agent_node
                assert any(isinstance(m, ToolMessage) and m.name == "list_repo_directory" for m in messages)
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_read_2",
                            "name": "read_repo_file",
                            "args": {"rel_path": "orders/router.py", "start_line": 1, "end_line": 40},
                        }
                    ],
                )
            assert any(isinstance(m, ToolMessage) and m.name == "read_repo_file" for m in messages)
            return AIMessage(content="已通过多轮 ReAct 读取 orders/router.py 上下文。")

    fake_llm = _FakeToolLLM()
    monkeypatch.setattr(llm_client_manager, "has_valid_key", lambda: True)
    monkeypatch.setattr(llm_client_manager, "get_llm", lambda temperature=0.0: fake_llm)
    monkeypatch.setattr(
        evidence_subgraph,
        "_fallback_code_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("模型 ReAct 循环成功后不应走硬编码兜底")),
    )

    result = await code_probe_subgraph.ainvoke(
        {
            "probe_type": "source_code",
            "intent": "feature",
            "target_question": "新增一个订单导出接口",
            "repo_map": "orders/router.py:\n  def get_order",
            "worker_messages": [],
            "chat_fnames": [],
            "evidences": [],
            "gaps": [],
        },
        context=_make_ctx(tmp_path),
    )

    assert fake_llm.step == 3
    assert set(fake_llm.bound_tool_names) == {
        "list_repo_directory",
        "search_repo_code",
        "read_repo_file",
        "git_recent_commits",
    }
    assert result["chat_fnames"] == ["orders/router.py"]
    assert [ev.tool_name for ev in result["evidences"]] == [
        "ls_directory",
        "read_file_window",
    ]

    block = load_chat_files_block(tmp_path, result["chat_fnames"])
    assert "def get_order" in block


@pytest.mark.anyio
async def test_qa_intent_short_circuits_to_answer_only_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "billing.py").write_text("def pay():\n    raise TimeoutError('upstream timeout')\n", encoding="utf-8")
    (tmp_path / "app.log").write_text("ERROR tr_991 TimeoutError upstream timeout\n", encoding="utf-8")
    monkeypatch.setattr(llm_client_manager, "has_valid_key", lambda: False)

    fake_repo = _FakeTaskRepo()
    ctx = _make_ctx(tmp_path, task_repo=fake_repo, log_path=str(tmp_path / "app.log"))
    res = await main_dev_graph.ainvoke(
        {
            "task_id": "task_qa_only",
            "project_id": "proj_1",
            "user_query": "分析 app.log 中 TimeoutError 原因，不要修改代码",
            "messages": [],
        },
        config={"configurable": {"thread_id": "task_qa_only"}},
        context=ctx,
    )

    assert res["intent"] == "qa"
    assert res["diagnosis_report"]
    assert res.get("edit_plan") in (None, [])
    assert res.get("final_commit_sha") is None
    assert fake_repo.updates[-1]["status"] == "completed"
    assert len(fake_repo.evidences) >= 2
