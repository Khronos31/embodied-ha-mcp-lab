"""Identity-only MCP Lab entrypoint for Increment 0."""

from __future__ import annotations

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import source_identity

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
    if (
        not isinstance(bundle_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", bundle_hash) is None
    ):
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
    """Return non-secret build/runtime identity information for the Lab UI."""

    runtime = source_identity.build_identity(app_dir)
    built = _load_build_identity(build_identity_path)
    built_hash = built.get("bundle_sha256") if built else None
    lab_version = (built.get("lab_version") if built else None) or _config_version(
        LAB_DIR
    )
    tested_eha_version = (
        built.get("tested_eha_version") if built else None
    ) or _config_version(app_dir)
    return {
        "lab_mode": "identity_only",
        "lab_version": lab_version,
        "tested_eha_version": tested_eha_version,
        "source_repository": built.get("source_repository") if built else None,
        "source_revision": built.get("source_revision") if built else None,
        "build_id": f"sha256:{built_hash or runtime['bundle_sha256']}",
        "mcp_bundle_sha256": built_hash or runtime["bundle_sha256"],
        "mcp_config_sha256": (
            built.get("mcp_config_sha256") if built else runtime["mcp_config_sha256"]
        ),
        "runtime_bundle_sha256": runtime["bundle_sha256"],
        "runtime_matches_build": (
            runtime["bundle_sha256"] == built_hash if built_hash else None
        ),
        "build_manifest_present": built is not None,
        "file_count": built.get("file_count") if built else runtime["file_count"],
        "execution_model": "identity_only",
        "lab_image": LAB_IMAGE,
    }


class LabHTTPServer(ThreadingHTTPServer):
    identity: dict[str, Any]


class LabHandler(BaseHTTPRequestHandler):
    server: LabHTTPServer

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.partition("?")[0]
        if path == "/healthz":
            self._send_json(200, {"ok": True, "mode": "identity_only"})
        elif path in {"/", "/api/identity"}:
            self._send_json(200, self.server.identity)
        else:
            self._send_json(404, {"error": "not_found"})

    def do_HEAD(self) -> None:
        self.do_GET()

    def log_message(self, message: str, *args: object) -> None:
        rendered = message % args
        print(f"[mcp-lab] HTTP {self.client_address[0]} {rendered}", flush=True)


def create_server(host: str, port: int, identity: dict[str, Any]) -> LabHTTPServer:
    server = LabHTTPServer((host, port), LabHandler)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default=os.environ.get("EHA_MCP_LAB_BIND", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=_port,
        default=_port(os.environ.get("EHA_MCP_LAB_PORT", "8099")),
    )
    args = parser.parse_args()

    identity = public_identity()
    server = create_server(args.bind, args.port, identity)
    host, port = server.server_address[:2]
    print(
        f"[mcp-lab] Identity-only server listening on {host}:{port}; "
        f"build_id={identity['build_id']}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
