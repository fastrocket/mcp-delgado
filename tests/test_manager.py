from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_delgado.manager import JobManager


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "initial")
    return workspace


def test_allowed_path_matching(tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    assert manager._is_allowed("src/app.py", ["src"])
    assert manager._is_allowed("tests/test_app.py", ["tests/*.py"])
    assert not manager._is_allowed("README.md", ["src"])


def test_rejects_parent_traversal(tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    with pytest.raises(ValueError):
        manager._normalize_allowed_paths(["../secret"])


def test_status_reports_new_file(repo: Path, tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    before = manager._status(repo, ["new.txt"])
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    after = manager._status(repo, ["new.txt"])
    assert "new.txt" not in before
    assert after["new.txt"].startswith("??:")


def test_status_detects_changes_to_an_already_dirty_file(repo: Path, tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    target = repo / "README.md"
    target.write_text("first change\n", encoding="utf-8")
    before = manager._status(repo)
    target.write_text("later change\n", encoding="utf-8")
    after = manager._status(repo)
    assert before["README.md"] != after["README.md"]


def test_live_job_is_not_marked_interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    job_dir = manager.jobs_dir / ("a" * 32)
    job_dir.mkdir()
    (job_dir / "job.json").write_text("""{
      "job_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "state": "running",
      "task": "A task long enough",
      "acceptance_criteria": [],
      "workspace_path": ".",
      "allowed_paths": ["src"],
      "required_commands": [],
      "provider": "deepseek",
      "model": "deepseek-flash",
      "max_minutes": 30,
      "created_at": "2026-01-01T00:00:00+00:00",
      "pid": 123,
      "summary": "",
      "error": "",
      "changed_paths": [],
      "policy_violations": [],
      "validation_results": []
    }""", encoding="utf-8")
    monkeypatch.setattr(JobManager, "_pid_is_running", staticmethod(lambda pid: True))

    JobManager(state_dir=tmp_path / "state")

    assert manager.get("a" * 32).state.value == "running"


def test_validation_command_uses_argument_list() -> None:
    assert JobManager._validation_args("python -m pytest tests/test_app.py") == [
        "python", "-m", "pytest", "tests/test_app.py",
    ]


@pytest.mark.parametrize("command", ["powershell Remove-Item file", "python -c 'print(1)'", "pytest ; whoami"])
def test_validation_command_rejects_unsafe_forms(command: str) -> None:
    with pytest.raises(ValueError):
        JobManager._validation_args(command)
