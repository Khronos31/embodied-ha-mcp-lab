import os

import pytest

from embodied_ha_mcp_lab.state_repository import (
    StateRepository,
    StateRepositoryError,
)


def repository(tmp_path):
    return StateRepository(
        tmp_path / "worktree", tmp_path / "repository", tmp_path / "hooks"
    )


def test_baseline_commit_generations_and_run_commits(tmp_path):
    state = repository(tmp_path)
    assert state.initialize() is None
    baseline = state._run("rev-parse", "baseline").stdout.strip()
    assert state.branch().startswith("generation/")
    assert state.head() == baseline
    assert state.remotes() == []

    (state.worktree / "body_location.json").write_text("{}\n", encoding="utf-8")
    committed = state.commit_run("safe-id")
    assert committed.before == baseline
    assert committed.after != baseline
    assert committed.changes == [{"status": "A", "path": "body_location.json"}]
    assert state.is_dirty() is False

    reset = state.reset()
    assert reset["old_head"] == committed.after
    assert reset["new_head"] == baseline
    assert reset["new_branch"] != reset["old_branch"]
    assert (state.worktree / "body_location.json").exists() is False
    assert len(state.branches()) == 3


def test_dirty_startup_is_preserved_as_recovery_commit(tmp_path):
    state = repository(tmp_path)
    state.initialize()
    (state.worktree / "orphan.txt").write_text("preserve me", encoding="utf-8")
    recovery = state.initialize()
    assert recovery["event"] == "unattributed_dirty_startup"
    assert state.is_dirty() is False
    assert (state.worktree / "orphan.txt").read_text() == "preserve me"
    assert state._run("log", "-1", "--pretty=%s").stdout.startswith("recovery:")


def test_remote_or_special_file_fails_closed(tmp_path):
    state = repository(tmp_path)
    state.initialize()
    state._run("remote", "add", "origin", "https://example.invalid/state.git")
    with pytest.raises(StateRepositoryError, match="must not have remotes"):
        state.initialize()
    state._run("remote", "remove", "origin")
    fifo = state.worktree / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(StateRepositoryError, match="special file"):
        state.validate_file_types()


def test_commit_messages_never_contain_input(tmp_path):
    state = repository(tmp_path)
    state.initialize()
    (state.worktree / "secret.txt").write_text("sentinel", encoding="utf-8")
    state.commit_run("opaque-run-id")
    message = state._run("log", "-1", "--pretty=%B").stdout
    assert message.strip() == "run:opaque-run-id"
    assert "sentinel" not in message
