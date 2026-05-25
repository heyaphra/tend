"""Bot filter and username resolution tests, ported from the prototype.

The bot filter is pattern-heavy because GitHub's ``type: "Bot"`` flag
misses service-account bots. These tests lock in the patterns that
dogfooding on Flutter, Vue, Rails, and other large repos uncovered.
"""

from __future__ import annotations

from tend.analyze.filtering import (
    extract_noreply_username,
    is_bot,
    parse_coauthors,
    resolve_github_username,
)


def test_bot_filter_basic_cases():
    assert is_bot("12345+alice@users.noreply.github.com", "Alice") is False
    assert is_bot("49699333+dependabot[bot]@users.noreply.github.com", "dependabot[bot]") is True
    assert (
        is_bot("41898282+github-actions[bot]@users.noreply.github.com", "github-actions[bot]")
        is True
    )
    assert is_bot("29139614+renovate[bot]@users.noreply.github.com", "renovate[bot]") is True
    assert is_bot("dependabot@github.com", "Dependabot") is True
    assert is_bot("foo-bot@example.com", "Foo Bot") is True
    assert is_bot("alice@example.com", "Alice") is False
    assert is_bot("anything@example.com", "test") is True


def test_bot_filter_api_type_shortcut():
    assert is_bot("not-a-bot-pattern@example.com", "Alice", api_type="Bot") is True
    assert is_bot("alice@example.com", "Alice", api_type="User") is False


def test_bot_filter_catches_autoroll_service_accounts():
    assert is_bot("engine-flutter-autoroll@google.com", "Engine Flutter Autoroll") is True
    assert is_bot("skia-roller@example.com", "Skia") is True
    assert is_bot("rollins@example.com", "Henry Rollins") is False


def test_bot_filter_catches_login_ending_in_bot():
    assert is_bot("anything@example.com", "Flutter Actions", api_login="flutteractionsbot") is True
    assert is_bot("a@x.com", "Cassidy Abbot", api_login="abbot") is False
    assert is_bot("a@x.com", "Henry Bot", api_login="bot") is False
    assert is_bot("a@x.com", "Dep", api_login="dependabot[bot]") is True
    assert is_bot("a@x.com", "Skia", api_login="skiaautoroll") is True


def test_bot_filter_catches_copilot_across_identification_surfaces():
    assert is_bot("a@x.com", "Some Name", api_login="copilot") is True
    assert is_bot("a@x.com", "Copilot", api_login="copilot[bot]") is True
    assert is_bot("a@x.com", "Copilot") is True
    assert is_bot("a@x.com", "GitHub Copilot") is True
    assert is_bot("copilot@github.com", "Anything") is True


def test_extract_noreply_username_handles_both_forms():
    assert extract_noreply_username("alice@users.noreply.github.com") == "alice"
    assert extract_noreply_username("12345+alice@users.noreply.github.com") == "alice"
    assert extract_noreply_username("alice@example.com") is None
    assert extract_noreply_username("") is None


def test_resolve_github_username_prefers_api_login():
    assert resolve_github_username("alice@x.com", "Alice", api_login="alice-gh") == "alice-gh"
    assert (
        resolve_github_username("alice@users.noreply.github.com", "Alice", api_login=None)
        == "alice"
    )
    assert resolve_github_username("alice@x.com", "Alice", api_login=None) is None


def test_parse_coauthors_picks_up_trailers():
    message = """Implement feature

Co-authored-by: Alice <alice@example.com>
Co-authored-by: Bob <bob@example.com>
"""
    assert parse_coauthors(message) == [
        ("Alice", "alice@example.com"),
        ("Bob", "bob@example.com"),
    ]


def test_parse_coauthors_returns_empty_when_no_trailer():
    assert parse_coauthors("Just a commit") == []
    assert parse_coauthors("") == []
