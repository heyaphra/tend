"""Ownership inference orchestrator.

Pipeline (each step is a composable function in its own module):

1. ``scoring.score_per_directory`` — group commits by directory, drop
   sub-volume-floor directories, aggregate per contributor, apply
   association weights.
2. ``breadth.apply_breadth_penalty`` — divide each contributor's
   weighted score by ``sqrt(breadth)`` to suppress generalists.
3. ``_emit_rules`` (private) — compute Wilson lower bounds, apply
   ``min_commits`` / ``min_confidence`` thresholds, cap at
   ``max_owners_per_path``, emit ``InferredOwner`` records.
4. Depth filter.
5. Collapse to fixpoint (ancestor-absorbs-descendant + siblings-create-
   parent until stable).

The wildcard-conservativeness pass (``suppress_individual_wildcard``) is
a separate post-pass — the caller decides whether to apply it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace

from tend.analyze._models import (
    FileContribution,
    InferenceResult,
    InferredOwner,
    OwnerCandidate,
    ScoredContributor,
)
from tend.analyze.breadth import apply_breadth_penalty
from tend.analyze.collapse import (
    collapse,
    collapse_siblings,
    dir_to_pattern,
    pattern_depth,
)
from tend.analyze.confidence import wilson_lower, z_from_level
from tend.analyze.scoring import score_per_directory
from tend.analyze.team_substitution import substitute_teams
from tend.config import InferenceConfig
from tend.github.associations import Association
from tend.github.teams import TeamMembership

_PINNED_EVIDENCE_MARKER = "pinned via .tend/owners.yml"


def _canonicalize_handle_casing(
    contributions: list[FileContribution],
) -> list[FileContribution]:
    """Pick one display casing per GitHub handle so the same person renders
    identically across all paths.

    GitHub usernames are case-insensitive at resolution, but the same person's
    commits can carry different casings in our data — API-resolved logins use
    the user's preferred profile casing (``YuriiMotov``) while noreply-email
    extraction always produces lowercase (``yuriimotov``). Without a unifying
    pass, ``.tend/owners.yml`` can list both as separate owners on different
    paths and ``collapse._aggregate_owners`` (case-sensitive on
    ``github_username``) won't merge them.

    Strategy: most-frequent casing wins, ties broken by first-seen.
    """
    casing_counts: dict[str, Counter[str]] = defaultdict(Counter)
    first_seen: dict[str, str] = {}
    for c in contributions:
        if not c.github_username:
            continue
        key = c.github_username.lower()
        casing_counts[key][c.github_username] += 1
        first_seen.setdefault(key, c.github_username)

    canonical: dict[str, str] = {}
    for key, counts in casing_counts.items():
        max_count = max(counts.values())
        tied = [casing for casing, count in counts.items() if count == max_count]
        canonical[key] = first_seen[key] if first_seen[key] in tied else tied[0]

    out: list[FileContribution] = []
    for c in contributions:
        if c.github_username:
            preferred = canonical[c.github_username.lower()]
            if preferred != c.github_username:
                out.append(replace(c, github_username=preferred))
                continue
        out.append(c)
    return out


def _rule_sort_key(pattern: str) -> tuple[int, str]:
    return (0 if pattern == "*" else 1, pattern)


def _is_team_handle_owner(owner: OwnerCandidate) -> bool:
    """``org/team`` shape — GitHub individual usernames cannot contain ``/``."""
    return bool(owner.github_username) and "/" in owner.github_username


def _is_pinned(rule: InferredOwner) -> bool:
    return any(_PINNED_EVIDENCE_MARKER in ev for ev in (rule.evidence or []))


def suppress_individual_wildcard(rules: list[InferredOwner]) -> list[InferredOwner]:
    """Drop the wildcard ``*`` rule when its owners are all individuals.

    A wildcard CODEOWNERS-shaped rule applies to every file the rest of
    the file doesn't otherwise match. Proposing a single individual at
    that scope is almost always wrong (the dogfood example: a 200k-star
    repo getting ``* @one-person`` based on a few weeks of activity).

    Three carve-outs: a wildcard whose owners include any team handle is
    kept (the team anchors the claim); a wildcard that was explicitly
    pinned by the user is kept; otherwise drop.
    """
    out: list[InferredOwner] = []
    for r in rules:
        if (
            r.path_pattern == "*"
            and not _is_pinned(r)
            and not any(_is_team_handle_owner(o) for o in r.owners)
        ):
            continue
        out.append(r)
    return out


def _emit_rules(
    per_dir: dict[str, list[ScoredContributor]],
    config: InferenceConfig,
) -> tuple[list[InferredOwner], int, int, set[str]]:
    """Wilson-bound and threshold each directory's contributors; emit rules.

    Returns ``(rules, dropped_below_threshold, dropped_below_min_commits,
    all_owner_handles)``. The drop counters are diagnostic; the handle
    set is used by ``infer`` for the single-contributor-warning check.
    """
    z = z_from_level(config.confidence_level)
    rules: list[InferredOwner] = []
    dropped_below_threshold = 0
    dropped_below_min_commits = 0
    all_owners: set[str] = set()

    for directory, contributors in per_dir.items():
        total = sum(c.weighted_score for c in contributors)
        if total <= 0:
            continue

        candidates: list[OwnerCandidate] = []
        for c in contributors:
            share_lower = wilson_lower(c.weighted_score, total, z=z)
            if c.commits < config.min_commits:
                dropped_below_min_commits += 1
                continue
            if share_lower < config.min_confidence:
                dropped_below_threshold += 1
                continue
            if not c.github_username:
                continue
            candidates.append(
                OwnerCandidate(
                    email=c.email,
                    name=c.name,
                    github_username=c.github_username,
                    confidence=round(share_lower, 3),
                    commit_count=round(c.commits),
                    last_active=c.last_active,
                )
            )

        if not candidates:
            continue

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        candidates = candidates[: config.max_owners_per_path]
        for cand in candidates:
            all_owners.add(cand.github_username.lower())

        rules.append(
            InferredOwner(
                path_pattern=dir_to_pattern(directory),
                owners=candidates,
            )
        )

    return rules, dropped_below_threshold, dropped_below_min_commits, all_owners


def infer(
    contributions: list[FileContribution],
    config: InferenceConfig,
    associations: dict[str, Association] | None = None,
    teams: list[TeamMembership] | None = None,
) -> InferenceResult:
    """Run the inference pipeline against pre-filtered contributions.

    ``contributions`` should already have bots and unresolved-handle
    contributors removed (the ``commits.py`` fetcher does that).

    ``associations`` (optional) maps lowercased handles to their
    ``CommentAuthorAssociation`` in the repo. When present, each
    contributor's raw score is multiplied by the corresponding weight
    (MEMBER × 1.0, NONE × 0.1, etc.) before the Wilson bound is
    computed. When absent, scores are used unweighted — the pipeline
    degrades to the v0.1 behavior gracefully.

    ``teams`` (optional) is the org's team roster. When supplied and
    ``config.team_resolution_enabled`` is true, ``substitute_teams``
    runs between the depth filter and the collapse loop — placing it
    before collapse lets sibling directories owned by the same team
    fold into one team-owned parent.

    This function does no I/O.
    """
    contributions = _canonicalize_handle_casing(contributions)
    per_dir = score_per_directory(contributions, config, associations)
    per_dir = apply_breadth_penalty(per_dir, presence_threshold=config.breadth_share_threshold)
    rules, dropped_threshold, dropped_min_commits, all_owners = _emit_rules(per_dir, config)

    rules = [r for r in rules if pattern_depth(r.path_pattern) <= config.max_depth]
    if teams and config.team_resolution_enabled:
        rules = substitute_teams(
            rules,
            teams,
            max_team_size=config.max_team_size,
            min_team_share=config.min_team_share,
        )
    while True:
        before = len(rules)
        rules = collapse(rules, config)
        rules = collapse_siblings(rules, config)
        if len(rules) == before:
            break
    rules.sort(key=lambda r: _rule_sort_key(r.path_pattern))

    return InferenceResult(
        rules=rules,
        dropped_below_threshold=dropped_threshold,
        dropped_below_min_commits=dropped_min_commits,
        single_contributor_warning=(len(all_owners) <= 1 and len(rules) > 0),
    )
