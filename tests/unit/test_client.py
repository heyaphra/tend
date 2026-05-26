"""GitHub client tests — auth header injection, pagination, rate limiting.

All HTTP is mocked via pytest-httpx; no test hits real GitHub.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pytest_httpx import HTTPXMock

from tend.github.auth import token_auth
from tend.github.client import GitHubClient, RateLimitState


@pytest.mark.asyncio
async def test_client_injects_auth_header(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.github.com/repos/octocat/hello/contents/README.md?ref=HEAD",
        json={"encoding": "base64", "content": "aGVsbG8="},  # "hello"
    )
    async with GitHubClient(auth=token_auth("test-token")) as gh:
        content = await gh.get_file_content("octocat", "hello", "README.md")

    assert content == "hello"
    sent = httpx_mock.get_request()
    assert sent.headers["Authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_paginate_follows_link_headers(httpx_mock: HTTPXMock):
    page1 = [{"sha": f"sha{i}"} for i in range(100)]
    page2 = [{"sha": f"sha{i}"} for i in range(100, 150)]
    httpx_mock.add_response(
        url="https://api.github.com/repos/o/r/commits?per_page=100",
        json=page1,
        headers={
            "Link": '<https://api.github.com/repos/o/r/commits?page=2&per_page=100>; rel="next"',
        },
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/o/r/commits?page=2&per_page=100",
        json=page2,
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        results = await gh.paginate("/repos/o/r/commits")

    assert len(results) == 150


@pytest.mark.asyncio
async def test_paginate_stops_when_no_next_link(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.github.com/repos/o/r/commits?per_page=100",
        json=[{"sha": "abc"}],
        # No Link header.
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        results = await gh.paginate("/repos/o/r/commits")
    assert len(results) == 1


def test_rate_limit_state_updates_from_headers():
    state = RateLimitState()
    headers = httpx.Headers(
        {
            "x-ratelimit-remaining": "42",
            "x-ratelimit-reset": "9999999999",
        }
    )
    state.update_from_headers(headers)
    assert state.remaining == 42
    assert state.reset_at.tzinfo is not None


@pytest.mark.asyncio
async def test_rate_limit_does_not_sleep_above_floor():
    """Above 100 remaining there's no need to sleep — verify wait_if_needed is a no-op."""
    state = RateLimitState(remaining=500, reset_at=datetime.now(UTC) + timedelta(hours=1))
    await state.wait_if_needed()  # would deadlock on real sleep if not no-op


@pytest.mark.asyncio
async def test_client_retries_on_500(httpx_mock: HTTPXMock):
    """First request returns 500, retry returns 200. Verifies the retry loop."""
    httpx_mock.add_response(
        url="https://api.github.com/repos/o/r",
        status_code=500,
        text="boom",
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/o/r",
        json={"default_branch": "main"},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        branch = await gh.get_default_branch("o", "r")
    assert branch == "main"


@pytest.mark.asyncio
async def test_get_file_content_returns_none_on_404(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.github.com/repos/o/r/contents/missing.md?ref=HEAD",
        status_code=404,
        json={"message": "Not Found"},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        content = await gh.get_file_content("o", "r", "missing.md")
    assert content is None


@pytest.mark.asyncio
async def test_client_follows_301_redirect(httpx_mock: HTTPXMock):
    """Renamed/transferred repos return 301 to the new location — follow it."""
    httpx_mock.add_response(
        url="https://api.github.com/repos/tiangolo/fastapi",
        status_code=301,
        headers={"Location": "https://api.github.com/repos/fastapi/fastapi"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/fastapi/fastapi",
        json={"default_branch": "master"},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        branch = await gh.get_default_branch("tiangolo", "fastapi")
    assert branch == "master"
