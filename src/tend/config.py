"""Resolved configuration for the inference pipeline.

Two layers:

- ``Sensitivity`` presets translate user intent into algorithm parameters.
  Users pick ``strict`` / ``balanced`` / ``permissive`` and never see the
  underlying thresholds directly — the presets are how we communicate
  intent without exposing the math.
- ``InferenceConfig`` is the resolved struct passed into the pipeline.
  All fields are explicit so that callers (CLI, tests, library users)
  never depend on hidden globals.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

Sensitivity = Literal["strict", "balanced", "permissive"]


@dataclass(frozen=True)
class InferenceConfig:
    """Resolved inference parameters consumed by ``infer()``."""

    halflife_days: float = 90.0
    min_confidence: float = 0.30
    # Confidence level for the Wilson lower-bound calculation. 0.95 →
    # z=1.96 (default for strict/balanced); 0.90 → z=1.645 (permissive
    # mode, wider tolerance for inclusion).
    confidence_level: float = 0.95
    min_commits: int = 3
    volume_floor: int = 10
    bot_filter_enabled: bool = True
    lookback_days: int = 180
    max_commits: int = 2000
    max_depth: int = 3
    max_owners_per_path: int = 3
    # Skip commits that touch more files than this — usually vendored bumps,
    # giant merges, or generated-file regenerations that would dominate the
    # scoring formula on raw line count.
    max_files_per_commit: int = 500
    # Raw-share threshold for the breadth penalty. A contributor with
    # ``raw_share >= breadth_share_threshold`` in a directory counts as
    # "present" there for the purpose of computing their breadth penalty.
    # Set low enough that legitimate cross-cutting contributors get
    # penalized; high enough that drive-by single-commit contributors
    # don't inflate everyone else's breadth count.
    breadth_share_threshold: float = 0.05
    # Team-default inference. Substitutes ``@org/team`` for individual
    # handles when a strict majority of a path's top contributors share
    # a small team. Off if the token can't list members (read:org).
    team_resolution_enabled: bool = True
    max_team_size: int = 50
    min_team_share: float = 0.5


SENSITIVITY_PRESETS: dict[Sensitivity, dict[str, Any]] = {
    "strict": {
        "min_confidence": 0.40,
        "confidence_level": 0.95,
        "min_commits": 5,
    },
    "balanced": {
        "min_confidence": 0.30,
        "confidence_level": 0.95,
        "min_commits": 3,
    },
    "permissive": {
        "min_confidence": 0.20,
        "confidence_level": 0.90,
        "min_commits": 2,
    },
}


def resolve_config(
    sensitivity: Sensitivity = "balanced",
    lookback_days: int = 180,
    overrides: dict[str, Any] | None = None,
) -> InferenceConfig:
    """Translate user-facing inputs into a resolved ``InferenceConfig``.

    Precedence: defaults < sensitivity preset < ``lookback_days`` arg <
    explicit ``overrides``. ``overrides`` is keyed by ``InferenceConfig``
    field names; unknown keys raise ``TypeError`` to catch typos at the
    boundary rather than silently dropping them.
    """
    preset = SENSITIVITY_PRESETS[sensitivity]
    cfg = InferenceConfig(lookback_days=lookback_days, **preset)
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg
