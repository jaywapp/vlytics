"""Deterministic fake transport for provider contract tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import final

from .models import ProviderInvocation

FakeHandler = Callable[[ProviderInvocation], bytes]


@final
@dataclass
class FakeProviderTransport:
    response_body: bytes | None = None
    error: Exception | None = None
    handler: FakeHandler | None = None
    invocations: list[ProviderInvocation] = field(default_factory=list)

    def send(self, invocation: ProviderInvocation) -> bytes:
        self.invocations.append(invocation)
        if self.error is not None:
            raise self.error
        if self.handler is not None:
            return self.handler(invocation)
        if self.response_body is None:
            raise RuntimeError("fake provider response is not configured")
        return self.response_body
