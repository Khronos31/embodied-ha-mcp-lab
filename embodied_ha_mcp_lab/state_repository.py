"""Persistent Lab state generations backed by a local, remote-free Git repository."""

from __future__ import annotations

import os
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class StateRepositoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommitResult:
    before: str
    after: str
    changes: list[dict[str, str]]


class StateRepository:
    def __init__(self, worktree: Path, git_dir: Path, hooks_dir: Path) -> None:
        self.worktree = worktree.resolve()
        self.git_dir = git_dir.resolve()
        self.hooks_dir = hooks_dir.resolve()

    def initialize(self) -> dict[str, Any] | None:
        """Create baseline/generation or preserve unattributed dirty state."""

        self.worktree.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.git_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.hooks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (self.git_dir / "HEAD").exists():
            self._run("init", "--initial-branch=baseline")
            self._run("config", "user.name", "Embodied HA MCP Lab")
            self._run("config", "user.email", "mcp-lab@localhost.invalid")
            self._run("config", "core.hooksPath", str(self.hooks_dir))
            self._run("commit", "--allow-empty", "-m", "baseline")
            self._run("switch", "--create", self._new_generation(), "baseline")
            return None

        self._assert_contract()
        self.validate_file_types()
        if self.is_dirty():
            event_id = uuid.uuid4().hex
            before = self.head()
            self._run("add", "-A", "--", ".")
            self._run("commit", "-m", f"recovery:{event_id}")
            return {
                "event": "unattributed_dirty_startup",
                "event_id": event_id,
                "state_generation_branch": self.branch(),
                "state_commit_before": before,
                "state_commit_after": self.head(),
            }
        return None

    def _assert_contract(self) -> None:
        if self.remotes():
            raise StateRepositoryError("Lab state repository must not have remotes")
        configured_hooks = self._run("config", "--get", "core.hooksPath").stdout.strip()
        if Path(configured_hooks).resolve() != self.hooks_dir:
            raise StateRepositoryError("Lab state repository hooks path is not isolated")
        branch = self.branch()
        if branch != "baseline" and not branch.startswith("generation/"):
            raise StateRepositoryError("unexpected Lab state branch")

    def validate_file_types(self) -> None:
        for root, directories, files in os.walk(self.worktree, followlinks=False):
            for name in [*directories, *files]:
                path = Path(root) / name
                mode = path.lstat().st_mode
                if not (
                    stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)
                ):
                    raise StateRepositoryError(
                        f"unsupported special file in Lab state: {path.relative_to(self.worktree)}"
                    )

    def commit_run(self, run_id: str) -> CommitResult:
        self.validate_file_types()
        before = self.head()
        self._run("add", "-A", "--", ".")
        staged = self._run("diff", "--cached", "--quiet", check=False)
        if staged.returncode == 1:
            self._run("commit", "-m", f"run:{run_id}")
        elif staged.returncode != 0:
            raise StateRepositoryError("could not inspect staged Lab state")
        after = self.head()
        return CommitResult(before, after, self.diff_names(before, after))

    def reset(self) -> dict[str, str]:
        self.validate_file_types()
        if self.is_dirty():
            raise StateRepositoryError("cannot reset dirty Lab state")
        old_branch = self.branch()
        old_head = self.head()
        new_branch = self._new_generation()
        self._run("switch", "--create", new_branch, "baseline")
        return {
            "old_branch": old_branch,
            "new_branch": new_branch,
            "old_head": old_head,
            "new_head": self.head(),
        }

    def state(self) -> dict[str, Any]:
        self.validate_file_types()
        return {
            "state_generation_branch": self.branch(),
            "state_head": self.head(),
            "worktree_bytes": _tree_bytes(self.worktree),
            "repository_bytes": _tree_bytes(self.git_dir),
            "generation_branch_count": len(
                [name for name in self.branches() if name.startswith("generation/")]
            ),
            "dirty": self.is_dirty(),
        }

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    def branch(self) -> str:
        return self._run("branch", "--show-current").stdout.strip()

    def branches(self) -> list[str]:
        output = self._run("for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        return [line for line in output.splitlines() if line]

    def remotes(self) -> list[str]:
        return [line for line in self._run("remote").stdout.splitlines() if line]

    def is_dirty(self) -> bool:
        return bool(self._run("status", "--porcelain=v1", "--untracked-files=all").stdout)

    def diff_names(self, before: str, after: str) -> list[dict[str, str]]:
        if before == after:
            return []
        changes: list[dict[str, str]] = []
        output = self._run("diff", "--name-status", "--no-renames", before, after).stdout
        for line in output.splitlines():
            status_code, separator, relative = line.partition("\t")
            if separator and relative:
                changes.append({"status": status_code, "path": relative})
        return changes

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": os.environ.get(
                "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C.UTF-8",
        }
        command = [
            "git",
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.worktree}",
            "-c",
            f"core.hooksPath={self.hooks_dir}",
            *arguments,
        ]
        completed = subprocess.run(
            command,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            message = completed.stderr.strip() or "git command failed"
            raise StateRepositoryError(message)
        return completed

    @staticmethod
    def _new_generation() -> str:
        return f"generation/{uuid.uuid4().hex}"


def _tree_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return total
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            path = Path(directory) / name
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                total += metadata.st_size
    return total
