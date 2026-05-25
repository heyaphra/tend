"""Commit history fetcher and per-(file, author) aggregator.

Two layers:

- ``analyze_contributions()`` is the async entrypoint — fetches commits
  via the GitHub REST client, parses author and co-author records,
  filters bots, drops unresolved-handle contributors, and aggregates to
  ``FileContribution`` rows ready for ``infer()``.
- ``_credit()`` is the pure aggregator. It's separated so the bot-filtering
  and resolution behavior can be unit-tested without spinning up an async
  GitHub client.

The bot filter runs *before* aggregation so dropped contributions don't
inflate any directory's volume floor.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tend.analyze._models import FileContribution
from tend.analyze.filtering import (
    COAUTHOR_WEIGHT,
    bot_label,
    is_bot,
    parse_coauthors,
    resolve_github_username,
)
from tend.config import InferenceConfig
from tend.github.client import GitHubClient

__all__ = ["ContributionResult", "FileContribution", "analyze_contributions"]


@dataclass
class ContributionResult:
    contributions: list[FileContribution]
    filtered_bots: dict[str, int] = field(default_factory=dict)
    dropped_unresolved: dict[str, int] = field(default_factory=dict)
    total_commits: int = 0


async def analyze_contributions(
    gh: GitHubClient,
    owner: str,
    repo: str,
    config: InferenceConfig,
    exclude_contributors: set[str] | None = None,
) -> ContributionResult:
    """Fetch commits and aggregate ``FileContribution`` rows.

    Network cost: 1 page-of-commits call per ``config.max_commits / 100``
    pages, plus one commit-detail call per commit (fanned out at the
    client's bounded concurrency).
    """
    excluded = {e.lower() for e in (exclude_contributors or set())}
    since = datetime.now(UTC) - timedelta(days=config.lookback_days)
    max_pages = max(1, (config.max_commits + 99) // 100)

    commits = await gh.get_commits(owner, repo, since=since, max_pages=max_pages)
    commits = commits[: config.max_commits]

    details = await asyncio.gather(
        *(gh.get_commit_detail(owner, repo, c["sha"]) for c in commits),
        return_exceptions=True,
    )

    agg: dict[tuple[str, str], FileContribution] = {}
    filtered_bots: dict[str, int] = defaultdict(int)
    dropped_unresolved: dict[str, int] = defaultdict(int)
    total_commits = 0

    for d in details:
        if isinstance(d, BaseException):
            continue
        files = d.get("files") or []
        if len(files) > config.max_files_per_commit:
            continue
        commit_obj = d.get("commit") or {}
        author_obj = commit_obj.get("author") or {}
        api_author = d.get("author") or {}

        primary_email = (author_obj.get("email") or "").strip()
        primary_name = (author_obj.get("name") or "").strip()
        primary_date = _parse_iso8601(author_obj.get("date"))

        primary_login = api_author.get("login") if isinstance(api_author, dict) else None
        primary_api_type = api_author.get("type") if isinstance(api_author, dict) else None

        total_commits += 1

        _credit(
            agg=agg,
            files=files,
            email=primary_email,
            name=primary_name,
            api_login=primary_login,
            api_type=primary_api_type,
            commit_date=primary_date,
            weight=1.0,
            filtered_bots=filtered_bots,
            dropped_unresolved=dropped_unresolved,
            exclude_contributors=excluded,
            bot_filter_enabled=config.bot_filter_enabled,
        )

        for co_name, co_email in parse_coauthors(commit_obj.get("message") or ""):
            _credit(
                agg=agg,
                files=files,
                email=co_email,
                name=co_name,
                api_login=None,
                api_type=None,
                commit_date=primary_date,
                weight=COAUTHOR_WEIGHT,
                filtered_bots=filtered_bots,
                dropped_unresolved=dropped_unresolved,
                exclude_contributors=excluded,
                bot_filter_enabled=config.bot_filter_enabled,
            )

    return ContributionResult(
        contributions=list(agg.values()),
        filtered_bots=dict(filtered_bots),
        dropped_unresolved=dict(dropped_unresolved),
        total_commits=total_commits,
    )


def _credit(
    *,
    agg: dict[tuple[str, str], FileContribution],
    files: list[dict[str, Any]],
    email: str,
    name: str,
    api_login: str | None,
    api_type: str | None,
    commit_date: datetime,
    weight: float,
    filtered_bots: dict[str, int],
    dropped_unresolved: dict[str, int],
    exclude_contributors: set[str],
    bot_filter_enabled: bool,
) -> bool:
    """Add this commit's per-file rows to ``agg`` for one contributor.

    Returns True if credited, False if filtered out as bot / excluded /
    unresolved. Exposed (private) so the filtering integration can be tested
    without an HTTP round-trip.
    """
    if not email:
        return False
    if email.lower() in exclude_contributors:
        filtered_bots[email] = filtered_bots.get(email, 0) + 1
        return False
    if bot_filter_enabled and is_bot(email, name, api_type=api_type, api_login=api_login):
        label = api_login or bot_label(email, name)
        filtered_bots[label] = filtered_bots.get(label, 0) + 1
        return False
    gh_username = resolve_github_username(email, name, api_login)
    if gh_username is None:
        key = name or email or "unknown"
        dropped_unresolved[key] = dropped_unresolved.get(key, 0) + 1
        return False

    for f in files:
        path = f.get("filename")
        if not path:
            continue
        added = float(f.get("additions") or 0)
        removed = float(f.get("deletions") or 0)
        key = (path, email.lower())
        existing = agg.get(key)
        if existing is None:
            agg[key] = FileContribution(
                file_path=path,
                author_email=email,
                author_name=name,
                github_username=gh_username,
                commit_count=weight,
                lines_added=added * weight,
                lines_removed=removed * weight,
                last_commit_at=commit_date,
            )
        else:
            existing.commit_count += weight
            existing.lines_added += added * weight
            existing.lines_removed += removed * weight
            if commit_date > existing.last_commit_at:
                existing.last_commit_at = commit_date
    return True


def _parse_iso8601(value: str | None) -> datetime:
    """Parse a GitHub-style ISO8601 timestamp, falling back to ``now`` on error."""
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(UTC)
