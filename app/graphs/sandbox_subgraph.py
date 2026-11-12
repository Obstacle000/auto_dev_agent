"""子图 2：Linux 容器沙箱子图 (`sandbox_subgraph`)."""

from pathlib import Path
from typing import Literal
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.clients.llm_client_manager import llm_client_manager
from app.config import settings
from app.core.chat_chunks import ChatChunks, load_chat_files_block
from app.core.edit_applier import apply_edit_blocks, parse_search_replace_blocks
from app.core.git_ops import compute_diff_against_dir
from app.graphs.context import AutoDevContext
from app.graphs.state import SandboxSubState
from app.sandbox.container_runner import LinuxContainerSandbox


def _emit_custom(event: str, data: dict) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if writer:
            writer({"event": event, "data": data})
    except Exception:
        pass


def _sandbox_for_state(ctx: AutoDevContext) -> LinuxContainerSandbox:
    return LinuxContainerSandbox(
        task_id=ctx["task_id"],
        src_repo=ctx["repo_root"],
        image=ctx["sandbox_image"],
    )


async def init_sandbox_node(
    state: SandboxSubState, runtime: Runtime[AutoDevContext]
) -> dict:
    """复制目标工作区至隔离沙箱目录并启动 Linux 容器."""
    ctx = runtime.context
    sandbox = _sandbox_for_state(ctx)
    local_dir = sandbox.prepare_local_workspace()
    sandbox.ensure_container_started()
    return {
        "local_sandbox_dir": str(local_dir),
        "retry_count": 0,
        "max_retries": settings.sandbox_max_retries,
        "edited_files": [],
        "patch_diff": "",
        "test_passed": False,
        "test_output": "",
        "pending_patch_text": "",
    }


async def generate_patch_node(
    state: SandboxSubState, runtime: Runtime[AutoDevContext]
) -> dict:
    """结合证据、规划与失败测试日志生成 SEARCH/REPLACE 补丁."""
    ctx = runtime.context
    if not llm_client_manager.has_valid_key():
        return {"pending_patch_text": ""}

    evidences_text = "\n".join(
        f"[{ev.id}] ({ev.evidence_type}) {ev.summary}\n{ev.raw_snippet[:800]}"
        for ev in state.get("evidences", [])[:8]
    )
    chat_files_block = load_chat_files_block(
        state.get("local_sandbox_dir") or ctx["repo_root"],
        list(state.get("chat_fnames") or []),
    )
    feedback = state.get("human_feedback") or ""
    prior_err = state.get("test_output") or ""
    role_desc = (
        "你是自动功能开发工程师"
        if state.get("intent") == "feature"
        else "你是自动代码修复工程师"
    )
    sys_prompt = (
        f"{role_desc}。请严格使用以下格式输出需要修改或新建的文件块：\n"
        "relative/path/to/file.py\n"
        "<<<<<<< SEARCH\n原有精确代码片段（若新建文件则 SEARCH 段留空）\n=======\n修改后或新建的完整代码片段\n>>>>>>> REPLACE\n"
        "所有修改或创建必须以提供的源码与上下文证据为依据，确保语法正确并通过测试。"
    )
    query_parts = [f"任务需求：{state.get('user_query', '')}"]
    if state.get("edit_plan"):
        query_parts.append(f"修改计划：{state['edit_plan']}")
    if feedback:
        query_parts.append(f"人工审批驳回意见：{feedback}")
    if prior_err:
        query_parts.append(f"上轮沙箱测试失败输出：\n{prior_err[-1500:]}")

    chunks = ChatChunks(
        system_prompt=sys_prompt,
        repo_map=state.get("repo_map", ""),
        db_repo_map=state.get("db_repo_map", ""),
        chat_files_block=chat_files_block,
        evidence_block=evidences_text,
        user_query="\n\n".join(query_parts),
    )
    resp = await llm_client_manager.get_llm(temperature=0.0).ainvoke(chunks.to_messages())
    return {"pending_patch_text": str(resp.content or "")}


