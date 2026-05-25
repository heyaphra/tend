"""Tests for YAML-shaped PR rendering and the idempotent create/update flow."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pytest_httpx import HTTPXMock

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.config import InferenceConfig
from tend.github.auth import token_auth
from tend.github.client import GitHubClient
from tend.github.pr import (
    BRANCH_NAME,
    HASH_MARKER_PREFIX,
    HASH_MARKER_RE,
    create_or_update_tend_pr,
    render_pr_body,
)
from tend.output.tend_yaml import OwnershipFile, content_hash, diff

WHEN = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _owner(handle: str, conf: float = 0.7, commits: int = 10) -> OwnerCandidate:
    return OwnerCandidate(
        email=f"{handle}@example.com",
        name=handle,
        github_username=handle,
        confidence=conf,
        commit_count=commits,
        last_active=WHEN,
    )


def _rule(pattern: str, owners: list[OwnerCandidate]) -> InferredOwner:
    return InferredOwner(path_pattern=pattern, owners=owners)


def _config() -> InferenceConfig:
    return InferenceConfig()


# -------- render_pr_body --------


def test_render_pr_body_first_time_lists_all_paths():
    rules = [
        _rule("/src/", [_owner("alice")]),
        _rule("/docs/", [_owner("bob")]),
    ]
    body = render_pr_body(
        rules=rules,
        config=_config(),
        sensitivity="balanced",
        repo_full_name="acme/widgets",
        content_hash_value=content_hash(rules, _config(), "balanced"),
        diff_against=None,
    )
    assert "Paths added" in body
    assert "/src/" in body
    assert "/docs/" in body
    assert "@alice" in body
    assert "@bob" in body


def test_render_pr_body_update_uses_diff_sections():
    old_rules = [_rule("/src/", [_owner("alice", 0.7)])]
    new_rules = [
        _rule("/src/", [_owner("bob", 0.7)]),  # owner change
        _rule("/docs/", [_owner("carol")]),  # added
    ]
    old_file = OwnershipFile(version=1, generated_at=None, config={}, paths=old_rules)
    new_file = OwnershipFile(version=1, generated_at=None, config={}, paths=new_rules)
    d = diff(old_file, new_file)

    body = render_pr_body(
        rules=new_rules,
        config=_config(),
        sensitivity="balanced",
        repo_full_name="acme/widgets",
        content_hash_value=content_hash(new_rules, _config(), "balanced"),
        diff_against=d,
    )
    assert "Paths added" in body
    assert "Owner changed" in body
    assert "/docs/" in body  # added
    assert "/src/" in body  # owner changed
    assert "alice" in body
    assert "bob" in body


def test_render_pr_body_embeds_hash_marker():
    rules = [_rule("/src/", [_owner("alice")])]
    body = render_pr_body(
        rules=rules,
        config=_config(),
        sensitivity="balanced",
        repo_full_name="acme/widgets",
        content_hash_value=content_hash(rules, _config(), "balanced"),
        diff_against=None,
    )
    m = HASH_MARKER_RE.search(body)
    assert m is not None
    assert len(m.group(1)) == 64  # sha256 hex


def test_render_pr_body_omits_codeowners_references():
    """v1 invariant: the PR body never mentions CODEOWNERS."""
    rules = [_rule("/src/", [_owner("alice")])]
    body = render_pr_body(
        rules=rules,
        config=_config(),
        sensitivity="balanced",
        repo_full_name="acme/widgets",
        content_hash_value=content_hash(rules, _config(), "balanced"),
        diff_against=None,
    )
    assert "CODEOWNERS" not in body
    assert "codeowners" not in body.lower()


# -------- create_or_update_tend_pr --------


@pytest.mark.asyncio
async def test_dry_run_returns_would_create_with_body():
    rules = [_rule("/src/", [_owner("alice")])]
    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await create_or_update_tend_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            rules=rules,
            config=_config(),
            sensitivity="balanced",
            dry_run=True,
            now=WHEN,
        )
    assert result.action == "would_create"
    assert result.branch == BRANCH_NAME
    assert result.body is not None
    assert "tend-hash:" in result.body


@pytest.mark.asyncio
async def test_creates_pr_when_none_exists(httpx_mock: HTTPXMock):
    rules = [_rule("/src/", [_owner("alice")])]

    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/widgets/pulls?state=open&head=acme%3Atend%2Fupdate&per_page=100",
        json=[],
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/widgets/contents/.tend/owners.yml?ref=HEAD",
        status_code=404,
        json={"message": "Not Found"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/widgets",
        json={"default_branch": "main"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/widgets/git/ref/heads/main",
        json={"object": {"sha": "deadbeef"}},
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/widgets/git/ref/heads/tend/update",
        status_code=404,
        json={"message": "Not Found"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/git/refs",
        json={},
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/widgets/contents/.tend/owners.yml?ref=tend%2Fupdate",
        status_code=404,
        json={"message": "Not Found"},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://api.github.com/repos/acme/widgets/contents/.tend/owners.yml",
        json={"content": {"sha": "newsha"}},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/acme/widgets/pulls",
        json={"html_url": "https://github.com/acme/widgets/pull/42", "number": 42},
    )

    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await create_or_update_tend_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            rules=rules,
            config=_config(),
            sensitivity="balanced",
            now=WHEN,
        )
    assert result.action == "created"
    assert result.url == "https://github.com/acme/widgets/pull/42"


@pytest.mark.asyncio
async def test_no_op_when_open_pr_has_same_hash(httpx_mock: HTTPXMock):
    rules = [_rule("/src/", [_owner("alice")])]
    target_hash = content_hash(rules, _config(), "balanced")
    pr_body = f"Existing body\n\n<!-- {HASH_MARKER_PREFIX}{target_hash} -->\n"

    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/widgets/pulls?state=open&head=acme%3Atend%2Fupdate&per_page=100",
        json=[
            {
                "number": 7,
                "html_url": "https://github.com/acme/widgets/pull/7",
                "body": pr_body,
            }
        ],
    )
    # No prior owners.yml on the default branch.
    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/widgets/contents/.tend/owners.yml?ref=HEAD",
        status_code=404,
        json={"message": "Not Found"},
    )

    async with GitHubClient(auth=token_auth("t")) as gh:
        result = await create_or_update_tend_pr(
            gh=gh,
            owner="acme",
            repo="widgets",
            rules=rules,
            config=_config(),
            sensitivity="balanced",
            now=WHEN,
        )
    assert result.action == "noop_same_hash"
    assert result.url == "https://github.com/acme/widgets/pull/7"
