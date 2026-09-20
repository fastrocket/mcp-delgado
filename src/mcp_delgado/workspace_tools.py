"""Bounded, workspace-confined file tools for a future direct runner.

The manager owns job policy: durable records, the workspace claim, the
allowlist, the before/after audit, and the decision about what a finished job
means. This module owns one narrow thing: the file surface a planning model is
allowed to touch inside a workspace the manager already resolved.

It exists because a direct API runner has no file tools of its own. A raw
completion cannot be handed a checkout; it can only be handed a small, explicit
set of operations whose every boundary is enforced here, in the same process,
without a shell:

* :meth:`WorkspaceTools.list_files` and :meth:`WorkspaceTools.search_text`
  describe the workspace within strict count and byte caps.
* :meth:`WorkspaceTools.read_file` returns UTF-8 text, never binary contents.
* :meth:`WorkspaceTools.replace_text` and :meth:`WorkspaceTools.write_file` are
  the only mutating operations, and both are workspace-contained, allowlisted,
  and atomic.

The invariants every operation holds:

* **Repository-relative input.** An absolute path, a drive-qualified or
  home-relative path, a UNC path, a ``:`` segment (Windows alternate data
  stream) or any ``..`` segment is refused before the filesystem is touched.
* **Resolved containment.** The requested path is resolved through every
  symlink and reparse point, and the result must stay inside the workspace, so
  a link that leaves the checkout cannot be read through or written through.
* **Allowlisted writes.** A write must additionally match the manager's
  allowed-path rules, so a write the tool accepts is a write the post-job audit
  also accepts.
* **Stale-write refusal.** Overwriting an existing file requires its expected
  SHA-256, and ``replace_text`` requires an exact occurrence count, so stale
  model context cannot clobber concurrent changes.
* **Bounded results.** Result counts, per-file bytes, aggregate scan bytes,
  search matches, and write sizes are capped, and a request that would exceed a
  cap fails with a clear error instead of returning partial data as complete.
* **No shell.** Nothing here runs a shell, a subprocess, git, or any other
  program.

Reads are deliberately narrower than the workspace: git metadata, CodeWhale's
own state, root-level pytest state, and secret-looking names are refused, so a
delegated job can never read a credential through the tool surface.

:class:`~mcp_delgado.runners.DirectDeepSeekRunner` constructs this class from a
:class:`~mcp_delgado.runners.TaskRun`, describes the five operations to the model
with :func:`tool_definitions`, and turns each model tool call into
:meth:`WorkspaceTools.call_json`, which reports a refusal as a structured tool
result the model can correct instead of raising into the loop. The runner owns
the step loop, its budgets, and its cancellation; the manager keeps its policy,
the MCP schema, and the CLI schema.
"""

from __future__ import annotations

import fnmatch
import hashlib
import inspect
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

# Byte and count ceilings. A caller may raise a limit up to its hard ceiling per
# call, but never past it, so one tool call cannot return an unbounded result.
DEFAULT_MAX_RESULTS = 200
HARD_MAX_RESULTS = 2_000
DEFAULT_MAX_MATCHES = 100
HARD_MAX_MATCHES = 1_000
DEFAULT_MAX_FILE_BYTES = 200_000
HARD_MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_SCAN_BYTES = 4_000_000
HARD_MAX_SCAN_BYTES = 16_000_000
DEFAULT_MAX_WRITE_BYTES = 200_000
HARD_MAX_WRITE_BYTES = 1_000_000

MAX_PATH_CHARS = 1_000
MAX_QUERY_CHARS = 200
MAX_MATCH_TEXT_CHARS = 400
BINARY_SNIFF_BYTES = 8_192
ROOT_PATH = "."

# Tool-owned state. These names mirror ``JobManager``'s audit exclusions so the
# in-loop tools and the post-job audit cannot drift apart: what the audit calls
# tool state is also what the model may not read or write.
TOOL_STATE_DIR_NAMES = frozenset({".codewhale"})
PYTEST_STATE_DIR_NAME = ".pytest_cache"
PYTEST_BASETEMP_PREFIX = ".pytest-"

# Repository metadata is never model-facing, at any depth.
REPOSITORY_METADATA_DIR_NAMES = frozenset({".git"})

# Generated trees are hidden from listings and walks because they are noise and
# would consume every cap. An explicit read of a path inside one is still
# allowed: ``pyvenv.cfg`` and a dependency's type stubs are legitimate context.
GENERATED_DIR_NAMES = frozenset({
    ".mypy_cache",
    ".nox",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
})

