import base64
import http.client
import json
import os
import threading
import time
from pathlib import Path

from embodied_ha_mcp_lab.auth import Authenticator
from embodied_ha_mcp_lab.execution_queue import ExecutionQueue
from embodied_ha_mcp_lab.ledger import RunLedger
from embodied_ha_mcp_lab.mcp_lab import MAX_REQUEST_BYTES, create_server
from embodied_ha_mcp_lab.runtime import LabPaths, LabRuntime
from embodied_ha_mcp_lab.service import MCPService
from embodied_ha_mcp_lab.state_repository import StateRepository

SOURCE = Path(__file__).parents[1] / ".upstream" / "embodied-ha-repo" / "embodied_ha"
SECRET = "supervisor-sentinel-must-not-leak"


def fixture_graph():
    return {
        "rooms": {
            "study": {"display_name": "Study"},
            "living_room": {"display_name": "Living"},
        },
        "edges": [{"from": "study", "to": "living_room", "cost": 1}],
    }


def application(tmp_path, *, ingress=False):
    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    (seed / "preferences.json").write_text("{}")
    (seed / "floorplan_room_graph_draft.json").write_text(json.dumps(fixture_graph()))
    paths = LabPaths.below(tmp_path / "lab")
    runtime = LabRuntime(
        SOURCE,
        paths,
        seed_data_dir=seed,
        inherited_env={"PATH": os.environ["PATH"], "SUPERVISOR_TOKEN": SECRET},
    )
    runtime.initialize()
    state = StateRepository(paths.worktree, paths.repository, paths.hooks)
    state.initialize()
    ledger = RunLedger(paths.ledger)
    identity = {
        "lab_version": "test",
        "tested_eha_version": "test",
        "build_id": "sha256:test",
        "execution_model": "fresh_process_per_run",
    }
    auth = Authenticator(paths.token, {"127.0.0.1"} if ingress else {"172.30.32.2"})
    service = MCPService(
        runtime, state, ExecutionQueue(), ledger, identity, timeout_seconds=1
    )
    server = create_server("127.0.0.1", 0, service, auth, identity)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, service, paths


def request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_direct_api_requires_correct_bearer_and_ingress_uses_peer_boundary(tmp_path):
    server, thread, _, paths = application(tmp_path)
    try:
        assert request(server, "GET", "/api/state")[0] == 401
        assert request(
            server,
            "GET",
            "/api/state",
            headers={"Authorization": "Bearer wrong", "X-Ingress-Path": "/forged"},
        )[0] == 401
        token = paths.token.read_text().strip()
        status, payload = request(
            server,
            "GET",
            "/api/state",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert status == 200
        assert payload["state_generation_branch"].startswith("generation/")
        assert token not in json.dumps(payload)
    finally:
        stop(server, thread)

    ingress_server, ingress_thread, _, _ = application(tmp_path / "ingress", ingress=True)
    try:
        assert request(ingress_server, "GET", "/api/state")[0] == 200
    finally:
        stop(ingress_server, ingress_thread)


def test_discovery_call_raw_evidence_runs_and_reset(tmp_path):
    server, thread, service, paths = application(tmp_path)
    token = paths.token.read_text().strip()
    auth = {"Authorization": f"Bearer {token}"}
    try:
        status, discovery = request(server, "GET", "/api/servers/body/tools", headers=auth)
        assert status == 200
        assert "get_location" in [tool["name"] for tool in discovery["tools"]]

        body = b'{"room":"living_room",  "reason":"two  spaces"}'
        status, result = request(
            server,
            "POST",
            "/api/servers/body/tools/move_to/call",
            body=body,
            headers={**auth, "Content-Type": "text/plain; charset=utf-8"},
        )
        assert status == 200
        assert result["input_raw"].encode() == body
        assert base64.b64decode(result["input_base64"]) == body
        assert result["input_class"] == "valid_json_object"
        assert result["response_id_observed"] is True
        assert result["state_commit_before"] != result["state_commit_after"]
        assert SECRET not in json.dumps(result)

        status, reset = request(server, "POST", "/api/state/reset", body=b"", headers=auth)
        assert status == 200
        assert reset["new_branch"] != reset["old_branch"]
        assert service.state_snapshot()["state_head"] == reset["new_head"]

        ledger_text = paths.ledger.read_text()
        assert SECRET not in ledger_text
        assert token not in ledger_text
        assert '"input_raw":"{\\"room\\":\\"living_room\\",  \\"reason\\":\\"two  spaces\\"}"' in ledger_text
    finally:
        stop(server, thread)


def test_invalid_json_is_forwarded_and_not_reported_as_tool_error(tmp_path):
    server, thread, _, paths = application(tmp_path)
    token = paths.token.read_text().strip()
    try:
        status, result = request(
            server,
            "POST",
            "/api/servers/body/tools/get_location/call",
            body=b'{"broken":',
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/plain",
            },
        )
        assert status == 200
        assert result["input_class"] == "invalid_json"
        assert result["request_layer"] == "protocol"
        assert result["response_id_observed"] is False
        assert result["timed_out"] is True
    finally:
        stop(server, thread)


def test_transport_limit_and_content_type_are_ledgered_before_mcp(tmp_path):
    server, thread, _, paths = application(tmp_path)
    token = paths.token.read_text().strip()
    authorization = {"Authorization": f"Bearer {token}"}
    try:
        status, wrong_type = request(
            server,
            "POST",
            "/api/servers/body/tools/get_location/call",
            body=b"{}",
            headers={**authorization, "Content-Type": "application/json"},
        )
        assert status == 415
        assert wrong_type["mcp_reached"] is False
        status, too_large = request(
            server,
            "POST",
            "/api/servers/body/tools/get_location/call",
            body=b"x" * (MAX_REQUEST_BYTES + 1),
            headers={**authorization, "Content-Type": "text/plain"},
        )
        assert status == 413
        assert too_large["mcp_reached"] is False
        events = [json.loads(line) for line in paths.ledger.read_text().splitlines()]
        assert [item["event_type"] for item in events] == [
            "transport_rejection",
            "transport_rejection",
        ]
    finally:
        stop(server, thread)


def test_running_call_and_reset_are_serialized_without_ledger_corruption(tmp_path):
    server, thread, service, paths = application(tmp_path)
    token = paths.token.read_text().strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/plain",
    }
    responses = {}

    def slow_invalid_call():
        responses["call"] = request(
            server,
            "POST",
            "/api/servers/body/tools/get_location/call",
            body=b"{",
            headers=headers,
        )

    def queued_reset():
        responses["reset"] = request(
            server,
            "POST",
            "/api/state/reset",
            body=b"",
            headers={"Authorization": f"Bearer {token}"},
        )

    call_thread = threading.Thread(target=slow_invalid_call)
    reset_thread = threading.Thread(target=queued_reset)
    try:
        call_thread.start()
        while service.queue.snapshot()["active_operation_id"] is None:
            time.sleep(0.005)
        reset_thread.start()
        call_thread.join(timeout=5)
        reset_thread.join(timeout=5)
        assert responses["call"][0] == 200
        assert responses["reset"][0] == 200
        assert responses["reset"][1]["queue_wait_ms"] > 0
        lines = paths.ledger.read_text().splitlines()
        events = [json.loads(line) for line in lines]
        assert [item["event_type"] for item in events] == ["call", "state_reset"]
        assert service.state_snapshot()["dirty"] is False
    finally:
        stop(server, thread)
