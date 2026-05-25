"""Shared dataclasses for the analyze pipeline.

Kept in one private module so ``collapse.py`` and ``inference.py`` can both
manipulate the same shapes without circular imports. The public re-exports
live in ``tend.analyze.__init__``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tend.github.associations import Association


@dataclass
class FileContribution:
    """One (file, author) row after bot-filtering and aggregation."""

    file_path: str
    author_email: str
    author_name: str
    github_username: str | None
    commit_count: float  # float so co-author credit (0.5×) works
    lines_added: float
    lines_removed: float
    last_commit_at: datetime


@dataclass
class OwnerCandidate:
    """A scored contributor for a path pattern.

    ``confidence`` is the contributor's per-directory share. v1.1+ this is
    the Wilson lower bound on a binomial proportion; older code may treat
    it as an ad-hoc shrunk share. The numeric value is internal to the
    pipeline — only the derived ``strength`` label appears in the YAML.
    """

    email: str
    name: str
    github_username: str
    confidence: float
    commit_count: int
    last_active: datetime
    strength: str | None = None  # populated by the render pipeline


@dataclass
class InferredOwner:
    """A path-pattern → owners rule. Survives collapse and shrinkage.

    The list is *ordered* — index 0 is the primary owner. ``evidence``
    holds optional free-text annotations populated by post-passes (e.g.
    team-resolution markers); the inference pipeline itself doesn't write
    to it.
    """

    path_pattern: str
    owners: list[OwnerCandidate]
    evidence: list[str] = field(default_factory=list)


@dataclass
class ScoredContributor:
    """Mid-pipeline state: one contributor's per-directory aggregate.

    Produced by ``score_per_directory`` (post-association weighting),
    optionally adjusted by ``apply_breadth_penalty``, then consumed by
    ``_emit_rules`` to produce ``OwnerCandidate`` records.

    ``weighted_score`` is the working number — Wilson bounds are computed
    on it. ``raw_score`` and ``breadth`` are carried along for diagnostic
    visibility (``tend explain`` displays both).
    """

    email: str
    name: str
    github_username: str | None
    weighted_score: float
    raw_score: float
    commits: float
    last_active: datetime
    association: Association | None = None
    breadth: int = 1


@dataclass
class InferenceResult:
    """Top-level output of ``infer()``.

    ``dropped_below_*`` counters are diagnostic. ``single_contributor_warning``
    flags the bus-factor-of-one case across the whole repo.
    """

    rules: list[InferredOwner]
    dropped_below_threshold: int = 0
    dropped_below_min_commits: int = 0
    single_contributor_warning: bool = False
