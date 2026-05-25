"""End-to-end CLI tests via Typer's CliRunner.

GitHub HTTP is fully mocked via ``pytest-httpx``. These tests verify
that:

- The CLI surface compiles and routes flags correctly.
- ``tend --help`` and per-command help exposes the documented options.
- The dry-run paths never call write-side GitHub APIs.
- ``tend route`` reads ``EVENT_NAME`` / ``EVENT_PATH`` env vars.

Tests that need a real GitHubClient are routed through pytest-httpx; no
test ever talks to api.github.com.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import typer.rich_utils
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from tend.cli import app

runner = CliRunner()


@pytest.fixture
def wide_terminal(monkeypatch):
    """Force Typer's help renderer to a fixed wide width.

    Typer builds its own Rich Console in `typer.rich_utils` and passes
    `width=MAX_WIDTH`. When MAX_WIDTH is None (the default), Rich falls
    back to terminal detection, which on GitHub Actions reports ~110
    cols and truncates long option names like `--against-codeowners`
    to `--against-codeown…`. Pin MAX_WIDTH so the layout is stable.
    """
    monkeypatch.setattr(typer.rich_utils, "MAX_WIDTH", 200)


# Tend issues GET /repos/.../commits with a since= query param whose value is
# computed from datetime.now(). Match the URL without binding to that value.
_COMMITS_URL = re.compile(r"https://api\.github\.com/repos/[^/]+/[^/]+/commits\?.*")


def test_top_level_help_lists_all_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("analyze", "diff", "explain", "route", "create-pr"):
        assert cmd in result.stdout


def test_top_level_help_omits_output_format_flag():
    """v1 invariant: no --output-format anywhere."""
    result = runner.invoke(app, ["--help"])
    assert "--output-format" not in result.stdout
    analyze_help = runner.invoke(app, ["analyze", "--help"])
    assert "--output-format" not in analyze_help.stdout


def test_analyze_help_describes_yaml_output():
    result = runner.invoke(app, ["analyze", "--help"])
    assert result.exit_code == 0
    assert "owners.yml" in result.stdout


def test_diff_help_shows_against_codeowners(wide_terminal):  # noqa: ARG001
    result = runner.invoke(app, ["diff", "--help"])
    assert result.exit_code == 0
    assert "--against-codeowners" in result.stdout


def test_version_flag_prints_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "tend" in result.stdout


def test_analyze_dry_run_outputs_yaml(httpx_mock: HTTPXMock, monkeypatch):
    """Dry-run with mocked GitHub returns yaml to stdout, writes nothing."""
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fake")

    # Minimal mock: one commit with one file by alice.
    httpx_mock.add_response(
        url=_COMMITS_URL,
        match_headers={"Authorization": "Bearer fake"},
        json=[{"sha": "abc"}],
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/octocat/hello/commits/abc",
        json={
            "commit": {
                "author": {
                    "name": "Alice",
                    "email": "alice@example.com",
                    "date": "2026-05-01T00:00:00Z",
                },
                "message": "Initial",
            },
            "author": {"login": "alice", "type": "User"},
            "files": [{"filename": "src/auth.py", "additions": 100, "deletions": 0}],
        },
    )

    # Single-commit test fixture; override thresholds so alice survives.
    overrides = {"min_commits": 1, "volume_floor": 0, "min_confidence": 0.10}
    result = runner.invoke(
        app,
        [
            "analyze",
            "--repo",
            "octocat/hello",
            "--dry-run",
            "--sensitivity",
            "permissive",
            "--lookback-days",
            "365",
            "--no-associations",  # skip GraphQL — test mocks REST only
            "--no-team-resolution",  # skip team REST — test mocks commits only
            "--advanced",
            json.dumps(overrides),
        ],
    )
    assert result.exit_code == 0
    assert "version: 1" in result.stdout
    assert "alice" in result.stdout


def test_analyze_rejects_bad_sensitivity(monkeypatch):
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fake")
    result = runner.invoke(
        app, ["analyze", "--repo", "o/r", "--sensitivity", "extreme", "--dry-run"]
    )
    # Exit 2 = Click BadParameter. The message lands on stderr; Typer's
    # CliRunner may route it differently across versions, so just assert
    # that the command rejected the bad input.
    assert result.exit_code != 0


def test_analyze_rejects_malformed_repo(monkeypatch):
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fake")
    result = runner.invoke(app, ["analyze", "--repo", "notvalid", "--dry-run"])
    assert result.exit_code != 0


def test_route_no_event_exits_zero(monkeypatch, tmp_path):
    """No EVENT_NAME → log and exit 0, never crashes."""
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fake")
    monkeypatch.delenv("EVENT_NAME", raising=False)
    monkeypatch.delenv("EVENT_PATH", raising=False)
    # Need a valid owners file even if route exits early.
    owners_file = tmp_path / "owners.yml"
    owners_file.write_text("version: 1\nconfig: {}\npaths: {}\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "route",
            "--repo",
            "o/r",
            "--owners-file",
            str(owners_file),
            "--mode",
            "assign",
        ],
    )
    assert result.exit_code == 0


def test_route_dependabot_alert_logs_and_exits_zero(monkeypatch, tmp_path):
    """dependabot_alert events are recognized but not routed in v1."""
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fake")
    monkeypatch.setenv("EVENT_NAME", "dependabot_alert")
    monkeypatch.setenv("EVENT_PATH", str(tmp_path / "event.json"))
    owners_file = tmp_path / "owners.yml"
    owners_file.write_text("version: 1\nconfig: {}\npaths: {}\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "route",
            "--repo",
            "o/r",
            "--owners-file",
            str(owners_file),
            "--mode",
            "assign",
        ],
    )
    assert result.exit_code == 0
    assert "dependabot_alert" in result.stdout or "v1.1" in result.stdout


def test_route_pull_request_dry_run_lists_owners(monkeypatch, tmp_path, httpx_mock: HTTPXMock):
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fake")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"number": 42, "user": {"login": "dependabot[bot]"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EVENT_PATH", str(event))

    owners_file = tmp_path / "owners.yml"
    owners_file.write_text(
        """version: 1
