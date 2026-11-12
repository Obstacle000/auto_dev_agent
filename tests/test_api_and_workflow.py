"""FastAPI 端到端链路测试 (项目注册 -> 数据源接入 -> 任务 SSE -> 沙箱子图 -> 人工审批归档)."""

import json
import subprocess
from pathlib import Path
from httpx import ASGITransport, AsyncClient
import pytest

from app.clients.mysql_client_manager import mysql_client_manager
from app.clients.redis_client_manager import redis_client_manager
from app.config import settings
from app.main import app
from app.models import Base
from app.schemas.domain_schemas import SandboxExecResult


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "calculator.py").write_text(
        "def subtract(a: int, b: int) -> int:\n    return a - b\n",
        encoding="utf-8",
    )
    (repo_dir / "test_calculator.py").write_text(
        "from calculator import subtract\n\ndef test_subtract():\n    assert subtract(5, 2) == 3\n",
        encoding="utf-8",
    )
    (repo_dir / "app.log").write_text(
        "ERROR subtract received unexpected value\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.name", "AutoDev"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)


def _parse_sse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        ev_name = ""
        ev_data = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                ev_name = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                ev_data = json.loads(line[len("data: ") :])
        if ev_name:
            events.append((ev_name, ev_data))
    return events


@pytest.mark.anyio
async def test_full_api_and_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "test_platform.db"
    repo_dir = tmp_path / "sample_repo"
    _init_git_repo(repo_dir)

    await mysql_client_manager.close()
    await redis_client_manager.close()

    monkeypatch.setattr(settings, "mysql_dsn", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(settings, "redis_enabled", False)
    monkeypatch.setattr(settings, "es_enabled", False)
    monkeypatch.setattr(settings, "qdrant_enabled", False)
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "sandbox_require_docker", False)

    # 在容器调用点注入沙箱测试桩，验证沙箱子图与主图中断/唤醒闭环
    from app.sandbox.container_runner import LinuxContainerSandbox

    monkeypatch.setattr(LinuxContainerSandbox, "ensure_container_started", lambda self: None)
    monkeypatch.setattr(LinuxContainerSandbox, "sync_to_container", lambda self, rel_path=None: True)
    monkeypatch.setattr(LinuxContainerSandbox, "cleanup", lambda self: None)
    monkeypatch.setattr(
        LinuxContainerSandbox,
        "run_command_in_container",
        lambda self, cmd, timeout=120: SandboxExecResult(
            passed=True,
            returncode=0,
            stdout="1 passed in 0.02s",
            combined_output="1 passed in 0.02s",
            command=cmd,
        ),
    )

    mysql_client_manager.init(f"sqlite+aiosqlite:///{db_file}")
    assert mysql_client_manager.engine is not None
    async with mysql_client_manager.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. 健康检查
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "healthy"

            # 2. 注册项目（不传 namespace 自动派生）
            proj_resp = await client.post(
                "/api/v1/projects",
                json={"name": "sample-calc", "repo_path": str(repo_dir)},
            )
            assert proj_resp.status_code == 200
            proj = proj_resp.json()
            assert proj["namespace"].startswith("sample_repo_")
            project_id = proj["project_id"]

            # 3. 发起 SSE 任务流 (不传数据库数据源，仅触发缓存/源码/本地日志降级)
            async with client.stream(
                "POST",
                "/api/v1/tasks/chat-stream",
                json={"project_id": project_id, "user_query": "排查 subtract 异常"},
            ) as stream_resp:
                assert stream_resp.status_code == 200
                raw_sse = (await stream_resp.aread()).decode("utf-8")

            events = _parse_sse_events(raw_sse)
            event_names = [name for name, _ in events]
            assert "task_init" in event_names
            assert "evidence_added" in event_names
            assert "plan_ready" in event_names
            assert "sandbox_result" in event_names
            assert "waiting_approval" in event_names

            task_init_data = next(data for name, data in events if name == "task_init")
            assert "db_query" not in task_init_data["active_probes"]
            task_id = task_init_data["task_id"]

            # 4. 查询任务详情与单条证据原文
            detail_resp = await client.get(f"/api/v1/tasks/{task_id}")
            assert detail_resp.status_code == 200
            detail = detail_resp.json()
            assert detail["status"] == "waiting_approval"
            assert len(detail["evidences"]) >= 2

            first_ev_id = detail["evidences"][0]["id"]
            ev_resp = await client.get(f"/api/v1/tasks/{task_id}/evidences/{first_ev_id}")
            assert ev_resp.status_code == 200
            assert ev_resp.json()["id"] == first_ev_id

            # 5. 人工驳回测试：应重新经过 sandbox_subgraph 并再次挂起为 waiting_approval (round 2)
            reject_resp = await client.post(
                f"/api/v1/tasks/{task_id}/approval",
                json={"decision": "rejected", "feedback": "补充边界条件校验"},
            )
            assert reject_resp.status_code == 200
            assert reject_resp.json()["status"] == "waiting_approval"

            detail_round2 = (await client.get(f"/api/v1/tasks/{task_id}")).json()
            assert len(detail_round2["patch_records"]) == 2
            assert detail_round2["patch_records"][0]["approval_status"] == "rejected"

            # 5.1 流式驳回接口测试：/approval-stream 实时推送 sandbox_result 与新一轮 waiting_approval (round 3)
            async with client.stream(
                "POST",
                f"/api/v1/tasks/{task_id}/approval-stream",
                json={"decision": "rejected", "feedback": "再补一条单测"},
            ) as reject_stream:
                assert reject_stream.status_code == 200
                reject_sse = (await reject_stream.aread()).decode("utf-8")
            reject_events = [name for name, _ in _parse_sse_events(reject_sse)]
            assert "sandbox_result" in reject_events
            assert "waiting_approval" in reject_events
            assert "task_done" not in reject_events

            # 6. 人工审批批准并归档报错缓存（验证自定义 commit_message 与重复审批防呆）
            approve_resp = await client.post(
                f"/api/v1/tasks/{task_id}/approval",
                json={"decision": "approved", "commit_message": "fix: custom approval commit"},
            )
            assert approve_resp.status_code == 200
            assert approve_resp.json()["status"] == "completed"

            repeat_resp = await client.post(
                f"/api/v1/tasks/{task_id}/approval",
                json={"decision": "approved"},
            )
            assert repeat_resp.status_code == 409
            assert repeat_resp.json()["error_code"] == "TASK_NOT_WAITING_APPROVAL"
            after_repeat = (await client.get(f"/api/v1/tasks/{task_id}")).json()
            assert after_repeat["status"] == "completed"

            # 7. 查询报错经验缓存表验证闭环归档
            cache_resp = await client.get(
                "/api/v1/error-cache",
                params={"namespace": proj["namespace"]},
            )
            assert cache_resp.status_code == 200
            assert cache_resp.json()["total"] == 1

            # 8. 数据源接入与脱敏检查
            ds_resp = await client.post(
                "/api/v1/datasources",
                json={
                    "project_id": project_id,
                    "name": "ro-mysql-demo",
                    "ds_type": "mysql",
                    "connection_uri": "mysql+aiomysql://readonly_user:secret_pwd@10.0.0.8:3306/orders",
                },
            )
            assert ds_resp.status_code == 200
            assert "secret_pwd" not in ds_resp.text
            assert ds_resp.json()["masked_endpoint"] == "10.0.0.8:3306/orders"
    finally:
        await mysql_client_manager.close()


