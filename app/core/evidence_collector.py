"""证据采集与敏感信息自动脱敏模块 (复用自 my_swe/app/core/evidence_collector.py)."""

import hashlib
import json
import re

from app.config import now_iso
from app.schemas.domain_schemas import Evidence, EvidenceType

_SECRET_PATTERNS = [
    (
        re.compile(
            r"(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{6,})['\"]?"
        ),
        r"\1=***REDACTED***",
    ),
    (re.compile(r"\b1[3-9]\d{9}\b"), "138****REDACTED"),
    (re.compile(r"\bsk-[A-Za-z0-9]{12,}\b"), "sk-***REDACTED***"),
]

_TOOL_SOURCE_MAP: dict[str, tuple[EvidenceType, str]] = {
    "build_db_repo_map": ("db_query", "ev_db"),
    "inspect_table_schema": ("db_query", "ev_db"),
    "execute_readonly_sql": ("db_query", "ev_db"),
    "search_error_logs": ("log_error", "ev_log"),
    "get_trace_logs": ("log_error", "ev_log"),
    "scan_local_logs": ("log_error", "ev_log"),
    "ls_directory": ("source_code", "ev_code"),
    "grep_code": ("source_code", "ev_code"),
    "read_file_window": ("source_code", "ev_code"),
    "git_log_recent": ("source_code", "ev_code"),
    "search_error_cache": ("error_cache", "ev_cache"),
}


def redact_text(text: str, max_chars: int = 3800) -> str:
    """对工具返回的原始输出执行敏感凭证脱敏与长度截断."""
    cleaned = text or ""
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "\n...[TRUNCATED BY EVIDENCE COLLECTOR]..."
    return cleaned


def build_evidence(
    tool_name: str,
    tool_args: dict,
    raw_output: str,
    *,
    datasource_id: str | None = None,
    summary_override: str | None = None,
) -> Evidence:
    """将工具调用结果封装并签发为确定性短哈希 ID 的 Evidence 对象."""
    evidence_type, prefix = _TOOL_SOURCE_MAP.get(tool_name, ("source_code", "ev_code"))
    args_json = redact_text(
        json.dumps(tool_args, ensure_ascii=False, sort_keys=True), max_chars=1024
    )
    digest_seed = f"{tool_name}:{args_json}:{raw_output[:512]}"
    short_hash = hashlib.sha1(digest_seed.encode("utf-8")).hexdigest()[:6]
    evidence_id = f"{prefix}:{short_hash}"

    redacted_snippet = redact_text(raw_output, max_chars=3800)
    if summary_override:
        summary = summary_override[:480]
    else:
        first_line = next(
            (line.strip() for line in redacted_snippet.splitlines() if line.strip()),
            "Tool Output",
        )
        summary = f"[{tool_name}] 参数 {args_json} -> {first_line}"[:480]

    return Evidence(
        id=evidence_id,
        evidence_type=evidence_type,
        datasource_id=datasource_id,
        tool_name=tool_name,
        query_args=args_json,
        summary=summary,
        raw_snippet=redacted_snippet,
        retrieved_at=now_iso(),
    )