async def apply_and_sync_node(
    state: SandboxSubState, runtime: Runtime[AutoDevContext]
) -> dict:
    """解析 SEARCH/REPLACE 落盘沙箱目录，并通过 `docker cp` 同步进 Linux 容器."""
    ctx = runtime.context
    local_dir = state.get("local_sandbox_dir", "")
    patch_text = state.get("pending_patch_text", "")
    edited = list(state.get("edited_files", []))

    blocks = parse_search_replace_blocks(patch_text)
    if blocks and local_dir:
        newly_edited = apply_edit_blocks(local_dir, blocks)
        for rel in newly_edited:
            if rel not in edited:
                edited.append(rel)

    sandbox = _sandbox_for_state(ctx)
    if sandbox.local_sandbox_dir.exists():
        if blocks:
            for rel in edited:
                sandbox.sync_to_container(rel)
        else:
            sandbox.sync_to_container()
    return {"edited_files": edited}


async def run_sandbox_tests_node(
    state: SandboxSubState, runtime: Runtime[AutoDevContext]
) -> dict:
    """通过 `docker exec` 在 Linux 容器内执行 lint 与单元测试."""
    ctx = runtime.context
    sandbox = _sandbox_for_state(ctx)
    outputs: list[str] = []
    passed = True

    if ctx.get("lint_cmd"):
        lint_res = sandbox.run_command_in_container(ctx["lint_cmd"])
        outputs.append(f"$ {lint_res.command}\n{lint_res.combined_output}")
        if not lint_res.passed:
            passed = False

    if passed:
        test_res = sandbox.run_command_in_container(ctx["test_cmd"])
        outputs.append(f"$ {test_res.command}\n{test_res.combined_output}")
        passed = test_res.passed

    combined = "\n\n".join(outputs).strip()
    _emit_custom(
        "sandbox_result",
        {
            "test_passed": passed,
            "retry_count": state.get("retry_count", 0),
            "test_output": combined[-1500:],
        },
    )
    return {"test_passed": passed, "test_output": combined}


async def collect_error_node(
    state: SandboxSubState, runtime: Runtime[AutoDevContext]
) -> dict:
    """递增自愈重试计数，带回失败堆栈给下一轮补丁生成."""
    return {"retry_count": state.get("retry_count", 0) + 1}


def route_after_tests(
    state: SandboxSubState,
) -> Literal["collect_error_node", "export_diff_node"]:
    if not state.get("test_passed") and state.get("retry_count", 0) < state.get(
        "max_retries", settings.sandbox_max_retries
    ):
        return "collect_error_node"
    return "export_diff_node"


async def export_diff_node(
    state: SandboxSubState, runtime: Runtime[AutoDevContext]
) -> dict:
    """生成标准 Unified Git Diff 并销毁测试容器."""
    ctx = runtime.context
    local_dir = Path(state["local_sandbox_dir"])
    edited_files = state.get("edited_files", [])
    diff_text = compute_diff_against_dir(ctx["repo_root"], local_dir, edited_files)

    sandbox = _sandbox_for_state(ctx)
    sandbox.cleanup()
    return {
        "edited_files": edited_files,
        "patch_diff": diff_text,
    }


def build_sandbox_subgraph():
    """构建 Linux 容器沙箱子图."""
    builder = StateGraph(
        state_schema=SandboxSubState,
        context_schema=AutoDevContext,
    )
    builder.add_node("init_sandbox_node", init_sandbox_node)
    builder.add_node("generate_patch_node", generate_patch_node)
    builder.add_node("apply_and_sync_node", apply_and_sync_node)
    builder.add_node("run_sandbox_tests_node", run_sandbox_tests_node)
    builder.add_node("collect_error_node", collect_error_node)
    builder.add_node("export_diff_node", export_diff_node)

    builder.add_edge(START, "init_sandbox_node")
    builder.add_edge("init_sandbox_node", "generate_patch_node")
    builder.add_edge("generate_patch_node", "apply_and_sync_node")
    builder.add_edge("apply_and_sync_node", "run_sandbox_tests_node")
    builder.add_conditional_edges(
        "run_sandbox_tests_node",
        route_after_tests,
        {
            "collect_error_node": "collect_error_node",
            "export_diff_node": "export_diff_node",
        },
    )
    builder.add_edge("collect_error_node", "generate_patch_node")
    builder.add_edge("export_diff_node", END)
    return builder.compile()


sandbox_subgraph = build_sandbox_subgraph()
