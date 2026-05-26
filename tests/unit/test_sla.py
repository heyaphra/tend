"""Tests for ``tend.route.sla`` — aging report + nudge bot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.github.auth import token_auth
from tend.github.client import GitHubClient
from tend.output.tend_yaml import OwnershipFile
from tend.route.sla import (
    NUDGE_MARKER_PREFIX,
    AgingPR,
    AgingReport,
    collect_aging_prs,
    post_nudges,
    render_markdown,
)

WHEN = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


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
                last_active=WHEN - timedelta(days=1),
            )
            for h in handles
        ],
    )


def _ownership(*rules: InferredOwner) -> OwnershipFile:
    return OwnershipFile(version=1, generated_at=None, config={}, paths=list(rules))


def _pr(
    *,
    number: int,
    author: str = "dependabot[bot]",
    title: str = "Bump foo from 1 to 2",
    labels: list[dict[str, str]] | None = None,
    created_at_offset: timedelta = timedelta(0),
) -> dict[str, Any]:
    """Build a list_open_pulls-style PR dict whose created_at is
    ``WHEN - offset``."""
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "user": {"login": author},
        "labels": labels or [{"name": "dependencies"}],
        "created_at": (WHEN - created_at_offset).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ----- collect_aging_prs -----


@pytest.mark.asyncio
async def test_only_dependabot_authors_are_included() -> None:
    fixture = {
        "open_pulls": [
            _pr(number=1, author="dependabot[bot]"),
            _pr(number=2, author="alice"),
            _pr(number=3, author="dependabot-preview[bot]"),
        ],
        "changed_files": {"1": [], "2": [], "3": []},
    }
    async with GitHubClient(auth=token_auth("t")) as gh:
        report = await collect_aging_prs(
            gh=gh,
            owner="acme",
            repo="widgets",
            ownership=_ownership(),
            fixture=fixture,
            now=WHEN,
        )
    nums = {pr.number for pr in report.prs}
    assert nums == {1, 3}


@pytest.mark.asyncio
async def test_security_label_uses_security_sla() -> None:
    fixture = {
        "open_pulls": [
            _pr(
                number=10,
                labels=[{"name": "security"}],
                created_at_offset=timedelta(hours=2),
            ),
        ],
        "changed_files": {"10": []},
    }
    async with GitHubClient(auth=token_auth("t")) as gh:
        report = await collect_aging_prs(
            gh=gh,
            owner="a",
            repo="b",
            ownership=_ownership(),
            default_sla_hours=72,
            security_sla_hours=24,
            fixture=fixture,
            now=WHEN,
        )
    [pr] = report.prs
    assert pr.severity == "security-advisory"
    assert pr.sla_hours == 24


@pytest.mark.asyncio
async def test_sla_state_boundaries() -> None:
    """At exactly sla_hours → breach; at 2× sla_hours → double_breach."""
    fixture = {
        "open_pulls": [
            _pr(number=1, created_at_offset=timedelta(hours=23)),  # ok
            _pr(number=2, created_at_offset=timedelta(hours=24)),  # breach
            _pr(number=3, created_at_offset=timedelta(hours=47)),  # breach
            _pr(number=4, created_at_offset=timedelta(hours=48)),  # double_breach
            _pr(number=5, created_at_offset=timedelta(hours=200)),  # double_breach
        ],
        "changed_files": {"1": [], "2": [], "3": [], "4": [], "5": []},
    }
    async with GitHubClient(auth=token_auth("t")) as gh:
        report = await collect_aging_prs(
            gh=gh,
            owner="a",
            repo="b",
            ownership=_ownership(),
            default_sla_hours=24,
            fixture=fixture,
            now=WHEN,
        )
    by_num = {pr.number: pr.sla_state for pr in report.prs}
    assert by_num == {
        1: "ok",
        2: "breach",
        3: "breach",
        4: "double_breach",
        5: "double_breach",
    }


@pytest.mark.asyncio
async def test_owner_resolution_via_owners_yml() -> None:
    fixture = {
        "open_pulls": [_pr(number=1, created_at_offset=timedelta(hours=100))],
        "changed_files": {"1": ["services/api/package.json"]},
    }
    ownership = _ownership(_rule("/services/api/", "alice", "bob"))
    async with GitHubClient(auth=token_auth("t")) as gh:
        report = await collect_aging_prs(
            gh=gh,
            owner="a",
            repo="b",
            ownership=ownership,
            fixture=fixture,
            now=WHEN,
        )
    [pr] = report.prs
    assert pr.primary_owner == "@alice"
    assert pr.next_owner == "@bob"


@pytest.mark.asyncio
async def test_skip_paths_strip_vendored_files_before_matching() -> None:
    fixture = {
        "open_pulls": [_pr(number=1, created_at_offset=timedelta(hours=100))],
        "changed_files": {"1": ["node_modules/lodash/index.js"]},
    }
    ownership = _ownership(_rule("/services/api/", "alice"))
    async with GitHubClient(auth=token_auth("t")) as gh:
        report = await collect_aging_prs(
            gh=gh,
            owner="a",
            repo="b",
            ownership=ownership,
            fixture=fixture,
            now=WHEN,
        )
    [pr] = report.prs
    assert pr.changed_files == []  # everything skipped
    assert pr.primary_owner is None  # nothing left to match


@pytest.mark.asyncio
async def test_sort_order_worst_state_first() -> None:
    fixture = {
        "open_pulls": [
            _pr(number=1, created_at_offset=timedelta(hours=10)),  # ok
            _pr(number=2, created_at_offset=timedelta(hours=200)),  # double_breach
            _pr(number=3, created_at_offset=timedelta(hours=50)),  # breach
        ],
        "changed_files": {"1": [], "2": [], "3": []},
    }
    async with GitHubClient(auth=token_auth("t")) as gh:
        report = await collect_aging_prs(
            gh=gh,
            owner="a",
            repo="b",
            ownership=_ownership(),
            default_sla_hours=24,
            fixture=fixture,
            now=WHEN,
        )
    assert [pr.number for pr in report.prs] == [2, 3, 1]


# ----- render_markdown -----


def test_render_markdown_empty_report() -> None:
    report = AgingReport(generated_at=WHEN, prs=[], by_owner={}, by_severity={})
    md = render_markdown(report)
    assert "# Tend SLA aging report" in md
    assert "No open Dependabot PRs" in md


def test_render_markdown_groups_by_owner_and_severity() -> None:
    prs = [
        AgingPR(
            number=1,
            title="t",
            url="u",
            author="dependabot[bot]",
            created_at=WHEN,
            severity="default",
            primary_owner="@alice",
            age_hours=5.0,
            sla_hours=24,
            sla_state="ok",
            next_owner=None,
            fallback_owner=None,
            changed_files=[],
        ),
        AgingPR(
            number=2,
            title="t",
            url="u",
            author="dependabot[bot]",
            created_at=WHEN,
            severity="security-advisory",
            primary_owner="@bob",
            age_hours=30.0,
            sla_hours=24,
            sla_state="breach",
            next_owner=None,
            fallback_owner=None,
            changed_files=[],
        ),
    ]
    report = AgingReport(
        generated_at=WHEN,
        prs=prs,
        by_owner={"@alice": [prs[0]], "@bob": [prs[1]]},
        by_severity={"default": [prs[0]], "security-advisory": [prs[1]]},
    )
    md = render_markdown(report)
    assert "## By severity" in md
    assert "## By owner" in md
    assert "### @alice" in md
    assert "### @bob" in md
    assert "security-advisory" in md


# ----- post_nudges -----


def _comment_with_marker(level: str, mention: str) -> dict[str, Any]:
    """Build a fake issue-comment dict with a prior nudge marker."""
    return {"body": f"...\n\n{NUDGE_MARKER_PREFIX}{level}:{mention} -->"}


def _aging_pr(
    *, number: int, state: str, primary: str | None, next_owner: str | None, fallback: str | None
) -> AgingPR:
    return AgingPR(
        number=number,
        title="x",
        url=f"https://x/{number}",
        author="dependabot[bot]",
        created_at=WHEN,
        severity="default",
        primary_owner=primary,
        age_hours=99.0,
        sla_hours=24,
        sla_state=state,  # type: ignore[arg-type]
        next_owner=next_owner,
        fallback_owner=fallback,
        changed_files=[],
    )


@pytest.mark.asyncio
async def test_post_nudges_skips_ok_state(httpx_mock: HTTPXMock) -> None:
    pr = _aging_pr(number=1, state="ok", primary="@alice", next_owner=None, fallback=None)
    report = AgingReport(generated_at=WHEN, prs=[pr], by_owner={}, by_severity={})
    async with GitHubClient(auth=token_auth("t")) as gh:
        posted = await post_nudges(gh=gh, owner="a", repo="b", report=report)
    assert posted == []
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_post_nudges_breach_mentions_next_owner(httpx_mock: HTTPXMock) -> None:
    """At breach state we widen visibility: ping next owner (or fallback),
    not the primary who already had the PR assigned."""
    pr = _aging_pr(
        number=1, state="breach", primary="@alice", next_owner="@bob", fallback="@acme/appsec"
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/a/b/issues/1/comments?per_page=100",
        json=[],
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/a/b/issues/1/comments",
        json={},
    )
    report = AgingReport(generated_at=WHEN, prs=[pr], by_owner={}, by_severity={})
    async with GitHubClient(auth=token_auth("t")) as gh:
        posted = await post_nudges(gh=gh, owner="a", repo="b", report=report)
    assert posted == [(1, "breach")]
    post = httpx_mock.get_request(method="POST")
    body = post.read().decode("utf-8")
    assert "@bob" in body  # next_owner mentioned
    assert "tend-nudge:breach:@bob" in body  # marker present


@pytest.mark.asyncio
async def test_post_nudges_double_breach_mentions_fallback(httpx_mock: HTTPXMock) -> None:
    pr = _aging_pr(
        number=1,
        state="double_breach",
        primary="@alice",
        next_owner="@bob",
        fallback="@acme/appsec",
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/a/b/issues/1/comments?per_page=100",
        json=[],
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/a/b/issues/1/comments",
        json={},
    )
    report = AgingReport(generated_at=WHEN, prs=[pr], by_owner={}, by_severity={})
    async with GitHubClient(auth=token_auth("t")) as gh:
        posted = await post_nudges(gh=gh, owner="a", repo="b", report=report)
    assert posted == [(1, "double_breach")]
    post = httpx_mock.get_request(method="POST")
    body = post.read().decode("utf-8")
    assert "@acme/appsec" in body
    assert "tend-nudge:double_breach:@acme/appsec" in body


@pytest.mark.asyncio
async def test_post_nudges_idempotent_at_same_level(httpx_mock: HTTPXMock) -> None:
    """If a prior tend-nudge:breach marker exists, don't post a duplicate."""
    pr = _aging_pr(number=1, state="breach", primary="@alice", next_owner="@bob", fallback=None)
    httpx_mock.add_response(
        url="https://api.github.com/repos/a/b/issues/1/comments?per_page=100",
        json=[_comment_with_marker("breach", "@bob")],
    )
    report = AgingReport(generated_at=WHEN, prs=[pr], by_owner={}, by_severity={})
    async with GitHubClient(auth=token_auth("t")) as gh:
        posted = await post_nudges(gh=gh, owner="a", repo="b", report=report)
    assert posted == []
    # No POST went out — only the GET for listing comments.
    assert all(r.method == "GET" for r in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_post_nudges_breach_to_double_breach_transitions(httpx_mock: HTTPXMock) -> None:
    """PR with a prior breach marker, now in double_breach, posts a second
    comment (different marker level) — by design, to escalate."""
    pr = _aging_pr(
        number=1, state="double_breach", primary="@alice", next_owner=None, fallback="@acme/appsec"
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/a/b/issues/1/comments?per_page=100",
        json=[_comment_with_marker("breach", "@alice")],  # earlier breach nudge
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/a/b/issues/1/comments",
        json={},
    )
    report = AgingReport(generated_at=WHEN, prs=[pr], by_owner={}, by_severity={})
    async with GitHubClient(auth=token_auth("t")) as gh:
        posted = await post_nudges(gh=gh, owner="a", repo="b", report=report)
    assert posted == [(1, "double_breach")]


@pytest.mark.asyncio
async def test_post_nudges_dry_run_lists_intent_without_posting(httpx_mock: HTTPXMock) -> None:
    pr = _aging_pr(number=1, state="breach", primary="@alice", next_owner="@bob", fallback=None)
    httpx_mock.add_response(
        url="https://api.github.com/repos/a/b/issues/1/comments?per_page=100",
        json=[],
    )
    report = AgingReport(generated_at=WHEN, prs=[pr], by_owner={}, by_severity={})
    async with GitHubClient(auth=token_auth("t")) as gh:
        posted = await post_nudges(gh=gh, owner="a", repo="b", report=report, dry_run=True)
    assert posted == [(1, "breach")]
    # No POST went out.
    assert all(r.method == "GET" for r in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_post_nudges_skips_when_no_mention_available(httpx_mock: HTTPXMock) -> None:
    """No primary, no next, no fallback → nothing to mention → skip."""
    pr = _aging_pr(number=1, state="breach", primary=None, next_owner=None, fallback=None)
    report = AgingReport(generated_at=WHEN, prs=[pr], by_owner={}, by_severity={})
    async with GitHubClient(auth=token_auth("t")) as gh:
        posted = await post_nudges(gh=gh, owner="a", repo="b", report=report)
    assert posted == []
    assert httpx_mock.get_requests() == []
