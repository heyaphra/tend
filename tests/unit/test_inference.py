"""Algorithm parity tests for inference, scoring, shrinkage, collapse.

Ported from the prototype's tests/test_inference.py. The fixtures and
assertions are identical so behavior drift between the prototype and
tend would show up here. Tests that exercise the prototype-only
``explain_directory`` or ``adaptive_max_depth`` are not ported (the
former lives in ``output/explain.py`` and gets its own file in Phase D;
the latter is out of scope for v1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tend.analyze.commits import FileContribution
from tend.analyze.inference import (
    InferredOwner,
    OwnerCandidate,
    infer,
    suppress_individual_wildcard,
)
from tend.config import InferenceConfig

NOW = datetime.now(UTC)
RECENT = NOW - timedelta(days=5)
OLD = NOW - timedelta(days=200)


def _fc(
    path: str,
    email: str,
    name: str,
    gh: str | None,
    commits: float,
    added: float,
    removed: float,
    when: datetime,
) -> FileContribution:
    return FileContribution(
        file_path=path,
        author_email=email,
        author_name=name,
        github_username=gh,
        commit_count=commits,
        lines_added=added,
        lines_removed=removed,
        last_commit_at=when,
    )


def _cfg(**overrides: Any) -> InferenceConfig:
    """Build an InferenceConfig with the test's overrides on top of defaults.

    ``alpha`` is no longer an ``InferenceConfig`` field (Wilson replaced
    ad-hoc shrinkage). Old test call sites still pass it for historical
    parity — accept and ignore so the fixtures keep compiling.
    """
    defaults: dict[str, Any] = {
        "min_confidence": 0.30,
        "min_commits": 3,
        "max_depth": 3,
        "max_owners_per_path": 3,
        "halflife_days": 90.0,
        "volume_floor": 10,
    }
    defaults.update(overrides)
    defaults.pop("alpha", None)
    return InferenceConfig(**defaults)


def test_ownership_scoring_picks_dominant_contributor():
    contribs = [
        _fc("src/auth/login.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("src/auth/logout.py", "alice@x.com", "Alice", "alice", 10, 100, 50, RECENT),
        _fc("src/auth/login.py", "bob@x.com", "Bob", "bob", 5, 30, 10, OLD),
    ]
    result = infer(contribs, _cfg())
    assert len(result.rules) == 1
    rule = result.rules[0]
    assert rule.path_pattern == "/src/auth/"
    assert rule.owners[0].github_username == "alice"


def test_path_collapsing_same_owner_collapses_to_parent():
    contribs = [
        _fc("src/foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("src/bar.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("src/sub/baz.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = [r.path_pattern for r in result.rules]
    assert "/src/" in patterns
    assert "/src/sub/" not in patterns


def test_path_collapsing_different_owner_is_preserved():
    contribs = [
        _fc("src/foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("src/bar.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("src/sub/baz.py", "bob@x.com", "Bob", "bob", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = {r.path_pattern for r in result.rules}
    assert "/src/" in patterns
    assert "/src/sub/" in patterns


def test_min_commits_filters_low_activity():
    contribs = [
        _fc("foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("foo.py", "bob@x.com", "Bob", "bob", 2, 50, 10, RECENT),
    ]
    result = infer(contribs, _cfg())
    assert result.dropped_below_min_commits >= 1
    for rule in result.rules:
        for owner in rule.owners:
            assert owner.github_username == "alice"


def test_unresolved_contributors_are_dropped():
    contribs = [
        _fc("foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("foo.py", "anon@x.com", "Anon", None, 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    handles = {o.github_username for r in result.rules for o in r.owners}
    assert "alice" in handles
    assert None not in handles


def test_max_depth_limits_granularity():
    contribs = [
        _fc("a/b/c/d/deep.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg(max_depth=2))
    assert all(r.path_pattern.count("/") - 1 <= 2 for r in result.rules)


def test_single_contributor_warning_fires():
    contribs = [
        _fc("foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("bar.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    assert result.single_contributor_warning is True


# -------- sibling collapse --------


def test_sibling_collapse_uniform_owner_creates_parent():
    contribs = [
        _fc("examples/a/foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("examples/b/foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("examples/c/foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = {r.path_pattern for r in result.rules}
    assert patterns == {"/examples/"}
    assert result.rules[0].owners[0].github_username == "alice"


def test_sibling_collapse_mixed_owners_does_not_collapse():
    contribs = [
        _fc("examples/a/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("examples/b/x.py", "bob@x.com", "Bob", "bob", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = {r.path_pattern for r in result.rules}
    assert "/examples/" not in patterns
    assert "/examples/a/" in patterns
    assert "/examples/b/" in patterns


def test_sibling_collapse_single_branch_stops_at_first_parent():
    contribs = [
        _fc("x/y/a/file.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("x/y/b/file.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("x/y/c/file.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = {r.path_pattern for r in result.rules}
    assert patterns == {"/x/y/"}


def test_sibling_collapse_recursive_two_levels():
    contribs = [
        _fc("x/y/a/file.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("x/y/b/file.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("x/y/c/file.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("x/z/a/file.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("x/z/b/file.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("x/z/c/file.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = {r.path_pattern for r in result.rules}
    assert patterns == {"/x/"}


def test_sibling_collapse_never_creates_wildcard_parent():
    contribs = [
        _fc("foo/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("bar/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = {r.path_pattern for r in result.rules}
    assert "*" not in patterns
    assert patterns == {"/foo/", "/bar/"}


def test_sibling_collapse_with_override_preserves_exception():
    contribs = [
        _fc("parent/p.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("parent/q.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("parent/a/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("parent/b/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("parent/c/x.py", "bob@x.com", "Bob", "bob", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = {r.path_pattern for r in result.rules}
    assert "/parent/" in patterns
    assert "/parent/c/" in patterns
    assert "/parent/a/" not in patterns
    assert "/parent/b/" not in patterns
    by_pat = {r.path_pattern: r.owners[0].github_username for r in result.rules}
    assert by_pat["/parent/"] == "alice"
    assert by_pat["/parent/c/"] == "bob"


def test_sibling_collapse_single_child_does_not_collapse():
    contribs = [
        _fc("lonely/a/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = {r.path_pattern for r in result.rules}
    assert "/lonely/a/" in patterns
    assert "/lonely/" not in patterns


def test_sibling_collapse_confidence_uses_min():
    contribs = [
        _fc("x/a/file.py", "alice@x.com", "Alice", "alice", 30, 1000, 0, RECENT),
        _fc("x/b/file.py", "alice@x.com", "Alice", "alice", 30, 1000, 0, RECENT),
        _fc("x/b/file.py", "bob@x.com", "Bob", "bob", 10, 90, 0, RECENT),
        _fc("x/c/file.py", "alice@x.com", "Alice", "alice", 30, 700, 0, RECENT),
        _fc("x/c/file.py", "bob@x.com", "Bob", "bob", 10, 200, 0, RECENT),
    ]
    result = infer(contribs, _cfg())
    by_pat = {r.path_pattern: r for r in result.rules}
    assert "/x/" in by_pat
    parent = by_pat["/x/"]
    assert parent.owners[0].github_username == "alice"
    assert parent.owners[0].confidence < 1.0


def test_sibling_collapse_supermajority_collapses_with_exception():
    """4 of 5 siblings owned by alice + 1 by bob (80% >= 75%) → parent for alice,
    bob's child preserved as override."""
    contribs = [
        _fc("dir/a/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/b/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/c/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/d/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/e/x.py", "bob@x.com", "Bob", "bob", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    by_pat = {r.path_pattern: r.owners[0].github_username for r in result.rules}
    assert by_pat.get("/dir/") == "alice"
    assert by_pat.get("/dir/e/") == "bob"
    assert "/dir/a/" not in by_pat
    assert "/dir/b/" not in by_pat
    assert "/dir/c/" not in by_pat
    assert "/dir/d/" not in by_pat


def test_sibling_collapse_below_threshold_does_not_collapse():
    """2 alice + 1 bob = 66% < 75% → no synthesis, all three children stand."""
    contribs = [
        _fc("dir/a/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/b/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/c/x.py", "bob@x.com", "Bob", "bob", 30, 500, 100, RECENT),
    ]
    result = infer(contribs, _cfg())
    patterns = {r.path_pattern for r in result.rules}
    assert "/dir/" not in patterns
    assert "/dir/a/" in patterns
    assert "/dir/b/" in patterns
    assert "/dir/c/" in patterns


def test_sibling_collapse_aggregates_only_dominant_children():
    """Synthesized parent's last_active/commit_count reflect only the dominant
    children — the dissenter's evidence stays on its own rule."""
    contribs = [
        _fc("dir/a/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/b/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/c/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/d/x.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("dir/e/x.py", "bob@x.com", "Bob", "bob", 30, 500, 100, OLD),
    ]
    result = infer(contribs, _cfg())
    by_pat = {r.path_pattern: r for r in result.rules}
    parent = by_pat["/dir/"]
    # last_active should reflect RECENT, not OLD — bob's evidence is not
    # mixed into the synthesized parent.
    assert parent.owners[0].last_active == RECENT


def test_sibling_collapse_preserves_secondary_owners_across_children():
    """When every dominant child has the same secondary, the secondary
    must appear in the synthesized parent.

    Regression for the .tend/owners.yml bug where a co-owner present in
    each child directory was dropped from the parent's owners list."""
    contribs = [
        # Three sibling dirs; alice is primary, bob is a meaningful secondary
        # in every one of them. Both should clear min_confidence per dir.
        _fc("parent/a/x.py", "alice@x.com", "Alice", "alice", 40, 800, 0, RECENT),
        _fc("parent/a/x.py", "bob@x.com", "Bob", "bob", 30, 600, 0, RECENT),
        _fc("parent/b/x.py", "alice@x.com", "Alice", "alice", 40, 800, 0, RECENT),
        _fc("parent/b/x.py", "bob@x.com", "Bob", "bob", 30, 600, 0, RECENT),
        _fc("parent/c/x.py", "alice@x.com", "Alice", "alice", 40, 800, 0, RECENT),
        _fc("parent/c/x.py", "bob@x.com", "Bob", "bob", 30, 600, 0, RECENT),
    ]
    result = infer(contribs, _cfg(min_confidence=0.20))
    by_pat = {r.path_pattern: r for r in result.rules}
    assert "/parent/" in by_pat
    parent = by_pat["/parent/"]
    handles = {o.github_username for o in parent.owners}
    assert handles == {"alice", "bob"}
    assert parent.owners[0].github_username == "alice"


def test_sibling_collapse_drops_secondary_below_appearance_floor():
    """A secondary present in only one dominant child must not propagate
    to the synthesized parent.

    Five sibling dirs, alice in all five, bob only in one. The
    appearance gate (ceil(N/2) with a floor of 2) requires three
    appearances; bob's single appearance fails the gate."""
    contribs = [
        _fc("dir/a/x.py", "alice@x.com", "Alice", "alice", 30, 800, 0, RECENT),
        _fc("dir/b/x.py", "alice@x.com", "Alice", "alice", 30, 800, 0, RECENT),
        _fc("dir/c/x.py", "alice@x.com", "Alice", "alice", 30, 800, 0, RECENT),
        _fc("dir/d/x.py", "alice@x.com", "Alice", "alice", 30, 800, 0, RECENT),
        _fc("dir/e/x.py", "alice@x.com", "Alice", "alice", 30, 800, 0, RECENT),
        # bob only in /dir/e/ — one appearance of five dominant children.
        _fc("dir/e/x.py", "bob@x.com", "Bob", "bob", 30, 600, 0, RECENT),
    ]
    result = infer(contribs, _cfg(min_confidence=0.20))
    by_pat = {r.path_pattern: r for r in result.rules}
    assert "/dir/" in by_pat
    parent = by_pat["/dir/"]
    handles = {o.github_username for o in parent.owners}
    assert handles == {"alice"}


@pytest.mark.xfail(
    reason="Site B (collapse-into-ancestor) doesn't propagate child secondaries "
    "into the ancestor's owners list — known issue, tracked separately."
)
def test_collapse_into_ancestor_propagates_child_secondary_owners():
    """When ``collapse()`` drops a child whose primary matches its ancestor's
    primary, the child's qualifying secondary owners are currently lost.

    The fix for the .tend/owners.yml co-owner-drop bug addressed the
    sibling-synthesis case; this companion failure mode in the
    ancestor-absorbs-descendant path is deliberately deferred.
    """
    contribs = [
        # /src/ direct files — alice sole contributor.
        _fc("src/foo.py", "alice@x.com", "Alice", "alice", 40, 800, 0, RECENT),
        _fc("src/bar.py", "alice@x.com", "Alice", "alice", 40, 800, 0, RECENT),
        # /src/sub/ — alice still primary, bob meaningful secondary.
        _fc("src/sub/x.py", "alice@x.com", "Alice", "alice", 40, 800, 0, RECENT),
        _fc("src/sub/x.py", "bob@x.com", "Bob", "bob", 30, 600, 0, RECENT),
    ]
    result = infer(contribs, _cfg(min_confidence=0.20))
    by_pat = {r.path_pattern: r for r in result.rules}
    # /src/sub/ is absorbed into /src/ (same primary). After Site B fix,
    # bob's claim on /src/sub/ should surface in the merged /src/ rule.
    assert "/src/" in by_pat
    handles = {o.github_username for o in by_pat["/src/"].owners}
    assert handles == {"alice", "bob"}


# -------- shrinkage --------


def test_shrinkage_preserves_relative_ranking():
    contribs = [
        _fc("dir/a.py", "a@x.com", "A", "alice", 10, 500, 0, RECENT),
        _fc("dir/a.py", "b@x.com", "B", "bob", 10, 100, 0, RECENT),
    ]
    result = infer(contribs, _cfg(min_confidence=0.10, alpha=5.0))
    rule = result.rules[0]
    assert rule.owners[0].github_username == "alice"
    assert any(o.github_username == "bob" for o in rule.owners)
    alice_conf = next(o.confidence for o in rule.owners if o.github_username == "alice")
    bob_conf = next(o.confidence for o in rule.owners if o.github_username == "bob")
    assert alice_conf > bob_conf


def test_volume_floor_suppresses_low_total_directory_activity():
    contribs = [
        _fc("dir/a.py", "alice@x.com", "Alice", "alice", 4, 80, 0, RECENT),
    ]
    result = infer(contribs, _cfg(alpha=5.0, volume_floor=10))
    assert result.rules == []


def test_volume_floor_does_not_suppress_high_volume_directory():
    contribs = [
        _fc("dir/a.py", "alice@x.com", "Alice", "alice", 15, 200, 0, RECENT),
    ]
    result = infer(contribs, _cfg(alpha=5.0, volume_floor=10))
    assert len(result.rules) == 1
    assert result.rules[0].owners[0].github_username == "alice"


def test_volume_floor_allows_multi_contributor_directory_at_floor():
    contribs = [
        _fc("dir/a.py", "a@x.com", "A", "alice", 4, 100, 0, RECENT),
        _fc("dir/a.py", "b@x.com", "B", "bob", 4, 50, 0, RECENT),
        _fc("dir/a.py", "c@x.com", "C", "carol", 4, 25, 0, RECENT),
    ]
    result = infer(contribs, _cfg(min_confidence=0.10, alpha=5.0, volume_floor=10))
    assert len(result.rules) == 1


def test_confidence_applied_before_sibling_collapse():
    """Three sibling dirs, alice sole contributor with 5 commits each.

    Wilson lower bound runs per-directory before collapse; the
    synthesized parent inherits ``min(children.confidence)``. With alice
    as the sole contributor in each child, every child gets a high
    Wilson lower bound, so the parent also gets one — but the parent
    confidence equals the min of the three rather than re-computing on
    aggregated data, which is what we want (collapse is conservative)."""
    contribs = [
        _fc("parent/a/x.py", "alice@x.com", "Alice", "alice", 5, 50, 0, RECENT),
        _fc("parent/b/x.py", "alice@x.com", "Alice", "alice", 5, 50, 0, RECENT),
        _fc("parent/c/x.py", "alice@x.com", "Alice", "alice", 5, 50, 0, RECENT),
    ]
    result = infer(contribs, _cfg(volume_floor=0))
    by_pat = {r.path_pattern: r for r in result.rules}
    assert "/parent/" in by_pat
    parent = by_pat["/parent/"]
    assert parent.owners[0].github_username == "alice"
    # Sole contributor's weighted score dominates each child; Wilson on
    # an effectively-unanimous high-weight sample → strong confidence.
    assert parent.owners[0].confidence > 0.7


# -------- wildcard conservativeness gate --------


def _candidate(handle: str, conf: float = 0.7, commits: int = 10) -> OwnerCandidate:
    return OwnerCandidate(
        email=f"{handle}@x.com",
        name=handle,
        github_username=handle,
        confidence=conf,
        commit_count=commits,
        last_active=RECENT,
    )


def _team_candidate(team_slug: str, conf: float = 0.7) -> OwnerCandidate:
    return OwnerCandidate(
        email="",
        name=f"@{team_slug}",
        github_username=team_slug,
        confidence=conf,
        commit_count=0,
        last_active=RECENT,
    )


def test_suppress_wildcard_drops_individual_only_rule():
    rules = [
        InferredOwner("*", [_candidate("alice", 0.64)]),
        InferredOwner("/src/", [_candidate("alice", 0.9)]),
    ]
    out = suppress_individual_wildcard(rules)
    out_patterns = [r.path_pattern for r in out]
    assert "*" not in out_patterns
    assert "/src/" in out_patterns


def test_suppress_wildcard_keeps_team_handle_rule():
    rules = [InferredOwner("*", [_team_candidate("acme/backend", 0.7)])]
    out = suppress_individual_wildcard(rules)
    assert len(out) == 1
    assert out[0].path_pattern == "*"


def test_suppress_wildcard_keeps_mixed_team_and_individuals():
    rules = [
        InferredOwner(
            "*",
            [_team_candidate("acme/core", 0.7), _candidate("alice", 0.5)],
        )
    ]
    out = suppress_individual_wildcard(rules)
    assert len(out) == 1


def test_suppress_wildcard_respects_pinned_evidence_marker():
    rules = [
        InferredOwner(
            "*",
            [_candidate("alice", 1.0)],
            evidence=["pinned via .tend/owners.yml"],
        )
    ]
    out = suppress_individual_wildcard(rules)
    assert len(out) == 1


def test_suppress_wildcard_drops_multiple_individuals_too():
    rules = [InferredOwner("*", [_candidate("alice", 0.4), _candidate("bob", 0.3)])]
    out = suppress_individual_wildcard(rules)
    assert out == []


def test_suppress_wildcard_handles_empty_rules_list():
    assert suppress_individual_wildcard([]) == []


def test_suppress_wildcard_preserves_rule_order():
    rules = [
        InferredOwner("/a/", [_candidate("alice", 0.9)]),
        InferredOwner("*", [_candidate("alice", 0.6)]),
        InferredOwner("/b/", [_candidate("bob", 0.9)]),
    ]
    out = suppress_individual_wildcard(rules)
    assert [r.path_pattern for r in out] == ["/a/", "/b/"]


# -------- team substitution --------


def _membership(slug: str, *members: str, org: str = "acme"):
    from tend.github.teams import TeamMembership

    return TeamMembership(
        team_slug=slug,
        team_name=slug,
        org=org,
        handle=f"@{org}/{slug}",
        members=set(members),
    )


def test_teams_none_is_no_op_parity_with_pre_team_pipeline():
    contribs = [
        _fc("src/api/foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("src/api/bar.py", "bob@x.com", "Bob", "bob", 30, 500, 100, RECENT),
    ]
    baseline = infer(contribs, _cfg())
    with_none = infer(contribs, _cfg(), teams=None)
    assert [r.path_pattern for r in with_none.rules] == [r.path_pattern for r in baseline.rules]
    assert [[o.github_username for o in r.owners] for r in with_none.rules] == [
        [o.github_username for o in r.owners] for r in baseline.rules
    ]


def test_teams_substitute_individual_handles_with_team_handle():
    contribs = [
        _fc("src/api/foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("src/api/bar.py", "bob@x.com", "Bob", "bob", 30, 500, 100, RECENT),
    ]
    teams = [_membership("backend", "alice", "bob")]
    result = infer(contribs, _cfg(), teams=teams)
    handles = {o.github_username for r in result.rules for o in r.owners}
    assert "acme/backend" in handles
    assert "alice" not in handles
    assert "bob" not in handles


def test_teams_substitution_lets_siblings_collapse_to_team_parent():
    """Sibling dirs owned by different individuals on the same team should
    collapse to one team-owned parent — proves substitution runs before collapse."""
    contribs = [
        _fc("examples/a/foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("examples/b/foo.py", "bob@x.com", "Bob", "bob", 30, 500, 100, RECENT),
        _fc("examples/c/foo.py", "carol@x.com", "Carol", "carol", 30, 500, 100, RECENT),
    ]
    teams = [_membership("backend", "alice", "bob", "carol")]
    result = infer(contribs, _cfg(), teams=teams)
    patterns = {r.path_pattern for r in result.rules}
    assert patterns == {"/examples/"}
    assert result.rules[0].owners[0].github_username == "acme/backend"


def test_teams_disabled_by_config_keeps_individuals_even_when_teams_present():
    contribs = [
        _fc("src/api/foo.py", "alice@x.com", "Alice", "alice", 30, 500, 100, RECENT),
        _fc("src/api/bar.py", "bob@x.com", "Bob", "bob", 30, 500, 100, RECENT),
    ]
    teams = [_membership("backend", "alice", "bob")]
    cfg = _cfg(team_resolution_enabled=False)
    result = infer(contribs, cfg, teams=teams)
    handles = {o.github_username for r in result.rules for o in r.owners}
    assert "acme/backend" not in handles
