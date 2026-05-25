"""Tests for the CODEOWNERS drift diagnostic.

Parser, diff classifier, and Rich report rendering. The pattern-matching
tests cover the prototype's container-fallback behavior that the diff
diagnostic depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.diagnose.codeowners_diff import (
    CodeownersDiff,
    CodeownersRule,
    _normalize_pattern_for_match,
    _pattern_directory_container,
    diff_codeowners,
    parse_codeowners,
    render_report,
)

WHEN = datetime(2026, 5, 1, tzinfo=UTC)


def _owner(handle: str, conf: float = 0.7) -> OwnerCandidate:
    return OwnerCandidate(
        email=f"{handle}@x.com",
        name=handle,
        github_username=handle,
        confidence=conf,
        commit_count=10,
        last_active=WHEN,
    )


def _rule(pattern: str, owners: list[OwnerCandidate]) -> InferredOwner:
    return InferredOwner(path_pattern=pattern, owners=owners)


# -------- Parser --------


def test_parse_strips_comments_and_blanks():
    content = """# Top-level comment

/src/auth/ @alice  # inline comment
# Another comment
/docs/  @bob @carol
"""
    rules = parse_codeowners(content)
    assert len(rules) == 2
    assert rules[0].pattern == "/src/auth/"
    assert rules[0].owners == ["@alice"]
    assert rules[0].line_number == 3
    assert rules[1].pattern == "/docs/"
    assert rules[1].owners == ["@bob", "@carol"]


def test_parse_skips_lines_without_owners():
    rules = parse_codeowners("/src/ \n/docs/\n")
    assert rules == []


def test_parse_handles_empty_file():
    assert parse_codeowners("") == []


# -------- Pattern normalization --------


def test_normalize_pattern_canonicalizes_directory_forms():
    assert _normalize_pattern_for_match("src/foo") == "/src/foo/"
    assert _normalize_pattern_for_match("/src/foo") == "/src/foo/"
    assert _normalize_pattern_for_match("/src/foo/") == "/src/foo/"
    assert _normalize_pattern_for_match("/src/foo/**") == "/src/foo/"
    assert _normalize_pattern_for_match("*") == "*"


def test_container_drops_glob_components():
    assert _pattern_directory_container("/src/foo/*.py") == "/src/foo/"
    assert _pattern_directory_container("/src/**") == "/src/"
    assert _pattern_directory_container("*.md") == "/"
    assert _pattern_directory_container("*") == "/"
    assert _pattern_directory_container("/src/foo/bar.py") == "/src/foo/"


# -------- Diff --------


def test_diff_confirmed_exact_match():
    existing = [CodeownersRule("/src/", ["@alice"], 1)]
    inferred = [_rule("/src/", [_owner("alice")])]
    d = diff_codeowners(existing, inferred)
    assert len(d.confirmed_rules) == 1
    assert d.drifted_rules == []
    assert d.new_rules == []


def test_diff_drifted_when_top_owner_differs():
    existing = [CodeownersRule("/src/", ["@alice"], 1)]
    inferred = [_rule("/src/", [_owner("bob")])]
    d = diff_codeowners(existing, inferred)
    assert len(d.drifted_rules) == 1
    assert d.confirmed_rules == []


def test_diff_finds_new_inferred_rules():
    existing = []
    inferred = [_rule("/docs/", [_owner("carol")])]
    d = diff_codeowners(existing, inferred)
    assert d.new_rules == inferred
    assert d.summary["new"] == 1


def test_diff_finds_existing_with_no_inference():
    existing = [CodeownersRule("/unused/", ["@alice"], 1)]
    inferred = [_rule("/src/", [_owner("bob")])]
    d = diff_codeowners(existing, inferred)
    assert len(d.existing_with_no_inference) == 1
    assert d.existing_with_no_inference[0].pattern == "/unused/"


def test_diff_container_fallback_matches_file_rules_to_dir_inference():
    """Declared file-level rule should match against directory-level inference."""
    existing = [CodeownersRule("/src/foo/*.py", ["@alice"], 1)]
    inferred = [_rule("/src/foo/", [_owner("alice")])]
    d = diff_codeowners(existing, inferred)
    # Should be confirmed via container-fallback, not stranded in new/existing.
    assert len(d.confirmed_rules) == 1
    assert d.new_rules == []
    assert d.existing_with_no_inference == []


def test_diff_preserves_team_handles_when_no_team_data():
    """Without team-membership data, declared team handles are preserved
    (treated as confirmed) rather than drowning the report in false positives."""
    existing = [CodeownersRule("/src/", ["@acme/backend"], 1)]
    inferred = [_rule("/src/", [_owner("alice")])]
    d = diff_codeowners(existing, inferred, teams_by_handle=None)
    assert len(d.confirmed_rules) == 1
    assert d.drifted_rules == []


def test_diff_confirms_team_when_majority_of_inferred_on_roster():
    """With team data, declared team is confirmed iff ≥50% of inferred
    contributors are on its roster."""
    existing = [CodeownersRule("/src/", ["@acme/backend"], 1)]
    inferred = [_rule("/src/", [_owner("alice"), _owner("bob")])]
    teams = {"@acme/backend": {"alice", "bob", "carol"}}
    d = diff_codeowners(existing, inferred, teams_by_handle=teams)
    assert len(d.confirmed_rules) == 1
    assert d.drifted_rules == []


def test_diff_drifts_team_when_inferred_not_on_roster():
    existing = [CodeownersRule("/src/", ["@acme/backend"], 1)]
    inferred = [_rule("/src/", [_owner("eve"), _owner("mallory")])]
    teams = {"@acme/backend": {"alice", "bob"}}
    d = diff_codeowners(existing, inferred, teams_by_handle=teams)
    assert len(d.drifted_rules) == 1
    assert d.confirmed_rules == []


# -------- Report --------


def test_render_report_returns_rich_renderable(tmp_path):
    """Smoke test: the rendered report is a Rich Group containing tables.
    We don't assert exact text; just verify it renders without errors and
    contains the expected sections for non-empty buckets."""
    from rich.console import Console

    diff = CodeownersDiff(
        confirmed_rules=[
            (CodeownersRule("/src/", ["@alice"], 1), _rule("/src/", [_owner("alice")]))
        ],
        drifted_rules=[(CodeownersRule("/docs/", ["@bob"], 2), _rule("/docs/", [_owner("carol")]))],
        new_rules=[_rule("/api/", [_owner("dave")])],
    )
    report = render_report(diff, codeowners_path=".github/CODEOWNERS")
    sink = tmp_path / "out.txt"
    with sink.open("w") as f:
        console = Console(record=True, width=80, file=f)
        console.print(report)
        output = console.export_text()
    assert "Confirmed" in output
    assert "Drifted" in output
    assert "Inferred-only" in output
    assert "alice" in output
    assert "bob" in output
