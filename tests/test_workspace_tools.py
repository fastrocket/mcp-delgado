"""Unit tests for the bounded workspace tool layer.

The suite runs against temp workspaces. ``.git`` is created as a plain directory
rather than through git, because the tool layer never runs git: the exclusion is
about the name, and building the checkout with a subprocess would prove nothing
about the tools. Every write and read is asserted on real bytes in the temp
workspace, and each security boundary — traversal, absolute and drive paths,
link escapes, tool state, secret names, binary content, size and count caps, the
allowlist, and stale hashes — has its own test.

Link tests skip where the platform refuses to create a link, which is the
portable behavior on a Windows machine without developer mode.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

import pytest

from mcp_delgado import workspace_tools
from mcp_delgado.manager import JobManager
from mcp_delgado.workspace_tools import (
    HARD_MAX_FILE_BYTES,
    BinaryFileError,
    ExcludedPathError,
    LimitExceededError,
    MatchCountError,
    MissingFileError,
    PathDeniedError,
    SizeLimitError,
    StaleFileError,
    UnsafePathError,
    WorkspaceToolError,
    WorkspaceTools,
    is_secret_name,
    is_tool_state_path,
    matches_allowed_path,
)

DEFAULT_PATTERNS = ("src", "tests/*.py", "*.md")
# fnmatch translates ``*`` to a match across separators, so this one pattern
# covers every path: the same glob the manager would accept for a whole job.
ALLOW_EVERYTHING = ("*",)


# Fixtures ------------------------------------------------------------------


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory beside the workspace that no operation may reach."""
    path = tmp_path / "outside"
    path.mkdir()
    (path / "loot.txt").write_bytes(b"outside the workspace\n")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A temp workspace holding every path class the tool layer classifies."""
    root = tmp_path / "repo"
    for directory in (
        ".codewhale/state",
        ".git",
        ".pytest-run",
        ".pytest_cache/v/cache",
        ".venv",
        "docs",
        "node_modules/pkg",
        "sizes",
        "src/.pytest-nested",
        "src/app",
        "tests",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)

    (root / "README.md").write_bytes(b"# Readme\n")
    (root / "binary.bin").write_bytes(b"\x00\x01\x02binary")
    (root / "credentials.json").write_bytes(b'{"token": "live-credential"}\n')
    (root / "deploy.key").write_bytes(b"-----BEGIN KEY-----\n")
    (root / "id_rsa").write_bytes(b"-----BEGIN RSA PRIVATE KEY-----\n")
    (root / ".env").write_bytes(b"TOKEN=live-secret\n")
    (root / ".env.example").write_bytes(b"TOKEN=example\n")
    (root / ".git" / "config").write_bytes(b"[core]\n")
    (root / ".codewhale" / "state" / "session.json").write_bytes(b'{"session": "live"}\n')
    (root / ".pytest-run" / "notes.txt").write_bytes(b"basetemp state\n")
    (root / ".pytest_cache" / "v" / "cache" / "lastfailed").write_bytes(b'{"v": 1}\n')
    (root / ".venv" / "pyvenv.cfg").write_bytes(b"home = python\n")
    (root / "docs" / "guide.md").write_bytes(b"# Guide\n")
    (root / "node_modules" / "pkg" / "index.js").write_bytes(b"marker\n")
    (root / "sizes" / "small.txt").write_bytes(b"tiny\n")
    (root / "sizes" / "large.txt").write_bytes(b"L" * 200)
    (root / "src" / "app" / "main.py").write_bytes(b"alpha\nbeta\ngamma\n")
    (root / "src" / "app" / "util.py").write_bytes(b"MARKER once\n")
    (root / "src" / "app" / "notes.md").write_bytes(b"# Notes\n")
    (root / "src" / ".pytest-nested" / "notes.txt").write_bytes(b"nested pytest state\n")
    (root / "tests" / "test_app.py").write_bytes(b"def test_x():\n    assert True\n")
    return root


@pytest.fixture
def tools(workspace: Path) -> WorkspaceTools:
    return WorkspaceTools(workspace, list(DEFAULT_PATTERNS))


def _tools(workspace: Path, patterns: Sequence[str] = DEFAULT_PATTERNS, **limits: int) -> WorkspaceTools:
    return WorkspaceTools(workspace, list(patterns), **limits)


def _link(link: Path, target: Path, *, directory: bool) -> None:
    """Create a link, skipping the test where the platform refuses one.

    A symlink needs a privilege on Windows outside developer mode; for a
    directory, a junction is tried next because it is unprivileged, and only
    then does the test skip.
    """
    try:
        os.symlink(target, link, target_is_directory=directory)
        return
    except (OSError, NotImplementedError, AttributeError) as exc:
        symlink_error = exc
    if os.name == "nt" and directory:
        try:
            import _winapi  # type: ignore[import-not-found]

            _winapi.CreateJunction(str(target), str(link))
            return
        except Exception:  # pragma: no cover - depends on the host privilege
            pass
    pytest.skip(f"links are unavailable on this platform: {symlink_error}")


def _names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


# Construction --------------------------------------------------------------


def test_workspace_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        WorkspaceTools(tmp_path / "missing", ["."])


def test_workspace_must_be_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_bytes(b"x\n")
    with pytest.raises(ValueError):
        WorkspaceTools(target, ["."])


def test_allowed_paths_are_normalized(workspace: Path) -> None:
    tools = WorkspaceTools(workspace, ["./src/", "tests\\*.py", "*.md"])
    assert tools.allowed_paths == ("src", "tests/*.py", "*.md")


@pytest.mark.parametrize("bad", ["../escape", "/absolute", "C:temp", "", "src/../../x"])
def test_invalid_allowed_paths_are_refused(workspace: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceTools(workspace, [bad])


def test_limits_are_validated_at_construction(workspace: Path) -> None:
    with pytest.raises(ValueError):
        _tools(workspace, max_results=0)
    with pytest.raises(ValueError):
        _tools(workspace, max_file_bytes=HARD_MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError):
        _tools(workspace, max_matches="many")  # type: ignore[arg-type]


# Path safety ---------------------------------------------------------------


def test_operations_refuse_absolute_paths(tools: WorkspaceTools, outside: Path) -> None:
    """An absolute path never reaches the filesystem, wherever it points."""
    absolute = str(outside / "loot.txt")
    with pytest.raises(UnsafePathError):
        tools.read_file(absolute)
    with pytest.raises(UnsafePathError):
        tools.list_files(absolute)
    with pytest.raises(UnsafePathError):
        tools.search_text("loot", absolute)
    with pytest.raises(UnsafePathError):
        tools.write_file(absolute, "x\n")
    with pytest.raises(UnsafePathError):
        tools.replace_text(absolute, "loot", "x")


@pytest.mark.parametrize(
    "raw",
    [
        "../outside/loot.txt",
        "src/../../outside/loot.txt",
        "..\\..\\outside\\loot.txt",
        "src/app/../../../etc/passwd",
        "src/app/../main.py",
    ],
)
def test_operations_refuse_parent_traversal(tools: WorkspaceTools, raw: str) -> None:
    """Any ``..`` segment is refused, even one that would resolve back inside."""
    with pytest.raises(UnsafePathError):
        tools.read_file(raw)
    with pytest.raises(UnsafePathError):
        tools.write_file(raw, "x\n")


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:evil.txt",
        "\\\\server\\share\\payload.txt",
        "~/.ssh/id_rsa",
        "src\\app\\main.py:secret",
    ],
)
def test_operations_refuse_other_path_syntaxes(tools: WorkspaceTools, raw: str) -> None:
    with pytest.raises(UnsafePathError):
        tools.read_file(raw)


@pytest.mark.parametrize("raw", [None, 42, b"src/app/main.py", "src/\x00main.py", "a" * 1_001])
def test_operations_refuse_unusable_path_values(tools: WorkspaceTools, raw: object) -> None:
    with pytest.raises(UnsafePathError):
        tools.read_file(raw)  # type: ignore[arg-type]


def test_root_path_spellings_are_equivalent(tools: WorkspaceTools) -> None:
    root = tools.list_files()["entries"]
    for spelling in (".", "./", "", "./."):
        assert tools.list_files(spelling)["entries"] == root


def test_reported_paths_are_repository_relative(tools: WorkspaceTools, workspace: Path) -> None:
    read = tools.read_file("src/app/main.py")
    listing = tools.list_files("src")
    found = tools.search_text("alpha")
    for reported in (read["path"], listing["path"], found["path"], listing["entries"][0]["path"]):
        assert not reported.startswith("/")
        assert ":" not in reported
        assert str(workspace) not in reported


def test_link_that_leaves_the_workspace_is_refused(workspace: Path, outside: Path) -> None:
    """A link out of the checkout is neither read through, written through, nor listed."""
    _link(workspace / "filelink.txt", outside / "loot.txt", directory=False)
    _link(workspace / "dirlink", outside, directory=True)
    tools = _tools(workspace, ALLOW_EVERYTHING)

    with pytest.raises(UnsafePathError):
        tools.read_file("filelink.txt")
    with pytest.raises(UnsafePathError):
        tools.read_file("dirlink/loot.txt")
    with pytest.raises(UnsafePathError):
        tools.write_file("dirlink/planted.txt", "planted\n")
    with pytest.raises(UnsafePathError):
        tools.replace_text("filelink.txt", "outside", "inside")
    with pytest.raises(UnsafePathError):
        tools.list_files("dirlink")

    listed = [entry["path"] for entry in tools.list_files(".")["entries"]]
    assert "filelink.txt" not in listed
    assert "dirlink" not in listed
    assert tools.search_text("outside the workspace")["count"] == 0
    assert not (outside / "planted.txt").exists()


def test_link_inside_the_workspace_is_resolved_and_reported(workspace: Path) -> None:
    """An in-workspace link reads and writes the real file, and reports that file."""
    target = workspace / "src" / "app" / "util.py"
    _link(workspace / "src" / "app" / "pointer.py", target, directory=False)
    tools = _tools(workspace, ALLOW_EVERYTHING)

    read = tools.read_file("src/app/pointer.py")
    assert read["path"] == "src/app/util.py"
    assert read["text"] == "MARKER once\n"

    tools.write_file("src/app/pointer.py", "REPLACED\n", expected_sha256=read["sha256"])
    assert target.read_bytes() == b"REPLACED\n"
    assert (workspace / "src" / "app" / "pointer.py").is_symlink()


def test_dangling_link_inside_the_workspace_stays_inside(workspace: Path) -> None:
    """A link whose target does not exist yet is still checked against the workspace."""
    _link(workspace / "src" / "app" / "later.py", workspace / "src" / "app" / "created.py", directory=False)
    tools = _tools(workspace, ALLOW_EVERYTHING)
    result = tools.write_file("src/app/later.py", "value = 1\n")
    assert result["path"] == "src/app/created.py"
    assert (workspace / "src" / "app" / "created.py").read_bytes() == b"value = 1\n"


# Read exclusions -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        ".git/config",
        "src/.git/config",
        ".codewhale/state/session.json",
        "src/.codewhale/session.json",
        ".pytest_cache/v/cache/lastfailed",
        ".pytest-run/notes.txt",
    ],
)
def test_reads_refuse_tool_state_and_repository_metadata(tools: WorkspaceTools, raw: str) -> None:
    with pytest.raises(ExcludedPathError):
        tools.read_file(raw)
    with pytest.raises(ExcludedPathError):
        tools.search_text("core", raw)


@pytest.mark.parametrize(
    "raw",
    [".env", ".env.example", "id_rsa", "deploy.key", "credentials.json", "nested/secrets.json"],
)
def test_reads_refuse_secret_looking_names(tools: WorkspaceTools, raw: str) -> None:
    with pytest.raises(ExcludedPathError):
        tools.read_file(raw)


def test_writes_refuse_tool_state_secrets_and_metadata(workspace: Path) -> None:
    """Even an allowlist covering the whole checkout cannot write these paths."""
    tools = _tools(workspace, ALLOW_EVERYTHING)
    for raw in (".codewhale/state/session.json", ".git/hooks/evil", ".env", "src/new.pem"):
        with pytest.raises(ExcludedPathError):
            tools.write_file(raw, "planted\n")


def test_metadata_tool_state_and_secrets_are_hidden_from_listings_and_search(
    tools: WorkspaceTools, workspace: Path
) -> None:
    listed = [entry["path"] for entry in tools.list_files(".")["entries"]]
    assert listed == sorted(["README.md", "binary.bin", "docs", "sizes", "src", "tests"], key=str.casefold)
    for hidden in (".git", ".codewhale", ".env", ".env.example", "id_rsa", "deploy.key", "credentials.json"):
        assert hidden not in listed
    assert tools.search_text("live-secret")["count"] == 0
    assert tools.search_text("live-credential")["count"] == 0
    assert tools.search_text("PRIVATE KEY")["count"] == 0
    assert all(".git/" not in match["path"] for match in tools.search_text("core")["matches"])


def test_nested_pytest_state_stays_visible_like_the_manager_rule(tools: WorkspaceTools) -> None:
    """Only root-level pytest state is tool-owned, exactly as the audit defines it."""
    assert tools.read_file("src/.pytest-nested/notes.txt")["text"] == "nested pytest state\n"


def test_generated_trees_are_hidden_from_walks_but_readable_on_request(
    tools: WorkspaceTools, workspace: Path
) -> None:
    listed = [entry["path"] for entry in tools.list_files(".")["entries"]]
    assert "node_modules" not in listed
    assert ".venv" not in listed
    assert tools.search_text("marker")["count"] == 0
    assert tools.read_file(".venv/pyvenv.cfg")["text"] == "home = python\n"
    dependency = tools.search_text("marker", path="node_modules")
    assert [match["path"] for match in dependency["matches"]] == ["node_modules/pkg/index.js"]


def test_binary_contents_are_never_returned(workspace: Path) -> None:
    tools = _tools(workspace, ALLOW_EVERYTHING)
    with pytest.raises(BinaryFileError):
        tools.read_file("binary.bin")
    with pytest.raises(BinaryFileError):
        tools.replace_text("binary.bin", "binary", "text")
    listing = {entry["path"]: entry for entry in tools.list_files(".")["entries"]}
    assert listing["binary.bin"] == {"path": "binary.bin", "type": "file", "size": 9}
    search = tools.search_text("binary")
    assert search["count"] == 0
    assert search["files_skipped_binary"] == 1


def test_missing_and_directory_targets_are_reported(tools: WorkspaceTools) -> None:
    with pytest.raises(MissingFileError):
        tools.read_file("src/app/absent.py")
    with pytest.raises(MissingFileError):
        tools.list_files("src/absent")
    with pytest.raises(PathDeniedError):
        tools.read_file("docs")
    with pytest.raises(PathDeniedError):
        tools.list_files("README.md")


# Caps ----------------------------------------------------------------------


def test_read_cap_refuses_a_large_file(workspace: Path) -> None:
    tools = _tools(workspace, max_file_bytes=8)
    with pytest.raises(SizeLimitError) as error:
        tools.read_file("README.md")
    assert "8-byte read cap" in str(error.value)


def test_read_cap_can_be_raised_per_call_but_not_past_the_ceiling(tools: WorkspaceTools) -> None:
    assert tools.read_file("README.md", max_bytes=1_000)["bytes"] == 9
    with pytest.raises(SizeLimitError):
        tools.read_file("README.md", max_bytes=HARD_MAX_FILE_BYTES + 1)
    with pytest.raises(SizeLimitError):
        tools.read_file("README.md", max_bytes=0)


def test_list_cap_refuses_a_partial_listing(workspace: Path) -> None:
    with pytest.raises(LimitExceededError):
        _tools(workspace, max_results=3).list_files(".")
    assert _tools(workspace, max_results=6).list_files(".")["count"] == 6


def test_search_match_cap_refuses_a_partial_answer(workspace: Path) -> None:
    target = workspace / "src" / "app" / "repeats.txt"
    target.write_bytes(b"needle\n" * 5)
    with pytest.raises(LimitExceededError):
        _tools(workspace, max_matches=2).search_text("needle")
    assert _tools(workspace, max_matches=5).search_text("needle")["count"] == 5


def test_search_scan_cap_refuses_an_oversized_walk(workspace: Path) -> None:
    with pytest.raises(SizeLimitError):
        _tools(workspace, max_scan_bytes=10).search_text("e", file_pattern="*.txt")


def test_search_skips_oversized_files_and_counts_them(workspace: Path) -> None:
    tools = _tools(workspace, max_file_bytes=100)
    result = tools.search_text("t", path="sizes")
    assert result["files_searched"] == 1
    assert result["files_skipped_large"] == 1
    assert [match["path"] for match in result["matches"]] == ["sizes/small.txt"]


def test_write_cap_refuses_an_oversized_write(workspace: Path) -> None:
    tools = _tools(workspace, max_write_bytes=16)
    with pytest.raises(SizeLimitError):
        tools.write_file("src/app/big.py", "x" * 100)
    with pytest.raises(SizeLimitError):
        tools.replace_text("README.md", "# Readme", "y" * 100)


def test_edit_cap_refuses_a_file_too_large_to_hold(workspace: Path) -> None:
    tools = _tools(workspace, ALLOW_EVERYTHING, max_file_bytes=16, max_write_bytes=16)
    with pytest.raises(SizeLimitError):
        tools.replace_text("sizes/large.txt", "LL", "L")
    with pytest.raises(SizeLimitError):
        tools.write_file("sizes/large.txt", "small\n", expected_sha256=hashlib.sha256(b"L" * 200).hexdigest())


def test_search_match_text_is_truncated_and_flagged(tools: WorkspaceTools, workspace: Path) -> None:
    long_line = "head " + "z" * 500
    (workspace / "src" / "app" / "long.py").write_bytes((long_line + "\n").encode("utf-8"))
    match = tools.search_text("head")["matches"][0]
    assert match["text_truncated"] is True
    assert len(match["text"]) == 400


def test_invalid_query_and_pattern_arguments(tools: WorkspaceTools) -> None:
    for bad in ("", "multi\nline", "x" * 201, "\x00"):
        with pytest.raises(WorkspaceToolError):
            tools.search_text(bad)
    with pytest.raises(WorkspaceToolError):
        tools.search_text("alpha", file_pattern="")
    with pytest.raises(WorkspaceToolError):
        tools.search_text("alpha", case_sensitive="yes")  # type: ignore[arg-type]


# Writes --------------------------------------------------------------------


def test_write_file_creates_a_new_file_with_parent_directories(workspace: Path) -> None:
    tools = _tools(workspace, ALLOW_EVERYTHING)
    result = tools.write_file("src/app/deep/new.py", "value = 1\n")
    payload = b"value = 1\n"
    assert result == {
        "path": "src/app/deep/new.py",
        "created": True,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert (workspace / "src" / "app" / "deep" / "new.py").read_bytes() == payload
    assert _names(workspace / "src" / "app" / "deep") == ["new.py"]


def test_write_file_requires_a_hash_before_overwriting(tools: WorkspaceTools, workspace: Path) -> None:
    current = hashlib.sha256((workspace / "README.md").read_bytes()).hexdigest()
    with pytest.raises(StaleFileError) as error:
        tools.write_file("README.md", "# Other\n")
    assert current in str(error.value)
    assert error.value.detail["sha256"] == current
    assert (workspace / "README.md").read_bytes() == b"# Readme\n"


def test_write_file_overwrites_with_a_matching_hash(tools: WorkspaceTools, workspace: Path) -> None:
    expected = tools.read_file("README.md")["sha256"]
    result = tools.write_file("README.md", "# Changed\n", expected_sha256=expected)
    assert result["created"] is False
    assert result["sha256"] == hashlib.sha256(b"# Changed\n").hexdigest()
    assert (workspace / "README.md").read_bytes() == b"# Changed\n"


def test_write_file_refuses_a_stale_hash(tools: WorkspaceTools, workspace: Path) -> None:
    stale = hashlib.sha256(b"an older revision\n").hexdigest()
    with pytest.raises(StaleFileError):
        tools.write_file("README.md", "# Changed\n", expected_sha256=stale)
    assert (workspace / "README.md").read_bytes() == b"# Readme\n"
    assert [name for name in os.listdir(workspace) if name.startswith(".README.md")] == []


def test_write_file_refuses_a_hash_for_a_file_that_does_not_exist(tools: WorkspaceTools) -> None:
    with pytest.raises(StaleFileError):
        tools.write_file("src/app/new.py", "value = 1\n", expected_sha256="a" * 64)
    with pytest.raises(StaleFileError):
        tools.write_file("src/app/main.py", "value = 1\n", expected_sha256="not-a-digest")


def test_write_file_refuses_paths_outside_the_allowlist(tools: WorkspaceTools) -> None:
    with pytest.raises(PathDeniedError) as error:
        tools.write_file("sizes/small.txt", "tiny\n")
    assert "sizes/small.txt" in str(error.value)
    with pytest.raises(PathDeniedError):
        tools.write_file("sizes/new.txt", "new\n")


def test_allowlist_globs_and_directory_patterns_govern_writes(workspace: Path) -> None:
    tools = WorkspaceTools(workspace, ["tests/*.py", "src"])
    assert tools.write_file("tests/test_new.py", "def test_z():\n    pass\n")["created"] is True
    # fnmatch globs cross separators, exactly like the manager's audit rule.
    assert tools.write_file("tests/deep/test_nested.py", "def test_w():\n    pass\n")["created"] is True
    read = tools.read_file("src/app/main.py")
    assert tools.write_file("src/app/main.py", "alpha\n", expected_sha256=read["sha256"])["created"] is False
    with pytest.raises(PathDeniedError):
        tools.write_file("docs/new.md", "# New\n")
    with pytest.raises(PathDeniedError):
        tools.write_file("README.md", "# Other\n", expected_sha256=read["sha256"])


def test_empty_allowlist_denies_every_write(workspace: Path) -> None:
    tools = WorkspaceTools(workspace, [])
    with pytest.raises(PathDeniedError):
        tools.write_file("src/app/main.py", "alpha\n")
    with pytest.raises(PathDeniedError):
        tools.replace_text("src/app/main.py", "alpha", "beta")


def test_write_file_refuses_a_directory_target(workspace: Path) -> None:
    tools = _tools(workspace, ALLOW_EVERYTHING)
    with pytest.raises(PathDeniedError):
        tools.write_file("src/app", "x\n")


def test_write_file_refuses_content_that_is_not_text(workspace: Path) -> None:
    tools = _tools(workspace, ALLOW_EVERYTHING)
    with pytest.raises(BinaryFileError):
        tools.write_file("src/app/nul_bytes.py", "before\x00after")
    with pytest.raises(WorkspaceToolError):
        tools.write_file("src/app/bytes.py", b"bytes\n")  # type: ignore[arg-type]
    assert not (workspace / "src" / "app" / "nul_bytes.py").exists()


@pytest.mark.skipif(os.name != "nt", reason="reserved device names are a Windows rule")
@pytest.mark.parametrize("raw", ["NUL", "src/app/con.py", "src/aux.txt", "src/nested/com1"])
def test_reserved_windows_device_names_are_refused(workspace: Path, raw: str) -> None:
    """Writing to a device name would discard the content and still look successful."""
    tools = _tools(workspace, ALLOW_EVERYTHING)
    with pytest.raises(UnsafePathError):
        tools.write_file(raw, "x\n")


def test_replace_text_is_exact_and_atomic(tools: WorkspaceTools, workspace: Path) -> None:
    target = workspace / "src" / "app" / "crlf.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\ntwo\r\n")
    with pytest.raises(MatchCountError) as error:
        tools.replace_text("src/app/crlf.txt", "two", "TWO")
    assert error.value.detail == {"occurrences": 2, "expected": 1}

    result = tools.replace_text("src/app/crlf.txt", "two", "TWO", expected_occurrences=2)
    assert result["replacements"] == 2
    assert result["sha256"] == hashlib.sha256(b"one\r\nTWO\r\nthree\r\nTWO\r\n").hexdigest()
    assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\nTWO\r\n"
    assert _names(target.parent) == ["crlf.txt", "main.py", "notes.md", "util.py"]


def test_replace_text_refuses_missing_and_empty_needles(tools: WorkspaceTools, workspace: Path) -> None:
    with pytest.raises(MatchCountError) as error:
        tools.replace_text("src/app/main.py", "delta", "epsilon")
    assert error.value.detail["occurrences"] == 0
    with pytest.raises(WorkspaceToolError):
        tools.replace_text("src/app/main.py", "", "epsilon")
    with pytest.raises(WorkspaceToolError):
        tools.replace_text("src/app/main.py", "alpha", "beta", expected_occurrences=0)
    assert (workspace / "src" / "app" / "main.py").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_replace_text_refuses_a_stale_hash(tools: WorkspaceTools, workspace: Path) -> None:
    stale = hashlib.sha256(b"older\n").hexdigest()
    with pytest.raises(StaleFileError):
        tools.replace_text("src/app/main.py", "alpha", "ALPHA", expected_sha256=stale)
    assert (workspace / "src" / "app" / "main.py").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_replace_text_accepts_the_hash_it_read(tools: WorkspaceTools, workspace: Path) -> None:
    read = tools.read_file("src/app/main.py")
    tools.replace_text("src/app/main.py", "gamma", "GAMMA", expected_sha256=read["sha256"])
    assert (workspace / "src" / "app" / "main.py").read_bytes() == b"alpha\nbeta\nGAMMA\n"


def test_replace_text_requires_an_existing_text_file(workspace: Path) -> None:
    tools = _tools(workspace, ALLOW_EVERYTHING)
    with pytest.raises(MissingFileError):
        tools.replace_text("src/app/absent.py", "a", "b")
    with pytest.raises(BinaryFileError):
        tools.replace_text("binary.bin", "binary", "text")
    with pytest.raises(PathDeniedError):
        tools.replace_text("docs", "# Guide", "# Other")


def test_replace_text_respects_the_allowlist(tools: WorkspaceTools) -> None:
    with pytest.raises(PathDeniedError):
        tools.replace_text("sizes/small.txt", "tiny", "TINY")
    with pytest.raises(PathDeniedError):
        tools.replace_text("binary.bin", "binary", "text")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not meaningful on Windows")
def test_replace_text_preserves_the_file_mode(tools: WorkspaceTools, workspace: Path) -> None:
    target = workspace / "src" / "app" / "script.py"
    target.write_bytes(b"print('x')\n")
    os.chmod(target, 0o640)
    tools.replace_text("src/app/script.py", "x", "y")
    assert os.stat(target).st_mode & 0o777 == 0o640


def test_operations_never_spawn_a_shell_or_subprocess(
    tools: WorkspaceTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole tool surface runs with process creation disabled."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the workspace tool layer must not spawn a process")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(os, "system", explode)

    assert tools.list_files("src")["count"] == 2
    assert tools.search_text("alpha")["count"] == 1
    assert tools.read_file("README.md")["bytes"] == 9
    assert tools.write_file("src/app/added.py", "value = 1\n")["created"] is True
    assert tools.replace_text("src/app/added.py", "value", "answer")["replacements"] == 1


def test_module_imports_no_process_or_shell_module() -> None:
    """A static check that no future edit quietly adds a process dependency."""
    source = Path(workspace_tools.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported
    assert "shutil" not in imported
    assert "shell=True" not in source


# Dispatch and contract -----------------------------------------------------


def test_call_dispatches_by_tool_name(tools: WorkspaceTools) -> None:
    assert tools.call("list_files", {"path": "src"})["count"] == 2
    assert tools.call("read_file", {"path": "README.md"})["text"] == "# Readme\n"
    assert tools.call("search_text", {"query": "alpha", "max_matches": 5})["count"] == 1
    assert tools.call("write_file", {"path": "src/app/called.py", "content": "x = 1\n"})["created"] is True
    assert tools.call("replace_text", {"path": "src/app/called.py", "old": "x", "new": "y"})["replacements"] == 1


def test_call_refuses_unknown_tools_and_bad_arguments(tools: WorkspaceTools) -> None:
    with pytest.raises(WorkspaceToolError) as unknown:
        tools.call("delete_everything", {})
    assert unknown.value.detail["tools"] == [
        "list_files",
        "read_file",
        "replace_text",
        "search_text",
        "write_file",
    ]
    with pytest.raises(WorkspaceToolError) as wrong_key:
        tools.call("read_file", {"pth": "README.md"})
    assert "Invalid arguments for read_file" in str(wrong_key.value)
    with pytest.raises(WorkspaceToolError) as missing:
        tools.call("read_file", {})
    assert "Invalid arguments for read_file" in str(missing.value)
    with pytest.raises(WorkspaceToolError):
        tools.call("read_file", ["README.md"])  # type: ignore[arg-type]


def test_tool_names_are_the_five_model_facing_operations(tools: WorkspaceTools) -> None:
    assert tools.tools == ("list_files", "read_file", "replace_text", "search_text", "write_file")


def test_refusals_carry_a_code_and_detail(tools: WorkspaceTools) -> None:
    with pytest.raises(WorkspaceToolError) as error:
        tools.read_file(".env")
    assert error.value.as_dict() == {
        "error": "ExcludedPathError",
        "message": ".env looks like a secret and is never read or written",
    }
    with pytest.raises(WorkspaceToolError) as missing:
        tools.call("no_such_tool", {})
    payload = missing.value.as_dict()
    assert payload["error"] == "WorkspaceToolError"
    assert payload["tools"] == ["list_files", "read_file", "replace_text", "search_text", "write_file"]


# Shared policy with the manager -------------------------------------------


@pytest.mark.parametrize(
    ("path", "patterns"),
    [
        ("src/app.py", ["src"]),
        ("src", ["src"]),
        ("SRC/app.py", ["src"]),
        ("src2/app.py", ["src"]),
        ("tests/test_app.py", ["tests/*.py"]),
        ("tests/nested/test_app.py", ["tests/*.py"]),
        ("README.md", ["src", "tests/*.py"]),
        ("docs/guide.md", ["*.md"]),
        ("anything/at/all.txt", ["."]),
    ],
)
def test_allowlist_matching_matches_the_manager(path: str, patterns: list[str]) -> None:
    """The in-loop allowlist rule is the same rule the post-job audit applies."""
    assert matches_allowed_path(path, patterns) == JobManager._is_allowed(path, patterns)


@pytest.mark.parametrize(
    "path",
    [
        ".codewhale/session.json",
        "src/.codewhale/session.json",
        ".pytest_cache/v/cache/lastfailed",
        ".pytest-run/notes.txt",
        "src/.pytest-run/notes.txt",
        "src/app/main.py",
        ".git/config",
    ],
)
def test_tool_state_rule_matches_the_manager(path: str) -> None:
    assert is_tool_state_path(path) == JobManager._is_tool_state_path(path)


@pytest.mark.parametrize(
    "patterns",
    [["./src/", "tests\\*.py"], ["src", "*.md"], ["."], ["src/"], ["docs/**"]],
)
def test_allowed_path_normalization_matches_the_manager(patterns: list[str]) -> None:
    assert workspace_tools._normalize_allowed_paths(patterns) == JobManager._normalize_allowed_paths(list(patterns))


@pytest.mark.parametrize("bad", ["", "../escape", "/absolute", "C:temp", "src/../../x"])
def test_invalid_allowed_paths_are_rejected_like_the_manager(bad: str) -> None:
    with pytest.raises(ValueError):
        JobManager._normalize_allowed_paths([bad])
    with pytest.raises(ValueError):
        workspace_tools._normalize_allowed_paths([bad])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".env", True),
        (".ENV", True),
        (".env.production", True),
        (".env.example", True),
        ("id_rsa", True),
        ("id_rsa.pub", False),
        ("deploy.key", True),
        ("server.pem", True),
        ("secrets.yaml", True),
        ("credentials.json", True),
        ("secrets.py", True),
        ("app.py", False),
        ("notes.md", False),
        ("tokenizer.py", False),
    ],
)
def test_secret_name_rule(name: str, expected: bool) -> None:
    assert is_secret_name(name) is expected


def test_documented_caps_and_limits_are_ordered() -> None:
    assert 0 < workspace_tools.DEFAULT_MAX_RESULTS < workspace_tools.HARD_MAX_RESULTS
    assert 0 < workspace_tools.DEFAULT_MAX_MATCHES < workspace_tools.HARD_MAX_MATCHES
    assert 0 < workspace_tools.DEFAULT_MAX_FILE_BYTES < workspace_tools.HARD_MAX_FILE_BYTES
    assert 0 < workspace_tools.DEFAULT_MAX_WRITE_BYTES < workspace_tools.HARD_MAX_WRITE_BYTES
    assert workspace_tools.DEFAULT_MAX_SCAN_BYTES < workspace_tools.HARD_MAX_SCAN_BYTES


# The model-facing contract --------------------------------------------------
#
# A direct runner hands the model a tool table and gets back a name plus JSON
# argument text. These tests pin the table to the operations that actually exist
# and pin the refusal to a structured result the model can act on.


def test_tool_definitions_describe_the_five_operations(tools: WorkspaceTools) -> None:
    definitions = workspace_tools.tool_definitions()

    assert [entry["name"] for entry in definitions] == list(tools.tools)
    for entry in definitions:
        assert entry["type"] == "function"
        assert entry["description"]
        parameters = entry["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        signature = inspect.signature(workspace_tools._OPERATIONS[entry["name"]])
        accepted = {
            name
            for name, parameter in signature.parameters.items()
            if name != "self"
            and parameter.kind in {parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY}
        }
        assert set(parameters["properties"]) == accepted, "a schema must describe exactly what call accepts"
        required = {
            name
            for name, parameter in signature.parameters.items()
            if name != "self" and parameter.default is inspect.Parameter.empty
        }
        assert set(parameters["required"]) == required
        assert set(parameters["required"]) <= set(parameters["properties"])


def test_tool_definitions_are_fresh_mappings() -> None:
    first = workspace_tools.tool_definitions()
    first[0]["name"] = "rewritten"

    assert workspace_tools.tool_definitions()[0]["name"] == "list_files"


def test_call_json_runs_model_shaped_calls(tools: WorkspaceTools) -> None:
    assert tools.call_json("list_files", '{"path": "src"}')["count"] == 2
    assert tools.call_json("read_file", '{"path": "README.md"}')["text"] == "# Readme\n"
    assert tools.call_json("search_text", '{"query": "alpha"}')["count"] == 1
    assert tools.call_json("list_files")["count"] > 0
    assert tools.call_json("list_files", "")["count"] > 0
    assert tools.call_json("list_files", None)["count"] > 0
    assert tools.call_json("read_file", {"path": "README.md"})["bytes"] == 9
    written = tools.call_json("write_file", json.dumps({"path": "src/app/called.py", "content": "x = 1\n"}))
    assert written["created"] is True
    assert tools.call_json("replace_text", '{"path": "src/app/called.py", "old": "x", "new": "y"}')[
        "replacements"
    ] == 1


def test_call_json_returns_structured_errors(tools: WorkspaceTools) -> None:
    malformed = tools.call_json("read_file", "{not json")
    assert malformed["error"] == "WorkspaceToolError"
    assert "Invalid arguments for read_file" in malformed["message"]

    unknown = tools.call_json("delete_everything", "{}")
    assert unknown["error"] == "WorkspaceToolError"
    assert unknown["tools"] == ["list_files", "read_file", "replace_text", "search_text", "write_file"]
    assert "delete_everything" in unknown["message"]

    refused = tools.call_json("read_file", '{"path": ".env"}')
    assert refused == {
        "error": "ExcludedPathError",
        "message": ".env looks like a secret and is never read or written",
    }

    denied = tools.call_json("write_file", '{"path": "sizes/small.txt", "content": "x"}')
    assert denied["error"] == "PathDeniedError"

    wrong_key = tools.call_json("read_file", '{"pth": "README.md"}')
    assert "Invalid arguments for read_file" in wrong_key["message"]

    not_an_object = tools.call_json("read_file", "[]")
    assert not_an_object["error"] == "WorkspaceToolError"
    assert "arguments must be an object" in not_an_object["message"]

    escaped = tools.call_json("read_file", '{"path": "../../outside.txt"}')
    assert escaped["error"] == "UnsafePathError"


def test_call_json_reports_the_same_refusal_as_call(tools: WorkspaceTools) -> None:
    with pytest.raises(WorkspaceToolError) as error:
        tools.call("read_file", {"path": ".env"})

    assert tools.call_json("read_file", {"path": ".env"}) == error.value.as_dict()

    with pytest.raises(WorkspaceToolError) as unknown:
        tools.call("no_such_tool", {})

    assert tools.call_json("no_such_tool", "{}") == unknown.value.as_dict()


def test_call_json_refuses_a_name_that_is_not_a_string(tools: WorkspaceTools) -> None:
    for name in (None, 42, ["read_file"], {"tool": "read_file"}):
        payload = tools.call_json(name, "{}")

        assert payload["error"] == "WorkspaceToolError"
        assert "Unknown workspace tool" in payload["message"]
        assert payload["tools"] == [
            "list_files", "read_file", "replace_text", "search_text", "write_file",
        ]
