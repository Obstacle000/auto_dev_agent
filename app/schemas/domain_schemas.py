"""核心领域模型与命名空间/ID 组成规范 (对应 docs/04_DATABASE.md 3.8 节)."""

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, Field

EvidenceType = Literal["db_query", "log_error", "source_code", "error_cache"]
DatasourceKind = Literal["mysql", "postgres", "elasticsearch", "log_file"]
TaskStatus = Literal["running", "waiting_approval", "completed", "failed"]
ApprovalDecision = Literal["approved", "rejected"]
TaskIntent = Literal["bugfix", "feature", "qa"]


def derive_project_namespace(
    repo_path: str,
    explicit_namespace: str | None = None,
) -> str:
    """推导项目隔离命名空间.

    优先采用用户传入的 explicit_namespace；未传入时生成
    `{dir_slug}_{sha1(abs_repo_path)[:4]}`。
    """
    if explicit_namespace and explicit_namespace.strip():
        slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", explicit_namespace.strip()).strip("_")
        if slug:
            return slug.lower()[:128]

    abs_path = str(Path(repo_path).resolve())
    dir_name = Path(abs_path).name or "project"
    dir_slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", dir_name).strip("_").lower() or "project"
    short_hash = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:4]
    return f"{dir_slug[:100]}_{short_hash}"


def build_cache_id(namespace: str, error_signature: str, root_cause: str) -> str:
    """基于归一化命名空间+异常签名+根因生成幂等报错缓存 ID (`err_{sha1[:10]}`)."""
    seed = f"{namespace.strip().lower()}|{error_signature.strip()}|{root_cause.strip()}"
    return f"err_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:10]}"


def build_es_cache_doc_id(namespace: str, cache_id: str) -> str:
    """生成 ES 报错缓存文档主键 `es_{namespace}_{cache_id}`."""
    return f"es_{namespace}_{cache_id}"


def build_qdrant_point_id(namespace: str, cache_id: str) -> str:
    """生成确定性 UUIDv5 Qdrant Point ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{namespace}:{cache_id}"))


class Evidence(BaseModel):
    """由 evidence_collector 签发的标准化证据对象."""

    id: str
    evidence_type: EvidenceType
    datasource_id: str | None = None
    tool_name: str
    query_args: str
    summary: str
    raw_snippet: str
    retrieved_at: str


class EditPlanStep(BaseModel):
    """文件级修改计划步骤."""

    file_path: str
    action: Literal["modify", "create", "delete"] = "modify"
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)


class ErrorCacheRecord(BaseModel):
    """报错经验缓存三端同步标准载荷."""

    cache_id: str
    namespace: str
    project_id: str
    source_task_id: str | None = None
    error_signature: str
    error_keywords: str
    symptoms: str
    root_cause: str
    resolution: str
    created_at: str | None = None


class ErrorCacheHit(BaseModel):
    """单路或 RRF 融合后的报错缓存命中记录."""

    cache_id: str
    error_signature: str = ""
    root_cause: str = ""
    resolution: str = ""
    bm25_score: float | None = None
    vector_score: float | None = None
    rrf_score: float | None = None


class SandboxExecResult(BaseModel):
    """Linux 容器沙箱执行结果."""

    passed: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    combined_output: str = ""
    command: str = ""
