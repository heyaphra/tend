"""Wilson score confidence intervals for share estimates.

The previous shrinkage approach (``raw × n / (n + α)``) pulled low-n
estimates toward zero, which is the right direction but isn't a proper
statistical bound. Wilson lower bound is a principled 95% (or 90% per
preset) binomial confidence interval that does the same job with
defensible interpretation: "we are 95% confident the true share is
at least ``wilson_lower(k, n)``."

Used by ``inference.py`` to score each contributor's per-path share.
The lower bound is what the filtering threshold compares against —
contributors whose lower bound clears ``min_confidence`` are owners;
others are dropped.
"""

from __future__ import annotations

import math

# Two-sided z-scores for common confidence levels. The mapping is
# 1 - α/2 quantile of the standard normal:
#   0.95 → 1.95996...
#   0.90 → 1.64485...
# Storing as a small table avoids pulling in scipy just for ppf().
_Z_TABLE = {
    0.99: 2.5758293035489004,
    0.95: 1.959963984540054,
    0.90: 1.6448536269514722,
    0.80: 1.2815515655446004,
}
DEFAULT_CONFIDENCE_LEVEL = 0.95


def z_from_level(confidence_level: float) -> float:
    """Look up the two-sided z-score for a confidence level.

    Common values (0.99, 0.95, 0.90, 0.80) are exact. For an
    unsupported value, fall back to the nearest in the table — Tend's
    presets only ever use one of these, so callers shouldn't pass odd
    values, but degrading gracefully beats raising on a typo.
    """
    if confidence_level in _Z_TABLE:
        return _Z_TABLE[confidence_level]
    nearest = min(_Z_TABLE, key=lambda level: abs(level - confidence_level))
    return _Z_TABLE[nearest]


def wilson_bounds(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    """95% (by default) Wilson score interval for a binomial proportion.

    Args:
        k: weighted successes — e.g. a contributor's weighted lines or
           commits in a directory.
        n: weighted total across all contributors in the same scope.
        z: standard-normal quantile (1.96 for 95%, 1.645 for 90%).

    Returns:
        ``(lower, upper)`` bounds on the true share, both clipped to
        ``[0.0, 1.0]``. When ``n <= 0``, returns ``(0.0, 0.0)``.
    """
    if n <= 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1.0 + (z * z) / n
    center = (phat + (z * z) / (2.0 * n)) / denom
    margin = (z * math.sqrt(phat * (1.0 - phat) / n + (z * z) / (4.0 * n * n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def wilson_lower(k: float, n: float, z: float = 1.96) -> float:
    """Convenience: just the lower bound of ``wilson_bounds``."""
    return wilson_bounds(k, n, z)[0]


__all__ = [
    "DEFAULT_CONFIDENCE_LEVEL",
    "wilson_bounds",
    "wilson_lower",
    "z_from_level",
]
