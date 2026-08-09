import os
import sys
from pathlib import Path

from embodied_ha_mcp_lab.runner import (
    MCPRunner,
    build_call_wire,
    classify_input,
    count_line_breaks,
)

DUMMY = Path(__file__).parent / "fixtures" / "dummy_mcp.py"


def runner(**env_overrides):
    env = dict(os.environ)
    env.update(env_overrides)
    return MCPRunner([sys.executable, str(DUMMY)], env=env, timeout_seconds=1)


def test_classification_does_not_restrict_shapes():
    assert classify_input(b'{"x":1}') == "valid_json_object"
    assert classify_input(b"[]") == "valid_json_non_object"
    assert classify_input(b'"x"') == "valid_json_non_object"
    assert classify_input(b"null") == "valid_json_non_object"
    assert classify_input(b'{"x":') == "invalid_json"


def test_call_wire_splices_body_byte_for_byte():
    body = b'{"value":"a  b"}\r\nBROKEN'
    wire = build_call_wire("ec\"ho", "request-1", body)
    assert body in wire
    assert wire.endswith(body + b"}}\n")
    assert count_line_breaks(body) == 1
    assert wire.count(body) == 1


def test_valid_object_reaches_handler_and_observes_response_id():
    result = runner().call("echo", b'{"value":"unchanged  value"}')
    assert result.input_class == "valid_json_object"
    assert result.request_layer == "tool"
    assert result.response_id_observed is True
    assert result.timed_out is False
    assert 'unchanged  value' in result.stdout_raw


def test_valid_non_object_is_sent_and_records_server_behavior():
    result = runner().call("echo", b"[]")
    assert result.input_class == "valid_json_non_object"
    assert result.response_id_observed is True
    assert result.stage == "complete"


def test_invalid_json_is_protocol_failure_not_tool_error():
    result = runner().call("echo", b'{"value":')
    assert result.input_class == "invalid_json"
    assert result.request_layer == "protocol"
    assert result.response_id_observed is False


def test_timeout_terminates_child_and_next_run_succeeds():
    timed_out = runner(DUMMY_MODE="hang").call("echo", b"{}")
    assert timed_out.timed_out is True
    assert timed_out.signal in {15, 9}
    success = runner().call("echo", b"{}")
    assert success.response_id_observed is True


def test_capture_limit_stops_child_and_reports_original_byte_count():
    env = dict(os.environ, DUMMY_MODE="flood", DUMMY_FLOOD_BYTES="8192")
    limited = MCPRunner(
        [sys.executable, str(DUMMY)],
        env=env,
        timeout_seconds=2,
        capture_limit=1024,
    ).call("echo", b"{}")
    assert limited.truncated is True
    assert limited.stderr_bytes >= 8192
    assert len(limited.stderr_raw.encode()) == 1024


def test_live_discovery_returns_actual_schema():
    tools, result = runner().discover_tools()
    assert result.response_id_observed is True
    assert [tool["name"] for tool in tools] == ["echo"]
    assert tools[0]["inputSchema"]["properties"]["value"]["type"] == "string"
