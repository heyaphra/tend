"""Generalist/specialist breadth penalty.

A contributor who touches 25 directories with a small share each is
mostly doing horizontal work (release management, refactors, build
maintenance). Their per-directory ownership signal is weaker than that
of a contributor who concentrates on one or two directories. This
module penalizes that by dividing each contributor's weighted score by
``sqrt(breadth)``, where breadth is the count of directories where the
contributor has at least ``presence_threshold`` of the raw share
(default 5%).

A specialist with breadth=1 keeps their full score; a generalist with
breadth=25 has their per-directory scores divided by 5. The
``sqrt`` shape is gentle enough that legitimate cross-cutting
contributors (security leads, framework authors) still surface as
owners in the directories where their share is large — they just
don't dominate every directory they sprinkled into.

Runs after ``score_per_directory`` (so association weighting is
applied) and before Wilson bounds (so the bound reflects the
penalized score). The cross-directory view is the structural reason
``infer()`` got decomposed in v0.2.0.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import replace

from tend.analyze._models import ScoredContributor


def count_directories_present(
    per_dir: dict[str, list[ScoredContributor]],
    presence_threshold: float = 0.05,
) -> dict[str, int]:
    """For each handle, count directories where their *raw share* is at
    least ``presence_threshold``.

    Uses raw_score, not weighted_score, so the breadth count isn't itself
    distorted by association weighting. (A FIRST_TIME_CONTRIBUTOR with 10%
    raw share still counts as "present"; downstream association weighting
    shrinks their actual contribution but their breadth is real.)
    """
    breadth: dict[str, int] = defaultdict(int)
    for contributors in per_dir.values():
        total_raw = sum(c.raw_score for c in contributors)
        if total_raw <= 0:
            continue
        for c in contributors:
            handle = c.github_username
            if not handle:
                continue
            if c.raw_score / total_raw >= presence_threshold:
                breadth[handle.lower()] += 1
    return breadth


def apply_breadth_penalty(
    per_dir: dict[str, list[ScoredContributor]],
    *,
    presence_threshold: float = 0.05,
) -> dict[str, list[ScoredContributor]]:
    """Divide each contributor's ``weighted_score`` by ``sqrt(breadth)``.

    Returns a fresh dict; the input is not mutated. Each ``ScoredContributor``
    has its ``weighted_score`` reduced and its ``breadth`` field updated
    to the computed count so diagnostic surfaces (``tend explain``) can
    show the penalty.

    A contributor with no observed breadth (e.g. only commit in a
    sub-floor directory that didn't make it into ``per_dir``) defaults
    to breadth=1 — no penalty.
    """
    breadth = count_directories_present(per_dir, presence_threshold=presence_threshold)

    out: dict[str, list[ScoredContributor]] = {}
    for path, contributors in per_dir.items():
        new_list: list[ScoredContributor] = []
        for c in contributors:
            handle = (c.github_username or "").lower()
            b = max(1, breadth.get(handle, 1))
            penalty = math.sqrt(b)
            new_list.append(replace(c, weighted_score=c.weighted_score / penalty, breadth=b))
        out[path] = new_list
    return out


__all__ = ["apply_breadth_penalty", "count_directories_present"]
