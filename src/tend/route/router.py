"""Route PRs (and, in v1.1, Dependabot alerts) to ``.tend/owners.yml`` owners.

v1 surface:

- ``route_pr`` — find owners for the PR's changed files via ``matcher``,
  then dispatch per the configured mode (assign / request-review /
  comment / all).

v1.1:

- ``route_dependabot_alert`` — planned. Raises ``NotImplementedError``
  in v1 so the CLI's ``tend route`` can detect the dependabot alert
  event type and log+exit cleanly rather than crash.

The router itself is async, doesn't touch the filesystem, and treats
the GitHub client as an injected dependency. Tests mock the client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from tend.github.client import GitHubClient
from tend.output.tend_yaml import OwnershipFile
from tend.route.matcher import find_owners_for_files

logger = logging.getLogger(__name__)

RoutingMode = Literal["assign", "request-review", "comment", "all"]
VALID_MODES: tuple[RoutingMode, ...] = ("assign", "request-review", "comment", "all")


@dataclass
class RoutingResult:
    """What the router actually did. ``dry_run=True`` populates the
    intent fields without making any write calls."""

    assigned: list[str] = field(default_factory=list)
    removed_assignees: list[str] = field(default_factory=list)
    reviewers_requested: list[str] = field(default_factory=list)
    team_reviewers_requested: list[str] = field(default_factory=list)
    comment_posted: bool = False
    owners_by_file: dict[str, list[str]] = field(default_factory=dict)
    skipped_files: list[str] = field(default_factory=list)  # no matching rule
    dry_run: bool = False


def _split_individuals_and_teams(handles: set[str]) -> tuple[list[str], list[str]]:
    """``@alice`` → individuals[]; ``@org/team`` → team_slugs[] (just ``team``)."""
    individuals: list[str] = []
    teams: list[str] = []
    for h in sorted(handles):
        bare = h.lstrip("@")
        if "/" in bare:
            _, _, team = bare.partition("/")
            teams.append(team)
        else:
            individuals.append(bare)
    return individuals, teams


def _format_comment(owners_by_file: dict[str, list[str]]) -> str:
    """Mention every owner once and list which files they're on the hook for."""
    by_owner: dict[str, list[str]] = {}
    for fp, owners in owners_by_file.items():
        for o in owners:
            by_owner.setdefault(o, []).append(fp)
    lines = [
        "Tend routed this PR based on `.tend/owners.yml`:",
        "",
    ]
    for owner in sorted(by_owner):
        files = by_owner[owner]
        lines.append(f"- {owner} owns:")
        for fp in sorted(files):
            lines.append(f"  - `{fp}`")
    lines.append("")
    lines.append(
        "_If this routing looks wrong, edit `.tend/owners.yml` — tend "
        "will pick up the change on the next analyze run._"
    )
    return "\n".join(lines)


async def route_pr(
    *,
    gh: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    changed_files: list[str],
    ownership: OwnershipFile,
    mode: RoutingMode,
    dry_run: bool = False,
) -> RoutingResult:
    """Dispatch the PR to the owners matched by ``ownership``.

    Algorithm:
      1. For each changed file, find the most specific matching rule via
         ``matcher.find_owner``.
      2. Aggregate all matched owners (deduplicated).
      3. Apply ``mode``:
         - ``assign`` → POST /repos/.../issues/{n}/assignees
         - ``request-review`` → POST /repos/.../pulls/{n}/requested_reviewers
         - ``comment`` → POST /repos/.../issues/{n}/comments mentioning owners
         - ``all`` → all three
      4. Return a ``RoutingResult`` describing what happened.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown routing mode {mode!r}; expected one of {VALID_MODES}")

    matches = find_owners_for_files(ownership, changed_files)
    owners_by_file: dict[str, list[str]] = {}
    all_handles: set[str] = set()
    skipped: list[str] = []
    for fp, rule in matches.items():
        if rule is None or not rule.owners:
            skipped.append(fp)
            continue
        handles = [
            o.github_username if o.github_username.startswith("@") else f"@{o.github_username}"
            for o in rule.owners
        ]
        owners_by_file[fp] = handles
        all_handles.update(handles)

    result = RoutingResult(owners_by_file=owners_by_file, skipped_files=skipped, dry_run=dry_run)
    if not all_handles:
        logger.info("No owners matched for PR #%s — nothing to route", pr_number)
        return result

    individuals, teams = _split_individuals_and_teams(all_handles)

    if mode in ("assign", "all"):
        result.assigned = list(individuals)
        tend_picks = set(individuals)
        pr = await gh.get_pr(owner, repo, pr_number)
        existing = {u["login"] for u in (pr.get("assignees") or []) if u.get("login")}
        to_remove = sorted(existing - tend_picks)
        result.removed_assignees = to_remove
        if not dry_run:
            if to_remove:
                await gh.remove_assignees(owner, repo, pr_number, to_remove)
            if individuals:
                await gh.add_assignees(owner, repo, pr_number, individuals)

    if mode in ("request-review", "all"):
        result.reviewers_requested = list(individuals)
        result.team_reviewers_requested = list(teams)
        if not dry_run and (individuals or teams):
            await gh.request_reviewers(
                owner,
                repo,
                pr_number,
                reviewers=individuals or None,
                team_reviewers=teams or None,
            )

    if mode in ("comment", "all"):
        result.comment_posted = True
        if not dry_run:
            await gh.post_issue_comment(owner, repo, pr_number, _format_comment(owners_by_file))

    return result


def route_dependabot_alert(*_args: object, **_kwargs: object) -> RoutingResult:
    """Dependabot alert routing — planned for v1.1.

    v1 does not route alerts. When v1.1 lands this will:

    1. Parse the affected manifest path from the alert payload.
    2. Find the owner via the same longest-prefix match used by ``route_pr``.
    3. Create a GitHub issue assigned to the owner, linking back to the alert.
    4. Optionally comment on the underlying Dependabot PR if one exists.

    The ``tend route`` CLI detects ``dependabot_alert`` events and logs
    the payload to stderr rather than calling this function, so customers
    get a clear "not yet supported" message instead of an exception. We
    raise here defensively in case future code wires up the call.
    """
    raise NotImplementedError(
        "Dependabot alert routing is planned for v1.1. "
        "v1 routes PRs (including Dependabot's auto-fix PRs), which covers "
        "most vulnerability remediation flows. See docs/configuration.md."
    )


__all__ = [
    "VALID_MODES",
    "RoutingMode",
    "RoutingResult",
    "route_dependabot_alert",
    "route_pr",
]
