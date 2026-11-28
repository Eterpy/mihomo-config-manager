class MissingDatabase(RuntimeError):
    """Safe startup error that may be shown to operators."""


class CurrentVersionError(OSError):
    """Integrity failure with only operator-safe identifiers in the message."""

    def __init__(self, config_id, version):
        super().__init__(f"配置 {config_id} 的当前版本 {version} 文件缺失或校验失败")


class AppError(Exception):
    def __init__(self, status: int, message: str, **fields):
        self.body = {"detail": message, **fields}
        self.status = status
        self.message = message
        super().__init__(message)

