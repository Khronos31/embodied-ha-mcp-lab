"""Run an ingress-like MCP Lab preview against fixture-only state."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from embodied_ha_mcp_lab.auth import Authenticator
from embodied_ha_mcp_lab.execution_queue import ExecutionQueue
from embodied_ha_mcp_lab.ledger import RunLedger
from embodied_ha_mcp_lab.mcp_lab import create_server, public_identity
from embodied_ha_mcp_lab.runtime import LabPaths, LabRuntime
from embodied_ha_mcp_lab.service import MCPService
from embodied_ha_mcp_lab.state_repository import StateRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-root", type=Path, required=True)
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18099)
    args = parser.parse_args()

    source = ROOT / ".upstream" / "embodied-ha-repo" / "embodied_ha"
    paths = LabPaths.below(args.control_root)
    runtime = LabRuntime(
        source,
        paths,
        seed_data_dir=args.seed_dir,
        inherited_env={"PATH": os.environ["PATH"], "SUPERVISOR_TOKEN": "ui-sentinel"},
    )
    runtime.initialize()
    state = StateRepository(paths.worktree, paths.repository, paths.hooks)
    state.initialize()
    identity = public_identity(source)
    service = MCPService(
        runtime,
        state,
        ExecutionQueue(),
        RunLedger(paths.ledger),
        identity,
        timeout_seconds=1,
    )
    authenticator = Authenticator(paths.token, {"127.0.0.1"})
    server = create_server("127.0.0.1", args.port, service, authenticator, identity)
    print(f"UI preview ready at http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
