"""Linux Docker 容器沙箱运行器 (参考 my_swe/app/sandbox/container_runner.py：docker cp + docker exec)."""

import shutil
import subprocess
from pathlib import Path

from app.config import PROJECT_ROOT, settings
from app.exceptions import SandboxUnavailableError
from app.schemas.domain_schemas import SandboxExecResult


def is_docker_engine_available() -> bool:
    """检测当前宿主机 Docker 守护进程是否可用."""
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


class LinuxContainerSandbox:
    """基于 `docker cp` 与 `docker exec` 的隔离 Linux 测试容器管理器."""

    def __init__(
        self,
        *,
        task_id: str,
        src_repo: str | Path,
        image: str | None = None,
        sandbox_root: str | Path | None = None,
    ) -> None:
        self.task_id = task_id
        self.src_repo = Path(src_repo).resolve()
        self.image = image or settings.sandbox_image
        safe_id = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in task_id)
        self.container_name = f"autodev_sandbox_{safe_id[:40]}"
        self.container_workdir = "/workspace"
        base_dir = Path(sandbox_root) if sandbox_root else (PROJECT_ROOT / ".sandboxes")
        self.local_sandbox_dir = (base_dir / safe_id).resolve()

    def prepare_local_workspace(self) -> Path:
        """将目标项目仓库复制到独立隔离临时目录 (不含 .git 破坏)."""
        if self.local_sandbox_dir.exists():
            shutil.rmtree(self.local_sandbox_dir, ignore_errors=True)
        self.local_sandbox_dir.parent.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            ".sandboxes",
            "node_modules",
            ".autodev.tags.cache*",
            ".aider.tags.cache*",
        )
        shutil.copytree(self.src_repo, self.local_sandbox_dir, ignore=ignore)
        return self.local_sandbox_dir

    def ensure_container_started(self) -> None:
        """启动长驻隔离 Linux 容器，禁止挂载宿主机目录."""
        if not is_docker_engine_available():
            if settings.sandbox_require_docker:
                raise SandboxUnavailableError()
            return

        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
            check=False,
        )
        res = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.container_name,
                "--workdir",
                self.container_workdir,
                self.image,
                "sleep",
                "3600",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if res.returncode != 0:
            raise SandboxUnavailableError(f"启动 Linux 容器失败: {res.stderr.strip()}")

    def sync_to_container(self, rel_path: str | None = None) -> bool:
        """通过 `docker cp` 推送本地沙箱改动至容器 `/workspace`."""
        if not is_docker_engine_available():
            if settings.sandbox_require_docker:
                raise SandboxUnavailableError()
            return True

        if rel_path:
            clean_rel = rel_path.replace("\\", "/").lstrip("/")
            local_file = self.local_sandbox_dir / clean_rel
            if not local_file.exists():
                return False
            parent_dir = str(Path(f"{self.container_workdir}/{clean_rel}").parent).replace("\\", "/")
            subprocess.run(
                ["docker", "exec", self.container_name, "mkdir", "-p", parent_dir],
                capture_output=True,
                check=False,
            )
            res = subprocess.run(
                [
                    "docker",
                    "cp",
                    str(local_file),
                    f"{self.container_name}:{self.container_workdir}/{clean_rel}",
                ],
                capture_output=True,
                check=False,
            )
            return res.returncode == 0

        subprocess.run(
            ["docker", "exec", self.container_name, "mkdir", "-p", self.container_workdir],
            capture_output=True,
            check=False,
        )
        res = subprocess.run(
            [
                "docker",
                "cp",
                f"{str(self.local_sandbox_dir)}/.",
                f"{self.container_name}:{self.container_workdir}/",
            ],
            capture_output=True,
            check=False,
        )
        return res.returncode == 0

    def run_command_in_container(self, command: str, timeout: int = 120) -> SandboxExecResult:
        """通过 `docker exec` 在 Linux 容器内运行 lint 或单元测试."""
        if not is_docker_engine_available():
            raise SandboxUnavailableError()

        proc = subprocess.run(
            ["docker", "exec", "-w", self.container_workdir, self.container_name, "sh", "-c", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            check=False,
        )
        combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        return SandboxExecResult(
            passed=(proc.returncode == 0),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            combined_output=combined,
            command=command,
        )

    def cleanup(self) -> None:
        """销毁临时 Linux 测试容器."""
        if is_docker_engine_available():
            subprocess.run(
                ["docker", "rm", "-f", self.container_name],
                capture_output=True,
                check=False,
            )
