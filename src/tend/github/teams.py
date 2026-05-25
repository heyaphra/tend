"""Fetch GitHub org teams + members for team-default ownership inference.

In-memory only for the lifetime of one CLI invocation. Stale on-disk team
membership would silently confirm ownership that's actually drifted — that's
the opposite of what tend exists to do, so no caching layer here.

Cost: one REST request for the team list plus one per team for members.
For a 100-team org averaging ~8 members, that's ~101 calls bounded by
``GitHubClient.semaphore``. On 403/404 (token lacks ``members:read``) we
log a warning and return ``[]`` so the caller falls through to individual-
handle inference, mirroring ``fetch_associations``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from tend.github.client import GitHubClient

logger = logging.getLogger(__name__)

MAX_TEAM_SIZE = 50


@dataclass
class TeamMembership:
    team_slug: str
    team_name: str
    org: str
    handle: str  # e.g. "@acme/backend"
    members: set[str] = field(default_factory=set)  # lowercase GitHub usernames

    def has(self, username: str) -> bool:
        return username.lower() in self.members


async def fetch_org_teams(gh: GitHubClient, org: str) -> list[TeamMembership]:
    """Fetch all teams in ``org`` and their members. Returns ``[]`` on 403/404.

    Returns an empty list on insufficient token scope so the caller falls
    back to individual-handle inference — same pattern as
    ``fetch_associations``.
    """
    try:
        team_dicts = await gh.get_org_teams(org)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            logger.warning(
                "Team resolution unavailable for %s (HTTP %d); "
                "token may lack members:read scope. Falling back to individual handles.",
                org,
                exc.response.status_code,
            )
            return []
        raise

    async def _one(team: dict[str, Any]) -> TeamMembership | None:
        slug = team.get("slug")
        if not slug:
            return None
        try:
            member_dicts = await gh.get_team_members(org, slug)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 404):
                return None
            raise
        members = {m["login"].lower() for m in member_dicts if m.get("login")}
        return TeamMembership(
            team_slug=slug,
            team_name=team.get("name", slug),
            org=org,
            handle=f"@{org}/{slug}",
            members=members,
        )

    fetched = await asyncio.gather(*(_one(t) for t in team_dicts))
    return [t for t in fetched if t is not None]


__all__ = ["MAX_TEAM_SIZE", "TeamMembership", "fetch_org_teams"]
