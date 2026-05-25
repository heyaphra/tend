"""Tests for CommentAuthorAssociation enum, weights, and the GraphQL fetcher."""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from tend.github.associations import (
    ASSOCIATION_WEIGHTS,
    Association,
    association_weight,
    fetch_associations,
    stronger,
)
from tend.github.auth import token_auth
from tend.github.client import GitHubClient


def test_association_weights_match_spec():
    """Spec: OWNER/MEMBER 1.0, COLLABORATOR 0.7, CONTRIBUTOR 0.4,
    FIRST_TIME_CONTRIBUTOR/FIRST_TIMER/NONE 0.1, UNKNOWN 0.5."""
    assert ASSOCIATION_WEIGHTS[Association.OWNER] == 1.0
    assert ASSOCIATION_WEIGHTS[Association.MEMBER] == 1.0
    assert ASSOCIATION_WEIGHTS[Association.COLLABORATOR] == 0.7
    assert ASSOCIATION_WEIGHTS[Association.CONTRIBUTOR] == 0.4
    assert ASSOCIATION_WEIGHTS[Association.FIRST_TIME_CONTRIBUTOR] == 0.1
    assert ASSOCIATION_WEIGHTS[Association.FIRST_TIMER] == 0.1
    assert ASSOCIATION_WEIGHTS[Association.NONE] == 0.1
    assert ASSOCIATION_WEIGHTS[Association.UNKNOWN] == 0.5


def test_association_weight_defaults_to_unknown_neutral_for_none():
    assert association_weight(None) == 0.5


def test_association_weight_handles_explicit_lookups():
    assert association_weight(Association.MEMBER) == 1.0
    assert association_weight(Association.NONE) == 0.1


def test_association_weight_respects_custom_overrides():
    """Callers can pass a custom weights table (e.g. test fixtures)."""
    custom = {Association.MEMBER: 0.5}
    assert association_weight(Association.MEMBER, custom) == 0.5
    # Falls back to UNKNOWN default when missing.
    assert association_weight(Association.OWNER, custom) == 0.5


def test_stronger_returns_higher_tier():
    """OWNER > MEMBER > COLLABORATOR > CONTRIBUTOR > UNKNOWN >
    FIRST_TIME_CONTRIBUTOR > FIRST_TIMER > NONE."""
    assert stronger(Association.MEMBER, Association.NONE) == Association.MEMBER
    assert stronger(Association.NONE, Association.MEMBER) == Association.MEMBER
    assert stronger(Association.OWNER, Association.MEMBER) == Association.OWNER
    assert stronger(Association.CONTRIBUTOR, Association.UNKNOWN) == Association.CONTRIBUTOR


@pytest.mark.asyncio
async def test_fetch_associations_parses_graphql_response(httpx_mock: HTTPXMock):
    """Single-page GraphQL response yields {handle: association} mapping."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/graphql",
        json={
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "author": {"login": "Alice"},
                                "authorAssociation": "MEMBER",
                            },
                            {
                                "author": {"login": "Bob"},
                                "authorAssociation": "FIRST_TIME_CONTRIBUTOR",
                            },
                            {
                                "author": None,  # deleted user; skipped
                                "authorAssociation": "NONE",
                            },
                        ],
                    }
                }
            }
        },
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        out = await fetch_associations(gh, "acme", "widgets")
    assert out == {
        "alice": Association.MEMBER,
        "bob": Association.FIRST_TIME_CONTRIBUTOR,
    }


@pytest.mark.asyncio
async def test_fetch_associations_keeps_strongest_observed(httpx_mock: HTTPXMock):
    """If a contributor appears as both NONE (old PR) and MEMBER (recent),
    keep MEMBER — they're trusted now, weight accordingly."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/graphql",
        json={
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {"author": {"login": "alice"}, "authorAssociation": "NONE"},
                            {"author": {"login": "alice"}, "authorAssociation": "MEMBER"},
                        ],
                    }
                }
            }
        },
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        out = await fetch_associations(gh, "acme", "widgets")
    assert out == {"alice": Association.MEMBER}


@pytest.mark.asyncio
async def test_fetch_associations_returns_empty_on_graphql_error(
    httpx_mock: HTTPXMock,
):
    """Token-scope errors / failed queries → empty dict; downstream
    pipeline treats every contributor as UNKNOWN (weight 0.5)."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/graphql",
        json={"errors": [{"message": "insufficient scope"}]},
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        out = await fetch_associations(gh, "acme", "widgets")
    assert out == {}


@pytest.mark.asyncio
async def test_fetch_associations_paginates(httpx_mock: HTTPXMock):
    """Cursor-paginated query: continues across pages, stops at max_items."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/graphql",
        json={
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        "nodes": [
                            {"author": {"login": "alice"}, "authorAssociation": "MEMBER"},
                        ],
                    }
                }
            }
        },
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/graphql",
        json={
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {"author": {"login": "bob"}, "authorAssociation": "CONTRIBUTOR"},
                        ],
                    }
                }
            }
        },
    )
    async with GitHubClient(auth=token_auth("t")) as gh:
        out = await fetch_associations(gh, "acme", "widgets", max_prs=500)
    assert out == {
        "alice": Association.MEMBER,
        "bob": Association.CONTRIBUTOR,
    }