# Deliberately fail-closed. A file whose whole name is a known secret container
# or whose name matches a key/credential pattern is refused even when it is
# documentation, because the cost of a false refusal is one clear error and the
# cost of a false read is a leaked credential.
SECRET_FILE_NAMES = frozenset({
    ".env",
    ".git-credentials",
    ".htpasswd",
    ".netrc",
    ".npmrc",
    ".pgpass",
    ".pypirc",
    "credential",
    "credentials",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secret",
    "secrets",
})
SECRET_FILE_PATTERNS = (
    ".env.*",
    "*.env",
    "*.jks",
    "*.kdbx",
    "*.key",
    "*.keystore",
    "*.p12",
    "*.pem",
    "*.pfx",
    "*.ppk",
    "*passwd*.txt",
    "*password*.txt",
    "credential.*",
    "credentials.*",
    "secret.*",
    "secrets.*",
)

# Windows device names are not files: writing to ``NUL`` would silently discard
# the content and still look like success.
WINDOWS_RESERVED_NAMES = frozenset({
    "aux", "clock$", "com1", "com2", "com3", "com4", "com5", "com6", "com7",
    "com8", "com9", "con", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6",
    "lpt7", "lpt8", "lpt9", "nul", "prn",
})


class WorkspaceToolError(RuntimeError):
    """A model-facing workspace tool refused the request.

    ``code`` is a stable, model-visible identifier, and :meth:`as_dict` returns
    the shape a tool result can carry directly.
    """

    code = "WorkspaceToolError"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = dict(detail)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": str(self)}
        payload.update(self.detail)
        return payload


class UnsafePathError(WorkspaceToolError):
    """The path is not a safe repository-relative path inside the workspace."""

    code = "UnsafePathError"


class PathDeniedError(WorkspaceToolError):
    """The path exists but this operation cannot use it, or the allowlist denies it."""

    code = "PathDeniedError"


class ExcludedPathError(WorkspaceToolError):
    """The path is tool state, repository metadata, or a secret-looking name."""

    code = "ExcludedPathError"


class BinaryFileError(WorkspaceToolError):
    """The contents are not UTF-8 text, so they are never returned."""

    code = "BinaryFileError"


class MissingFileError(WorkspaceToolError):
    """The requested file or directory does not exist."""

    code = "MissingFileError"


class SizeLimitError(WorkspaceToolError):
    """A byte cap would be exceeded."""

    code = "SizeLimitError"


class LimitExceededError(WorkspaceToolError):
    """A result or match count cap would be exceeded."""

    code = "LimitExceededError"


class MatchCountError(WorkspaceToolError):
    """``replace_text`` found a different number of occurrences than expected."""

    code = "MatchCountError"


class StaleFileError(WorkspaceToolError):
    """A required SHA-256 is missing or does not match the current file."""

    code = "StaleFileError"


