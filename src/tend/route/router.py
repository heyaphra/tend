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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from tend.github.client import GitHubClient
from tend.output.tend_yaml import OwnershipFile
from tend.route.matcher import find_owners_for_files

logger = logging.getLogger(__name__)

RoutingMode = Literal["assign", "request-review", "comment", "all"]
VALID_MODES: tuple[RoutingMode, ...] = ("assign", "request-review", "comment", "all")

# Sentinel key used in ``owners_by_file`` when fallback handles are routed.
# A real GitHub file path can never start with ``_`` because git paths are
# relative without leading-underscore directories in practice, and this is
# easy to detect in result inspection.
FALLBACK_FILES_KEY = "_fallback"


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
    # v0.3 additions
    fallback_used: bool = False
    effective_mode: RoutingMode | None = None
    cc_handles: list[str] = field(default_factory=list)


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
    # ---- v0.3 additions, all optional so existing callers/tests work ----
    is_bot_author: bool = False,
    extra_reviewers: Sequence[str] | None = None,
    fallback_handles: Sequence[str] | None = None,
) -> RoutingResult:
    """Dispatch the PR to the owners matched by ``ownership``.

    Algorithm:
      1. For each changed file, find the most specific matching rule via
         ``matcher.find_owner``.
      2. Aggregate all matched owners (deduplicated). If nothing matched
         and ``fallback_handles`` is set, route to the fallback.
      3. Append ``extra_reviewers`` (CC list) to the handle set — always
         additive; never replaces per-file owners.
      4. Compute ``effective_mode``: when ``is_bot_author=True`` and
         ``mode="assign"``, downgrade to ``request-review``. Bot PRs need
         an approver, not an assignee. Other modes are respected.
      5. Apply ``effective_mode``:
         - ``assign`` → POST /repos/.../issues/{n}/assignees
         - ``request-review`` → POST /repos/.../pulls/{n}/requested_reviewers
         - ``comment`` → POST /repos/.../issues/{n}/comments mentioning owners
         - ``all`` → all three
      6. Return a ``RoutingResult`` describing what happened.

    Note on dedupe: ``all_handles`` is a ``set[str]``. A PR touching
    ``package.json`` + ``package-lock.json`` (both matched by the same
    rule) produces exactly one entry per handle, so review-request POSTs
    are issued once, not per-file.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown routing mode {mode!r}; expected one of {VALID_MODES}")

    # Bot-author override: bot PRs default to request-review since "assign"
    # implies ownership of the work item, which doesn't apply to a bot.
    # Only "assign" mode is overridden — "comment", "request-review", and
    # "all" are explicit choices we respect.
    effective_mode: RoutingMode = "request-review" if (is_bot_author and mode == "assign") else mode

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

    # Fallback: nothing matched directly, but caller gave us a default.
    fallback_used = False
    if not all_handles and fallback_handles:
        fallback_used = True
        fb_handles = [h if h.startswith("@") else f"@{h}" for h in fallback_handles]
        all_handles.update(fb_handles)
        owners_by_file[FALLBACK_FILES_KEY] = list(fb_handles)
        # When fallback rescues the routing, the "skipped" list is no
        # longer accurate — those files are covered by the fallback.
        skipped = []

    # CC list: always additive. Even when no per-file owners matched and
    # no fallback fires, a non-empty CC ensures (e.g.) AppSec is reached
    # on a security-advisory PR with an unmapped path.
    cc_added: list[str] = []
    if extra_reviewers:
        cc_added = [h if h.startswith("@") else f"@{h}" for h in extra_reviewers]
        all_handles.update(cc_added)

    result = RoutingResult(
        owners_by_file=owners_by_file,
        skipped_files=skipped,
        dry_run=dry_run,
        fallback_used=fallback_used,
        effective_mode=effective_mode,
        cc_handles=list(cc_added),
    )
    if not all_handles:
        logger.info("No owners matched for PR #%s — nothing to route", pr_number)
        return result

    individuals, teams = _split_individuals_and_teams(all_handles)

    if effective_mode in ("assign", "all"):
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

    if effective_mode in ("request-review", "all"):
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

    if effective_mode in ("comment", "all"):
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
