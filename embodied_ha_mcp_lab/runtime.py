"""Resolve the pinned EHA MCP registry into a resident-isolated Lab runtime."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONTROL_ROOT = Path("/config/embodied-ha-mcp-lab")
SEED_FILES = ("preferences.json", "floorplan_room_graph_draft.json")
SUPPORTED_HARNESSES = {"claude", "codex", "agy"}


@dataclass(frozen=True)
class LabPaths:
    control_root: Path
    config_dir: Path
    worktree: Path
    repository: Path
    hooks: Path
    log_dir: Path
    ledger: Path
    token: Path

    @classmethod
    def below(cls, root: Path = DEFAULT_CONTROL_ROOT) -> LabPaths:
        root = root.resolve()
        return cls(
            control_root=root,
            config_dir=root,
            worktree=root / "state" / "worktree",
            repository=root / "state" / "repository",
            hooks=root / "state" / "hooks-disabled",
            log_dir=root / "log",
            ledger=root / "log" / "mcp_lab_runs.jsonl",
            token=root / "eha-mcp-lab-token.config.toml",
        )

    def create(self) -> None:
        for directory in (
            self.control_root,
            self.config_dir,
            self.worktree,
            self.repository.parent,
            self.hooks,
            self.log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)


@dataclass(frozen=True)
class ServerCommand:
    name: str
    command: tuple[str, ...]
    env: dict[str, str]
    registry_tools: tuple[str, ...]


class LabRuntime:
    def __init__(
        self,
        source_dir: Path,
        paths: LabPaths,
        *,
        tested_harness: str = "claude",
        seed_data_dir: Path | None = None,
        inherited_env: Mapping[str, str] | None = None,
    ) -> None:
        self.source_dir = source_dir.resolve()
        self.paths = paths
        self.tested_harness = tested_harness.strip().lower()
        self.seed_data_dir = seed_data_dir.resolve() if seed_data_dir else None
        self.inherited_env = dict(inherited_env or os.environ)
        if self.tested_harness not in SUPPORTED_HARNESSES:
            raise ValueError("tested_harness must be claude, codex, or agy")
        if not (self.source_dir / "mcp-config.py").is_file():
            raise ValueError("pinned EHA source does not contain mcp-config.py")

    def initialize(self) -> list[str]:
        self.paths.create()
        seeded = self._seed_configuration()
        for filename, fallback in (
            ("preferences.json", {}),
            ("floorplan_room_graph_draft.json", {"rooms": {}, "edges": []}),
        ):
            destination = self.paths.config_dir / filename
            if not destination.exists():
                _atomic_json(destination, fallback)
        self._validate_mutable_paths(self.environment())
        return seeded

    def environment(self) -> dict[str, str]:
        worktree = self.paths.worktree
        config = self.paths.config_dir
        environment = {
            "PATH": self.inherited_env.get(
                "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "HA_URL": self.inherited_env.get("HA_URL", "http://supervisor/core/api"),
            "GO2RTC_BASE": self.inherited_env.get(
                "GO2RTC_BASE", "http://homeassistant.local:1984"
            ),
            "EHA_AGENT_HARNESS": self.tested_harness,
            "EHA_ACTOR": "mcp_lab",
            "EHA_MQTT_PREFIX": "embodied_ha_mcp_lab",
            "EHA_DATA_DIR": str(worktree),
            "EHA_PREFS_FILE": str(config / "preferences.json"),
            "EHA_ROOM_GRAPH_FILE": str(config / "floorplan_room_graph_draft.json"),
            "EHA_LOG_DIR": str(worktree / "log"),
            "EHA_AUDIO_LOG_FILE": str(worktree / "log" / "audio.jsonl"),
            "EHA_AUDITORY_EVENTS_FILE": str(worktree / "log" / "auditory_events.jsonl"),
            "EHA_ACTIVE_LISTEN_LOG_FILE": str(worktree / "log" / "active_listen.jsonl"),
            "EHA_BACKGROUND_AUDIO_LOG_FILE": str(worktree / "log" / "background_audio.jsonl"),
            "EHA_NON_SPEECH_AUDIO_EVENTS_FILE": str(worktree / "log" / "non_speech_audio_events.jsonl"),
            "EHA_AUDIO_EVENT_TAGS_FILE": str(worktree / "audio_event_tags.json"),
            "EHA_AUDIO_WAV_DIR": str(worktree / "audio"),
            "EHA_BODY_LOCATION_FILE": str(worktree / "body_location.json"),
            "EHA_BODY_LOCATION_LOG_FILE": str(worktree / "log" / "body_location_log.jsonl"),
            "EHA_ANOMALY_STATE_FILE": str(worktree / "anomaly_state.json"),
            "EHA_CAMERA_HISTORY_DIR": str(worktree / "camera_history"),
            "EHA_GITHUB_APP_PEM": str(config / "github_app.pem"),
            "EHA_TOOLS_PATH": self.inherited_env.get("EHA_TOOLS_PATH", "/usr/local/bin"),
        }
        token = self.inherited_env.get("SUPERVISOR_TOKEN")
        if token:
            environment["SUPERVISOR_TOKEN"] = token
        retention = self.inherited_env.get("EHA_ACTIVE_LISTEN_RETENTION_HOURS")
        if retention:
            environment["EHA_ACTIVE_LISTEN_RETENTION_HOURS"] = retention
        return environment

    def servers(self) -> tuple[str, ...]:
        registry = self._load_registry()
        return tuple(registry["SERVER_SPECS"])

    def server(self, name: str) -> ServerCommand:
        registry = self._load_registry()
        specifications = registry["SERVER_SPECS"]
        if name not in specifications:
            raise KeyError("unknown MCP server")
        spec = specifications[name]
        # Some pinned specs (camera) resolve required values when build() runs,
        # not when mcp-config.py is imported. Keep the same curated environment
        # for both phases, then restore the backend process environment.
        with _temporary_environment(self.environment()):
            built = spec.build()
            registry_tools = tuple(spec.active_tools())
        command = (str(built["command"]), *map(str, built.get("args", ())))
        if len(command) < 2:
            raise RuntimeError("invalid MCP server command")
        script = Path(command[1]).resolve()
        if not script.is_relative_to(self.source_dir) or not script.is_file():
            raise RuntimeError("MCP server script escaped the pinned source bundle")
        environment = {str(key): str(value) for key, value in built.get("env", {}).items()}
        self._validate_mutable_paths(environment)
        return ServerCommand(
            name=name,
            command=command,
            env=environment,
            registry_tools=registry_tools,
        )

    def _seed_configuration(self) -> list[str]:
        if self.seed_data_dir is None:
            return []
        seeded: list[str] = []
        for filename in SEED_FILES:
            source = self.seed_data_dir / filename
            destination = self.paths.config_dir / filename
            if destination.exists() or not source.is_file():
                continue
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                with source.open("rb") as reader, temporary.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            seeded.append(filename)
        return seeded

    def _load_registry(self) -> dict[str, Any]:
        environment = self.environment()
        module_name = f"_mcp_lab_registry_{uuid.uuid4().hex}"
        path = self.source_dir / "mcp-config.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load pinned MCP registry")
        module = importlib.util.module_from_spec(spec)
        with _temporary_environment(environment):
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)
        registry = getattr(module, "SERVER_SPECS", None)
        if not isinstance(registry, dict):
            raise TypeError("pinned MCP registry is invalid")
        return {"SERVER_SPECS": registry}

    def _validate_mutable_paths(self, environment: Mapping[str, str]) -> None:
        known_mutable = {
            "EHA_DATA_DIR",
            "EHA_PREFS_FILE",
            "EHA_ROOM_GRAPH_FILE",
            "EHA_LOG_DIR",
            "EHA_AUDIO_LOG_FILE",
            "EHA_AUDITORY_EVENTS_FILE",
            "EHA_ACTIVE_LISTEN_LOG_FILE",
            "EHA_BACKGROUND_AUDIO_LOG_FILE",
            "EHA_NON_SPEECH_AUDIO_EVENTS_FILE",
            "EHA_AUDIO_EVENT_TAGS_FILE",
            "EHA_AUDIO_WAV_DIR",
            "EHA_BODY_LOCATION_FILE",
            "EHA_BODY_LOCATION_LOG_FILE",
            "EHA_ANOMALY_STATE_FILE",
            "EHA_CAMERA_HISTORY_DIR",
            "EHA_GITHUB_APP_PEM",
        }
        root = self.paths.control_root
        for key in known_mutable:
            raw = environment.get(key)
            if raw and not Path(raw).resolve().is_relative_to(root):
                raise RuntimeError(f"Lab mutable path escaped control root: {key}")


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
