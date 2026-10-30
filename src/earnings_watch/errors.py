from typing import Any


class EarningsError(Exception):
    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.detail}}
