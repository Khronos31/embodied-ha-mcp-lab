"""Process-safe append-only JSONL evidence ledger with explicit retention."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_BYTES = 100 * 1024 * 1024


class RunLedger:
    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self._thread_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._ensure_private(self.path)
        self._ensure_private(self.lock_path)

    def append(self, event: dict[str, Any]) -> dict[str, int] | None:
        encoded = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self._locked():
            rotation = self._rotate_if_needed(len(encoded))
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(self.path, 0o600)
            return rotation

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._locked():
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
        values: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                values.append(value)
        return values

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._locked():
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return None
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("run_id") == run_id or event.get("event_id") == run_id:
                return event
        return None

    def _rotate_if_needed(self, incoming_bytes: int) -> dict[str, int] | None:
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(days=self.retention_days)
        try:
            original = self.path.read_bytes()
        except FileNotFoundError:
            original = b""
        lines = original.splitlines(keepends=True)
        kept: list[bytes] = []
        dropped = 0
        for line in lines:
            try:
                value = json.loads(line)
                timestamp = dt.datetime.fromisoformat(
                    str(value.get("timestamp", "")).replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
            except (ValueError, TypeError, json.JSONDecodeError):
                # Corrupt or unparseable evidence is never silently discarded.
                kept.append(line)
                continue
            if timestamp < cutoff:
                dropped += 1
            else:
                kept.append(line)

        kept_bytes = sum(map(len, kept))
        if kept_bytes + incoming_bytes > self.max_bytes and kept:
            while kept and kept_bytes + incoming_bytes > self.max_bytes:
                kept_bytes -= len(kept.pop(0))
                dropped += 1

        if dropped == 0:
            return None
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.writelines(kept)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return {"dropped_events": dropped, "dropped_bytes": len(original) - kept_bytes}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _ensure_private(path: Path) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
