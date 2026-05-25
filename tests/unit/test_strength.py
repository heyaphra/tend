"""Tests for strength labels and recency-based demotion.

``strength_label`` maps a Wilson lower-bound share to a qualitative tier.
``adjust_strength_for_recency`` demotes that tier when the owner's last
activity is older than half the lookback window. ``annotate_rules`` is
the integration point — it stamps the adjusted label onto every owner
in a rule set so downstream code (diff, render, PR body) reads a
consistent value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.output.strength import (
    adjust_strength_for_recency,
    annotate_rules,
    staleness_threshold_days,
    strength_label,
)

NOW = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)


def _owner(handle: str, confidence: float, *, last_active: datetime) -> OwnerCandidate:
    return OwnerCandidate(
        email=f"{handle}@example.com",
        name=handle,
        github_username=handle,
        confidence=confidence,
        commit_count=10,
        last_active=last_active,
    )


# ---- strength_label (preserved behavior) ----


def test_strength_label_strong():
    assert strength_label(0.70, min_confidence=0.30) == "strong"


def test_strength_label_moderate():
    assert strength_label(0.40, min_confidence=0.30) == "moderate"


def test_strength_label_suggestive():
    assert strength_label(0.32, min_confidence=0.30) == "suggestive"


def test_strength_label_below_floor_returns_none():
    assert strength_label(0.10, min_confidence=0.30) is None


# ---- adjust_strength_for_recency ----


def test_recency_demotion_recent_is_unchanged():
    assert adjust_strength_for_recency("strong", 10, lookback_days=180) == "strong"


def test_recency_demotion_at_threshold_is_unchanged():
    # 90 == lookback_days // 2 → still within window (the `<=` boundary).
    assert adjust_strength_for_recency("strong", 90, lookback_days=180) == "strong"


def test_recency_demotion_just_past_threshold_demotes_strong():
    assert adjust_strength_for_recency("strong", 91, lookback_days=180) == "moderate"


def test_recency_demotion_demotes_moderate_to_suggestive():
    assert adjust_strength_for_recency("moderate", 120, lookback_days=180) == "suggestive"


def test_recency_demotion_suggestive_stays_suggestive():
    assert adjust_strength_for_recency("suggestive", 365, lookback_days=180) == "suggestive"


def test_recency_demotion_none_stays_none():
    assert adjust_strength_for_recency(None, 365, lookback_days=180) is None


def test_recency_demotion_custom_lookback_window():
    # lookback=90 → threshold at 45. 30 days stays, 60 days demotes.
    assert adjust_strength_for_recency("strong", 30, lookback_days=90) == "strong"
    assert adjust_strength_for_recency("strong", 60, lookback_days=90) == "moderate"


def test_staleness_threshold_floors():
    assert staleness_threshold_days(180) == 90
    assert staleness_threshold_days(90) == 45
    assert staleness_threshold_days(30) == 15
    # Integer floor for odd inputs.
    assert staleness_threshold_days(31) == 15


# ---- annotate_rules ----


def test_annotate_rules_stamps_base_strength_when_recent():
    rule = InferredOwner(
        path_pattern="/src/",
        owners=[_owner("alice", 0.70, last_active=NOW - timedelta(days=10))],
    )
    annotate_rules([rule], min_confidence=0.30, lookback_days=180, now=NOW)
    assert rule.owners[0].strength == "strong"


def test_annotate_rules_demotes_when_stale():
    rule = InferredOwner(
        path_pattern="/src/",
        owners=[_owner("alice", 0.95, last_active=NOW - timedelta(days=120))],
    )
    annotate_rules([rule], min_confidence=0.30, lookback_days=180, now=NOW)
    # 120 days > 90 day threshold → strong → moderate.
    assert rule.owners[0].strength == "moderate"


def test_annotate_rules_is_idempotent_for_same_now():
    rule = InferredOwner(
        path_pattern="/src/",
        owners=[_owner("alice", 0.70, last_active=NOW - timedelta(days=10))],
    )
    annotate_rules([rule], min_confidence=0.30, lookback_days=180, now=NOW)
    first = rule.owners[0].strength
    annotate_rules([rule], min_confidence=0.30, lookback_days=180, now=NOW)
    assert rule.owners[0].strength == first


def test_annotate_rules_handles_naive_last_active():
    """Owners loaded from older fixtures may have tz-naive ``last_active``;
    annotate_rules should normalize rather than blow up on subtraction."""
    naive = (NOW - timedelta(days=10)).replace(tzinfo=None)
    rule = InferredOwner(
        path_pattern="/src/",
        owners=[_owner("alice", 0.70, last_active=naive)],
    )
    annotate_rules([rule], min_confidence=0.30, lookback_days=180, now=NOW)
    assert rule.owners[0].strength == "strong"
