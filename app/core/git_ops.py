"""轻量 Git CLI 包装器 (参考 aider/repo.py，使用 subprocess 替代重型句柄)."""

import subprocess
from pathlib import Path


def _run_git(repo_root: str | Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(Path(repo_root).resolve()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=check,
    )


def get_head_sha(repo_root: str | Path) -> str:
    res = _run_git(repo_root, ["rev-parse", "--short", "HEAD"], check=False)
    return res.stdout.strip() if res.returncode == 0 else "NO_COMMIT"


def get_current_branch(repo_root: str | Path) -> str:
    res = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    return res.stdout.strip() if res.returncode == 0 else "main"


def checkout_or_create_branch(repo_root: str | Path, branch_name: str) -> str:
    check = _run_git(repo_root, ["rev-parse", "--verify", branch_name], check=False)
    if check.returncode == 0:
        _run_git(repo_root, ["checkout", branch_name])
    else:
        _run_git(repo_root, ["checkout", "-b", branch_name])
    return branch_name


def compute_diff_against_dir(base_dir: str | Path, modified_dir: str | Path, files: list[str]) -> str:
    """对比基线仓库与沙箱修改目录生成标准 Unified Diff."""
    base = Path(base_dir).resolve()
    mod = Path(modified_dir).resolve()
    diffs: list[str] = []
    for rel in files:
        norm = rel.replace("\\", "/").lstrip("/")
        a_file = base / norm
        b_file = mod / norm
        if not b_file.exists():
            continue
        a_arg = str(a_file) if a_file.exists() else "/dev/null"
        res = subprocess.run(
            ["git", "diff", "--no-index", "--unified=3", "--", a_arg, str(b_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if res.stdout:
            cleaned = res.stdout.replace(str(a_file), f"a/{norm}").replace(str(b_file), f"b/{norm}")
            diffs.append(cleaned)
    return "\n".join(diffs)


def apply_files_and_commit(
    repo_root: str | Path,
    sandbox_dir: str | Path,
    files: list[str],
    branch_name: str,
    commit_message: str,
) -> str:
    """将人工审批通过后的沙箱修改同步回目标仓库特性分支并提交."""
    repo = Path(repo_root).resolve()
    sbox = Path(sandbox_dir).resolve()
    checkout_or_create_branch(repo, branch_name)
    for rel in files:
        norm = rel.replace("\\", "/").lstrip("/")
        src = sbox / norm
        dst = repo / norm
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    if files:
        _run_git(repo, ["add", "--", *[f.replace("\\", "/").lstrip("/") for f in files]])
    res = _run_git(repo, ["commit", "-m", commit_message], check=False)
    if res.returncode != 0 and "nothing to commit" not in (res.stdout + res.stderr).lower():
        raise RuntimeError(f"Git commit 失败: {res.stderr}")
    return get_head_sha(repo)
