"""Application service joining registry, runner, state Git, queue, and ledger."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import uuid
from typing import Any

from .execution_queue import ExecutionQueue
from .ledger import RunLedger
from .runner import MCPRunner
from .runtime import LabRuntime
from .state_repository import StateRepository


class NotFoundError(LookupError):
    pass


class MCPService:
    def __init__(
        self,
        runtime: LabRuntime,
        state: StateRepository,
        queue: ExecutionQueue,
        ledger: RunLedger,
        identity: dict[str, Any],
        *,
        timeout_seconds: int = 45,
    ) -> None:
        self.runtime = runtime
        self.state = state
        self.queue = queue
        self.ledger = ledger
        self.identity = identity
        self.timeout_seconds = timeout_seconds

    def servers(self) -> list[dict[str, Any]]:
        with self.queue.acquire(f"servers-{uuid.uuid4().hex}"):
            return [{"name": name} for name in self.runtime.servers()]

    def tools(self, server_name: str, actor: str) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        with self.queue.acquire(run_id) as lease:
            command = self._server(server_name)
            before = self.state.head()
            tools, result = MCPRunner(
                command.command,
                env=command.env,
                cwd=self.runtime.source_dir,
                timeout_seconds=self.timeout_seconds,
            ).discover_tools()
            commit = self.state.commit_run(run_id)
            live_names = tuple(
                item.get("name") for item in tools if isinstance(item.get("name"), str)
            )
            registry_consistent = set(live_names) == set(command.registry_tools)
            event = {
                **self._base_event(run_id, actor, "discovery"),
                "server": server_name,
                "selected_tool": None,
                "tool": None,
                "state_generation_branch": self.state.branch(),
                "state_commit_before": before,
                "state_commit_after": commit.after,
                "state_changes": commit.changes,
                "queue_wait_ms": lease.queue_wait_ms,
                **_result_fields(result.as_dict()),
                "registry_consistent": registry_consistent,
            }
            self._append(event)
            if not result.response_id_observed:
                raise RuntimeError("live MCP tools/list failed")
            if not registry_consistent:
                raise RuntimeError("live MCP tools differ from the pinned registry")
            return {"server": server_name, "tools": tools, "run_id": run_id}

    def call(self, server_name: str, tool_name: str, body: bytes, actor: str) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        with self.queue.acquire(run_id) as lease:
            command = self._server(server_name)
            before = self.state.head()
            discovery_runner = MCPRunner(
                command.command,
                env=command.env,
                cwd=self.runtime.source_dir,
                timeout_seconds=self.timeout_seconds,
            )
            live_tools, discovery = discovery_runner.discover_tools()
            live_names = {
                item.get("name") for item in live_tools if isinstance(item.get("name"), str)
            }
            if (
                not discovery.response_id_observed
                or set(command.registry_tools) != live_names
                or tool_name not in live_names
            ):
                commit = self.state.commit_run(run_id)
                event = {
                    **self._base_event(run_id, actor, "registry_check"),
                    "server": server_name,
                    "selected_tool": tool_name,
                    "tool": tool_name,
                    "state_generation_branch": self.state.branch(),
                    "state_commit_before": before,
                    "state_commit_after": commit.after,
                    "state_changes": commit.changes,
                    "queue_wait_ms": lease.queue_wait_ms,
                    **_result_fields(discovery.as_dict()),
                }
                self._append(event)
                if tool_name not in command.registry_tools:
                    raise NotFoundError("unknown MCP tool")
                raise RuntimeError("live MCP registry check failed")

            result = MCPRunner(
                command.command,
                env=command.env,
                cwd=self.runtime.source_dir,
                timeout_seconds=self.timeout_seconds,
            ).call(tool_name, body, run_id=run_id)
            commit = self.state.commit_run(run_id)
            event = {
                **self._base_event(run_id, actor, "call"),
                "server": server_name,
                "selected_tool": tool_name,
                "tool": tool_name,
                "tested_harness": self.runtime.tested_harness,
                "state_generation_branch": self.state.branch(),
                "state_commit_before": before,
                "state_commit_after": commit.after,
                "state_changes": commit.changes,
                "queue_wait_ms": lease.queue_wait_ms,
                "input_raw": _raw_text(body),
                "input_base64": base64.b64encode(body).decode("ascii"),
                "input_sha256": hashlib.sha256(body).hexdigest(),
                "input_bytes": len(body),
                "known_side_effect_class": _side_effect(server_name, tool_name),
                **result.as_dict(),
            }
            self._append(event)
            return event

    def reset(self, actor: str) -> dict[str, Any]:
        event_id = uuid.uuid4().hex
        with self.queue.acquire(event_id) as lease:
            reset = self.state.reset()
            event = {
                **self._base_event(event_id, actor, "state_reset"),
                "event_id": event_id,
                "queue_wait_ms": lease.queue_wait_ms,
                **reset,
            }
            self._append(event)
            return event

    def state_snapshot(self) -> dict[str, Any]:
        return {**self.state.state(), **self.queue.snapshot()}

    def record_transport_rejection(
        self,
        actor: str,
        server: str,
        tool: str,
        reason: str,
        input_bytes: int | None,
    ) -> str:
        run_id = uuid.uuid4().hex
        event = {
            **self._base_event(run_id, actor, "transport_rejection"),
            "server": server,
            "selected_tool": tool,
            "tool": tool,
            "stage": "api",
            "rejection_reason": reason,
            "input_bytes": input_bytes,
            "mcp_reached": False,
        }
        self._append(event)
        return run_id

    def _server(self, name: str):
        try:
            return self.runtime.server(name)
        except KeyError as error:
            raise NotFoundError("unknown MCP server") from error

    def _base_event(self, identifier: str, actor: str, event_type: str) -> dict[str, Any]:
        return {
            "run_id": identifier,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "actor": actor,
            "event_type": event_type,
            "lab_version": self.identity.get("lab_version"),
            "tested_eha_version": self.identity.get("tested_eha_version"),
            "source_revision": self.identity.get("source_revision"),
            "build_id": self.identity.get("build_id"),
            "mcp_bundle_sha256": self.identity.get("mcp_bundle_sha256"),
            "mcp_config_sha256": self.identity.get("mcp_config_sha256"),
            "execution_model": "fresh_process_per_run",
        }

    def _append(self, event: dict[str, Any]) -> None:
        rotation = self.ledger.append(event)
        if rotation:
            print(
                "[mcp-lab] Ledger retention removed "
                f"{rotation['dropped_events']} events / {rotation['dropped_bytes']} bytes",
                flush=True,
            )


def _raw_text(body: bytes) -> str | None:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _result_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "run_id"}


def _side_effect(server: str, tool: str) -> str:
    if server == "hacontrol":
        return "home_assistant_control"
    if server == "audio" and tool in {"speak", "use_device_speaker"}:
        return "physical_audio_output"
    if server == "song":
        return "physical_audio_output"
    if server in {"http", "lounge", "game"}:
        return "external_network_or_compute"
    if server in {"audio", "camera", "ha", "sensors"}:
        return "household_observation"
    return "local_state"
