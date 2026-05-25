"""Wilson confidence interval tests.

Locks in the Wilson lower-bound values that the inference threshold
checks against. Test cases are chosen to cover the edge cases the
prototype's ad-hoc shrinkage didn't (n=0, k=n unanimous, equal share
with different n, monotonicity).
"""

from __future__ import annotations

import pytest

from tend.analyze.confidence import (
    DEFAULT_CONFIDENCE_LEVEL,
    wilson_bounds,
    wilson_lower,
    z_from_level,
)


def test_wilson_bounds_zero_n_returns_zero_zero():
    """No data → no claim. Don't raise on division-by-zero."""
    assert wilson_bounds(0, 0) == (0.0, 0.0)
    assert wilson_lower(0, 0) == 0.0


def test_wilson_bounds_unanimous_evidence():
    """k=n=10 → lower bound ~0.72 at 95%. Wide upper bound (1.0) because
    we still have only 10 observations."""
    lower, upper = wilson_bounds(10, 10)
    assert 0.70 < lower < 0.74
    assert upper == 1.0


def test_wilson_bounds_50_50_split():
    """Half successes, half failures with n=10 → roughly (0.24, 0.76)."""
    lower, upper = wilson_bounds(5, 10)
    assert 0.22 < lower < 0.26
    assert 0.74 < upper < 0.78


def test_wilson_lower_monotonic_with_n_at_fixed_share():
    """Same proportion (0.8) computed with more observations → tighter
    (higher) lower bound. The whole point of using a CI is that more
    evidence equals stronger claims."""
    lowers = [wilson_lower(int(0.8 * n), n) for n in (5, 10, 50, 500)]
    assert lowers[0] < lowers[1] < lowers[2] < lowers[3]


def test_wilson_lower_below_threshold_when_lone_low_n():
    """Sole contributor with 1 commit (k=1, n=1) → lower bound ~0.21.
    Well below the default ``min_confidence`` of 0.30 — the lone-survivor
    pathology that motivated shrinkage in v0.1 is handled natively here:
    the contributor gets dropped by the threshold check."""
    lower = wilson_lower(1, 1)
    assert lower < 0.30  # below the default min_confidence threshold


def test_wilson_lower_high_n_lone_contributor_clears_threshold():
    """100 unanimous observations is genuine signal; lower bound ~0.96
    clears any reasonable threshold."""
    lower = wilson_lower(100, 100)
    assert lower > 0.95


def test_wilson_bounds_at_90_percent_level_is_wider_on_lower_end():
    """Permissive mode uses z=1.645, giving a wider tolerance for
    inclusion (a higher lower bound for the same data)."""
    z_95 = z_from_level(0.95)
    z_90 = z_from_level(0.90)
    lower_95 = wilson_lower(10, 20, z=z_95)
    lower_90 = wilson_lower(10, 20, z=z_90)
    # Lower z → less margin → higher lower bound (more permissive)
    assert lower_90 > lower_95


def test_z_from_level_returns_expected_values():
    assert z_from_level(0.95) == pytest.approx(1.96, abs=0.001)
    assert z_from_level(0.90) == pytest.approx(1.645, abs=0.001)
    assert z_from_level(0.99) == pytest.approx(2.576, abs=0.001)


def test_z_from_level_unknown_level_picks_nearest():
    """Defensive fallback for unsupported levels."""
    assert z_from_level(0.945) == z_from_level(0.95)


def test_default_confidence_level_constant_matches_balanced_preset():
    """Sanity check: the module-level default lines up with what
    InferenceConfig uses."""
    assert DEFAULT_CONFIDENCE_LEVEL == 0.95
