"""Fresh-process stdio MCP runner with byte-faithful argument forwarding."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 45
MAX_TIMEOUT_SECONDS = 300
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
TERMINATE_GRACE_SECONDS = 2.0


def classify_input(body: bytes) -> str:
    """Classify bytes without changing or gating what will be sent."""

    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid_json"
    return "valid_json_object" if isinstance(value, dict) else "valid_json_non_object"


def count_line_breaks(body: bytes) -> int:
    """Count CRLF, lone CR, and lone LF as one line break each."""

    count = 0
    index = 0
    while index < len(body):
        if body[index : index + 2] == b"\r\n":
            count += 1
            index += 2
        elif body[index : index + 1] in {b"\r", b"\n"}:
            count += 1
            index += 1
        else:
            index += 1
    return count


def build_call_wire(tool: str, request_id: str, body: bytes) -> bytes:
    """Splice *body* into tools/call exactly, then append only the MCP LF."""

    encoded_tool = json.dumps(tool, ensure_ascii=False, separators=(",", ":"))
    encoded_id = json.dumps(request_id, ensure_ascii=False, separators=(",", ":"))
    prefix = (
        '{"jsonrpc":"2.0","id":'
        + encoded_id
        + ',"method":"tools/call","params":{"name":'
        + encoded_tool
        + ',"arguments":'
    ).encode("utf-8")
    return prefix + body + b"}}\n"


def _json_line(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    ) + b"\n"


@dataclass
class RunResult:
    run_id: str
    expected_response_id: str
    stdout_raw: str
    stderr_raw: str
    stdout_bytes: int
    stderr_bytes: int
    exit_code: int | None
    signal: int | None
    timed_out: bool
    elapsed_ms: int
    input_class: str
    input_line_breaks: int
    request_layer: str
    response_id_observed: bool
    truncated: bool
    stage: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Capture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.total = 0
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


class MCPRunner:
    """Run one MCP lifecycle per discovery or tool call."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        capture_limit: int = MAX_CAPTURE_BYTES,
    ) -> None:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("command must contain non-empty strings")
        if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be between 1 and 300")
        if capture_limit < 1:
            raise ValueError("capture_limit must be positive")
        self.command = tuple(command)
        self.env = dict(env) if env is not None else None
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.capture_limit = capture_limit

    def call(
        self,
        tool: str,
        body: bytes,
        *,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> RunResult:
        run_id = run_id or uuid.uuid4().hex
        request_id = request_id or f"call-{run_id}"
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        stdout = _Capture(self.capture_limit)
        stderr = _Capture(self.capture_limit)
        response_id_observed = False
        initialized = False
        timed_out = False
        stage = "initialize"

        process = self._spawn()
        selector = self._selector(process)
        stdout_pending = bytearray()
        try:
            self._write(
                process,
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": "initialize",
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "embodied-ha-mcp-lab", "version": "0.1"},
                        },
                    }
                ),
            )
            while not initialized and process.poll() is None:
                if self._expired(deadline):
                    timed_out = True
                    break
                lines = self._read_ready(
                    selector, stdout, stderr, stdout_pending, deadline
                )
                initialized = any(_line_has_id(line, "initialize") for line in lines)
                if stdout.truncated or stderr.truncated:
                    break

            if initialized and not timed_out and not stdout.truncated and not stderr.truncated:
                stage = "call"
                self._write(
                    process,
                    _json_line(
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}
                    ),
                )
                self._write(process, build_call_wire(tool, request_id, body))
                while not response_id_observed and process.poll() is None:
                    if self._expired(deadline):
                        timed_out = True
                        break
                    lines = self._read_ready(
                        selector, stdout, stderr, stdout_pending, deadline
                    )
                    response_id_observed = any(
                        _line_has_id(line, request_id) for line in lines
                    )
                    if stdout.truncated or stderr.truncated:
                        break
            if response_id_observed:
                stage = "complete"
        finally:
            self._close_stdin(process)
            if process.poll() is None:
                if timed_out or stdout.truncated or stderr.truncated or not response_id_observed:
                    self._terminate(process)
                else:
                    self._wait_briefly(process)
            self._drain(selector, process, stdout, stderr, stdout_pending)
            selector.close()

        returncode = process.poll()
        signum = -returncode if returncode is not None and returncode < 0 else None
        return RunResult(
            run_id=run_id,
            expected_response_id=request_id,
            stdout_raw=stdout.text(),
            stderr_raw=stderr.text(),
            stdout_bytes=stdout.total,
            stderr_bytes=stderr.total,
            exit_code=returncode if returncode is not None and returncode >= 0 else None,
            signal=signum,
            timed_out=timed_out,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            input_class=classify_input(body),
            input_line_breaks=count_line_breaks(body),
            request_layer="protocol" if classify_input(body) == "invalid_json" else "tool",
            response_id_observed=response_id_observed,
            truncated=stdout.truncated or stderr.truncated,
            stage=stage,
        )

    def discover_tools(self) -> tuple[list[dict[str, Any]], RunResult]:
        """Return a live tools/list result using the same lifecycle guarantees."""

        body = b"{}"
        result = self._request("tools/list", {}, body=body)
        tools: list[dict[str, Any]] = []
        for line in result.stdout_raw.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if value.get("id") != result.expected_response_id:
                continue
            candidate = value.get("result", {}).get("tools", [])
            if isinstance(candidate, list):
                tools = [item for item in candidate if isinstance(item, dict)]
        return tools, result

    def _request(
        self, method: str, params: Mapping[str, Any], *, body: bytes
    ) -> RunResult:
        # Discovery needs the same process safety but not the raw tools/call splice.
        if method != "tools/list":
            raise ValueError("unsupported discovery method")
        run_id = uuid.uuid4().hex
        request_id = f"discover-{run_id}"
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        stdout = _Capture(self.capture_limit)
        stderr = _Capture(self.capture_limit)
        process = self._spawn()
        selector = self._selector(process)
        pending = bytearray()
        initialized = False
        observed = False
        timed_out = False
        stage = "initialize"
        try:
            self._write(process, _json_line({"jsonrpc": "2.0", "id": "initialize", "method": "initialize", "params": {}}))
            while not initialized and process.poll() is None:
                if self._expired(deadline):
                    timed_out = True
                    break
                lines = self._read_ready(selector, stdout, stderr, pending, deadline)
                initialized = any(_line_has_id(line, "initialize") for line in lines)
            if initialized and not timed_out:
                stage = "discovery"
                self._write(process, _json_line({"jsonrpc": "2.0", "method": "notifications/initialized"}))
                self._write(process, _json_line({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}))
                while not observed and process.poll() is None:
                    if self._expired(deadline):
                        timed_out = True
                        break
                    lines = self._read_ready(selector, stdout, stderr, pending, deadline)
                    observed = any(_line_has_id(line, request_id) for line in lines)
            if observed:
                stage = "complete"
        finally:
            self._close_stdin(process)
            if process.poll() is None:
                if timed_out or not observed:
                    self._terminate(process)
                else:
                    self._wait_briefly(process)
            self._drain(selector, process, stdout, stderr, pending)
            selector.close()
        returncode = process.poll()
        return RunResult(
            run_id=run_id,
            expected_response_id=request_id,
            stdout_raw=stdout.text(), stderr_raw=stderr.text(),
            stdout_bytes=stdout.total, stderr_bytes=stderr.total,
            exit_code=returncode if returncode is not None and returncode >= 0 else None,
            signal=-returncode if returncode is not None and returncode < 0 else None,
            timed_out=timed_out, elapsed_ms=round((time.monotonic() - started) * 1000),
            input_class=classify_input(body), input_line_breaks=0,
            request_layer="protocol", response_id_observed=observed,
            truncated=stdout.truncated or stderr.truncated, stage=stage,
        )

    def _spawn(self) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    @staticmethod
    def _selector(process: subprocess.Popen[bytes]) -> selectors.BaseSelector:
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        return selector

    @staticmethod
    def _write(process: subprocess.Popen[bytes], payload: bytes) -> None:
        if process.stdin is None:
            raise RuntimeError("MCP stdin is unavailable")
        process.stdin.write(payload)
        process.stdin.flush()

    @staticmethod
    def _expired(deadline: float) -> bool:
        return time.monotonic() >= deadline

    def _read_ready(
        self,
        selector: selectors.BaseSelector,
        stdout: _Capture,
        stderr: _Capture,
        pending: bytearray,
        deadline: float,
    ) -> list[bytes]:
        wait = max(0.0, min(0.1, deadline - time.monotonic()))
        lines: list[bytes] = []
        for key, _ in selector.select(wait):
            try:
                chunk = os.read(key.fileobj.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                try:
                    selector.unregister(key.fileobj)
                except KeyError:
                    pass
                continue
            capture = stdout if key.data == "stdout" else stderr
            capture.append(chunk)
            if key.data == "stdout":
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, remainder = pending.partition(b"\n")
                    pending[:] = remainder
                    lines.append(line)
        return lines

    def _drain(
        self,
        selector: selectors.BaseSelector,
        process: subprocess.Popen[bytes],
        stdout: _Capture,
        stderr: _Capture,
        pending: bytearray,
    ) -> None:
        deadline = time.monotonic() + 0.5
        while selector.get_map() and time.monotonic() < deadline:
            self._read_ready(selector, stdout, stderr, pending, deadline)
            if process.poll() is not None and not selector.get_map():
                break
        if process.poll() is None:
            self._terminate(process)

    @staticmethod
    def _close_stdin(process: subprocess.Popen[bytes]) -> None:
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass

    @staticmethod
    def _wait_briefly(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            MCPRunner._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=TERMINATE_GRACE_SECONDS)


def _line_has_id(line: bytes, expected: str) -> bool:
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("id") == expected
