class DomainError(Exception):
    """A rule of the domain was violated. Routers map it to HTTP 4xx; the action layer records it."""

    def __init__(self, code: str, message: str, status: int = 409, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}  # more detail for the client, e.g. every conflict found
