"""Tests for the PR router and the v1.1 NotImplementedError stub."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pytest_httpx import HTTPXMock

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.github.auth import token_auth
from tend.github.client import GitHubClient
from tend.output.tend_yaml import OwnershipFile
from tend.route.router import (
    VALID_MODES,
    route_dependabot_alert,
    route_pr,
)

WHEN = datetime(2026, 5, 1, tzinfo=UTC)


def _rule(pattern: str, *handles: str) -> InferredOwner:
    return InferredOwner(
        path_pattern=pattern,
        owners=[
            OwnerCandidate(
                email=f"{h}@x.com",
                name=h,
                github_username=h,
                confidence=0.7,
                commit_count=10,
                last_active=WHEN,
            )
            for h in handles
        ],
    )


def _file(rules: list[InferredOwner]) -> OwnershipFile:
    return OwnershipFile(version=1, generated_at=None, config={}, paths=rules)


@pytest.mark.asyncio
async def test_dry_run_does_not_issue_write_calls(httpx_mock: HTTPXMock):
    """Dry-run reads existing assignees but never writes (no POST/DELETE)."""
    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/widgets/pulls/42",
        json={"assignees": []},
    )
    ownership = _file([_rule("/src/", "alice")])
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["src/auth.py"],
            ownership=ownership,
            mode="assign",
            dry_run=True,
        )
    assert result.dry_run is True
    assert result.assigned == ["alice"]
    assert result.owners_by_file == {"src/auth.py": ["@alice"]}
    # Only the GET happened — no POST/DELETE.
    assert all(r.method == "GET" for r in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_assign_calls_add_assignees(httpx_mock: HTTPXMock):
    ownership = _file([_rule("/src/", "alice")])
    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/repos/acme/widgets/pulls/42",
        json={"assignees": []},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/issues/42/assignees",
        json={},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["src/auth.py"],
            ownership=ownership,
            mode="assign",
        )
    assert result.assigned == ["alice"]
    assert result.removed_assignees == []
    sent = httpx_mock.get_request(method="POST")
    import json

    assert json.loads(sent.content) == {"assignees": ["alice"]}


@pytest.mark.asyncio
async def test_request_review_splits_individuals_and_teams(httpx_mock: HTTPXMock):
    ownership = _file([_rule("/src/", "alice", "acme/backend")])
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/pulls/42/requested_reviewers",
        json={},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["src/auth.py"],
            ownership=ownership,
            mode="request-review",
        )
    assert result.reviewers_requested == ["alice"]
    assert result.team_reviewers_requested == ["backend"]


@pytest.mark.asyncio
async def test_comment_mentions_owners(httpx_mock: HTTPXMock):
    ownership = _file(
        [
            _rule("/src/", "alice"),
            _rule("/docs/", "bob"),
        ]
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/issues/42/comments",
        json={},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["src/auth.py", "docs/intro.md"],
            ownership=ownership,
            mode="comment",
        )
    assert result.comment_posted is True
    sent = httpx_mock.get_request()
    body_text = sent.content.decode("utf-8")
    assert "@alice" in body_text
    assert "@bob" in body_text
    assert "src/auth.py" in body_text


@pytest.mark.asyncio
async def test_all_mode_dispatches_all_three_actions(httpx_mock: HTTPXMock):
    ownership = _file([_rule("/src/", "alice")])
    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/repos/acme/widgets/pulls/42",
        json={"assignees": []},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/issues/42/assignees",
        json={},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/pulls/42/requested_reviewers",
        json={},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/issues/42/comments",
        json={},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["src/auth.py"],
            ownership=ownership,
            mode="all",
        )
    assert result.assigned == ["alice"]
    assert result.reviewers_requested == ["alice"]
    assert result.comment_posted is True


@pytest.mark.asyncio
async def test_skipped_files_listed_when_no_match():
    ownership = _file([_rule("/src/", "alice")])
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["docs/intro.md"],
            ownership=ownership,
            mode="assign",
            dry_run=True,
        )
    assert result.skipped_files == ["docs/intro.md"]
    assert result.assigned == []


@pytest.mark.asyncio
async def test_invalid_mode_raises():
    ownership = _file([_rule("/src/", "alice")])
    async with GitHubClient(auth=token_auth("t")) as gh:
        with pytest.raises(ValueError, match="Unknown routing mode"):
            await route_pr(
                gh=gh,
                owner="acme",
                repo="widgets",
                pr_number=42,
                changed_files=["src/auth.py"],
                ownership=ownership,
                mode="bogus",  # type: ignore[arg-type]
            )


def test_dependabot_alert_raises_not_implemented():
    with pytest.raises(NotImplementedError, match=r"v1\.1"):
        route_dependabot_alert({})


def test_valid_modes_exposes_all_supported():
    assert set(VALID_MODES) == {"assign", "request-review", "comment", "all"}


@pytest.mark.asyncio
async def test_assign_mode_removes_pre_existing_assignees_not_in_tend_picks(
    httpx_mock: HTTPXMock,
):
    """Pre-existing assignees not in tend's picks are removed; overlaps are preserved."""
    ownership = _file([_rule("/src/", "alice", "bob")])
    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/repos/acme/widgets/pulls/42",
        json={"assignees": [{"login": "octocat"}, {"login": "alice"}]},
    )
    httpx_mock.add_response(
        method="DELETE",
        url="https://api.github.com/repos/acme/widgets/issues/42/assignees",
        json={},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/issues/42/assignees",
        json={},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["src/auth.py"],
            ownership=ownership,
            mode="assign",
        )
    assert result.assigned == ["alice", "bob"]
    assert result.removed_assignees == ["octocat"]

    import json

    delete_req = httpx_mock.get_request(method="DELETE")
    assert json.loads(delete_req.content) == {"assignees": ["octocat"]}
    post_req = httpx_mock.get_request(method="POST")
    assert json.loads(post_req.content) == {"assignees": ["alice", "bob"]}


