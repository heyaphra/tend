"""GitHub GraphQL helpers.

Thin wrapper over the REST ``GitHubClient`` — reuses its transport,
authentication, and rate-limit handling. The only API surface today is
``graphql()`` (single query) and ``graphql_paginate()`` (cursor-paginated
queries), both async.

Used primarily by ``tend.github.associations`` to fetch
``authorAssociation`` data in one paginated query rather than N REST
calls.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tend.github.client import GitHubClient

logger = logging.getLogger(__name__)

GRAPHQL_PATH = "/graphql"


async def graphql(
    gh: GitHubClient,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a single GraphQL query. Returns the ``data`` portion of the response.

    Raises ``RuntimeError`` if the response carries a top-level ``errors``
    block — those are GraphQL-level errors (invalid query, missing
    permission, etc.) that the caller usually can't recover from.
    """
    resp = await gh.post(
        GRAPHQL_PATH,
        json={"query": query, "variables": variables or {}},
    )
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("errors"):
        msg = "; ".join(
            str(e.get("message") or e) for e in payload["errors"] if isinstance(e, dict)
        ) or str(payload["errors"])
        raise RuntimeError(f"GraphQL error: {msg}")
    return payload.get("data") or {}


async def graphql_paginate(
    gh: GitHubClient,
    query: str,
    variables: dict[str, Any],
    *,
    data_path: tuple[str, ...],
    max_items: int = 500,
    page_size: int = 100,
) -> AsyncIterator[dict[str, Any]]:
    """Paginate a cursor-based GraphQL query, yielding individual nodes.

    ``data_path`` walks into the response to the connection (e.g.
    ``("repository", "pullRequests")`` for ``data.repository.pullRequests``).
    The connection must expose ``pageInfo { hasNextPage endCursor }`` and
    a ``nodes`` array.

    Stops at ``max_items`` nodes (defaults to 500 — enough to cover any
    reasonable repo's recent activity without blowing through GraphQL
    quota on a 100k-PR monorepo).
    """
    yielded = 0
    cursor: str | None = None
    pages = 0
    max_pages = max(1, (max_items + page_size - 1) // page_size)
    while pages < max_pages:
        pages += 1
        page_vars = {**variables, "cursor": cursor}
        data = await graphql(gh, query, page_vars)
        connection: Any = data
        for key in data_path:
            connection = connection.get(key) if isinstance(connection, dict) else None
            if connection is None:
                logger.warning("GraphQL response missing %r path; aborting paginator", key)
                return
        nodes = connection.get("nodes") or []
        for node in nodes:
            yield node
            yielded += 1
            if yielded >= max_items:
                return
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return
        cursor = page_info.get("endCursor")
        if not cursor:
            return


__all__ = ["GRAPHQL_PATH", "graphql", "graphql_paginate"]
