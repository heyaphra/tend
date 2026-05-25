"""Replace individual owners with ``@org/team`` handles when justified.

Runs as a post-pass on inferred rules, after ``_emit_rules`` + depth filter
and before the collapse loop in ``infer()``. Substituting before collapse
lets sibling directories owned by the same team fold into one team-owned
parent via the existing ``collapse_siblings`` supermajority path.

Three substitution shapes, in priority order:

- **All-on-team** — every candidate sits in a single eligible team →
  replace owners with the team handle.
- **Strict majority** — one team holds ``> n/2`` of the candidates →
  team handle plus outlier individuals.
- **Multi-team** — no single majority but ≥2 teams each hold ≥2
  candidates → list the top two team handles.
- Otherwise the rule is returned unchanged.

A team is **eligible** when it has between 1 and ``max_team_size``
members (default 50). Larger groups (``@org/engineering``) are too broad
to encode ownership and are skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.github.teams import TeamMembership


def teams_by_handle(teams: list[TeamMembership]) -> dict[str, set[str]]:
    """Build the ``@org/team -> {members}`` mapping consumed by ``diff_codeowners``."""
    return {t.handle.lower(): t.members for t in teams}


def _eligible(teams: list[TeamMembership], max_team_size: int) -> list[TeamMembership]:
    return [t for t in teams if 0 < len(t.members) <= max_team_size]


def _team_owner(team: TeamMembership, confidence: float) -> OwnerCandidate:
    return OwnerCandidate(
        email="",
        name=team.handle,
        github_username=team.handle.lstrip("@"),  # "@org/slug" → "org/slug"
        confidence=confidence,
        commit_count=0,
        last_active=datetime.now(UTC),
    )


def substitute_teams(
    rules: list[InferredOwner],
    teams: list[TeamMembership],
    *,
    max_team_size: int,
    min_team_share: float,
) -> list[InferredOwner]:
    """Substitute team handles into rules whose top contributors share a team."""
    if not teams or not rules:
        return rules

    eligible = _eligible(teams, max_team_size)
    if not eligible:
        return rules

    out: list[InferredOwner] = []
    for rule in rules:
        candidate_logins = [o.github_username.lower() for o in rule.owners if o.github_username]
        candidate_set = set(candidate_logins)
        if not candidate_set:
            out.append(rule)
            continue

        team_hits: list[tuple[TeamMembership, set[str]]] = []
        for t in eligible:
            matched = candidate_set & t.members
            if matched:
                team_hits.append((t, matched))

        if not team_hits:
            out.append(rule)
            continue

        team_hits.sort(key=lambda x: -len(x[1]))
        top_team, top_matched = team_hits[0]
        n_candidates = len(candidate_set)
        max_conf = max(o.confidence for o in rule.owners)

        if len(top_matched) == n_candidates:
            out.append(
                InferredOwner(
                    path_pattern=rule.path_pattern,
                    owners=[_team_owner(top_team, max_conf)],
                    evidence=[*rule.evidence, *sorted(top_matched)],
                )
            )
            continue

        if len(top_matched) > n_candidates * min_team_share:
            outlier_logins = candidate_set - top_matched
            outlier_owners = [
                o
                for o in rule.owners
                if o.github_username and o.github_username.lower() in outlier_logins
            ]
            out.append(
                InferredOwner(
                    path_pattern=rule.path_pattern,
                    owners=[_team_owner(top_team, max_conf), *outlier_owners],
                    evidence=[
                        *rule.evidence,
                        *sorted(top_matched),
                        *(f"outlier: {o}" for o in sorted(outlier_logins)),
                    ],
                )
            )
            continue

        multi = [(t, m) for (t, m) in team_hits if len(m) >= 2]
        if len(multi) >= 2:
            out.append(
                InferredOwner(
                    path_pattern=rule.path_pattern,
                    owners=[_team_owner(t, max_conf) for (t, _) in multi[:2]],
                    evidence=[
                        *rule.evidence,
                        *sorted({u for (_, m) in multi[:2] for u in m}),
                    ],
                )
            )
            continue

        out.append(rule)

    return out


__all__ = ["substitute_teams", "teams_by_handle"]
