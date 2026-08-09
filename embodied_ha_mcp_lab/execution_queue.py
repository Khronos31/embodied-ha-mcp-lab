"""FIFO process-wide serialization for MCP lifecycles and state resets."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class QueueLease:
    operation_id: str
    queue_wait_ms: int


class ExecutionQueue:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._tickets: list[str] = []
        self._active: str | None = None

    @contextmanager
    def acquire(self, operation_id: str | None = None) -> Iterator[QueueLease]:
        operation_id = operation_id or uuid.uuid4().hex
        started = time.monotonic()
        with self._condition:
            self._tickets.append(operation_id)
            while self._active is not None or self._tickets[0] != operation_id:
                self._condition.wait()
            self._tickets.pop(0)
            self._active = operation_id
        try:
            yield QueueLease(
                operation_id=operation_id,
                queue_wait_ms=round((time.monotonic() - started) * 1000),
            )
        finally:
            with self._condition:
                if self._active != operation_id:
                    raise RuntimeError("execution queue ownership was corrupted")
                self._active = None
                self._condition.notify_all()

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "queue_depth": len(self._tickets),
                "active_operation_id": self._active,
            }
