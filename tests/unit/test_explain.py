"""Tests for ``explain_directory`` — per-contributor scoring breakdown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tend.analyze._models import FileContribution
from tend.config import InferenceConfig
from tend.output.explain import explain_directory

NOW = datetime.now(UTC)
RECENT = NOW - timedelta(days=5)


def _fc(path, email, name, gh, commits, added, removed=0):
    return FileContribution(
        file_path=path,
        author_email=email,
        author_name=name,
        github_username=gh,
        commit_count=commits,
        lines_added=added,
        lines_removed=removed,
        last_commit_at=RECENT,
    )


def _cfg(**overrides):
    defaults = {
        "min_confidence": 0.30,
        "min_commits": 3,
        "volume_floor": 10,
        "halflife_days": 90.0,
    }
    defaults.update(overrides)
    defaults.pop("alpha", None)  # historical fixture parameter; no-op under Wilson
    return InferenceConfig(**defaults)


def test_explain_returns_empty_for_unknown_dir():
    contribs = [_fc("src/x/y.py", "a@x.com", "A", "alice", 5, 100)]
    assert explain_directory(contribs, "/nonexistent/", _cfg()) == []


def test_explain_accepts_codeowners_pattern_or_bare_dir():
    contribs = [_fc("src/x/y.py", "a@x.com", "A", "alice", 5, 100)]
    a = explain_directory(contribs, "/src/x/", _cfg(min_commits=3))
    b = explain_directory(contribs, "src/x", _cfg(min_commits=3))
    assert len(a) == 1
    assert len(b) == 1
    assert a[0]["username"] == "alice"
    assert b[0]["username"] == "alice"


def test_explain_marks_lone_survivor_with_wilson_lower_bound():
    """Alice has 5 commits (clears min_commits=3). Bob has 1 commit (dropped).
    After Bob is filtered, Alice is the sole qualified contributor and gets
    a Wilson lower bound on a unanimous-of-5 sample — comfortably above the
    0.30 threshold. Volume floor disabled to isolate the share calculation."""
    contribs = [
        _fc("dir/a.py", "a@x.com", "A", "alice", 5, 10),
        _fc("dir/a.py", "b@x.com", "B", "bob", 1, 1000),
    ]
    cfg = _cfg(min_commits=3, min_confidence=0.30, volume_floor=0)
    rows = explain_directory(contribs, "/dir/", cfg)
    by_user = {r["username"]: r for r in rows}
    assert by_user["alice"]["status"] == "owner"
    assert by_user["alice"]["share_lower"] is not None
    assert by_user["alice"]["share_lower"] > 0.30
    assert by_user["bob"]["status"].startswith("dropped (commits<3)")
    assert by_user["bob"]["share_lower"] is None


def test_explain_marks_below_confidence_threshold():
    contribs = [
        _fc("dir/a.py", "a@x.com", "A", "alice", 10, 1000),
        _fc("dir/b.py", "b@x.com", "B", "bob", 4, 5),
    ]
    rows = explain_directory(contribs, "/dir/", _cfg(min_commits=3, min_confidence=0.30))
    by_user = {r["username"]: r for r in rows}
    assert by_user["alice"]["status"] == "owner"
    assert by_user["bob"]["status"].startswith("dropped (share_lower<")


def test_explain_marks_unresolved_handles_as_dropped():
    contribs = [
        FileContribution(
            file_path="dir/a.py",
            author_email="ghost@x.com",
            author_name="Ghost",
            github_username=None,
            commit_count=5,
            lines_added=100,
            lines_removed=0,
            last_commit_at=RECENT,
        )
    ]
    rows = explain_directory(contribs, "/dir/", _cfg(min_commits=3))
    assert rows[0]["status"] == "dropped (unresolved handle)"


def test_explain_matches_subtree_for_collapsed_parent_rules():
    """Collapsed parent rules have contributions living in child directories;
    explain must follow the same subtree semantic the inference pipeline uses."""
    contribs = [
        _fc("dir/lib/a.py", "a@x.com", "A", "alice", 5, 100),
        _fc("dir/test/b.py", "b@x.com", "B", "bob", 5, 50),
        _fc("other/c.py", "c@x.com", "C", "carol", 5, 10),
    ]
    rows = explain_directory(contribs, "/dir/", _cfg(min_commits=3))
    users = {r["username"] for r in rows}
    assert users == {"alice", "bob"}


def test_explain_rows_sorted_by_score_descending():
    contribs = [
        _fc("dir/a.py", "a@x.com", "A", "alice", 5, 10),
        _fc("dir/a.py", "b@x.com", "B", "bob", 1, 1000),
        _fc("dir/a.py", "c@x.com", "C", "carol", 3, 50),
    ]
    rows = explain_directory(contribs, "/dir/", _cfg(min_commits=3))
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_explain_marks_volume_floor_in_status():
    contribs = [_fc("dir/a.py", "alice@x.com", "Alice", "alice", 4, 80)]
    rows = explain_directory(contribs, "/dir/", _cfg(min_commits=3, alpha=5.0, volume_floor=10))
    assert len(rows) == 1
    assert rows[0]["status"].startswith("dropped (dir volume<10)")
