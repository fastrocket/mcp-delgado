from __future__ import annotations

from mcp_delgado.cli import _build_parser


def test_run_parses_repeatable_scope_and_checks() -> None:
    args = _build_parser().parse_args([
        "run",
        "Implement the requested change",
        "--allow", "src",
        "--allow", "tests/test_app.py",
        "--check", "python -m pytest",
    ])

    assert args.command == "run"
    assert args.allowed_paths == ["src", "tests/test_app.py"]
    assert args.required_commands == ["python -m pytest"]


def test_review_defaults_to_current_workspace() -> None:
    args = _build_parser().parse_args(["review", "Review the current changes"])

    assert args.command == "review"
    assert args.workspace == "."
