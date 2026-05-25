"""Longest-prefix matching tests for tend.route.matcher."""

from __future__ import annotations

from datetime import UTC, datetime

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.output.tend_yaml import OwnershipFile
from tend.route.matcher import find_owner, find_owners_for_files

WHEN = datetime(2026, 5, 1, tzinfo=UTC)


def _rule(pattern: str, handle: str) -> InferredOwner:
    return InferredOwner(
        path_pattern=pattern,
        owners=[
            OwnerCandidate(
                email=f"{handle}@x.com",
                name=handle,
                github_username=handle,
                confidence=0.7,
                commit_count=10,
                last_active=WHEN,
            )
        ],
    )


def _file(rules: list[InferredOwner]) -> OwnershipFile:
    return OwnershipFile(version=1, generated_at=None, config={}, paths=rules)


def test_directory_rule_matches_files_under_it():
    f = _file([_rule("/src/auth/", "alice")])
    match = find_owner(f, "src/auth/login.py")
    assert match is not None
    assert match.owners[0].github_username == "alice"


def test_directory_rule_does_not_match_other_directories():
    f = _file([_rule("/src/auth/", "alice")])
    assert find_owner(f, "src/billing/api.py") is None


def test_more_specific_rule_wins():
    f = _file(
        [
            _rule("/src/", "alice"),
            _rule("/src/auth/", "bob"),
        ]
    )
    match = find_owner(f, "src/auth/login.py")
    assert match is not None
    assert match.owners[0].github_username == "bob"


def test_wildcard_is_lowest_priority():
    f = _file(
        [
            _rule("*", "alice"),
            _rule("/src/", "bob"),
        ]
    )
    src_match = find_owner(f, "src/auth.py")
    assert src_match is not None
    assert src_match.owners[0].github_username == "bob"
    other_match = find_owner(f, "README.md")
    assert other_match is not None
    assert other_match.owners[0].github_username == "alice"


def test_file_rule_matches_only_exact_path():
    f = _file([_rule("/LICENSE", "alice")])
    match = find_owner(f, "LICENSE")
    assert match is not None
    assert match.owners[0].github_username == "alice"
    assert find_owner(f, "LICENSE.md") is None


def test_no_match_returns_none():
    f = _file([_rule("/src/", "alice")])
    assert find_owner(f, "docs/intro.md") is None


def test_find_owners_for_files_returns_per_file_mapping():
    f = _file(
        [
            _rule("/src/", "alice"),
            _rule("/docs/", "bob"),
        ]
    )
    out = find_owners_for_files(f, ["src/a.py", "docs/x.md", "unmatched.txt"])
    assert out["src/a.py"].owners[0].github_username == "alice"
    assert out["docs/x.md"].owners[0].github_username == "bob"
    assert out["unmatched.txt"] is None


def test_directory_rule_matches_exact_directory_path_too():
    """``/foo/`` should match both ``foo/x.py`` and the bare ``foo`` directory."""
    f = _file([_rule("/src/", "alice")])
    match = find_owner(f, "src")
    assert match is not None
    assert match.owners[0].github_username == "alice"