def _checked_limit(value: object, default: int, ceiling: int, field: str) -> int:
    """Return a validated limit, raising ``ValueError`` for a bad configuration."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < 1:
        raise ValueError(f"{field} must be at least 1")
    if value > ceiling:
        raise ValueError(f"{field} must not exceed {ceiling}")
    return value


def _decode_text(data: bytes) -> Optional[str]:
    """Decode UTF-8 text, or report that the bytes are not text.

    A NUL byte in the sniff window is the cheap, portable binary signal that
    catches UTF-16 and compiled artifacts before decoding, and a strict UTF-8
    decode rejects the rest.
    """
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def matches_allowed_path(path: str, patterns: Sequence[str]) -> bool:
    """Report whether a repository-relative path matches the job allowlist.

    This mirrors ``JobManager._is_allowed`` exactly: an fnmatch glob, or an
    exact match, or a match below a named directory. Globs keep fnmatch
    semantics, so ``src/*.py`` also matches ``src/nested/app.py``; a directory
    pattern such as ``src`` covers everything beneath it.
    """
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return True
        if normalized == pattern or normalized.startswith(pattern + "/"):
            return True
    return False


def is_tool_state_path(path: str) -> bool:
    """Report whether tool-owned transient state owns this repository-relative path.

    This mirrors ``JobManager._is_tool_state_path``: ``.codewhale`` matches at
    any depth, and pytest state matches at the repository root only, as the
    exact ``.pytest_cache`` name or a ``.pytest-*`` basetemp tree.
    """
    segments = path.replace("\\", "/").split("/")
    if any(segment in TOOL_STATE_DIR_NAMES for segment in segments):
        return True
    root = segments[0]
    return root == PYTEST_STATE_DIR_NAME or root.startswith(PYTEST_BASETEMP_PREFIX)


def is_secret_name(name: str) -> bool:
    """Report whether a single path segment names an obvious secret container."""
    lowered = name.casefold()
    if lowered in SECRET_FILE_NAMES:
        return True
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in SECRET_FILE_PATTERNS)


def _normalize_allowed_paths(paths: Sequence[str]) -> list[str]:
    """Normalize allowlist patterns, mirroring ``JobManager._normalize_allowed_paths``."""
    normalized: list[str] = []
    for raw in paths:
        if not isinstance(raw, str):
            raise ValueError(f"Invalid allowed path: {raw!r}")
        value = raw.replace("\\", "/").strip()
        while value.startswith("./"):
            value = value[2:]
        parts = value.split("/")
        if not value or value.startswith("/") or ".." in parts or ":" in parts[0]:
            raise ValueError(f"Invalid allowed path: {raw}")
        normalized.append(value.rstrip("/"))
    return normalized


class WorkspaceTools:
    """Bounded model-facing file tools for one resolved workspace and one allowlist.

    ``workspace`` is the checkout the manager already resolved, and
    ``allowed_paths`` are the manager-normalized patterns from the job record.
    Reads may inspect the workspace anywhere they are not excluded; writes must
    also match the allowlist.

    Construction validates configuration and raises ``ValueError``; every call
    after that raises a :class:`WorkspaceToolError` subclass for a refusal the
    model should see and act on.
    """

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        allowed_paths: Sequence[str],
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_matches: int = DEFAULT_MAX_MATCHES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
        max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
    ) -> None:
        raw = Path(workspace).expanduser()
        try:
            resolved = raw.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Workspace does not exist: {raw}") from exc
        if not resolved.is_dir():
            raise ValueError(f"Workspace is not a directory: {resolved}")

        self.workspace = resolved
        self.allowed_paths = tuple(_normalize_allowed_paths(list(allowed_paths)))
        self.max_results = _checked_limit(max_results, DEFAULT_MAX_RESULTS, HARD_MAX_RESULTS, "max_results")
        self.max_matches = _checked_limit(max_matches, DEFAULT_MAX_MATCHES, HARD_MAX_MATCHES, "max_matches")
        self.max_file_bytes = _checked_limit(max_file_bytes, DEFAULT_MAX_FILE_BYTES, HARD_MAX_FILE_BYTES, "max_file_bytes")
        self.max_scan_bytes = _checked_limit(max_scan_bytes, DEFAULT_MAX_SCAN_BYTES, HARD_MAX_SCAN_BYTES, "max_scan_bytes")
        self.max_write_bytes = _checked_limit(max_write_bytes, DEFAULT_MAX_WRITE_BYTES, HARD_MAX_WRITE_BYTES, "max_write_bytes")

    def __repr__(self) -> str:
        return f"WorkspaceTools(workspace={str(self.workspace)!r}, allowed_paths={self.allowed_paths!r})"

    # Model-facing operations ----------------------------------------------

    def list_files(self, path: str = ROOT_PATH, *, max_results: Optional[int] = None) -> dict[str, Any]:
        """List one directory inside the workspace.

        Entries are repository-relative, sorted by name case-insensitively.
        Repository metadata, tool state, generated trees, and secret-looking
        names are never listed, and a directory holding more entries than the
        cap fails with :class:`LimitExceededError` rather than reporting a
        partial list.
        """
        limit = self._count_cap(max_results, "max_results", self.max_results, HARD_MAX_RESULTS)
        relative = self._relative(path)
        target = self._resolve_target(relative)
        self._refuse_unreadable(self._relative_of(target))
        if not target.exists():
            raise MissingFileError(f"{self._display(relative)} does not exist")
        if not target.is_dir():
            raise PathDeniedError(f"{self._display(relative)} is not a directory")

        entries: list[dict[str, Any]] = []
        for child in self._sorted_children(target):
            child_relative = self._relative_of(child)
            if self._hidden(child_relative) or self._escapes(child):
                continue
            if len(entries) >= limit:
                raise LimitExceededError(
                    f"{self._display(relative)} holds more than {limit} visible entries; "
                    "list a narrower directory or raise max_results"
                )
            entries.append(self._entry(child, child_relative))
        return {"path": self._display(relative), "entries": entries, "count": len(entries)}

    def search_text(
        self,
        query: str,
        path: str = ROOT_PATH,
        *,
        max_matches: Optional[int] = None,
        file_pattern: Optional[str] = None,
        case_sensitive: bool = True,
    ) -> dict[str, Any]:
        """Search UTF-8 text under a directory for a literal, single-line query.

        ``file_pattern`` is an fnmatch glob on the repository-relative path, and
        ``case_sensitive`` defaults to an exact match. Directory links are not
        followed, binary files are skipped and counted, and matches beyond the
        cap fail with :class:`LimitExceededError` instead of truncating the
        answer silently.
        """
        needle, ignore_case = self._query(query, case_sensitive)
        limit = self._count_cap(max_matches, "max_matches", self.max_matches, HARD_MAX_MATCHES)
        if file_pattern is not None:
            if not isinstance(file_pattern, str) or not file_pattern.strip() or "\x00" in file_pattern:
                raise WorkspaceToolError("file_pattern must be a non-empty glob string")
        # fnmatchcase keeps a pattern's behavior identical on every platform;
        # both sides are folded only when the caller asked for a loose match.
        pattern = file_pattern.casefold() if file_pattern is not None and not case_sensitive else file_pattern

        relative = self._relative(path)
        root = self._resolve_target(relative)
        self._refuse_unreadable(self._relative_of(root))
        if not root.exists():
            raise MissingFileError(f"{self._display(relative)} does not exist")

        matches: list[dict[str, Any]] = []
        files_searched = 0
        files_skipped_binary = 0
        files_skipped_large = 0
        bytes_scanned = 0
        for candidate in self._iter_files(root):
            candidate_relative = self._relative_of(candidate)
            if pattern is not None:
                subject = candidate_relative.casefold() if not case_sensitive else candidate_relative
                if not fnmatch.fnmatchcase(subject, pattern):
                    continue
            try:
                size = candidate.stat().st_size
            except OSError as exc:
                raise WorkspaceToolError(f"Could not inspect {candidate_relative}: {exc}") from exc
            if size > self.max_file_bytes:
                files_skipped_large += 1
                continue
            if bytes_scanned + size > self.max_scan_bytes:
                raise SizeLimitError(
                    f"search would scan more than {self.max_scan_bytes} bytes under "
                    f"{self._display(relative)}; narrow the path or the file_pattern"
                )
            try:
                data = candidate.read_bytes()
            except OSError as exc:
                raise WorkspaceToolError(f"Could not read {candidate_relative}: {exc}") from exc
            bytes_scanned += len(data)
            text = _decode_text(data)
            if text is None:
                files_skipped_binary += 1
                continue
            files_searched += 1
            for number, line in enumerate(text.splitlines(), start=1):
                haystack = line.casefold() if ignore_case else line
                if needle not in haystack:
                    continue
                if len(matches) >= limit:
                    raise LimitExceededError(
                        f"search found more than {limit} matches under {self._display(relative)}; "
                        "narrow the query, the path, or the file_pattern, or raise max_matches"
                    )
                matches.append(self._match(candidate_relative, number, line))
        return {
            "query": query,
            "path": self._display(relative),
            "matches": matches,
            "count": len(matches),
            "files_searched": files_searched,
            "bytes_scanned": bytes_scanned,
            "files_skipped_binary": files_skipped_binary,
            "files_skipped_large": files_skipped_large,
        }

    def read_file(self, path: str, *, max_bytes: Optional[int] = None) -> dict[str, Any]:
        """Return one UTF-8 text file with the SHA-256 a later write must carry.

        A file above the byte cap fails with :class:`SizeLimitError` and binary
        content fails with :class:`BinaryFileError`, so a caller never receives
        a partial or undecodable file that looks complete.
        """
        limit = self._size_cap(max_bytes, "max_bytes", self.max_file_bytes, HARD_MAX_FILE_BYTES)
        relative = self._relative(path)
        target = self._resolve_target(relative)
        resolved_relative = self._relative_of(target)
        self._refuse_unreadable(resolved_relative)
        if not target.exists():
            raise MissingFileError(f"{self._display(relative)} does not exist")
        if not target.is_file():
            raise PathDeniedError(f"{self._display(relative)} is not a regular file")

        self._refuse_oversized(target, resolved_relative, limit, "read")
        data = self._read_bytes(target, resolved_relative)
        if len(data) > limit:
            raise SizeLimitError(
                f"{resolved_relative} is {len(data)} bytes, above the {limit}-byte read cap; "
                "read a narrower file or raise max_bytes"
            )
        text = _decode_text(data)
        if text is None:
            raise BinaryFileError(f"{resolved_relative} is not UTF-8 text; binary contents are never returned")
        return {
            "path": resolved_relative,
            "text": text,
            "bytes": len(data),
            "lines": len(text.splitlines()),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def replace_text(
        self,
        path: str,
        old: str,
        new: str,
        *,
        expected_occurrences: int = 1,
        expected_sha256: Optional[str] = None,
    ) -> dict[str, Any]:
        """Replace exact text in one existing file and write it atomically.

        The file must contain exactly ``expected_occurrences`` copies of
        ``old``; zero, or more than expected, fails with
        :class:`MatchCountError`, so an ambiguous replacement is never guessed
        at. Pass ``expected_sha256`` (as returned by :meth:`read_file`) to also
        refuse a file that changed since it was read.
        """
        if not isinstance(old, str) or not old:
            raise WorkspaceToolError("old must be a non-empty string")
        if not isinstance(new, str):
            raise WorkspaceToolError("new must be a string")
        if isinstance(expected_occurrences, bool) or not isinstance(expected_occurrences, int) or expected_occurrences < 1:
            raise WorkspaceToolError("expected_occurrences must be an integer of at least 1")

        relative = self._relative(path)
        target = self._resolve_target(relative)
        resolved_relative = self._relative_of(target)
        self._refuse_unreadable(resolved_relative)
        self._refuse_unwritable(resolved_relative)
        if not target.exists():
            raise MissingFileError(f"{self._display(relative)} does not exist")
        if not target.is_file():
            raise PathDeniedError(f"{self._display(relative)} is not a regular file")

        data = self._read_bounded(target, resolved_relative)
        if expected_sha256 is not None:
            self._require_sha256(expected_sha256, data, resolved_relative)
        text = _decode_text(data)
        if text is None:
            raise BinaryFileError(f"{resolved_relative} is not UTF-8 text; binary contents are never edited")

        found = text.count(old)
        if found != expected_occurrences:
            raise MatchCountError(
                f"{resolved_relative} contains {found} exact occurrence(s) of the given text, "
                f"but expected_occurrences is {expected_occurrences}",
                occurrences=found,
                expected=expected_occurrences,
            )
        updated = text.replace(old, new)
        payload = self._encode_write(updated, resolved_relative)
        self._atomic_write(target, payload)
        return {
            "path": resolved_relative,
            "replacements": found,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def write_file(self, path: str, content: str, *, expected_sha256: Optional[str] = None) -> dict[str, Any]:
        """Create a file, or overwrite one whose SHA-256 the caller already knows.

        A new file is created — missing parent directories included. An existing
        file is only replaced when ``expected_sha256`` matches its current
        contents, so a model working from stale context cannot silently clobber
        a change it never saw. The replacement is atomic: the file is either the
        old bytes or the new bytes, never a partial write.
        """
        if not isinstance(content, str):
            raise WorkspaceToolError("content must be a string")
        relative = self._relative(path)
        target = self._resolve_target(relative)
        resolved_relative = self._relative_of(target)
        self._refuse_unreadable(resolved_relative)
        self._refuse_unwritable(resolved_relative)

        exists = target.exists()
        if exists and not target.is_file():
            raise PathDeniedError(f"{self._display(relative)} is not a regular file")
        if exists:
            current = self._read_bounded(target, resolved_relative)
            current_digest = hashlib.sha256(current).hexdigest()
            if expected_sha256 is None:
                raise StaleFileError(
                    f"{resolved_relative} already exists; pass expected_sha256 "
                    f"{current_digest} to overwrite it",
                    sha256=current_digest,
                )
            self._require_sha256(expected_sha256, current, resolved_relative)
        elif expected_sha256 is not None:
            raise StaleFileError(
                f"{resolved_relative} does not exist, so expected_sha256 cannot be satisfied; "
                "omit it to create the file"
            )

        payload = self._encode_write(content, resolved_relative)
        self._atomic_write(target, payload)
        return {
            "path": resolved_relative,
            "created": not exists,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def call(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Run one tool by name with a model-supplied argument mapping.

        This is the shape a direct runner receives from a model: a tool name and
        its arguments. An unknown name or an argument the operation does not
        accept is refused with a clear error instead of raising ``TypeError``.
        """
        handler = _OPERATIONS.get(name)
        if handler is None:
            raise WorkspaceToolError(
                f"Unknown workspace tool: {name!r}",
                tools=sorted(_OPERATIONS),
            )
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise WorkspaceToolError("arguments must be an object")
        try:
            inspect.signature(handler).bind(self, **arguments)
        except TypeError as exc:
            raise WorkspaceToolError(f"Invalid arguments for {name}: {exc}") from None
        return handler(self, **arguments)

    def call_json(self, name: object, arguments: object = "") -> dict[str, Any]:
        """Run one tool from a model-shaped call: a name and JSON argument text.

        A direct runner receives exactly this shape from the provider, so this is
        the loop's single entry point. A refusal is returned as the same
        structured mapping :meth:`as_dict` produces, because the model must see
        what was refused and why instead of losing the turn to an exception. The
        error text never quotes the workspace beyond the relative path the model
        already supplied, and no argument value is echoed back.
        """
        if not isinstance(name, str) or name not in _OPERATIONS:
            return WorkspaceToolError(
                f"Unknown workspace tool: {name!r}", tools=sorted(_OPERATIONS)
            ).as_dict()
        if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
            arguments = {}
        elif isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return WorkspaceToolError(
                    f"Invalid arguments for {name}: arguments must be a JSON object"
                ).as_dict()
        try:
            return self.call(name, arguments)  # type: ignore[arg-type]
        except WorkspaceToolError as exc:
            return exc.as_dict()

    @property
    def tools(self) -> tuple[str, ...]:
        """The model-facing operation names, in a stable order."""
        return tuple(sorted(_OPERATIONS))

    # Path handling ---------------------------------------------------------

    def _relative(self, raw: object, field: str = "path") -> str:
        """Validate model input as a repository-relative path and normalize it.

        Any ``..`` segment is refused even when it would resolve back inside the
        workspace, because the caller's intent is unknowable and a refusal is
        cheap. Absolute, drive-qualified, UNC, home-relative, NUL-bearing, and
        over-long paths are refused before the filesystem is touched.
        """
        if not isinstance(raw, str):
            raise UnsafePathError(f"{field} must be a string")
        value = raw.strip()
        if len(value) > MAX_PATH_CHARS:
            raise UnsafePathError(f"{field} is longer than {MAX_PATH_CHARS} characters")
        if "\x00" in value:
            raise UnsafePathError(f"{field} contains a NUL byte")
        value = value.replace("\\", "/").strip()
        if not value:
            return ROOT_PATH
        if value.startswith("~"):
            raise UnsafePathError(f"{field} must be repository-relative, not home-relative: {raw!r}")
        if value.startswith("/"):
            raise UnsafePathError(f"{field} must be repository-relative, not absolute: {raw!r}")
        if re.match(r"^[A-Za-z]:", value):
            raise UnsafePathError(f"{field} must be repository-relative, not drive-qualified: {raw!r}")
        parts = [part for part in value.split("/") if part not in ("", ".")]
        if not parts:
            return ROOT_PATH
        if ".." in parts:
            raise UnsafePathError(f"{field} must not contain a parent traversal: {raw!r}")
        if ":" in value:
            raise UnsafePathError(f"{field} must not contain ':' (drive or stream syntax): {raw!r}")
        if os.name == "nt":
            for part in parts:
                if part.casefold().split(".", 1)[0] in WINDOWS_RESERVED_NAMES:
                    raise UnsafePathError(f"{field} names a reserved Windows device: {part!r}")
        return "/".join(parts)

    def _join(self, relative: str) -> Path:
        if relative == ROOT_PATH:
            return self.workspace
        return self.workspace.joinpath(*relative.split("/"))

    def _relative_of(self, target: Path) -> str:
        """Report a path inside the workspace as a repository-relative path."""
        if target == self.workspace:
            return ROOT_PATH
        try:
            return target.relative_to(self.workspace).as_posix()
        except ValueError:
            raise UnsafePathError(f"Path is outside the workspace: {target}") from None

    def _contains(self, target: Path) -> bool:
        return target == self.workspace or self.workspace in target.parents

    def _resolve_target(self, relative: str) -> Path:
        """Resolve a validated relative path, refusing anything outside the workspace.

        ``resolve`` follows symlinks, junctions, and every other reparse point,
        including a link whose target does not exist yet, so a link that leaves
        the checkout is caught here. The deepest lexically existing ancestor is
        then resolved again as a cross-check: it covers a platform whose
        ``realpath`` leaves a link unresolved while the link still exists.
        """
        candidate = self._join(relative)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise UnsafePathError(f"Could not resolve {self._display(relative)}: {exc}") from exc
        if not self._contains(resolved):
            raise UnsafePathError(
                f"{self._display(relative)} resolves outside the workspace: {resolved}"
            )

        probe = candidate
        while not os.path.lexists(probe) and probe != probe.parent:
            probe = probe.parent
        if os.path.lexists(probe):
            try:
                ancestor = probe.resolve(strict=True)
            except FileNotFoundError:
                ancestor = probe.resolve(strict=False)
            except OSError as exc:
                raise UnsafePathError(f"Could not resolve {self._display(relative)}: {exc}") from exc
            if not self._contains(ancestor):
                raise UnsafePathError(
                    f"{self._display(relative)} resolves outside the workspace: {ancestor}"
                )
        return resolved

    def _display(self, relative: str) -> str:
        return relative

    # Exclusions ------------------------------------------------------------

    def _refuse_unreadable(self, relative: str) -> None:
        """Refuse metadata, tool state, and secret-looking paths on any operation."""
        if is_tool_state_path(relative):
            raise ExcludedPathError(f"{relative} is tool state and is not model-facing")
        for segment in relative.replace("\\", "/").split("/"):
            if segment in REPOSITORY_METADATA_DIR_NAMES:
                raise ExcludedPathError(f"{relative} is repository metadata and is not model-facing")
            if is_secret_name(segment):
                raise ExcludedPathError(f"{relative} looks like a secret and is never read or written")

    def _refuse_unwritable(self, relative: str) -> None:
        """Refuse a write the allowlist does not cover."""
        if not matches_allowed_path(relative, self.allowed_paths):
            patterns = ", ".join(self.allowed_paths) or "(none)"
            raise PathDeniedError(
                f"{relative} is outside the allowed paths for this job: {patterns}"
            )

    def _hidden(self, relative: str) -> bool:
        """Report whether a listing or a walk skips this child entirely."""
        if is_tool_state_path(relative):
            return True
        name = relative.replace("\\", "/").split("/")[-1]
        if name in REPOSITORY_METADATA_DIR_NAMES or name in GENERATED_DIR_NAMES:
            return True
        return is_secret_name(name)

    def _escapes(self, target: Path) -> bool:
        """Report whether a link in the workspace points out of it.

        A link that leaves the checkout is not part of the workspace, so it is
        not listed and not walked; reading or writing through it is refused by
        :meth:`_resolve_target`.
        """
        try:
            return not self._contains(target.resolve(strict=False))
        except OSError:
            return True

    # Filesystem helpers ----------------------------------------------------

    def _sorted_children(self, directory: Path) -> list[Path]:
        try:
            return sorted(directory.iterdir(), key=lambda child: child.name.casefold())
        except OSError as exc:
            raise WorkspaceToolError(f"Could not list {self._relative_of(directory)}: {exc}") from exc

    def _entry(self, child: Path, relative: str) -> dict[str, Any]:
        if child.is_dir():
            return {"path": relative, "type": "directory"}
        if child.is_file():
            try:
                size = child.stat().st_size
            except OSError:
                size = None
            return {"path": relative, "type": "file", "size": size}
        return {"path": relative, "type": "other"}

    def _iter_files(self, root: Path) -> Iterator[Path]:
        """Yield the readable files under a directory, never descending into links."""
        if root.is_file():
            yield root
            return
        for directory, subdirectories, filenames in os.walk(root, followlinks=False):
            current = Path(directory)
            subdirectories[:] = sorted(
                (name for name in subdirectories if not self._hidden(self._relative_of(current / name))),
                key=str.casefold,
            )
            for name in sorted(filenames, key=str.casefold):
                candidate = current / name
                relative = self._relative_of(candidate)
                if self._hidden(relative):
                    continue
                if self._escapes(candidate):
                    continue
                yield candidate

    def _read_bytes(self, target: Path, relative: str) -> bytes:
        try:
            return target.read_bytes()
        except OSError as exc:
            raise WorkspaceToolError(f"Could not read {relative}: {exc}") from exc

    def _read_bounded(self, target: Path, relative: str) -> bytes:
        """Read an existing file for an edit, refusing one that is too large to hold.

        An edit needs the whole file, so it is bounded by the larger of the read
        and write caps: anything beyond that could not be rewritten within the
        write cap in the first place.
        """
        limit = max(self.max_file_bytes, self.max_write_bytes)
        self._refuse_oversized(target, relative, limit, "edit")
        return self._read_bytes(target, relative)

    def _refuse_oversized(self, target: Path, relative: str, limit: int, purpose: str) -> None:
        """Refuse a file that is already too large, before any of its bytes are read."""
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise WorkspaceToolError(f"Could not inspect {relative}: {exc}") from exc
        if size > limit:
            raise SizeLimitError(f"{relative} is {size} bytes, above the {limit}-byte {purpose} cap")

    def _query(self, query: object, case_sensitive: object) -> tuple[str, bool]:
        if not isinstance(query, str) or not query:
            raise WorkspaceToolError("query must be a non-empty string")
        if len(query) > MAX_QUERY_CHARS:
            raise WorkspaceToolError(f"query is longer than {MAX_QUERY_CHARS} characters")
        if "\x00" in query or "\n" in query or "\r" in query:
            raise WorkspaceToolError("query must be a single line of text")
        if not isinstance(case_sensitive, bool):
            raise WorkspaceToolError("case_sensitive must be a boolean")
        return (query if case_sensitive else query.casefold()), not case_sensitive

    def _match(self, relative: str, number: int, line: str) -> dict[str, Any]:
        truncated = len(line) > MAX_MATCH_TEXT_CHARS
        return {
            "path": relative,
            "line": number,
            "text": line[:MAX_MATCH_TEXT_CHARS],
            "text_truncated": truncated,
        }

    def _count_cap(self, value: object, field: str, default: int, ceiling: int) -> int:
        try:
            return _checked_limit(value, default, ceiling, field)
        except ValueError as exc:
            raise LimitExceededError(str(exc)) from None

    def _size_cap(self, value: object, field: str, default: int, ceiling: int) -> int:
        try:
            return _checked_limit(value, default, ceiling, field)
        except ValueError as exc:
            raise SizeLimitError(str(exc)) from None

    def _require_sha256(self, expected: object, data: bytes, relative: str) -> None:
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected.strip()):
            raise StaleFileError("expected_sha256 must be a 64-character hexadecimal SHA-256")
        current = hashlib.sha256(data).hexdigest()
        if expected.strip().casefold() != current:
            raise StaleFileError(
                f"{relative} has changed since it was read; its current SHA-256 is {current}",
                sha256=current,
            )

    def _encode_write(self, text: str, relative: str) -> bytes:
        if "\x00" in text:
            raise BinaryFileError(f"{relative} would be written with NUL bytes; this tool writes UTF-8 text only")
        try:
            payload = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise BinaryFileError(f"{relative} would be written with text that is not valid UTF-8: {exc}") from None
        if len(payload) > self.max_write_bytes:
            raise SizeLimitError(
                f"{relative} would be {len(payload)} bytes, above the {self.max_write_bytes}-byte write cap"
            )
        return payload

    def _atomic_write(self, target: Path, payload: bytes) -> None:
        """Replace a file in one step, so a reader sees the old or the new bytes.

        The payload lands in a temporary file beside the target, is flushed to
        the filesystem, and only then replaces the target, which keeps a partial
        write from ever being observable. An existing file keeps its mode.
        """
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceToolError(f"Could not create {target.parent}: {exc}") from exc
        existing_mode: Optional[int] = None
        if target.exists():
            try:
                existing_mode = stat.S_IMODE(target.stat().st_mode)
            except OSError:
                existing_mode = None
        try:
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
            )
        except OSError as exc:
            raise WorkspaceToolError(f"Could not prepare a write beside {target.name}: {exc}") from exc
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if existing_mode is not None:
                try:
                    os.chmod(temporary, existing_mode)
                except OSError:
                    pass
            os.replace(temporary, target)
        except OSError as exc:
            raise WorkspaceToolError(f"Could not write {target.name}: {exc}") from exc
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass


_OPERATIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_files": WorkspaceTools.list_files,
    "read_file": WorkspaceTools.read_file,
    "replace_text": WorkspaceTools.replace_text,
    "search_text": WorkspaceTools.search_text,
    "write_file": WorkspaceTools.write_file,
}

# The model-facing description of those same five operations, in Responses-API
# function-tool shape. ``required`` mirrors each operation's positional
# parameters and ``additionalProperties`` is false, because :meth:`call` refuses
# any name outside this table and any argument the operation does not accept.
TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "name": "list_files",
        "description": (
            "List one workspace directory. Paths are repository-relative; '.' is the workspace root."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative directory, default '.'."},
                "max_results": {"type": "integer", "description": "Entry cap for this call, default 200."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "description": (
            "Read one UTF-8 text file inside the workspace. Returns its text, byte size, line count, "
            "and SHA-256."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative file path."},
                "max_bytes": {"type": "integer", "description": "Byte cap for this call, default 200000."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "replace_text",
        "description": (
            "Replace an exact number of occurrences of text in one existing file. Pass expected_sha256 "
            "from read_file to also refuse a file that changed since it was read."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative file path."},
                "old": {"type": "string", "description": "Exact text to replace; must be non-empty."},
                "new": {"type": "string", "description": "Replacement text."},
                "expected_occurrences": {
                    "type": "integer",
                    "description": "Exact number of occurrences to replace, default 1.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "SHA-256 the file must still have, as returned by read_file.",
                },
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_text",
        "description": (
            "Search UTF-8 text files under one directory for a literal, single-line query."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Literal text to find on a single line."},
                "path": {"type": "string", "description": "Repository-relative directory, default '.'."},
                "max_matches": {"type": "integer", "description": "Match cap for this call, default 100."},
                "file_pattern": {
                    "type": "string",
                    "description": "Optional fnmatch glob on the repository-relative path.",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Exact match by default; set false to ignore case.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": (
            "Write UTF-8 text to one file inside the workspace. Creating a new file is allowed; "
            "overwriting must carry the SHA-256 read_file returned."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative file path."},
                "content": {"type": "string", "description": "Complete new file content."},
                "expected_sha256": {
                    "type": "string",
                    "description": "SHA-256 the existing file must still have.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
)


def tool_definitions() -> tuple[dict[str, Any], ...]:
    """Return the five operations as model-facing function tools.

    The order is stable and the names are exactly :attr:`WorkspaceTools.tools`.
    Fresh mappings are returned per call, so a caller cannot mutate the shared
    table or leak one request's edits into the next one.
    """
    return json.loads(json.dumps(TOOL_DEFINITIONS))
