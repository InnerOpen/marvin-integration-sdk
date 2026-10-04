"""
Errors and error policies.

A provider reports a failure by raising ``IntegrationError`` with a ``code`` it owns ("rate_limited",
"auth_expired", "duplicate", …). It says how each code should be handled by *declaring* an
``ErrorPolicy`` — a dict of code → ``Handle`` — on the provider class and/or on individual actions.
The core applies the policy (retries, review, alerts); the provider never schedules anything itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import IntegrationProvider

FALLBACK = "*"  # the policy key that matches any code without an entry of its own


class IntegrationError(ValueError):
    """An action failure the provider understands well enough to name.

    ``code`` selects the handling from the error policy. ``partial`` is progress worth keeping — the
    core saves it and hands it back as ``ctx.resume`` on the retry, so the action continues where it
    stopped. ``retry_after`` (seconds) is the remote's own hint, e.g. from a ``Retry-After`` header.
    """

    def __init__(self, message: str, *, code: str = "unknown", partial: dict | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.partial = partial
        self.retry_after = retry_after

    def __reduce__(self):
        # Keyword-only arguments don't survive the default exception pickling, and the core may move
        # a failure across a process boundary before applying the policy.
        return (_restore_error, (type(self), self.args, self.__dict__))


def _restore_error(cls, args, state):
    err = cls.__new__(cls)
    Exception.__init__(err, *args)
    err.__dict__.update(state)
    return err


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _fmt_seconds(seconds: float) -> str:
    """120 → "2m", 5400 → "1h30m", 86400 → "1d"."""
    if seconds != int(seconds):
        return f"{seconds:g}s"
    remaining, out = int(seconds), []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if remaining >= size:
            out.append(f"{remaining // size}{unit}")
            remaining %= size
    return "".join(out) or "0s"


@dataclass(frozen=True)
class Retry:
    """Try the action again later.

    ``backoff`` is the wait in seconds before each attempt; ``max_attempts`` defaults to its length
    and any attempts past the end reuse the last delay. ``on_recovery`` parks the entry until the
    connection is healthy again (for auth/config failures a timer won't fix), then retries — with
    it, ``backoff`` may be empty, meaning one retry as soon as the connection recovers.
    """

    backoff: tuple[float, ...]
    max_attempts: int | None = None
    on_recovery: bool = False

    def __post_init__(self):
        if isinstance(self.backoff, list):
            object.__setattr__(self, "backoff", tuple(self.backoff))
        if not isinstance(self.backoff, tuple) or not all(_is_number(d) and d >= 0 for d in self.backoff):
            raise TypeError(f"Retry.backoff must be a tuple of non-negative seconds, got {self.backoff!r}")
        if not self.backoff and not self.on_recovery:
            raise ValueError("Retry.backoff is empty: give at least one delay, or set on_recovery=True")
        if self.max_attempts is not None and (not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool) or self.max_attempts < 1):
            raise ValueError(f"Retry.max_attempts must be a positive int or None, got {self.max_attempts!r}")
        if not isinstance(self.on_recovery, bool):
            raise TypeError(f"Retry.on_recovery must be a bool, got {self.on_recovery!r}")

    @property
    def attempts(self) -> int:
        """How many retries this allows, with the default applied."""
        return self.max_attempts if self.max_attempts is not None else (len(self.backoff) or 1)

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before retry number ``attempt`` (1-based). Past the end of ``backoff``,
        the last delay repeats; whether the attempt is still allowed is ``attempts``' business."""
        if attempt < 1:
            raise ValueError(f"attempt is 1-based, got {attempt}")
        if not self.backoff:
            return 0.0
        return float(self.backoff[min(attempt, len(self.backoff)) - 1])

    def to_dict(self) -> dict:
        return {"backoff": list(self.backoff), "max_attempts": self.attempts, "on_recovery": self.on_recovery}

    def describe(self) -> str:
        count = "once" if self.attempts == 1 else f"{self.attempts}×"
        shown = [_fmt_seconds(self.delay_for(i)) for i in range(1, min(self.attempts, len(self.backoff)) + 1)]
        if self.attempts > len(self.backoff) and self.backoff:
            shown.append(f"then every {_fmt_seconds(self.backoff[-1])}")
        text = f"retry {count}" + (f" ({', '.join(shown)})" if shown else "")
        return f"wait for the connection to recover, then {text}" if self.on_recovery else text


@dataclass(frozen=True)
class Handle:
    """What to do with an error of one code.

    ``review``, ``notify`` and ``succeed`` happen as soon as the error does. ``retry`` schedules
    further attempts, and ``then`` is applied once they are exhausted. A Handle with nothing set
    lets the action fail as usual — useful to override a broader ``"*"`` entry.
    """

    review: bool = False
    notify: bool = False
    succeed: bool = False
    retry: Retry | None = None
    then: Handle | None = None

    def __post_init__(self):
        for name in ("review", "notify", "succeed"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"Handle.{name} must be a bool, got {getattr(self, name)!r}")
        if self.retry is not None and not isinstance(self.retry, Retry):
            raise TypeError(f"Handle.retry must be a Retry or None, got {type(self.retry).__name__}")
        if self.then is not None and not isinstance(self.then, Handle):
            raise TypeError(f"Handle.then must be a Handle or None, got {type(self.then).__name__}")
        if self.then is not None and self.retry is None:
            raise ValueError("Handle.then only applies once retries are exhausted, so it needs a retry")
        if self.succeed and self.retry is not None:
            # The pipeline moves on after a success, so a later retry would re-run the steps after it.
            raise ValueError("Handle.succeed and Handle.retry can't be combined: a success has nothing left to retry")

    def to_dict(self) -> dict:
        return {
            "review": self.review,
            "notify": self.notify,
            "succeed": self.succeed,
            "retry": self.retry.to_dict() if self.retry else None,
            "then": self.then.to_dict() if self.then else None,
            "summary": self.describe(),
        }

    def describe(self) -> str:
        """Short human text, e.g. "retry 3× (2m, 10m, 1h), then send to review"."""
        parts = []
        if self.notify:
            parts.append("notify admins")
        if self.review:
            parts.append("send to review")
        if self.succeed:
            parts.append("treat as success")
        if self.retry:
            parts.append(self.retry.describe())
            parts.append(f"then {self.then.describe()}" if self.then else "then fail")
        return ", ".join(parts) or "fail"


ErrorPolicy = dict[str, Handle]  # error code -> Handle; "*" is the fallback


def validate_policy(policy, where: str) -> None:
    """Raise a clear error unless ``policy`` is a mapping of code → Handle. Code names aren't
    checked — they belong to the provider."""
    if not isinstance(policy, Mapping):
        raise TypeError(f"{where}: error_policy must be a dict of code -> Handle, got {type(policy).__name__}")
    for code, handle in policy.items():
        if not isinstance(code, str) or not code:
            raise TypeError(f"{where}: error_policy keys must be non-empty error codes, got {code!r}")
        if not isinstance(handle, Handle):
            raise TypeError(f"{where}: error_policy[{code!r}] must be a Handle, got {type(handle).__name__}")


def _action_policy(provider, action_name: str) -> Mapping[str, Handle]:
    action = next((a for a in provider.actions if a.key == action_name), None)
    return action.error_policy if action else {}


def resolve_policy(provider: IntegrationProvider | type[IntegrationProvider], action_name: str, code: str) -> Handle | None:
    """The Handle for ``code`` raised by ``action_name``: the most specific declaration wins.

    action[code] > provider[code] > action["*"] > provider["*"] > None (fail as usual).
    """
    action, prov = _action_policy(provider, action_name), provider.error_policy
    for policy, key in ((action, code), (prov, code), (action, FALLBACK), (prov, FALLBACK)):
        if key in policy:
            return policy[key]
    return None


def policy_info(provider: IntegrationProvider | type[IntegrationProvider]) -> dict:
    """The declared policies, JSON-safe — what the catalog shows as "How errors are handled"."""
    return {
        "provider": {code: h.to_dict() for code, h in provider.error_policy.items()},
        "actions": {a.key: {code: h.to_dict() for code, h in a.error_policy.items()} for a in provider.actions},
    }
