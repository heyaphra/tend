"""Tests for the pure ``_credit`` aggregator.

The async ``analyze_contributions`` fetcher composes this with the
GitHubClient; its end-to-end behavior is covered in an integration test
that mocks the API. Here we cover the aggregator's filtering and
deduplication semantics directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tend.analyze._models import FileContribution
from tend.analyze.commits import _credit


def _files(*paths: str, added: int = 100, removed: int = 10) -> list[dict]:
    return [{"filename": p, "additions": added, "deletions": removed} for p in paths]


def _empty_state():
    return {
        "agg": {},
        "filtered_bots": {},
        "dropped_unresolved": {},
        "exclude_contributors": set(),
        "bot_filter_enabled": True,
    }


def test_credit_records_files_for_real_user():
    state = _empty_state()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    credited = _credit(
        agg=state["agg"],
        files=_files("src/a.py", "src/b.py"),
        email="alice@example.com",
        name="Alice",
        api_login="alice",
        api_type="User",
        commit_date=when,
        weight=1.0,
        filtered_bots=state["filtered_bots"],
        dropped_unresolved=state["dropped_unresolved"],
        exclude_contributors=state["exclude_contributors"],
        bot_filter_enabled=state["bot_filter_enabled"],
    )
    assert credited is True
    assert len(state["agg"]) == 2
    contrib = state["agg"][("src/a.py", "alice@example.com")]
    assert isinstance(contrib, FileContribution)
    assert contrib.github_username == "alice"
    assert contrib.commit_count == 1.0
    assert contrib.lines_added == 100.0


def test_credit_filters_bot_and_counts_label():
    state = _empty_state()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    credited = _credit(
        agg=state["agg"],
        files=_files("src/a.py"),
        email="bot@dependabot.com",
        name="dependabot[bot]",
        api_login="dependabot[bot]",
        api_type="Bot",
        commit_date=when,
        weight=1.0,
        filtered_bots=state["filtered_bots"],
        dropped_unresolved=state["dropped_unresolved"],
        exclude_contributors=state["exclude_contributors"],
        bot_filter_enabled=state["bot_filter_enabled"],
    )
    assert credited is False
    assert state["agg"] == {}
    assert state["filtered_bots"]["dependabot[bot]"] == 1


def test_credit_drops_unresolved_contributors():
    state = _empty_state()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    credited = _credit(
        agg=state["agg"],
        files=_files("src/a.py"),
        email="anon@example.com",
        name="Anon",
        api_login=None,
        api_type=None,
        commit_date=when,
        weight=1.0,
        filtered_bots=state["filtered_bots"],
        dropped_unresolved=state["dropped_unresolved"],
        exclude_contributors=state["exclude_contributors"],
        bot_filter_enabled=state["bot_filter_enabled"],
    )
    assert credited is False
    assert state["agg"] == {}
    assert state["dropped_unresolved"]["Anon"] == 1


def test_credit_handles_coauthor_weight():
    state = _empty_state()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    _credit(
        agg=state["agg"],
        files=_files("src/a.py", added=200, removed=0),
        email="alice@users.noreply.github.com",
        name="Alice",
        api_login=None,
        api_type=None,
        commit_date=when,
        weight=0.5,  # co-author credit
        filtered_bots=state["filtered_bots"],
        dropped_unresolved=state["dropped_unresolved"],
        exclude_contributors=state["exclude_contributors"],
        bot_filter_enabled=state["bot_filter_enabled"],
    )
    contrib = state["agg"][("src/a.py", "alice@users.noreply.github.com")]
    assert contrib.commit_count == 0.5
    assert contrib.lines_added == 100.0  # 200 × 0.5


def test_credit_merges_repeated_contributions():
    state = _empty_state()
    early = datetime(2026, 1, 1, tzinfo=UTC)
    later = datetime(2026, 2, 1, tzinfo=UTC)
    for when in (early, later):
        _credit(
            agg=state["agg"],
            files=_files("src/a.py", added=10, removed=2),
            email="alice@example.com",
            name="Alice",
            api_login="alice",
            api_type="User",
            commit_date=when,
            weight=1.0,
            filtered_bots=state["filtered_bots"],
            dropped_unresolved=state["dropped_unresolved"],
            exclude_contributors=state["exclude_contributors"],
            bot_filter_enabled=state["bot_filter_enabled"],
        )
    contrib = state["agg"][("src/a.py", "alice@example.com")]
    assert contrib.commit_count == 2.0
    assert contrib.lines_added == 20.0
    assert contrib.last_commit_at == later


def test_credit_respects_exclude_list():
    state = _empty_state()
    state["exclude_contributors"] = {"alice@example.com"}
    when = datetime(2026, 1, 1, tzinfo=UTC)
    credited = _credit(
        agg=state["agg"],
        files=_files("src/a.py"),
        email="alice@example.com",
        name="Alice",
        api_login="alice",
        api_type="User",
        commit_date=when,
        weight=1.0,
        filtered_bots=state["filtered_bots"],
        dropped_unresolved=state["dropped_unresolved"],
        exclude_contributors=state["exclude_contributors"],
        bot_filter_enabled=state["bot_filter_enabled"],
    )
    assert credited is False
    assert state["filtered_bots"]["alice@example.com"] == 1


def test_credit_bypasses_bot_filter_when_disabled():
    state = _empty_state()
    state["bot_filter_enabled"] = False
    when = datetime(2026, 1, 1, tzinfo=UTC)
    credited = _credit(
        agg=state["agg"],
        files=_files("src/a.py"),
        email="alice@example.com",
        name="dependabot",  # would match bot patterns
        api_login="dependabot",  # would match too
        api_type="Bot",  # and this
        commit_date=when,
        weight=1.0,
        filtered_bots=state["filtered_bots"],
        dropped_unresolved=state["dropped_unresolved"],
        exclude_contributors=state["exclude_contributors"],
        bot_filter_enabled=state["bot_filter_enabled"],
    )
    assert credited is True
