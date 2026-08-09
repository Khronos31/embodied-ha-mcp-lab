import json
import os
from pathlib import Path

from embodied_ha_mcp_lab.runner import MCPRunner
from embodied_ha_mcp_lab.runtime import LabPaths, LabRuntime
from embodied_ha_mcp_lab.state_repository import StateRepository

SOURCE = (
    Path(__file__).parents[1]
    / ".upstream"
    / "embodied-ha-repo"
    / "embodied_ha"
)


def graph():
    return {
        "rooms": {
            "study": {"display_name": "Study"},
            "living_room": {"display_name": "Living room"},
        },
        "edges": [{"from": "study", "to": "living_room", "cost": 1}],
    }


def test_seed_only_allowlisted_configuration_and_never_resyncs(tmp_path):
    seed = tmp_path / "resident"
    seed.mkdir()
    (seed / "preferences.json").write_text('{"resident":"fixture"}')
    (seed / "floorplan_room_graph_draft.json").write_text(json.dumps(graph()))
    (seed / "character.md").write_text("must not copy")
    (seed / "memory.md").write_text("must not copy")
    (seed / "github_app.pem").write_text("must not copy")
    paths = LabPaths.below(tmp_path / "lab")
    runtime = LabRuntime(SOURCE, paths, seed_data_dir=seed, inherited_env={})
    assert runtime.initialize() == [
        "preferences.json",
        "floorplan_room_graph_draft.json",
    ]
    assert sorted(path.name for path in paths.config_dir.iterdir()) == [
        "floorplan_room_graph_draft.json",
        "preferences.json",
    ]
    (seed / "preferences.json").write_text('{"resident":"changed"}')
    assert runtime.initialize() == []
    assert json.loads((paths.config_dir / "preferences.json").read_text()) == {
        "resident": "fixture"
    }


def test_registry_uses_real_pinned_config_and_lab_paths(tmp_path):
    paths = LabPaths.below(tmp_path / "lab")
    runtime = LabRuntime(
        SOURCE,
        paths,
        tested_harness="agy",
        inherited_env={"SUPERVISOR_TOKEN": "sentinel-secret", "PATH": os.environ["PATH"]},
    )
    runtime.initialize()
    assert "body" in runtime.servers()
    audio = runtime.server("audio")
    assert "concentrate_hearing" in audio.registry_tools
    assert audio.env["SUPERVISOR_TOKEN"] == "sentinel-secret"
    assert audio.env["EHA_DATA_DIR"] == str(paths.worktree)
    assert "/config/embodied-ha/" not in "\n".join(audio.env.values())


def test_real_body_server_persists_across_fresh_children_and_commits(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "preferences.json").write_text("{}")
    (seed / "floorplan_room_graph_draft.json").write_text(json.dumps(graph()))
    paths = LabPaths.below(tmp_path / "lab")
    runtime = LabRuntime(
        SOURCE,
        paths,
        seed_data_dir=seed,
        inherited_env={"PATH": os.environ["PATH"]},
    )
    runtime.initialize()
    state = StateRepository(paths.worktree, paths.repository, paths.hooks)
    state.initialize()
    command = runtime.server("body")

    moved = MCPRunner(command.command, env=command.env, timeout_seconds=3).call(
        "move_to", b'{"room":"living_room"}', run_id="move"
    )
    assert moved.response_id_observed is True
    move_commit = state.commit_run("move")
    assert move_commit.before != move_commit.after

    location = MCPRunner(command.command, env=command.env, timeout_seconds=3).call(
        "get_location", b"{}", run_id="location"
    )
    assert location.response_id_observed is True
    assert "living_room" in location.stdout_raw
    location_commit = state.commit_run("location")
    assert location_commit.before == move_commit.after
    assert location_commit.after == move_commit.after


def test_every_real_server_live_tool_list_matches_the_pinned_registry(tmp_path):
    paths = LabPaths.below(tmp_path / "lab")
    runtime = LabRuntime(
        SOURCE,
        paths,
        inherited_env={"PATH": os.environ["PATH"], "SUPERVISOR_TOKEN": "sentinel"},
    )
    runtime.initialize()
    observed = {}
    for server_name in runtime.servers():
        command = runtime.server(server_name)
        tools, result = MCPRunner(
            command.command,
            env=command.env,
            cwd=SOURCE,
            timeout_seconds=3,
        ).discover_tools()
        assert result.response_id_observed, (server_name, result.stderr_raw)
        observed[server_name] = {tool["name"] for tool in tools}
        assert observed[server_name] == set(command.registry_tools)
        assert "sentinel" not in result.stdout_raw
        assert "sentinel" not in result.stderr_raw
    assert observed.keys() == set(runtime.servers())


def test_real_files_server_cannot_read_the_lab_direct_api_token(tmp_path):
    paths = LabPaths.below(tmp_path / "lab")
    runtime = LabRuntime(
        SOURCE,
        paths,
        inherited_env={"PATH": os.environ["PATH"]},
    )
    runtime.initialize()
    token = tmp_path / "eha-mcp-lab-token.config.toml"
    token.write_text("private-token-sentinel", encoding="utf-8")
    command = runtime.server("files")
    result = MCPRunner(
        command.command,
        env=command.env,
        cwd=SOURCE,
        timeout_seconds=3,
    ).call("read_file", json.dumps({"path": str(token)}).encode())
    assert result.response_id_observed is True
    assert "private-token-sentinel" not in result.stdout_raw
    assert "一時的なエージェント設定は読めません" in result.stdout_raw
