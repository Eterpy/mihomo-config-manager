class MissingDatabase(RuntimeError):
    """Safe startup error that may be shown to operators."""


class AppError(Exception):
    def __init__(self, status: int, message: str, **fields):
        self.body = {"detail": message, **fields}
        self.status = status
        self.message = message
        super().__init__(message)

