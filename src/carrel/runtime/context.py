from __future__ import annotations

from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from contextlib import ExitStack
from carrel.profile import Profile

priority: ContextVar[int] = ContextVar('carrel_priority', default=1_000_000)


class RunInterrupted(BaseException):
    """Cooperative stop; never convert cancellation into a paper failure."""


@dataclass
class RunContext:
    force: bool = False
    profile: Profile = field(default_factory=Profile)
    partial_sections: list[str] = field(default_factory=list)
    doc_fingerprint: str = ''
    stack: ExitStack = field(default_factory=ExitStack)


def submit(pool, fn, *args, **kwargs):
    """Each module inherits its paper's admission priority."""
    return pool.submit(copy_context().run, fn, *args, **kwargs)