@pytest.mark.asyncio
async def test_assign_mode_dry_run_populates_intent_without_writing(httpx_mock: HTTPXMock):
    """Dry-run reads existing assignees and surfaces what would change, but writes nothing."""
    ownership = _file([_rule("/src/", "alice", "bob")])
    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/repos/acme/widgets/pulls/42",
        json={"assignees": [{"login": "octocat"}, {"login": "alice"}]},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["src/auth.py"],
            ownership=ownership,
            mode="assign",
            dry_run=True,
        )
    assert result.assigned == ["alice", "bob"]
    assert result.removed_assignees == ["octocat"]
    # No DELETE/POST happened.
    assert all(r.method == "GET" for r in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_assign_mode_no_pre_existing_assignees_skips_remove(httpx_mock: HTTPXMock):
    """No existing assignees → only add_assignees fires, no DELETE."""
    ownership = _file([_rule("/src/", "alice")])
    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/repos/acme/widgets/pulls/42",
        json={"assignees": []},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/issues/42/assignees",
        json={},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["src/auth.py"],
            ownership=ownership,
            mode="assign",
        )
    assert result.assigned == ["alice"]
    assert result.removed_assignees == []
    # DELETE was not issued.
    methods = [r.method for r in httpx_mock.get_requests()]
    assert "DELETE" not in methods


@pytest.mark.asyncio
async def test_request_review_mode_does_not_call_get_pr(httpx_mock: HTTPXMock):
    """request-review mode is reviewer-only and never fetches the PR for assignees."""
    ownership = _file([_rule("/src/", "alice")])
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/pulls/42/requested_reviewers",
        json={},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await route_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            pr_number=42,
            changed_files=["src/auth.py"],
            ownership=ownership,
            mode="request-review",
        )
    assert result.reviewers_requested == ["alice"]
    # The PR-fetch (GET /pulls/{n}) never happened.
    paths = [r.url.path for r in httpx_mock.get_requests()]
    assert "/repos/acme/widgets/pulls/42" not in paths
