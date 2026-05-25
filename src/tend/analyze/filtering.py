"""Bot filtering, co-author parsing, and GitHub username resolution.

Runs before aggregation in ``commits.py``. The bot filter is intentionally
pattern-heavy because GitHub's ``type: "Bot"`` flag misses service-account
bots that look like real users (``engine-flutter-autoroll``, ``skia-roller``,
``flutteractionsbot``). The patterns here are the same ones the prototype
ported to dogfooding on Flutter, Vue, Rails, and other large repos.

Username resolution is *strict*: if we can't derive a handle from GitHub's
own API or a ``users.noreply.github.com`` email, we drop the contributor
rather than guessing from the email local-part. PRs that mention people
must mention the right people.
"""

from __future__ import annotations

import re

COAUTHOR_WEIGHT = 0.5
COAUTHOR_RE = re.compile(
    r"^\s*Co-authored-by:\s*(.+?)\s*<\s*([^>]+?)\s*>\s*$",
    re.IGNORECASE | re.MULTILINE,
)

BOT_EMAIL_PATTERNS = (
    "noreply",
    "bot@",
    "github-actions",
    "dependabot",
    "renovate",
    "greenkeeper",
    "snyk-bot",
    "codecov",
    "mergify",
    "semantic-release",
)
BOT_LOCAL_EXACT = {"dependabot", "renovate", "github-actions", "snyk-bot", "copilot"}
BOT_LOCAL_SUFFIXES = ("-bot", "[bot]", "-autoroll", "-roller", "-roll-bot")
BOT_NAME_PATTERNS = (
    "[bot]",
    "github actions",
    "dependabot",
    "renovate",
    "automated",
    "autoroll",
    "auto-roll",
    "copilot",
)
GENERIC_NAMES = {"hello", "test", "admin", "root", "git", "user", "unknown", "bot"}


def extract_noreply_username(email: str) -> str | None:
    """Extract the username from a GitHub noreply email, or None if not one.

    Handles both ``username@users.noreply.github.com`` and the newer
    ``12345+username@users.noreply.github.com`` form.
    """
    lower = email.lower().strip()
    if not lower.endswith("@users.noreply.github.com"):
        return None
    prefix = lower.split("@", 1)[0]
    if "+" in prefix:
        prefix = prefix.split("+", 1)[1]
    return prefix or None


def _matches_bot_suffix(local: str) -> bool:
    return any(local.endswith(suf) for suf in BOT_LOCAL_SUFFIXES)


def _login_looks_botty(login: str) -> bool:
    """Heuristic check on a GitHub login.

    Catches bracketed App handles (``dependabot[bot]``) and service-account
    names ending in ``bot`` / ``roll`` (``flutteractionsbot``,
    ``engine-flutter-autoroll``). The length guard prevents false positives
    on short personal handles like ``abbot`` or ``roll``.
    """
    login = login.lower()
    if not login:
        return False
    if "[bot]" in login:
        return True
    if _matches_bot_suffix(login):
        return True
    if login in BOT_LOCAL_EXACT:
        return True
    if len(login) >= 8 and login.endswith("bot"):
        return True
    return len(login) >= 8 and login.endswith("roll")


def is_bot(
    email: str,
    name: str | None,
    api_type: str | None = None,
    api_login: str | None = None,
) -> bool:
    """Decide whether this contributor is a bot.

    ``api_type`` is the GitHub API's ``commit.author.type`` (``"Bot"`` for
    GitHub Apps). ``api_login`` is the GitHub API's ``commit.author.login`` —
    used to catch service-account bots that look like real users.

    The noreply email exception runs only when API signals don't fire,
    because GitHub Apps and humans both use ``users.noreply.github.com``.
    """
    if api_type == "Bot":
        return True
    if api_login and _login_looks_botty(api_login):
        return True

    email = (email or "").strip()
    name = (name or "").strip()

    noreply_user = extract_noreply_username(email)
    if noreply_user:
        u = noreply_user.lower()
        return "[bot]" in u or _matches_bot_suffix(u) or u in BOT_LOCAL_EXACT

    email_lower = email.lower()
    local = email_lower.split("@", 1)[0] if "@" in email_lower else email_lower

    if any(pat in email_lower for pat in BOT_EMAIL_PATTERNS):
        return True
    if _matches_bot_suffix(local):
        return True
    if local in BOT_LOCAL_EXACT:
        return True

    name_lower = name.lower()
    if any(pat in name_lower for pat in BOT_NAME_PATTERNS):
        return True
    return name_lower in GENERIC_NAMES


def resolve_github_username(
    email: str,
    name: str | None,  # noqa: ARG001  (kept for API symmetry; future heuristics may use it)
    api_login: str | None,
) -> str | None:
    """Authoritative-only username resolution.

    Preference: API login → noreply username → ``None``. Returning ``None``
    means the caller should drop this contributor; we don't guess from the
    email local-part because security-routing PRs must mention the right
    person.
    """
    if api_login:
        return api_login
    return extract_noreply_username(email)


def parse_coauthors(message: str) -> list[tuple[str, str]]:
    """Return ``(name, email)`` tuples from ``Co-authored-by:`` trailers."""
    if not message or "co-authored-by" not in message.lower():
        return []
    return [(m.group(1).strip(), m.group(2).strip()) for m in COAUTHOR_RE.finditer(message)]


def bot_label(email: str, name: str | None) -> str:
    """Stable label used in the filtered-bot summary (e.g. ``dependabot[bot]``)."""
    if name and "[bot]" in name.lower():
        return name
    local = (email or "").split("@", 1)[0]
    if local:
        return local
    return name or email or "unknown"
