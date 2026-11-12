"""统一业务异常与错误码映射 (对应 docs/03_API.md)."""


class AppError(Exception):
    """应用层标准业务异常."""

    def __init__(
        self,
        error_code: str,
        message: str,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.http_status = http_status


class InvalidParamError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("INVALID_PARAM", message, http_status=400)


class UnsafeSQLBlockedError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("UNSAFE_SQL_BLOCKED", message, http_status=400)


class ProjectNotFoundError(AppError):
    def __init__(self, message: str = "指定项目不存在") -> None:
        super().__init__("PROJECT_NOT_FOUND", message, http_status=404)


class DatasourceNotFoundError(AppError):
    def __init__(self, message: str = "指定数据源不存在") -> None:
        super().__init__("DATASOURCE_NOT_FOUND", message, http_status=404)


class TaskNotFoundError(AppError):
    def __init__(self, message: str = "指定任务不存在") -> None:
        super().__init__("TASK_NOT_FOUND", message, http_status=404)


class TaskAlreadyRunningError(AppError):
    def __init__(self, message: str = "同一任务正在执行中") -> None:
        super().__init__("TASK_ALREADY_RUNNING", message, http_status=409)


class TaskNotWaitingApprovalError(AppError):
    def __init__(self, message: str = "当前任务未处于人工审批挂起态") -> None:
        super().__init__("TASK_NOT_WAITING_APPROVAL", message, http_status=409)


class DatasourceUnreachableError(AppError):
    def __init__(self, message: str = "外部数据源连接或结构校验失败") -> None:
        super().__init__("DATASOURCE_UNREACHABLE", message, http_status=422)


class SandboxUnavailableError(AppError):
    def __init__(self, message: str = "Docker 引擎不可用，无法启动 Linux 沙箱") -> None:
        super().__init__("SANDBOX_UNAVAILABLE", message, http_status=503)
