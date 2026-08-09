# Embodied HA MCP Lab — executable acceptance plan

Authoritative product contract: `embodied_ha_mcp_lab_spec.md` in the Embodied HA
project memory. This file keeps the repository-local commands and delivery gates.

## Objective

Ship a separate HAOS add-on that lets an administrator exercise the exact bundled
Embodied HA MCP servers without a resident LLM, preserves raw request/response
evidence, and isolates persistent Lab state from every resident.

## Non-goals

- No resident daemon or agent harness.
- No schema validation, repair, formatting, or editor gating of call bodies.
- No claim that HA, physical devices, LAN services, or GitHub side effects are
  sandboxed.
- No automatic remote export of state history or run evidence.

## Constraints and rollback

- The EHA source revision is immutable and recorded in `tested_eha.json`.
- State uses a separate local Git repository with no remotes and disabled hooks.
- Every MCP process is fresh, while state persists on one serialized generation.
- Installation is a manual gate. Uninstalling removes the runtime; deleting
  `/config/embodied-ha-mcp-lab` is a separate, destructive operation and is never
  automated.
- External side effects are not rolled back; the run ledger is the recovery record.

## Verification matrix

| Increment | Contract | Executable gate |
|---|---|---|
| 0 | Packaging/source identity | `python3 scripts/verify_mcp_lab_packaging.py --upstream-root .upstream/embodied-ha-repo/embodied_ha` |
| 1 | Raw runner, classification, timeout, capture, serialization | `pytest -q tests/test_runner.py tests/test_execution_queue.py` |
| 2 | Live registry, isolated state, local Git generations | `pytest -q tests/test_runtime.py tests/test_state_repository.py` |
| 3 | Authenticated API, immutable raw envelope, ledger | `pytest -q tests/test_api.py tests/test_ledger.py` |
| 4 | Minimal UI | Playwright against the local backend; Antigravity implementation and Claude final gate |
| 5 | Full static/security suite | `ruff check . && pytest -q && python3 scripts/verify_mcp_lab_packaging.py --upstream-root .upstream/embodied-ha-repo/embodied_ha` |
| 6 | Installed canary | Manual only; stop and obtain user permission before installation |

Each increment must keep every earlier gate green. A failed gate blocks the next
increment and any publication or installation.

For the `0.1.1` storage migration, the installed canary must additionally verify all of
the following before it is accepted:

- startup reports `control_root=/config/embodied-ha-mcp-lab`;
- the host `preferences.json` and room graph match the prepared files;
- changing `camera_history_enabled` changes the next fresh camera tool discovery, then
  the original value is restored;
- state commits, the run ledger, and the direct API token are created below that root;
- the legacy `/addon_configs/83e16454_embodied_ha_mcp_lab/` directory remains untouched
  and is documented as unused by `0.1.1`.
