"""Async GitHub REST client.

Bounded concurrency via ``self.semaphore`` for fan-out fetches (commit
details, team members). Callers should use ``async with self.semaphore``
around concurrent requests — secondary rate limits trigger on volume of
in-flight requests, not just the primary quota.

HTTP/2 is enabled by default. The prototype shipped an ``hishel``-based
response cache that's been intentionally removed for v1 simplicity; if
quota becomes a problem in dogfooding we can revisit.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from tend.github.auth import DEFAULT_API_BASE, Auth, get_auth_from_env

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 10
RATE_LIMIT_FLOOR = 100
MAX_RATE_LIMIT_SLEEP = 300


@dataclass
class RateLimitState:
    remaining: int = 5000
    reset_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def update_from_headers(self, headers: httpx.Headers) -> None:
        if "x-ratelimit-remaining" in headers:
            with contextlib.suppress(ValueError):
                self.remaining = int(headers["x-ratelimit-remaining"])
        if "x-ratelimit-reset" in headers:
            with contextlib.suppress(ValueError):
                self.reset_at = datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), tz=UTC)

    async def wait_if_needed(self) -> None:
        if self.remaining >= RATE_LIMIT_FLOOR:
            return
        wait = (self.reset_at - datetime.now(UTC)).total_seconds()
        if wait <= 0:
            # Window has elapsed but no fresh response has refilled local state.
            # Optimistically reset and let the next request's headers correct us.
            self.remaining = 5000
            return
        logger.warning("Rate limit low (%d); sleeping %.0fs", self.remaining, wait)
        await asyncio.sleep(min(wait + 1, MAX_RATE_LIMIT_SLEEP))


class GitHubClient:
    """Async GitHub REST client.

    Use as::

        async with GitHubClient(auth=token_auth(token)) as gh:
            commits = await gh.get_commits("octocat", "hello-world")
    """

    def __init__(
        self,
        auth: Auth | None = None,
        client: httpx.AsyncClient | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        api_base: str = DEFAULT_API_BASE,
    ) -> None:
        self.auth = auth or get_auth_from_env()
        self.rate_limit = RateLimitState()
        self._client = client
        self._owns_client = client is None
        self._api_base = api_base
        self.semaphore = asyncio.Semaphore(concurrency)

    async def __aenter__(self) -> GitHubClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._api_base,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
                http2=True,
            )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        await self.rate_limit.wait_if_needed()
        if self._client is None:
            raise RuntimeError("GitHubClient used outside its `async with` context")
        auth_headers = await self.auth.headers()
        headers = {**kwargs.pop("headers", {}), **auth_headers}

        last_resp: httpx.Response | None = None
        for attempt in range(3):
            resp = await self._client.request(method, url, headers=headers, **kwargs)
            self.rate_limit.update_from_headers(resp.headers)
            last_resp = resp

            if 200 <= resp.status_code < 300:
                return resp
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                await self.rate_limit.wait_if_needed()
                continue
            if resp.status_code >= 500:
                wait = 2**attempt
                logger.warning("GitHub %d, retrying in %ds", resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()

        assert last_resp is not None
        last_resp.raise_for_status()
        return last_resp  # unreachable

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._request("DELETE", url, **kwargs)

    async def paginate(
        self, url: str, params: dict[str, Any] | None = None, max_pages: int = 50
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        results: list[dict[str, Any]] = []

        for _ in range(max_pages):
            resp = await self.get(url, params=params)
            data = resp.json()
            if isinstance(data, list):
                results.extend(data)
            else:
                return [data]

            link = resp.headers.get("link", "")
            if 'rel="next"' not in link:
                break
            next_url: str | None = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
                    break
            if not next_url:
                break
            url = next_url
            # Must be None, not {}: httpx treats params={} as "replace URL query
            # with empty", which strips the ?page=N the Link header gave us.
            params = None  # type: ignore[assignment]

        return results

    # ---- Read methods ----

    async def get_default_branch(self, owner: str, repo: str) -> str:
        resp = await self.get(f"/repos/{owner}/{repo}")
        return resp.json()["default_branch"]

    async def get_file_content(self, owner: str, repo: str, path: str) -> str | None:
        try:
            resp = await self.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": "HEAD"})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        data = resp.json()
        if isinstance(data, list):
            return None
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return data.get("content", "")

    async def get_file_sha(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        try:
            resp = await self.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        data = resp.json()
        if isinstance(data, list):
            return None
        return data.get("sha")

    async def get_commits(
        self,
        owner: str,
        repo: str,
        since: datetime | None = None,
        path: str | None = None,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if since:
            params["since"] = since.isoformat()
        if path:
            params["path"] = path
        return await self.paginate(
            f"/repos/{owner}/{repo}/commits", params=params, max_pages=max_pages
        )

    async def get_commit_detail(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        async with self.semaphore:
            resp = await self.get(f"/repos/{owner}/{repo}/commits/{sha}")
        return resp.json()

    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return await self.paginate(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")

    async def get_pr(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        resp = await self.get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
        return resp.json()

    async def get_org_teams(self, org: str) -> list[dict[str, Any]]:
        return await self.paginate(f"/orgs/{org}/teams")

    async def get_team_members(self, org: str, slug: str) -> list[dict[str, Any]]:
        async with self.semaphore:
            return await self.paginate(f"/orgs/{org}/teams/{slug}/members")

    # ---- Write methods (branch / file / PR) ----

    async def get_ref(self, owner: str, repo: str, ref: str) -> dict[str, Any] | None:
        try:
            resp = await self.get(f"/repos/{owner}/{repo}/git/ref/{ref}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return resp.json()

    async def create_branch(self, owner: str, repo: str, branch: str, from_sha: str) -> None:
        await self.post(
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": from_sha},
        )

    async def update_branch(
        self, owner: str, repo: str, branch: str, sha: str, force: bool = True
    ) -> None:
        await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
            json={"sha": sha, "force": force},
        )

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        resp = await self.put(f"/repos/{owner}/{repo}/contents/{path}", json=payload)
        return resp.json()

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        resp = await self.post(
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        return resp.json()

    async def list_open_pulls(
        self, owner: str, repo: str, head: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": "open"}
        if head:
            params["head"] = head
        return await self.paginate(f"/repos/{owner}/{repo}/pulls", params=params)

    # ---- PR routing helpers ----

    async def add_assignees(
        self, owner: str, repo: str, pr_number: int, assignees: list[str]
    ) -> dict[str, Any]:
        resp = await self.post(
            f"/repos/{owner}/{repo}/issues/{pr_number}/assignees",
            json={"assignees": assignees},
        )
        return resp.json()

    async def remove_assignees(
        self, owner: str, repo: str, pr_number: int, assignees: list[str]
    ) -> dict[str, Any]:
        resp = await self.delete(
            f"/repos/{owner}/{repo}/issues/{pr_number}/assignees",
            json={"assignees": assignees},
        )
        return resp.json()

    async def request_reviewers(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        reviewers: list[str] | None = None,
        team_reviewers: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if reviewers:
            payload["reviewers"] = reviewers
        if team_reviewers:
            payload["team_reviewers"] = team_reviewers
        resp = await self.post(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
            json=payload,
        )
        return resp.json()

    async def post_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict[str, Any]:
        resp = await self.post(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return resp.json()

    async def list_issue_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> list[dict[str, Any]]:
        """List comments on an issue/PR. Used by the SLA nudge bot to
        check for prior tend-nudge markers (idempotency)."""
        return await self.paginate(f"/repos/{owner}/{repo}/issues/{issue_number}/comments")
