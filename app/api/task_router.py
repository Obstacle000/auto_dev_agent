"""研发任务编排、SSE 推流与人工审批路由 (`/api/v1/tasks`)."""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.deps import TaskServiceDep
from app.schemas.api_schemas import (
    EvidenceDetailResponse,
    TaskApprovalRequest,
    TaskApprovalResponse,
    TaskDetailResponse,
    TaskStreamRequest,
)

router = APIRouter(prefix="/api/v1/tasks", tags=["任务编排"])


@router.post("/chat-stream")
async def chat_stream(
    req: TaskStreamRequest,
    svc: TaskServiceDep,
) -> StreamingResponse:
    return StreamingResponse(
        svc.stream_task(req),
        media_type="text/event-stream",
    )


@router.post("/{task_id}/approval", response_model=TaskApprovalResponse)
async def approve_task(
    task_id: str,
    req: TaskApprovalRequest,
    svc: TaskServiceDep,
) -> TaskApprovalResponse:
    return await svc.approve_task(task_id, req)


@router.post("/{task_id}/approval-stream")
async def approve_task_stream(
    task_id: str,
    req: TaskApprovalRequest,
    svc: TaskServiceDep,
) -> StreamingResponse:
    return StreamingResponse(
        svc.approve_task_stream(task_id, req),
        media_type="text/event-stream",
    )


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task_detail(
    task_id: str,
    svc: TaskServiceDep,
) -> TaskDetailResponse:
    return await svc.get_task_detail(task_id)


@router.get("/{task_id}/evidences/{ev_id}", response_model=EvidenceDetailResponse)
async def get_evidence_detail(
    task_id: str,
    ev_id: str,
    svc: TaskServiceDep,
) -> EvidenceDetailResponse:
    return await svc.get_evidence_detail(task_id, ev_id)
