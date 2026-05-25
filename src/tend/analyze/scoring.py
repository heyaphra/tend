"""Per-(file, author) contribution scoring.

The composite formula is the original from the prototype:

    score = lines × exp(-days_ago / halflife) × log(1 + commits)

- ``lines`` is the total lines touched (added + removed). A single 1,000-line
  change scores like a 1,000-commit string of trivial fixes — the
  ``log1p(commits)`` factor compresses commit count so we don't reward
  contributors who split work into many small commits over the same change.
- ``exp(-days/halflife)`` decays old contributions geometrically. Default
  halflife is 90 days — a commit from a year ago counts ~1/16 of one today.
- ``commits`` is the deduplicated commit count for this contributor on this
  file. Fractional values (co-author credit at 0.5×) are supported.

The function is intentionally pure so it can be exercised in isolation
and reused by any aggregator (current per-directory, future per-component).

``apply_association_weight`` is the small helper that multiplies a raw
score by the contributor's ``CommentAuthorAssociation`` weight (MEMBER
× 1.0, NONE × 0.1, etc.) — kept separate from ``score()`` so the
recency/frequency formula stays orthogonal to the membership signal.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime

from tend.analyze._models import FileContribution, ScoredContributor
from tend.analyze.collapse import directory_of
from tend.config import InferenceConfig
from tend.github.associations import Association, association_weight


def score(lines: float, days_ago: float, commits: float, halflife_days: float) -> float:
    """Composite contribution score. See module docstring for the formula."""
    recency_w = math.exp(-max(0.0, days_ago) / halflife_days)
    freq_w = math.log1p(commits)
    return lines * recency_w * freq_w


def apply_association_weight(
    raw_score: float,
    assoc: Association | None,
    weights: dict[Association, float] | None = None,
) -> float:
    """Multiply ``raw_score`` by the weight for ``assoc``.

    Trivial wrapper, kept for symmetry with future per-pipeline-step
    helpers (breadth penalty etc.). Returns the raw score unmodified
    when no association data is available — the multiplier defaults to
    UNKNOWN's neutral 0.5 only when ``assoc`` is explicitly set to
    ``Association.UNKNOWN``; ``None`` means "we never tried to look this
    up", which leaves the score alone.
    """
    if assoc is None:
        return raw_score
    return raw_score * association_weight(assoc, weights)


def score_per_directory(
    contributions: list[FileContribution],
    config: InferenceConfig,
    associations: dict[str, Association] | None = None,
) -> dict[str, list[ScoredContributor]]:
    """Aggregate file-level contributions into per-(directory, contributor) scores.

    Pipeline steps owned here (in order):

    1. Group by directory.
    2. Drop directories whose total commit volume is below ``volume_floor``.
    3. Per directory, aggregate by contributor: sum raw scores, sum commits,
       track latest activity timestamp.
    4. Apply ``CommentAuthorAssociation`` weight (if ``associations`` is
       provided) to produce ``weighted_score``.

    Returns ``{path_pattern_dir_path: [ScoredContributor, ...]}``. The
    cross-directory shape is what ``apply_breadth_penalty`` needs.
    """
    by_dir: dict[str, list[FileContribution]] = defaultdict(list)
    for c in contributions:
        by_dir[directory_of(c.file_path)].append(c)

    now = datetime.now(UTC)
    out: dict[str, list[ScoredContributor]] = {}

    for directory, contribs in by_dir.items():
        if sum(c.commit_count for c in contribs) < config.volume_floor:
            continue

        per_author: dict[str, ScoredContributor] = {}
        for c in contribs:
            last = c.last_commit_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            days = max(0, (now - last).days)
            lines = c.lines_added + c.lines_removed
            raw = score(lines, days, c.commit_count, config.halflife_days)

            assoc: Association | None = None
            weighted = raw
            if associations is not None:
                handle_key = (c.github_username or "").lower()
                assoc = associations.get(handle_key, Association.UNKNOWN)
                weighted = apply_association_weight(raw, assoc)

            key = (c.github_username or c.author_email).lower()
            existing = per_author.get(key)
            if existing is None:
                per_author[key] = ScoredContributor(
                    email=c.author_email,
                    name=c.author_name,
                    github_username=c.github_username,
                    weighted_score=weighted,
                    raw_score=raw,
                    commits=c.commit_count,
                    last_active=last,
                    association=assoc,
                )
            else:
                existing.weighted_score += weighted
                existing.raw_score += raw
                existing.commits += c.commit_count
                if last > existing.last_active:
                    existing.last_active = last
                if existing.github_username is None and c.github_username:
                    existing.github_username = c.github_username
                if existing.association is None and assoc is not None:
                    existing.association = assoc

        if per_author:
            out[directory] = list(per_author.values())

    return out
