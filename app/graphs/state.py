"""LangGraph 主图与双子图 State 定义 (对应 docs/05_LANGGRAPH.md 4.1~4.3 节)."""

import operator
from typing import Annotated, Any, Literal, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.schemas.domain_schemas import Evidence, EvidenceType, TaskIntent


def _dedup_evidences(
    left: list[Evidence] | None, right: list[Evidence] | None
) -> list[Evidence]:
    """合并并按 evidence.id 去重，避免并发探针或子图回写重复."""
    merged: list[Evidence] = []
    seen: set[str] = set()
    for item in [*(left or []), *(right or [])]:
        if item.id not in seen:
            seen.add(item.id)
            merged.append(item)
    return merged


def _merge_str_list(
    left: list[str] | None, right: list[str] | None
) -> list[str]:
    """按顺序去重合并文件路径/标识列表，跨节点与跨轮对话累积上下文."""
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*(left or []), *(right or [])]:
        norm = item.replace("\\", "/").strip()
        if norm and norm not in seen:
            seen.add(norm)
            merged.append(norm)
    return merged


def _merge_non_empty_str(left: str | None, right: str | None) -> str:
    """并发子图回写字符串状态时保留最新的非空文本，避免空闲分支覆写为空."""
    if right and right.strip():
        return right
    return left or ""


class AutoDevMainState(TypedDict):
    """主图状态."""

    task_id: str
    project_id: str
    user_query: str
    messages: Annotated[list[AnyMessage], add_messages]

    # 上下文与探查开关 (对齐 aider RepoMap / get_ranked_tags_map 参数契约 + 独立 DBRepoMap)
    repo_map: str
    db_repo_map: Annotated[str, _merge_non_empty_str]
    chat_fnames: Annotated[list[str], _merge_str_list]
    other_fnames: list[str]
    max_map_tokens: int | None
    mentioned_fnames: list[str]
    mentioned_idents: list[str]
    base_commit_sha: str
    has_db_datasource: bool
    has_log_datasource: bool
    intent: TaskIntent
    enabled_probes: list[EvidenceType]

    # 证据探查输出
    evidences: Annotated[list[Evidence], _dedup_evidences]
    gaps: Annotated[list[str], operator.add]
    diagnosis_report: str | None

    # 修改规划与沙箱输出
    edit_plan: list[dict[str, Any]]
    edited_files: list[str]
    patch_diff: str
    test_passed: bool
    test_output: str
    local_sandbox_dir: str

    # 人工审批与交付
    approval_status: Literal["pending", "approved", "rejected"]
    human_feedback: str | None
    commit_message: str | None
    target_branch: str | None
    final_commit_sha: str | None
    cache_archived: bool


class EvidenceSubState(TypedDict):
    """证据子图入口/汇聚状态."""

    user_query: str
    repo_map: str
    db_repo_map: Annotated[str, _merge_non_empty_str]
    has_db_datasource: bool
    has_log_datasource: bool
    intent: TaskIntent
    enabled_probes: list[EvidenceType]
    chat_fnames: Annotated[list[str], _merge_str_list]
    evidences: Annotated[list[Evidence], _dedup_evidences]
    gaps: Annotated[list[str], operator.add]


class ProbeWorkerState(TypedDict):
    """单个证据探针并发 Worker 状态."""

    probe_type: Literal["error_cache", "db_query", "log_error", "source_code"]
    intent: TaskIntent
    target_question: str
    repo_map: str
    db_repo_map: Annotated[str, _merge_non_empty_str]
    worker_messages: Annotated[list[AnyMessage], add_messages]
    chat_fnames: Annotated[list[str], _merge_str_list]
    evidences: Annotated[list[Evidence], _dedup_evidences]
    gaps: Annotated[list[str], operator.add]


class CodeProbeOutputState(TypedDict):
    """源码 ReAct 探针出图精简契约（屏蔽 worker_messages / probe_type 等内部字段，避免并发冲突）."""

    chat_fnames: Annotated[list[str], _merge_str_list]
    evidences: Annotated[list[Evidence], _dedup_evidences]
    gaps: Annotated[list[str], operator.add]


class DBProbeOutputState(TypedDict):
    """数据库 ReAct 探针出图精简契约（屏蔽 worker_messages / probe_type 等内部字段，避免并发冲突）."""

    db_repo_map: Annotated[str, _merge_non_empty_str]
    evidences: Annotated[list[Evidence], _dedup_evidences]
    gaps: Annotated[list[str], operator.add]


class SandboxSubState(TypedDict):
    """Linux 容器沙箱子图状态."""

    edit_plan: list[dict[str, Any]]
    evidences: list[Evidence]
    repo_map: str
    db_repo_map: str
    chat_fnames: list[str]
    intent: TaskIntent
    user_query: str
    human_feedback: str | None
    local_sandbox_dir: str
    pending_patch_text: str
    edited_files: list[str]
    patch_diff: str
    test_passed: bool
    test_output: str
    retry_count: int
    max_retries: int
