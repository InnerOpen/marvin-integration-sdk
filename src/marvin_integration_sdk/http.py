"""The HTTP surface plugins use to reach external services.

Plugins receive a concrete implementation on ``ctx.http`` — the core supplies one with timeouts,
bounded retries, a size cap, and an SSRF guard. Plugins should prefer ``ctx.http`` over rolling
their own client so that safety is enforced in one place.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Response:
    """A minimal HTTP response. Enough for the common integration cases; not a full client."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return _json.loads(self.content)


@runtime_checkable
class HttpHelper(Protocol):
    """The blessed HTTP client handed to providers. Implemented by the core."""

    def get(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 15) -> Response: ...

    def post(
        self,
        url: str,
        *,
        json: Any = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15,
    ) -> Response: ...
