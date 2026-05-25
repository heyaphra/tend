"""Fetch and weight contributors by GitHub ``CommentAuthorAssociation``.

GitHub annotates every commit / comment / review author with an
``authorAssociation`` enum indicating the contributor's relationship to
the repo at the time of the action. ``MEMBER`` and ``COLLABORATOR``
signal "trusted insider"; ``FIRST_TIME_CONTRIBUTOR`` and ``NONE`` signal
"drive-by". Tend uses this as a free, no-extra-data signal to
de-weight external contributors when computing ownership.

Fetch is via GraphQL — one paginated query per repo returns the author
of each of the most recent N pull requests. Contributors not seen in
that window get ``Association.UNKNOWN`` (weight 0.5, a neutral midpoint).

The weighting is applied as a multiplier on the per-(file, author) raw
score before Wilson confidence bounds are computed. A ``FIRST_TIME``
contributor's score gets multiplied by 0.1; their Wilson lower bound
then naturally falls below the inclusion threshold.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tend.github.client import GitHubClient

logger = logging.getLogger(__name__)


class Association(StrEnum):
    OWNER = "OWNER"
    MEMBER = "MEMBER"
    COLLABORATOR = "COLLABORATOR"
    CONTRIBUTOR = "CONTRIBUTOR"
    FIRST_TIME_CONTRIBUTOR = "FIRST_TIME_CONTRIBUTOR"
    FIRST_TIMER = "FIRST_TIMER"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"  # sentinel for handles we couldn't determine


# Higher value = more trusted insider, more ownership weight.
ASSOCIATION_WEIGHTS: dict[Association, float] = {
    Association.OWNER: 1.0,
    Association.MEMBER: 1.0,
    Association.COLLABORATOR: 0.7,
    Association.CONTRIBUTOR: 0.4,
    Association.FIRST_TIME_CONTRIBUTOR: 0.1,
    Association.FIRST_TIMER: 0.1,
    Association.NONE: 0.1,
    Association.UNKNOWN: 0.5,
}

# Strength order: when a handle is observed with multiple associations
# (e.g. NONE in one PR, then MEMBER in another), keep the strongest.
_STRENGTH_ORDER = (
    Association.OWNER,
    Association.MEMBER,
    Association.COLLABORATOR,
    Association.CONTRIBUTOR,
    Association.UNKNOWN,
    Association.FIRST_TIME_CONTRIBUTOR,
    Association.FIRST_TIMER,
    Association.NONE,
)
_STRENGTH_RANK = {a: i for i, a in enumerate(_STRENGTH_ORDER)}


def stronger(a: Association, b: Association) -> Association:
    """Return the stronger of two associations."""
    return a if _STRENGTH_RANK[a] <= _STRENGTH_RANK[b] else b


_QUERY = """
query AssociationsForRepo($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        author { login }
        authorAssociation
      }
    }
  }
}
""".strip()


async def fetch_associations(
    gh: GitHubClient,
    owner: str,
    repo: str,
    *,
    max_prs: int = 500,
) -> dict[str, Association]:
    """For each PR author seen in the last ``max_prs`` PRs, return their
    strongest observed ``authorAssociation``.

    Returns ``{handle: Association}``. Empty dict on GraphQL failure or
    insufficient token scope — the caller should treat unknown handles
    as ``Association.UNKNOWN`` (weight 0.5, neutral midpoint) and the
    pipeline behaves correctly without this data.
    """
    from tend.github.graphql import graphql_paginate  # local import — avoids cycle

    out: dict[str, Association] = {}
    try:
        async for node in graphql_paginate(
            gh,
            _QUERY,
            variables={"owner": owner, "name": repo},
            data_path=("repository", "pullRequests"),
            max_items=max_prs,
        ):
            author = node.get("author") or {}
            login = author.get("login") if isinstance(author, dict) else None
            assoc_raw = node.get("authorAssociation")
            if not login or not assoc_raw:
                continue
            try:
                assoc = Association(assoc_raw)
            except ValueError:
                logger.debug("Unknown authorAssociation %r; treating as UNKNOWN", assoc_raw)
                assoc = Association.UNKNOWN
            existing = out.get(login.lower())
            out[login.lower()] = stronger(existing, assoc) if existing else assoc
    except Exception as exc:
        logger.warning(
            "Failed to fetch authorAssociations for %s/%s (%s); "
            "every contributor will get the neutral UNKNOWN weight",
            owner,
            repo,
            exc,
        )
        return {}
    return out


def association_weight(
    assoc: Association | None,
    weights: dict[Association, float] | None = None,
) -> float:
    """Look up the weight for an association, defaulting to UNKNOWN's neutral 0.5."""
    table = weights or ASSOCIATION_WEIGHTS
    if assoc is None:
        return table.get(Association.UNKNOWN, 0.5)
    return table.get(assoc, table.get(Association.UNKNOWN, 0.5))


__all__ = [
    "ASSOCIATION_WEIGHTS",
    "Association",
    "association_weight",
    "fetch_associations",
    "stronger",
]