config: {}
paths:
  /src/:
    - handle: alice
      strength: strong
      commits: 5
      last_touched: 2026-05-01
""",
        encoding="utf-8",
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/o/r/pulls/42/files?per_page=100",
        json=[{"filename": "src/auth.py"}],
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/o/r/pulls/42",
        json={"assignees": []},
    )

    result = runner.invoke(
        app,
        [
            "route",
            "--repo",
            "o/r",
            "--owners-file",
            str(owners_file),
            "--mode",
            "assign",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "alice" in result.stdout
    assert "dry-run" in result.stdout


def test_route_non_dependabot_author_skips(monkeypatch, tmp_path):
    """Non-Dependabot PRs are skipped with a dim notice and exit 0."""
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fake")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"number": 7, "user": {"login": "octocat"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EVENT_PATH", str(event))

    owners_file = tmp_path / "owners.yml"
    owners_file.write_text("version: 1\nconfig: {}\npaths: {}\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "route",
            "--repo",
            "o/r",
            "--owners-file",
            str(owners_file),
            "--mode",
            "assign",
        ],
    )
    assert result.exit_code == 0
    assert "not a Dependabot PR" in result.stdout
    assert "octocat" in result.stdout


def test_route_missing_user_field_skips(monkeypatch, tmp_path):
    """A malformed PR payload (no user.login) is treated as non-Dependabot."""
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fake")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
    monkeypatch.setenv("EVENT_PATH", str(event))

    owners_file = tmp_path / "owners.yml"
    owners_file.write_text("version: 1\nconfig: {}\npaths: {}\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "route",
            "--repo",
            "o/r",
            "--owners-file",
            str(owners_file),
            "--mode",
            "assign",
        ],
    )
    assert result.exit_code == 0
    assert "not a Dependabot PR" in result.stdout


def test_diff_against_local_codeowners_renders_report(
    tmp_path: Path, monkeypatch, httpx_mock: HTTPXMock
):
    """tend diff produces terminal output and writes no files."""
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fake")

    co_file = tmp_path / "CODEOWNERS"
    co_file.write_text("/src/ @alice\n/docs/ @bob\n", encoding="utf-8")

    httpx_mock.add_response(url=_COMMITS_URL, json=[{"sha": "a"}])
    httpx_mock.add_response(
        url="https://api.github.com/repos/o/r/commits/a",
        json={
            "commit": {
                "author": {
                    "name": "Alice",
                    "email": "alice@example.com",
                    "date": "2026-05-01T00:00:00Z",
                },
                "message": "init",
            },
            "author": {"login": "alice", "type": "User"},
            "files": [{"filename": "src/auth.py", "additions": 1000, "deletions": 0}],
        },
    )

    result = runner.invoke(
        app,
        [
            "diff",
            "--repo",
            "o/r",
            "--against-codeowners",
            str(co_file),
            "--sensitivity",
            "permissive",
            "--lookback-days",
            "365",
            "--no-associations",
            "--no-team-resolution",
        ],
    )
    assert result.exit_code == 0
    # The CODEOWNERS file should not have been modified.
    assert co_file.read_text(encoding="utf-8") == "/src/ @alice\n/docs/ @bob\n"


def test_analyze_fails_with_clear_message_when_no_auth(monkeypatch):
    for env in ("TEND_GITHUB_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("TEND_GITHUB_APP_ID", raising=False)
    result = runner.invoke(app, ["analyze", "--repo", "o/r", "--dry-run"])
    assert result.exit_code != 0
    # CliRunner stores the raised exception on result.exception when the
    # command exits via an uncaught exception (the auth path raises
    # RuntimeError before any output is generated).
    assert isinstance(result.exception, RuntimeError)
    assert "credentials" in str(result.exception).lower()
