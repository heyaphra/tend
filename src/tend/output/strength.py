"""Qualitative strength labels derived from confidence bounds.

Tend's YAML output describes each owner with a single qualitative label
rather than a float, because:

- A confidence float gives a false sense of precision (0.78 is not
  meaningfully different from 0.81 for routing purposes).
- Human reviewers process `strong` / `moderate` / `suggestive` faster
  than they parse percentages.
- Floats invite arguments about rounding; labels do not.

The internal pipeline still tracks ``share_lower`` (the Wilson lower
bound on the contributor's share); the label is a one-way derivation
for the YAML and PR-body surfaces. The diagnostic ``tend explain``
output shows both for power users.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tend.analyze._models import InferredOwner

StrengthLabel = Literal["strong", "moderate", "suggestive"]

STRONG_THRESHOLD = 0.60
MODERATE_THRESHOLD = 0.35


def strength_label(share_lower: float, *, min_confidence: float) -> StrengthLabel | None:
    """Map a Wilson lower-bound share to a qualitative label.

    ``share_lower >= 0.60`` → ``"strong"``
    ``share_lower >= 0.35`` → ``"moderate"``
    ``share_lower >= min_confidence`` → ``"suggestive"``
    below → ``None`` (caller should exclude from YAML)

    The lowest tier uses the caller's ``min_confidence`` rather than a
    hard-coded value so the sensitivity preset (strict/balanced/permissive)
    controls the exclusion cutoff coherently with everything else.
    """
    if share_lower >= STRONG_THRESHOLD:
        return "strong"
    if share_lower >= MODERATE_THRESHOLD:
        return "moderate"
    if share_lower >= min_confidence:
        return "suggestive"
    return None


def staleness_threshold_days(lookback_days: int) -> int:
    """Demote-after threshold: half the lookback window, integer-floored."""
    return lookback_days // 2


def adjust_strength_for_recency(
    base: StrengthLabel | None,
    days_since_last_touch: int,
    *,
    lookback_days: int,
) -> StrengthLabel | None:
    """Demote ``base`` by one tier when the owner hasn't touched the path recently.

    Within ``lookback_days // 2`` of today the label is unchanged. Past
    that window: strong → moderate, moderate → suggestive. Suggestive
    stays suggestive (no tier below it that's still a label). ``None``
    in, ``None`` out.
    """
    if base is None:
        return None
    if days_since_last_touch <= staleness_threshold_days(lookback_days):
        return base
    demotion: dict[StrengthLabel, StrengthLabel] = {
        "strong": "moderate",
        "moderate": "suggestive",
        "suggestive": "suggestive",
    }
    return demotion[base]


def annotate_rules(
    rules: list[InferredOwner],
    *,
    min_confidence: float,
    lookback_days: int,
    now: datetime | None = None,
) -> list[InferredOwner]:
    """Populate ``OwnerCandidate.strength`` on every owner of every rule.

    Strength is the share-based label adjusted for recency: an owner who
    hasn't touched the path within ``lookback_days // 2`` days is demoted
    one tier so reviewers can spot stale claims at a glance.

    Mutates the candidates in place (they're dataclasses with mutable
    fields) and returns the list for convenient chaining. Idempotent for
    a given ``now`` — re-running with the same inputs is a no-op.

    Call this on freshly-inferred rules before handing them to ``diff``
    or ``dump_full`` so the strength field is consistent everywhere.
    Rules loaded from YAML already have strength populated by the parser.
    """
    reference = now or datetime.now(UTC)
    for rule in rules:
        for owner in rule.owners:
            base = strength_label(owner.confidence, min_confidence=min_confidence)
            last = owner.last_active
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            days = max(0, (reference - last).days)
            owner.strength = adjust_strength_for_recency(base, days, lookback_days=lookback_days)
    return rules


__all__ = [
    "MODERATE_THRESHOLD",
    "STRONG_THRESHOLD",
    "StrengthLabel",
    "adjust_strength_for_recency",
    "annotate_rules",
    "staleness_threshold_days",
    "strength_label",
]
