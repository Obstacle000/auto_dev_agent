"""REST 与 SSE 接口请求响应模型 (严格对应 docs/03_API.md)."""

from typing import Any
from pydantic import BaseModel, Field

from app.schemas.domain_schemas import ApprovalDecision, DatasourceKind, EvidenceType, TaskStatus


class ErrorResponse(BaseModel):
    """统一业务错误响应体."""

    error_code: str
    message: str


class TaskStreamRequest(BaseModel):
    """`POST /api/v1/tasks/chat-stream` 入参."""

    task_id: str | None = None
    project_id: str
    user_query: str = Field(..., min_length=1)
    datasource_ids: list[str] | None = None
    log_path: str | None = None


class TaskApprovalRequest(BaseModel):
    """`POST /api/v1/tasks/{task_id}/approval` 入参."""

    decision: ApprovalDecision
    feedback: str | None = None
    commit_message: str | None = None


class TaskApprovalResponse(BaseModel):
    """`POST /api/v1/tasks/{task_id}/approval` 出参."""

    task_id: str
    status: TaskStatus
    decision: ApprovalDecision
    branch_name: str | None = None
    commit_sha: str | None = None
    message: str


class EvidenceItem(BaseModel):
    id: str
    evidence_type: EvidenceType
    datasource_id: str | None = None
    tool_name: str
    summary: str
    created_at: str


class EvidenceDetailResponse(BaseModel):
    id: str
    task_id: str
    evidence_type: EvidenceType
    datasource_id: str | None = None
    tool_name: str
    query_args: str
    summary: str
    raw_snippet: str
    created_at: str


class PatchItem(BaseModel):
    id: str
    round_no: int
    edited_files: list[str]
    patch_diff: str
    test_passed: bool
    retry_count: int
    test_output: str | None = None
    approval_status: str
    human_feedback: str | None = None
    created_at: str


class MessageItem(BaseModel):
    turn_index: int
    role: str
    content: str
    created_at: str


class TaskDetailResponse(BaseModel):
    task_id: str
    project_id: str
    status: TaskStatus
    title: str
    user_query: str
    evidences: list[EvidenceItem] = Field(default_factory=list)
    patch_records: list[PatchItem] = Field(default_factory=list)
    messages: list[MessageItem] = Field(default_factory=list)


class ProjectCreateRequest(BaseModel):
    """`POST /api/v1/projects` 入参."""

    name: str = Field(..., min_length=1, max_length=128)
    namespace: str | None = Field(default=None, max_length=128)
    repo_path: str = Field(..., min_length=1)
    default_branch: str = "main"
    test_cmd: str = "pytest -q"
    lint_cmd: str | None = None
    sandbox_image: str = "python:3.12-slim"


class ProjectResponse(BaseModel):
    project_id: str
    name: str
    namespace: str
    repo_path: str
    default_branch: str
    test_cmd: str
    lint_cmd: str | None = None
    sandbox_image: str


class DatasourceCreateRequest(BaseModel):
    """`POST /api/v1/datasources` 入参."""

    project_id: str
    name: str = Field(..., min_length=1, max_length=128)
    ds_type: DatasourceKind
    es_index_name: str | None = Field(default=None, max_length=128)
    connection_uri: str = Field(..., min_length=1)
    readonly_verified: bool = True


class DatasourceResponse(BaseModel):
    ds_id: str
    project_id: str
    name: str
    ds_type: DatasourceKind
    es_index_name: str | None = None
    masked_endpoint: str
    enabled: bool


class DatasourceTestResponse(BaseModel):
    ds_id: str
    reachable: bool
    message: str


class ErrorCacheSearchRequest(BaseModel):
    """`POST /api/v1/error-cache/search` 入参."""

    namespace: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=3, ge=1, le=20)


class ErrorCacheSearchResponse(BaseModel):
    bm25_hits: list[dict[str, Any]]
    vector_hits: list[dict[str, Any]]
    fused_results: list[dict[str, Any]]


class ErrorCacheItem(BaseModel):
    cache_id: str
    namespace: str
    project_id: str
    source_task_id: str | None = None
    error_signature: str
    error_keywords: str
    symptoms: str
    root_cause: str
    resolution: str
    created_at: str