@pytest.mark.anyio
async def test_sandbox_unavailable_and_seed_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.seed.seed_runner import run_seed
    import app.sandbox.container_runner as cr

    db_file = tmp_path / "seed_test.db"
    repo_dir = tmp_path / "seed_repo"
    _init_git_repo(repo_dir)

    await mysql_client_manager.close()
    monkeypatch.setattr(settings, "mysql_dsn", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(settings, "redis_enabled", False)
    monkeypatch.setattr(settings, "es_enabled", False)
    monkeypatch.setattr(settings, "qdrant_enabled", False)
    monkeypatch.setattr(settings, "sandbox_require_docker", True)
    monkeypatch.setattr(cr, "is_docker_engine_available", lambda: False)

    # 验证独立测试种子脚本运行正常
    await run_seed(repo_path=str(repo_dir))

    mysql_client_manager.init(f"sqlite+aiosqlite:///{db_file}")
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            proj = (
                await client.post(
                    "/api/v1/projects",
                    json={"name": "docker-check-proj", "repo_path": str(repo_dir), "namespace": "docker_chk"},
                )
            ).json()
            async with client.stream(
                "POST",
                "/api/v1/tasks/chat-stream",
                json={"project_id": proj["project_id"], "user_query": "触发沙箱检查"},
            ) as stream_resp:
                raw_sse = (await stream_resp.aread()).decode("utf-8")

            events = _parse_sse_events(raw_sse)
            err_events = [data for name, data in events if name == "task_error"]
            assert len(err_events) == 1
            assert err_events[0]["error_code"] == "SANDBOX_UNAVAILABLE"
    finally:
        await mysql_client_manager.close()
