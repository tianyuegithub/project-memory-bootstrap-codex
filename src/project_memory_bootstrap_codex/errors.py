"""Structured failures at the shared-memory boundary."""

from __future__ import annotations


class MemoryProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}
