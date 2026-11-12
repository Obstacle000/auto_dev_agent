"""只读源码探查工具 (严格限制在目标项目 repo_root 内)."""

import re
import subprocess
from pathlib import Path

from app.core.evidence_collector import build_evidence
from app.schemas.domain_schemas import Evidence

_IGNORE_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".sandboxes"}


def _safe_repo_path(repo_root: str | Path, rel_path: str) -> Path:
    root = Path(repo_root).resolve()
    clean = rel_path.replace("\\", "/").lstrip("/")
    target = (root / clean).resolve() if clean and clean != "." else root
    if target != root and root not in target.parents:
        raise ValueError(f"非法越界路径禁止读取: {rel_path}")
    return target


def ls_directory(repo_root: str | Path, rel_dir: str = ".") -> Evidence:
    """列出目标仓库目录树片段并签发源码证据."""
    target = _safe_repo_path(repo_root, rel_dir)
    entries: list[str] = []
    if target.is_dir():
        for item in sorted(target.iterdir()):
            if item.name in _IGNORE_DIRS:
                continue
            suffix = "/" if item.is_dir() else ""
            entries.append(f"{item.name}{suffix}")
    raw = "\n".join(entries) or "(empty directory)"
    return build_evidence(
        tool_name="ls_directory",
        tool_args={"dir": rel_dir},
        raw_output=raw,
        summary_override=f"[ls_directory] 目录 `{rel_dir}` 共 {len(entries)} 项",
    )


def grep_code(repo_root: str | Path, pattern: str, max_hits: int = 25) -> Evidence:
    """在仓库源码内按关键字/正则检索匹配行并签发源码证据."""
    root = Path(repo_root).resolve()
    regex = re.compile(pattern or ".", re.IGNORECASE)
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(
            p in _IGNORE_DIRS or p.startswith((".autodev.tags.cache", ".aider.tags.cache"))
            for p in path.parts
        ):
            continue
        if path.suffix not in {".py", ".js", ".ts", ".sql", ".md", ".yaml", ".yml"}:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if regex.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()}")
                if len(hits) >= max_hits:
                    break
        if len(hits) >= max_hits:
            break
    raw = "\n".join(hits) or f"未匹配到关键字 `{pattern}`"
    return build_evidence(
        tool_name="grep_code",
        tool_args={"pattern": pattern},
        raw_output=raw,
        summary_override=f"[grep_code] `{pattern}` 命中 {len(hits)} 处源码位置",
    )


def read_file_window(
    repo_root: str | Path,
    rel_path: str,
    start_line: int = 1,
    end_line: int = 120,
) -> Evidence:
    """读取文件指定行号窗口并签发 `source_code` 证据."""
    target = _safe_repo_path(repo_root, rel_path)
    if not target.is_file():
        raw = f"文件不存在: {rel_path}"
    else:
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line)
        numbered = [f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1)]
        raw = "\n".join(numbered) or "(empty file)"
    return build_evidence(
        tool_name="read_file_window",
        tool_args={"file": rel_path, "start_line": start_line, "end_line": end_line},
        raw_output=raw,
        summary_override=f"[read_file_window] 读取 `{rel_path}` L{start_line}-L{end_line}",
    )


def git_log_recent(repo_root: str | Path, limit: int = 5) -> Evidence:
    """查询仓库近期 Git 提交历史并签发证据."""
    res = subprocess.run(
        ["git", "log", f"-n{limit}", "--oneline"],
        cwd=str(Path(repo_root).resolve()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    raw = res.stdout.strip() or "无 Git 提交历史"
    return build_evidence(
        tool_name="git_log_recent",
        tool_args={"limit": limit},
        raw_output=raw,
        summary_override=f"[git_log_recent] 获取最近 {limit} 条提交记录",
    )
