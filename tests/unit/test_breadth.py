"""Tests for the generalist/specialist breadth penalty.

Covers the cross-directory presence count, the sqrt-based weighting,
and the presence-threshold cutoff that keeps drive-by contributors
from inflating the count.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from tend.analyze._models import ScoredContributor
from tend.analyze.breadth import apply_breadth_penalty, count_directories_present

WHEN = datetime(2026, 5, 1, tzinfo=UTC)


def _sc(
    handle: str,
    *,
    raw_score: float = 100.0,
    weighted_score: float | None = None,
    commits: float = 5.0,
) -> ScoredContributor:
    return ScoredContributor(
        email=f"{handle}@x.com",
        name=handle,
        github_username=handle,
        weighted_score=weighted_score if weighted_score is not None else raw_score,
        raw_score=raw_score,
        commits=commits,
        last_active=WHEN,
    )


def test_specialist_has_breadth_one_and_no_penalty():
    """Sole contributor in one directory keeps full weighted score."""
    per_dir = {"/src/": [_sc("alice", weighted_score=100)]}
    out = apply_breadth_penalty(per_dir)
    assert out["/src/"][0].weighted_score == 100
    assert out["/src/"][0].breadth == 1


def test_generalist_score_divided_by_sqrt_breadth():
    """Same contributor across 4 directories at full raw share each →
    breadth 4 → weighted score divided by 2."""
    per_dir = {
        "/a/": [_sc("alice", weighted_score=100)],
        "/b/": [_sc("alice", weighted_score=100)],
        "/c/": [_sc("alice", weighted_score=100)],
        "/d/": [_sc("alice", weighted_score=100)],
    }
    out = apply_breadth_penalty(per_dir)
    for path in out:
        assert out[path][0].breadth == 4
        assert out[path][0].weighted_score == 50.0  # 100 / sqrt(4)


def test_extreme_generalist_25_directories():
    """Breadth=25 → divide weighted score by 5."""
    per_dir = {f"/d{i}/": [_sc("alice", weighted_score=100)] for i in range(25)}
    out = apply_breadth_penalty(per_dir)
    for path in out:
        assert out[path][0].breadth == 25
        assert out[path][0].weighted_score == 100 / math.sqrt(25)


def test_presence_threshold_excludes_drive_by_contributors():
    """A contributor whose raw share is below the presence threshold
    doesn't count toward their own breadth."""
    # alice is dominant in /a/ and /b/; she also has 1% share in /c/ (drive-by).
    per_dir = {
        "/a/": [_sc("alice", raw_score=99), _sc("bob", raw_score=1)],
        "/b/": [_sc("alice", raw_score=99), _sc("bob", raw_score=1)],
        "/c/": [_sc("alice", raw_score=1), _sc("carol", raw_score=99)],
    }
    breadth = count_directories_present(per_dir, presence_threshold=0.05)
    # alice is "present" only in /a/ and /b/ — /c/ is below 5%.
    assert breadth["alice"] == 2
    assert breadth["bob"] == 0  # 1% share everywhere, never above threshold
    assert breadth["carol"] == 1


def test_presence_threshold_configurable():
    """Lowering the threshold makes more contributors count as present."""
    per_dir = {
        "/a/": [_sc("alice", raw_score=99), _sc("bob", raw_score=1)],
        "/b/": [_sc("alice", raw_score=99), _sc("bob", raw_score=1)],
    }
    breadth_strict = count_directories_present(per_dir, presence_threshold=0.05)
    breadth_loose = count_directories_present(per_dir, presence_threshold=0.005)
    assert breadth_strict["bob"] == 0
    assert breadth_loose["bob"] == 2


def test_apply_breadth_penalty_returns_fresh_dict_does_not_mutate_input():
    """Defensive: callers shouldn't see their input mutated."""
    original = ScoredContributor(
        email="a@x.com",
        name="a",
        github_username="alice",
        weighted_score=100.0,
        raw_score=100.0,
        commits=5,
        last_active=WHEN,
    )
    per_dir = {"/a/": [original], "/b/": [original]}
    apply_breadth_penalty(per_dir)
    # Original object unchanged.
    assert original.weighted_score == 100.0
    assert original.breadth == 1


def test_specialist_outranks_generalist_post_penalty():
    """Synthetic scenario: specialist with 100 weight in one dir vs.
    generalist with 100 weight in 25 dirs. Both have the same raw
    weight in /shared/ — after penalty, the specialist wins."""
    dirs = {f"/d{i}/": [_sc("generalist", weighted_score=100)] for i in range(25)}
    dirs["/specialist-home/"] = [_sc("specialist", weighted_score=100)]
    # /shared/ has both at equal weighted_score
    dirs["/shared/"] = [
        _sc("specialist", weighted_score=100),
        _sc("generalist", weighted_score=100),
    ]
    out = apply_breadth_penalty(dirs)
    specialist_score = next(
        c.weighted_score for c in out["/shared/"] if c.github_username == "specialist"
    )
    generalist_score = next(
        c.weighted_score for c in out["/shared/"] if c.github_username == "generalist"
    )
    assert specialist_score > generalist_score
