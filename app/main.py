"""Auto-Dev Agent FastAPI 主入口."""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.datasource_router import router as datasource_router
from app.api.error_cache_router import router as error_cache_router
from app.api.lifespan import lifespan
from app.api.project_router import router as project_router
from app.api.task_router import router as task_router
from app.exceptions import AppError

app = FastAPI(
    title="Auto-Dev Agent",
    description="基于多源证据探查与 Linux 容器沙箱的自主代码研发代理平台",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error_code": exc.error_code, "message": exc.message},
    )


app.include_router(project_router)
app.include_router(datasource_router)
app.include_router(error_cache_router)
app.include_router(task_router)


@app.get("/health", tags=["健康检查"])
async def health_check() -> dict[str, str]:
    return {"status": "healthy", "service": "auto-dev-agent"}
