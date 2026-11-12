"""日志探针工具：优先直读用户配置的 ES 日志索引，未配置则降级扫描本地日志文件."""

import json
import re
from pathlib import Path

from app.config import settings
from app.core.evidence_collector import build_evidence
from app.repositories.es.runtime_log_es_repo import RuntimeLogESRepository
from app.schemas.domain_schemas import Evidence

_IGNORE_PARTS = {".git", ".venv", "venv", "node_modules", ".sandboxes", "__pycache__"}
_ERROR_LINE_RE = re.compile(r"(ERROR|CRITICAL|Exception|Traceback|Error:)", re.IGNORECASE)


def _discover_local_log_files(repo_root: str | Path, log_path: str | None) -> list[Path]:
    root = Path(repo_root).resolve()
    candidates: list[Path] = []
    if log_path:
        clean_rel = log_path.replace("\\", "/").lstrip("/")
        explicit = (root / clean_rel).resolve()
        if (explicit == root or root in explicit.parents) and explicit.is_file():
            candidates.append(explicit)
            return candidates

    for pattern in settings.log_scan_globs:
        for matched in sorted(root.glob(pattern)):
            if not matched.is_file():
                continue
            if any(part in _IGNORE_PARTS for part in matched.parts):
                continue
            if matched not in candidates:
                candidates.append(matched)
    if not candidates:
        for matched in sorted(root.rglob("*.log")):
            if not matched.is_file():
                continue
            if any(part in _IGNORE_PARTS for part in matched.parts):
                continue
            candidates.append(matched)
            if len(candidates) >= 5:
                break
    return candidates[:5]


def _scan_files_for_errors(
    files: list[Path], repo_root: Path, keyword: str = "", max_lines: int = 80
) -> str:
    extracted: list[str] = []
    kw = keyword.strip().lower()
    for fpath in files:
        rel = fpath.relative_to(repo_root).as_posix()
        try:
            lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        tail = lines[-1000:]
        matched_chunks: list[str] = []
        for idx, line in enumerate(tail):
            if _ERROR_LINE_RE.search(line) or (kw and kw in line.lower()):
                start = max(0, idx - 1)
                end = min(len(tail), idx + 4)
                block = "\n".join(tail[start:end])
                if block not in matched_chunks:
                    matched_chunks.append(block)
        if matched_chunks:
            extracted.append(f"=== {rel} ===\n" + "\n---\n".join(matched_chunks[-10:]))
        elif tail:
            extracted.append(f"=== {rel} (tail) ===\n" + "\n".join(tail[-20:]))

    combined = "\n\n".join(extracted)
    lines_out = combined.splitlines()
    if len(lines_out) > max_lines:
        return "\n".join(lines_out[:max_lines])
    return combined


async def probe_runtime_logs(
    *,
    repo_root: str,
    keyword: str,
    runtime_log_es_repo: RuntimeLogESRepository | None = None,
    es_log_index: str | None = None,
    datasource_id: str | None = None,
    log_path: str | None = None,
    trace_id: str | None = None,
) -> tuple[list[Evidence], list[str]]:
    """按 ES 直读 -> 本地日志文件降级策略采集 `log_error` 证据."""
    evidences: list[Evidence] = []
    gaps: list[str] = []

    if runtime_log_es_repo is not None and es_log_index:
        if trace_id:
            trace_logs = await runtime_log_es_repo.get_trace_logs(
                es_log_index, trace_id=trace_id
            )
            if trace_logs:
                raw = json.dumps(trace_logs, ensure_ascii=False, indent=2)
                evidences.append(
                    build_evidence(
                        tool_name="get_trace_logs",
                        tool_args={"es_index": es_log_index, "trace_id": trace_id},
                        raw_output=raw,
                        datasource_id=datasource_id,
                        summary_override=f"[get_trace_logs] ES 索引 {es_log_index} 命中 {len(trace_logs)} 条链路日志",
                    )
                )

        err_logs = await runtime_log_es_repo.search_error_logs(
            es_log_index, keyword=keyword, level="ERROR"
        )
        if err_logs:
            raw = json.dumps(err_logs, ensure_ascii=False, indent=2)
            evidences.append(
                build_evidence(
                    tool_name="search_error_logs",
                    tool_args={"es_index": es_log_index, "keyword": keyword},
                    raw_output=raw,
                    datasource_id=datasource_id,
                    summary_override=f"[search_error_logs] ES 索引 {es_log_index} 命中 {len(err_logs)} 条报错日志",
                )
            )
        elif not evidences:
            gaps.append(f"ES 日志索引 `{es_log_index}` 未检索到匹配报错日志")
        return evidences, gaps

    root = Path(repo_root).resolve()
    files = _discover_local_log_files(root, log_path)
    if not files:
        gaps.append("未配置 ES 日志索引且工作区未发现可扫描的本地日志文件")
        return evidences, gaps

    snippet = _scan_files_for_errors(files, root, keyword=keyword)
    if not snippet.strip():
        gaps.append("本地日志文件中未检索到报错堆栈")
        return evidences, gaps

    scanned_rel = [f.relative_to(root).as_posix() for f in files]
    evidences.append(
        build_evidence(
            tool_name="scan_local_logs",
            tool_args={"files": scanned_rel, "keyword": keyword},
            raw_output=snippet,
            datasource_id=None,
            summary_override=f"[scan_local_logs] 本地降级扫描 {', '.join(scanned_rel)} 提取报错片段",
        )
    )
    return evidences, gaps
