"""Team-substitution algorithm + fetch_org_teams transport tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pytest_httpx import HTTPXMock

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.analyze.team_substitution import substitute_teams, teams_by_handle
from tend.github.auth import token_auth
from tend.github.client import GitHubClient
from tend.github.teams import MAX_TEAM_SIZE, TeamMembership, fetch_org_teams

NOW = datetime.now(UTC)


def _oc(login: str, conf: float = 0.7) -> OwnerCandidate:
    return OwnerCandidate(
        email=f"{login}@x.com",
        name=login,
        github_username=login,
        confidence=conf,
        commit_count=10,
        last_active=NOW,
    )


def _team(slug: str, *members: str, org: str = "acme") -> TeamMembership:
    return TeamMembership(
        team_slug=slug,
        team_name=slug,
        org=org,
        handle=f"@{org}/{slug}",
        members=set(members),
    )


def _subst(rules: list[InferredOwner], teams: list[TeamMembership]) -> list[InferredOwner]:
    return substitute_teams(rules, teams, max_team_size=MAX_TEAM_SIZE, min_team_share=0.5)


# -------- substitute_teams: algorithm --------


def test_all_contributors_on_same_team_collapses_to_team_handle():
    teams = [_team("backend", "alice", "bob")]
    rule = InferredOwner("/api/", [_oc("alice", 0.8), _oc("bob", 0.6)])
    out = _subst([rule], teams)
    assert [o.github_username for o in out[0].owners] == ["acme/backend"]
    assert "alice" in out[0].evidence
    assert "bob" in out[0].evidence


def test_strict_majority_keeps_outliers_as_individuals():
    teams = [_team("backend", "alice", "bob")]
    rule = InferredOwner(
        "/api/",
        [_oc("alice", 0.8), _oc("bob", 0.6), _oc("drive-by", 0.3)],
    )
    out = _subst([rule], teams)
    handles = [o.github_username for o in out[0].owners]
    assert "acme/backend" in handles
    assert "drive-by" in handles
    assert any("outlier" in e for e in out[0].evidence)


def test_contributor_not_on_any_team_kept_as_individual():
    teams = [_team("backend", "alice")]
    rule = InferredOwner("/scripts/", [_oc("solo", 0.8)])
    out = _subst([rule], teams)
    assert [o.github_username for o in out[0].owners] == ["solo"]


def test_oversize_team_is_skipped():
    big = _team("eng", *(f"p{i}" for i in range(MAX_TEAM_SIZE + 5)))
    rule = InferredOwner("/api/", [_oc("p1", 0.7), _oc("p2", 0.3)])
    out = _subst([rule], [big])
    assert [o.github_username for o in out[0].owners] == ["p1", "p2"]


def test_empty_team_list_passes_through_unchanged():
    rule = InferredOwner("/api/", [_oc("alice", 0.7)])
    out = _subst([rule], [])
    assert [o.github_username for o in out[0].owners] == ["alice"]


def test_multi_team_lists_both_when_each_has_two_contributors():
    team_a = _team("a", "alice", "bob")
    team_b = _team("b", "carol", "dave")
    rule = InferredOwner(
        "/x/",
        [_oc("alice", 0.5), _oc("bob", 0.4), _oc("carol", 0.3), _oc("dave", 0.2)],
    )
    out = _subst([rule], [team_a, team_b])
    handles = sorted(o.github_username for o in out[0].owners)
    assert "acme/a" in handles
    assert "acme/b" in handles


def test_teams_by_handle_lowercases_keys():
    t = _team("Backend", "Alice")
    t.handle = "@Acme/Backend"
    mapping = teams_by_handle([t])
    assert "@acme/backend" in mapping
    assert mapping["@acme/backend"] == {"Alice"}


# -------- fetch_org_teams: transport --------


@pytest.mark.asyncio
async def test_fetch_org_teams_happy_path_lowercases_members(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams?per_page=100",
        json=[
            {"slug": "backend", "name": "Backend"},
            {"slug": "frontend", "name": "Frontend"},
        ],
    )
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams/backend/members?per_page=100",
        json=[{"login": "Alice"}, {"login": "bob"}],
    )
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams/frontend/members?per_page=100",
        json=[{"login": "Carol"}],
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        teams = await fetch_org_teams(gh, "acme")

    by_slug = {t.team_slug: t for t in teams}
    assert set(by_slug) == {"backend", "frontend"}
    assert by_slug["backend"].handle == "@acme/backend"
    assert by_slug["backend"].members == {"alice", "bob"}
    assert by_slug["frontend"].members == {"carol"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_fetch_org_teams_returns_empty_on_listing_denied(
    httpx_mock: HTTPXMock, status: int
):
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams?per_page=100",
        status_code=status,
        json={"message": "nope"},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        teams = await fetch_org_teams(gh, "acme")
    assert teams == []


@pytest.mark.asyncio
async def test_fetch_org_teams_drops_team_when_member_fetch_forbidden(
    httpx_mock: HTTPXMock,
):
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams?per_page=100",
        json=[
            {"slug": "backend", "name": "Backend"},
            {"slug": "secret", "name": "Secret"},
        ],
    )
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams/backend/members?per_page=100",
        json=[{"login": "alice"}],
    )
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams/secret/members?per_page=100",
        status_code=403,
        json={"message": "forbidden"},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        teams = await fetch_org_teams(gh, "acme")
    assert [t.team_slug for t in teams] == ["backend"]


@pytest.mark.asyncio
async def test_fetch_org_teams_skips_entries_without_slug(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams?per_page=100",
        json=[{"slug": "backend", "name": "Backend"}, {"name": "Slugless"}],
    )
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams/backend/members?per_page=100",
        json=[{"login": "alice"}],
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        teams = await fetch_org_teams(gh, "acme")
    assert [t.team_slug for t in teams] == ["backend"]


@pytest.mark.asyncio
async def test_fetch_org_teams_empty_org_returns_empty_list(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.github.com/orgs/acme/teams?per_page=100",
        json=[],
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        teams = await fetch_org_teams(gh, "acme")
    assert teams == []
