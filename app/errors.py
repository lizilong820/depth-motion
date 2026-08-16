class AppError(Exception):
    def __init__(self, message: str, code: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class InvalidUploadError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "INVALID_UPLOAD", 422)


class JobNotFoundError(AppError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"任务不存在：{job_id}", "JOB_NOT_FOUND", 404)


class JobNotReadyError(AppError):
    def __init__(self) -> None:
        super().__init__("任务尚未处理完成", "JOB_NOT_READY", 409)


class ServerBusyError(AppError):
    def __init__(self) -> None:
        super().__init__("服务器正在处理另一个视频，请稍后再试", "SERVER_BUSY", 429)
