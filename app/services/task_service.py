"""研发任务生命周期编排、SSE 推流与人工审批唤醒服务 (严禁任何测试代码或硬编码)."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.config import now_iso, settings
from app.core.crypto import decrypt_uri
from app.core.datasource_registry import DatasourceConfig, DatasourceRegistry
from app.exceptions import (
    AppError,
    DatasourceNotFoundError,
    ProjectNotFoundError,
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskNotWaitingApprovalError,
)
from app.graphs.context import AutoDevContext
from app.graphs.main_graph import main_dev_graph
from app.models.orm_models import DevTaskORM
from app.repositories.es.error_cache_es_repo import ErrorCacheESRepository
from app.repositories.es.runtime_log_es_repo import RuntimeLogESRepository
from app.repositories.mysql.datasource_repo import DatasourceRepository
from app.repositories.mysql.error_cache_repo import ErrorCacheRepository
from app.repositories.mysql.project_repo import ProjectRepository
from app.repositories.mysql.task_repo import TaskRepository
from app.repositories.qdrant.error_cache_qdrant_repo import ErrorCacheQdrantRepository
from app.repositories.redis.task_cache_repo import TaskCacheRepository
from app.schemas.api_schemas import (
    EvidenceDetailResponse,
    EvidenceItem,
    MessageItem,
    PatchItem,
    TaskApprovalRequest,
    TaskApprovalResponse,
    TaskDetailResponse,
    TaskStreamRequest,
)


def _format_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class TaskService:
    """协调任务创建、上下文装配、LangGraph 流式推流与审批恢复."""

    def __init__(
        self,
        *,
        project_repo: ProjectRepository,
        datasource_repo: DatasourceRepository,
        task_repo: TaskRepository,
        error_cache_repo: ErrorCacheRepository,
        error_cache_es_repo: ErrorCacheESRepository,
        error_cache_qdrant_repo: ErrorCacheQdrantRepository,
        runtime_log_es_repo: RuntimeLogESRepository | None,
        task_cache_repo: TaskCacheRepository,
        embedding_client: Any,
        graph: Any = None,
    ) -> None:
        self.project_repo = project_repo
        self.datasource_repo = datasource_repo
        self.task_repo = task_repo
        self.error_cache_repo = error_cache_repo
        self.error_cache_es_repo = error_cache_es_repo
        self.error_cache_qdrant_repo = error_cache_qdrant_repo
        self.runtime_log_es_repo = runtime_log_es_repo
        self.task_cache_repo = task_cache_repo
        self.embedding_client = embedding_client
        self.graph = graph or main_dev_graph

    async def _build_context(
        self,
        *,
        task_id: str,
        project_id: str,
        datasource_ids: list[str],
        log_path: str | None,
    ) -> tuple[AutoDevContext, list[str]]:
        project = await self.project_repo.get_by_id(project_id)
        if project is None:
            raise ProjectNotFoundError()

        registry = DatasourceRegistry()
        es_log_index: str | None = None
        if datasource_ids:
            rows = await self.datasource_repo.get_by_ids(project.id, datasource_ids)
            if len(rows) != len(set(datasource_ids)):
                raise DatasourceNotFoundError("存在未注册或未启用的数据源 ID")
            for ds in rows:
                raw_uri = decrypt_uri(ds.encrypted_uri, settings.secret_key)
                registry.register(
                    DatasourceConfig(
                        ds_id=ds.id,
                        name=ds.name,
                        ds_type=ds.ds_type,  # type: ignore[arg-type]
                        connection_uri=raw_uri,
                        es_index_name=ds.es_index_name,
                    )
                )
                if ds.ds_type == "elasticsearch" and ds.es_index_name:
                    es_log_index = ds.es_index_name

        active_probes = ["error_cache", "source_code", "log_error"]
        if registry.has_db_datasources():
            active_probes.append("db_query")

        ctx: AutoDevContext = {
            "task_id": task_id,
            "project_id": project.id,
            "namespace": project.namespace,
            "repo_root": project.repo_path,
            "datasource_registry": registry,
            "active_datasource_ids": datasource_ids,
            "es_log_index": es_log_index,
            "log_path": log_path,
            "task_repo": self.task_repo,
            "error_cache_repo": self.error_cache_repo,
            "error_cache_es_repo": self.error_cache_es_repo,
            "error_cache_qdrant_repo": self.error_cache_qdrant_repo,
            "runtime_log_es_repo": self.runtime_log_es_repo,
            "embedding_client": self.embedding_client,
            "sandbox_image": project.sandbox_image,
            "test_cmd": project.test_cmd,
            "lint_cmd": project.lint_cmd,
        }
        return ctx, active_probes

    async def _stream_graph_events(
        self,
        graph_input: Any,
        config: dict[str, Any],
        ctx: AutoDevContext,
        task_id: str,
    ) -> AsyncIterator[str]:
        """执行 LangGraph 流并将 token / node_status / custom 事件转换为 SSE 帧."""
        async for chunk in self.graph.astream(
            graph_input,
            config=config,
            context=ctx,
            stream_mode=["messages", "updates", "custom"],
            subgraphs=True,
        ):
            ns_path, mode, payload = chunk
            subgraph_name = ":".join(str(p) for p in ns_path) if ns_path else "main"
            if mode == "messages":
                msg_chunk, meta = payload
                node_name = (meta or {}).get("langgraph_node", "")
                probe_type = (meta or {}).get("probe_type")
                run_id = getattr(msg_chunk, "id", None) or ""
                reasoning = getattr(msg_chunk, "additional_kwargs", {}).get("reasoning_content")
                if reasoning:
                    yield _format_sse(
                        "reasoning_token",
                        {
                            "content": reasoning,
                            "node_name": node_name,
                            "subgraph": subgraph_name,
                            "probe_type": probe_type,
                            "run_id": run_id,
                        },
                    )
                content = getattr(msg_chunk, "content", "")
                if isinstance(content, str) and content:
                    yield _format_sse(
                        "token",
                        {
                            "content": content,
                            "node_name": node_name,
                            "subgraph": subgraph_name,
                            "probe_type": probe_type,
                            "run_id": run_id,
                        },
                    )
            elif mode == "custom" and isinstance(payload, dict):
                yield _format_sse(payload.get("event", "node_status"), payload.get("data", {}))
            elif mode == "updates" and isinstance(payload, dict):
                for node_name in payload.keys():
                    if node_name == "__interrupt__":
                        continue
                    yield _format_sse(
                        "node_status",
                        {
                            "node_name": node_name,
                            "subgraph": subgraph_name,
                            "timestamp": now_iso(),
                        },
                    )
        #  astream结束
        # 从 Checkpointer 里读取当前任务（thread_id=task_id）的最新状态快照 StateSnapshot。
        state_snap = await self.graph.aget_state(config)
        vals = state_snap.values if state_snap else {}
        # 在 LangGraph 中，只要图运行到了 human_approval_node 并触发 interrupt() 挂起，
        # 外层的 astream 也会正常退出循环（不报错），此时快照里的 state_snap.next 非空（指明下一个待恢复节点）。
        # 所以发现 state_snap.next 非空，说明任务正处于等待审批状态
        if state_snap and state_snap.next:
            return
        yield _format_sse(
            "task_done",
            {
                "task_id": task_id,
                "status": "completed",
                "commit_sha": vals.get("final_commit_sha"),
                "cache_archived": bool(vals.get("cache_archived", False)),
                "diagnosis_report": vals.get("diagnosis_report"),
            },
        )

    async def stream_task(self, req: TaskStreamRequest) -> AsyncIterator[str]:
        """运行或恢复研发任务并将 LangGraph 事件映射为 SSE 流."""
        task_id = req.task_id or f"task_{uuid.uuid4().hex[:12]}"
        ds_ids = list(req.datasource_ids or [])
        # 这是防并发重复执行同一个任务的分布式互斥锁
        if not await self.task_cache_repo.acquire_task_lock(task_id):
            raise TaskAlreadyRunningError()

        try:
            ctx, active_probes = await self._build_context(
                task_id=task_id,
                project_id=req.project_id,
                datasource_ids=ds_ids,
                log_path=req.log_path,
            )

            existing = await self.task_repo.get_task(task_id)
            if existing is None:
                await self.task_repo.create_task(
                    DevTaskORM(
                        id=task_id,
                        project_id=req.project_id,
                        title=req.user_query[:100],
                        user_query=req.user_query,
                        status="running",
                        used_datasource_ids=ds_ids,
                    )
                )

            existing_msgs = await self.task_repo.list_messages(task_id)
            turn_idx = len(existing_msgs) + 1
            await self.task_repo.append_message(task_id, "user", req.user_query, turn_idx)

            yield _format_sse(
                "task_init",
                {
                    "task_id": task_id,
                    "status": "running",
                    "active_probes": active_probes,
                },
            )

            config = {"configurable": {"thread_id": task_id}}
            input_state = {
                "task_id": task_id,
                "project_id": req.project_id,
                "user_query": req.user_query,
                "messages": [HumanMessage(content=req.user_query)],
            }
            async for frame in self._stream_graph_events(input_state, config, ctx, task_id):
                yield frame
        except AppError as exc:
            await self.task_repo.update_task(task_id, status="failed")
            yield _format_sse(
                "task_error",
                {"error_code": exc.error_code, "message": exc.message},
            )
        except Exception as exc:
            await self.task_repo.update_task(task_id, status="failed")
            yield _format_sse(
                "task_error",
                {"error_code": "INTERNAL_ERROR", "message": str(exc)},
            )
        finally:
            await self.task_cache_repo.release_task_lock(task_id)

    async def _prepare_approval_resume(
        self, task_id: str, req: TaskApprovalRequest
    ) -> tuple[AutoDevContext, dict[str, Any], Command]:
        task = await self.task_repo.get_task(task_id)
        if task is None:
            raise TaskNotFoundError()
        if task.status != "waiting_approval":
            raise TaskNotWaitingApprovalError()

        ctx, _ = await self._build_context(
            task_id=task_id,
            project_id=task.project_id,
            datasource_ids=list(task.used_datasource_ids or []),
            log_path=None,
        )
        config = {"configurable": {"thread_id": task_id}}
        cmd = Command(
            resume={
                "decision": req.decision,
                "feedback": req.feedback,
                "commit_message": req.commit_message,
            }
        )
        return ctx, config, cmd

    async def approve_task(
        self, task_id: str, req: TaskApprovalRequest
    ) -> TaskApprovalResponse:
        if not await self.task_cache_repo.acquire_task_lock(task_id):
            raise TaskAlreadyRunningError()
        resumed = False
        try:
            ctx, config, cmd = await self._prepare_approval_resume(task_id, req)
            resumed = True
            result = await self.graph.ainvoke(cmd, config=config, context=ctx)
            refreshed = await self.task_repo.get_task(task_id)
            status_now = refreshed.status if refreshed else "waiting_approval"
            return TaskApprovalResponse(
                task_id=task_id,
                status=status_now,  # type: ignore[arg-type]
                decision=req.decision,
                branch_name=result.get("target_branch") or (refreshed.target_branch if refreshed else None),
                commit_sha=result.get("final_commit_sha"),
                message="补丁已应用并提交到目标分支"
                if req.decision == "approved"
                else "已根据驳回意见重新进入沙箱修复",
            )
        except Exception:
            if resumed:
                await self.task_repo.update_task(task_id, status="failed")
            raise
        finally:
            await self.task_cache_repo.release_task_lock(task_id)

    async def approve_task_stream(
        self, task_id: str, req: TaskApprovalRequest
    ) -> AsyncIterator[str]:
        """以 SSE 流式唤醒人工审批中断 (适用于 rejected 重跑沙箱实时推流)."""
        if not await self.task_cache_repo.acquire_task_lock(task_id):
            raise TaskAlreadyRunningError()
        resumed = False
        try:
            ctx, config, cmd = await self._prepare_approval_resume(task_id, req)
            resumed = True
            async for frame in self._stream_graph_events(cmd, config, ctx, task_id):
                yield frame
        except AppError as exc:
            if not resumed:
                raise
            await self.task_repo.update_task(task_id, status="failed")
            yield _format_sse(
                "task_error",
                {"error_code": exc.error_code, "message": exc.message},
            )
        except Exception as exc:
            if resumed:
                await self.task_repo.update_task(task_id, status="failed")
            yield _format_sse(
                "task_error",
                {"error_code": "INTERNAL_ERROR", "message": str(exc)},
            )
        finally:
            await self.task_cache_repo.release_task_lock(task_id)

    async def get_task_detail(self, task_id: str) -> TaskDetailResponse:
        task = await self.task_repo.get_task(task_id)
        if task is None:
            raise TaskNotFoundError()
        evidences = await self.task_repo.list_evidences(task_id)
        patches = await self.task_repo.list_patch_records(task_id)
        msgs = await self.task_repo.list_messages(task_id)
        return TaskDetailResponse(
            task_id=task.id,
            project_id=task.project_id,
            status=task.status,  # type: ignore[arg-type]
            title=task.title,
            user_query=task.user_query,
            evidences=[
                EvidenceItem(
                    id=e.id,
                    evidence_type=e.evidence_type,  # type: ignore[arg-type]
                    datasource_id=e.datasource_id,
                    tool_name=e.tool_name,
                    summary=e.summary,
                    created_at=e.created_at.isoformat() if e.created_at else "",
                )
                for e in evidences
            ],
            patch_records=[
                PatchItem(
                    id=p.id,
                    round_no=p.round_no,
                    edited_files=list(p.edited_files or []),
                    patch_diff=p.patch_diff,
                    test_passed=bool(p.test_passed),
                    retry_count=p.retry_count,
                    test_output=p.test_output,
                    approval_status=p.approval_status,
                    human_feedback=p.human_feedback,
                    created_at=p.created_at.isoformat() if p.created_at else "",
                )
                for p in patches
            ],
            messages=[
                MessageItem(
                    turn_index=m.turn_index,
                    role=m.role,
                    content=m.content,
                    created_at=m.created_at.isoformat() if m.created_at else "",
                )
                for m in msgs
            ],
        )

    async def get_evidence_detail(
        self, task_id: str, ev_id: str
    ) -> EvidenceDetailResponse:
        task = await self.task_repo.get_task(task_id)
        if task is None:
            raise TaskNotFoundError()
        ev = await self.task_repo.get_evidence(task_id, ev_id)
        if ev is None:
            raise TaskNotFoundError(f"证据 `{ev_id}` 不存在")
        return EvidenceDetailResponse(
            id=ev.id,
            task_id=ev.task_id,
            evidence_type=ev.evidence_type,  # type: ignore[arg-type]
            datasource_id=ev.datasource_id,
            tool_name=ev.tool_name,
            query_args=ev.query_args,
            summary=ev.summary,
            raw_snippet=ev.raw_snippet,
            created_at=ev.created_at.isoformat() if ev.created_at else "",
        )
