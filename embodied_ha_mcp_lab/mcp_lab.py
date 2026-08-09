"""Embodied HA MCP Lab backend and authenticated HTTP API."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import source_identity
from .auth import Actor, Authenticator
from .execution_queue import ExecutionQueue
from .ledger import RunLedger
from .runtime import LabPaths, LabRuntime
from .service import MCPService, NotFoundError
from .state_repository import StateRepository

LAB_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_DIR = LAB_DIR.parent / ".upstream" / "embodied-ha-repo" / "embodied_ha"
APP_DIR = Path(os.environ.get("EHA_MCP_LAB_SOURCE_DIR", DEFAULT_SOURCE_DIR)).resolve()
BUILD_IDENTITY_PATH = Path(
    os.environ.get(
        "EHA_MCP_LAB_BUILD_IDENTITY",
        LAB_DIR / source_identity.IDENTITY_FILENAME,
    )
).resolve()
LAB_IMAGE = "ghcr.io/khronos31/embodied-ha-mcp-lab"
MAX_REQUEST_BYTES = 1024 * 1024
WEB_DIR = LAB_DIR / "web"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_build_identity(path: Path) -> dict[str, Any] | None:
    value = _load_json(path)
    if value is None or value.get("schema_version") != 1:
        return None
    bundle_hash = value.get("bundle_sha256")
    if not isinstance(bundle_hash, str) or re.fullmatch(r"[0-9a-f]{64}", bundle_hash) is None:
        return None
    return value


def _config_version(config_dir: Path) -> str | None:
    try:
        text = (config_dir / "config.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = re.search(r'^version:\s*["\']?([^"\'\s#]+)', text, flags=re.MULTILINE)
    return match.group(1) if match else None


def public_identity(
    app_dir: Path = APP_DIR,
    build_identity_path: Path = BUILD_IDENTITY_PATH,
) -> dict[str, Any]:
    runtime = source_identity.build_identity(app_dir)
    built = _load_build_identity(build_identity_path)
    built_hash = built.get("bundle_sha256") if built else None
    lab_version = (built.get("lab_version") if built else None) or _config_version(LAB_DIR)
    tested_eha_version = (built.get("tested_eha_version") if built else None) or _config_version(app_dir)
    return {
        "lab_mode": "mcp_lab",
        "lab_version": lab_version,
        "tested_eha_version": tested_eha_version,
        "source_repository": built.get("source_repository") if built else None,
        "source_revision": built.get("source_revision") if built else None,
        "build_id": f"sha256:{built_hash or runtime['bundle_sha256']}",
        "mcp_bundle_sha256": built_hash or runtime["bundle_sha256"],
        "mcp_config_sha256": built.get("mcp_config_sha256") if built else runtime["mcp_config_sha256"],
        "runtime_bundle_sha256": runtime["bundle_sha256"],
        "runtime_matches_build": runtime["bundle_sha256"] == built_hash if built_hash else None,
        "build_manifest_present": built is not None,
        "file_count": built.get("file_count") if built else runtime["file_count"],
        "execution_model": "fresh_process_per_run",
        "lab_image": LAB_IMAGE,
    }


class LabHTTPServer(ThreadingHTTPServer):
    service: MCPService
    authenticator: Authenticator
    identity: dict[str, Any]


class LabHandler(BaseHTTPRequestHandler):
    server: LabHTTPServer

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        content_security_policy: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_security_policy:
            self.send_header("Content-Security-Policy", content_security_policy)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_index(self) -> None:
        try:
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            self._send_json(500, {"error": "frontend_unavailable"})
            return
        ingress_path = self.headers.get("X-Ingress-Path", "")
        encoded_path = json.dumps(ingress_path, ensure_ascii=False).replace("<", "\\u003c")
        nonce = secrets.token_urlsafe(24)
        injection = f'<script nonce="{nonce}">window.INGRESS_PATH={encoded_path};</script>'
        html = html.replace("</head>", injection + "\n</head>", 1)
        policy = (
            "default-src 'none'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self'; connect-src 'self'; frame-ancestors 'self'; "
            "base-uri 'none'; form-action 'none'"
        )
        self._send_bytes(
            200,
            html.encode("utf-8"),
            "text/html; charset=utf-8",
            content_security_policy=policy,
        )

    def _serve_asset(self, filename: str, content_type: str) -> None:
        try:
            body = (WEB_DIR / filename).read_bytes()
        except OSError:
            self._send_json(404, {"error": "not_found"})
            return
        self._send_bytes(200, body, content_type)

    def _route_path(self) -> tuple[str, dict[str, list[str]]]:
        split = urlsplit(self.path)
        path = split.path
        ingress_path = self.headers.get("X-Ingress-Path", "").rstrip("/")
        if ingress_path and (path == ingress_path or path.startswith(ingress_path + "/")):
            path = path[len(ingress_path) :]
        path = path or "/"
        return path, parse_qs(split.query)

    def _actor(self) -> Actor | None:
        return self.server.authenticator.authorize(
            self.client_address[0], self.headers.get("Authorization")
        )

    def _require_actor(self) -> Actor | None:
        actor = self._actor()
        if actor is None:
            self._send_json(401, {"error": "unauthorized"})
        return actor

    def do_GET(self) -> None:
        path, query = self._route_path()
        if path == "/healthz":
            self._send_json(200, {"ok": True, "mode": "mcp_lab"})
            return
        actor = self._require_actor()
        if actor is None:
            return
        try:
            if path == "/":
                self._serve_index()
            elif path == "/app.js":
                self._serve_asset("app.js", "text/javascript; charset=utf-8")
            elif path == "/style.css":
                self._serve_asset("style.css", "text/css; charset=utf-8")
            elif path == "/api/identity":
                self._send_json(200, self.server.identity)
            elif path == "/api/servers":
                self._send_json(200, {"servers": self.server.service.servers()})
            elif path == "/api/state":
                self._send_json(200, self.server.service.state_snapshot())
            elif path == "/api/runs":
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError:
                    self._send_json(400, {"error": "invalid_limit"})
                    return
                self._send_json(200, {"runs": self.server.service.ledger.recent(limit)})
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})", path):
                event = self.server.service.ledger.get(match.group(1))
                self._send_json(200, event) if event else self._send_json(404, {"error": "not_found"})
            elif match := re.fullmatch(r"/api/servers/([^/]+)/tools", path):
                server_name = unquote(match.group(1))
                self._send_json(200, self.server.service.tools(server_name, actor.name))
            else:
                self._send_json(404, {"error": "not_found"})
        except NotFoundError as error:
            self._send_json(404, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - HTTP exception boundary
            print(f"[mcp-lab] Request failed: {type(error).__name__}", flush=True)
            self._send_json(500, {"error": "internal_error"})

    def do_POST(self) -> None:
        path, _ = self._route_path()
        actor = self._require_actor()
        if actor is None:
            return
        if path == "/api/state/reset":
            if self._content_length() not in {0, None}:
                self._send_json(400, {"error": "reset_body_must_be_empty"})
                return
            try:
                self._send_json(200, self.server.service.reset(actor.name))
            except Exception as error:  # noqa: BLE001 - HTTP exception boundary
                print(f"[mcp-lab] Reset failed: {type(error).__name__}", flush=True)
                self._send_json(500, {"error": "internal_error"})
            return

        match = re.fullmatch(r"/api/servers/([^/]+)/tools/([^/]+)/call", path)
        if match is None:
            self._send_json(404, {"error": "not_found"})
            return
        server_name, tool_name = map(unquote, match.groups())
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "text/plain":
            run_id = self.server.service.record_transport_rejection(
                actor.name, server_name, tool_name, "content_type", self._content_length()
            )
            self._send_json(415, {"error": "content_type_must_be_text_plain", "run_id": run_id, "mcp_reached": False})
            return
        length = self._content_length()
        if length is None or length < 0:
            run_id = self.server.service.record_transport_rejection(
                actor.name, server_name, tool_name, "content_length", length
            )
            self._send_json(411, {"error": "content_length_required", "run_id": run_id, "mcp_reached": False})
            return
        if length > MAX_REQUEST_BYTES:
            run_id = self.server.service.record_transport_rejection(
                actor.name, server_name, tool_name, "request_too_large", length
            )
            self.close_connection = True
            self._send_json(413, {"error": "request_too_large", "limit_bytes": MAX_REQUEST_BYTES, "run_id": run_id, "mcp_reached": False})
            return
        body = self.rfile.read(length)
        if len(body) != length:
            run_id = self.server.service.record_transport_rejection(
                actor.name, server_name, tool_name, "incomplete_body", len(body)
            )
            self._send_json(400, {"error": "incomplete_body", "run_id": run_id, "mcp_reached": False})
            return
        try:
            self._send_json(200, self.server.service.call(server_name, tool_name, body, actor.name))
        except NotFoundError as error:
            self._send_json(404, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - HTTP exception boundary
            print(f"[mcp-lab] Call failed: {type(error).__name__}", flush=True)
            self._send_json(500, {"error": "internal_error"})

    def do_HEAD(self) -> None:
        self.do_GET()

    def _content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return -1

    def log_message(self, message: str, *args: object) -> None:
        rendered = message % args
        print(f"[mcp-lab] HTTP {self.client_address[0]} {rendered}", flush=True)


def create_server(
    host: str,
    port: int,
    service: MCPService,
    authenticator: Authenticator,
    identity: dict[str, Any],
) -> LabHTTPServer:
    server = LabHTTPServer((host, port), LabHandler)
    server.service = service
    server.authenticator = authenticator
    server.identity = identity
    return server


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _timeout(value: Any) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be an integer") from exc
    if not 1 <= timeout <= 300:
        raise ValueError("timeout_seconds must be between 1 and 300")
    return timeout


def _options(path: Path) -> dict[str, Any]:
    return _load_json(path) or {}


def build_application(options_path: Path | None = None) -> tuple[MCPService, Authenticator, dict[str, Any]]:
    options = _options(options_path or Path("/data/options.json"))
    paths = LabPaths.below(
        Path(
            os.environ.get(
                "EHA_MCP_LAB_CONTROL_ROOT",
                "/data/embodied-ha-mcp-lab",
            )
        )
    )
    runtime = LabRuntime(
        APP_DIR,
        paths,
        tested_harness=str(options.get("tested_harness", "claude")),
        seed_data_dir=Path(
            str(options.get("seed_data_dir", "/config"))
        ),
    )
    seeded = runtime.initialize()
    state = StateRepository(paths.worktree, paths.repository, paths.hooks)
    recovery = state.initialize()
    ledger = RunLedger(paths.ledger)
    identity = public_identity()
    if recovery:
        ledger.append(
            {
                **recovery,
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "actor": "startup_recovery",
            }
        )
    sources = {
        item.strip()
        for item in os.environ.get("EHA_MCP_LAB_INGRESS_SOURCE", "172.30.32.2").split(",")
        if item.strip()
    }
    token_path = Path(os.environ.get("EHA_MCP_LAB_AUTH_FILE", str(paths.token)))
    authenticator = Authenticator(token_path, sources)
    service = MCPService(
        runtime,
        state,
        ExecutionQueue(),
        ledger,
        identity,
        timeout_seconds=_timeout(options.get("timeout_seconds", 45)),
    )
    snapshot = state.state()
    print(
        "[mcp-lab] Runtime initialized: "
        f"control_root={paths.control_root} worktree={paths.worktree} "
        f"generation={snapshot['state_generation_branch']} head={snapshot['state_head']} "
        f"seeded={','.join(seeded) or 'none'} build_id={identity['build_id']}",
        flush=True,
    )
    return service, authenticator, identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default=os.environ.get("EHA_MCP_LAB_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=_port, default=_port(os.environ.get("EHA_MCP_LAB_PORT", "8099")))
    parser.add_argument("--options", type=Path, default=Path("/data/options.json"))
    args = parser.parse_args()
    service, authenticator, identity = build_application(args.options)
    server = create_server(args.bind, args.port, service, authenticator, identity)
    host, port = server.server_address[:2]
    print(f"[mcp-lab] Server listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
