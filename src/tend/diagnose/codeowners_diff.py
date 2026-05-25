"""CODEOWNERS drift diagnostic — read-only.

Powers ``tend diff --against-codeowners``: parses an existing CODEOWNERS
file, runs Tend's inference, and produces a Rich-formatted terminal
report describing what the existing rules agree with, what looks drifted,
and what Tend would own that CODEOWNERS doesn't cover.

This module **never writes** to CODEOWNERS. It exists so a customer can
evaluate Tend against their current routing without committing to it.

The parsing and pattern-matching logic is ported from the prototype's
``src/codeowners_parser.py`` and ``src/generator.py:95-502``. The CODEOWNERS-
rendering logic (``generate_codeowners``) is intentionally omitted — Tend
v1 does not write CODEOWNERS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tend.analyze._models import InferredOwner

if TYPE_CHECKING:
    from tend.github.client import GitHubClient

CODEOWNERS_PATHS = (
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
)


@dataclass
class CodeownersRule:
    pattern: str
    owners: list[str]
    line_number: int


@dataclass
class CodeownersDiff:
    new_rules: list[InferredOwner] = field(default_factory=list)
    drifted_rules: list[tuple[CodeownersRule, InferredOwner]] = field(default_factory=list)
    confirmed_rules: list[tuple[CodeownersRule, InferredOwner]] = field(default_factory=list)
    existing_with_no_inference: list[CodeownersRule] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "new": len(self.new_rules),
            "drifted": len(self.drifted_rules),
            "confirmed": len(self.confirmed_rules),
            "existing_with_no_inference": len(self.existing_with_no_inference),
        }


# ---- Parsing ----


def parse_codeowners(content: str) -> list[CodeownersRule]:
    """Parse a CODEOWNERS file body into rules.

    Strips full-line and inline comments, requires ``pattern + ≥1 owner``
    per line, otherwise skips. Line numbers are 1-indexed for error reporting.
    """
    rules: list[CodeownersRule] = []
    for lineno, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        owners = parts[1:]
        if owners:
            rules.append(CodeownersRule(pattern=pattern, owners=owners, line_number=lineno))
    return rules


def load_local_codeowners(
    repo_root: str | Path | None = None,
) -> tuple[str | None, list[CodeownersRule]]:
    """Look for a CODEOWNERS file under ``repo_root`` at the three standard paths.

    Returns ``(path_found, rules)`` where ``path_found`` is the relative
    path that matched (or ``None`` if no CODEOWNERS exists locally).
    """
    base = Path(repo_root) if repo_root else Path.cwd()
    for rel in CODEOWNERS_PATHS:
        candidate = base / rel
        if candidate.exists():
            return rel, parse_codeowners(candidate.read_text(encoding="utf-8"))
    return None, []


async def fetch_codeowners(
    gh: GitHubClient, owner: str, repo: str
) -> tuple[str | None, list[CodeownersRule]]:
    """Fetch the CODEOWNERS file from a remote repo. Returns ``(path, rules)``."""
    for path in CODEOWNERS_PATHS:
        content = await gh.get_file_content(owner, repo, path)
        if content is not None:
            return path, parse_codeowners(content)
    return None, []


# ---- Pattern matching ----


def _normalize_pattern_for_match(pattern: str) -> str:
    """Canonical form for exact-pattern matching.

    Both inferred and declared rules normalize to ``/foo/bar/`` form
    regardless of whether they were written as ``foo/bar``, ``/foo/bar/``,
    ``/foo/bar/**``, etc.
    """
    if pattern == "*":
        return "*"
    p = pattern.strip()
    if p.endswith("/**"):
        p = p[:-3]
    if not p.startswith("/"):
        p = "/" + p
    if not p.endswith("/"):
        p = p + "/"
    return p


_GLOB_CHARS = frozenset("*?[")


def _pattern_directory_container(pattern: str) -> str:
    """Most specific repo-rooted directory containing every file the pattern matches.

    Used in the container-fallback matching phase so a declared file-level
    rule (``/src/foo/bar.py``) and an inferred directory rule (``/src/foo/``)
    can be compared on ownership.
    """
    if not pattern or pattern.strip() in {"", "*"}:
        return "/"
    p = pattern.strip()
    if p.endswith("/**"):
        p = p[:-3] + "/"
    pattern_ends_in_slash = p.endswith("/")
    if p.startswith("/"):
        p = p[1:]
    if not p:
        return "/"

    components = p.split("/")
    container_parts: list[str] = []
    saw_glob = False
    for c in components:
        if any(ch in _GLOB_CHARS for ch in c):
            saw_glob = True
            break
        container_parts.append(c)

    while container_parts and container_parts[-1] == "":
        container_parts.pop()
    if not pattern_ends_in_slash and not saw_glob and container_parts:
        container_parts.pop()
    if not container_parts:
        return "/"
    return "/" + "/".join(container_parts) + "/"


def _containers_overlap(a: str, b: str) -> bool:
    """Two normalized containers overlap when one is a prefix of the other.

    Root ``/`` only overlaps with itself so wildcard patterns don't absorb
    every directory rule on the right-hand side.
    """
    if a == "/" or b == "/":
        return a == b
    return a.startswith(b) or b.startswith(a)


def _is_team_handle(handle: str) -> bool:
    return handle.startswith("@") and "/" in handle


def _owner_handle(owner) -> str:
    h = owner.github_username
    if not h:
        return ""
    return h if h.startswith("@") else f"@{h}"


def _owner_matches(
    existing_owner: str,
    inferred_logins: set[str],
    teams_by_handle: dict[str, set[str]] | None,
) -> bool:
    """Does an existing CODEOWNERS owner match the inferred contributors?

    Team handles use preserve-on-no-data: when ``teams_by_handle`` is
    empty (or the caller couldn't fetch team memberships at all) we
    trust the human-declared team rather than flooding the drift report
    with false positives. When membership data is available the team is
    confirmed only if ≥50% of the inferred logins are on its roster.
    """
    handle = existing_owner.lower()
    if _is_team_handle(existing_owner):
        if not teams_by_handle:
            return True
        members = teams_by_handle.get(handle, set())
        if not members:
            return False
        if not inferred_logins:
            return False
        hits = len(inferred_logins & members)
        return hits * 2 >= len(inferred_logins)
    bare = handle.lstrip("@")
    return bare in {x.lower() for x in inferred_logins}


def _classify_pair(
    existing: CodeownersRule,
    inferred_rule: InferredOwner,
    diff: CodeownersDiff,
    teams_by_handle: dict[str, set[str]] | None,
) -> None:
    inferred_logins: set[str] = set()
    for o in inferred_rule.owners:
        handle = _owner_handle(o)
        if _is_team_handle(handle):
            inferred_logins.add(handle.lower())
        elif o.github_username:
            inferred_logins.add(o.github_username.lower())
    confirmed = any(_owner_matches(eo, inferred_logins, teams_by_handle) for eo in existing.owners)
    if confirmed:
        diff.confirmed_rules.append((existing, inferred_rule))
    else:
        diff.drifted_rules.append((existing, inferred_rule))


def diff_codeowners(
    existing_rules: list[CodeownersRule],
    inferred: list[InferredOwner],
    teams_by_handle: dict[str, set[str]] | None = None,
) -> CodeownersDiff:
    """Two-phase matching: exact-pattern, then container-overlap fallback.

    The fallback handles real-world CODEOWNERS files where declared rules
    are at finer granularity than the directory-level inference (``/src/foo/*.py``
    vs ``/src/foo/``). Without it, most mature CODEOWNERS files end up
    in ``existing_with_no_inference`` even when the inference has signal
    for the surrounding directory.
    """
    existing_by_pattern = {_normalize_pattern_for_match(r.pattern): r for r in existing_rules}
    inferred_by_pattern = {r.path_pattern: r for r in inferred}

    diff = CodeownersDiff()
    matched_existing: set[str] = set()
    matched_inferred: set[str] = set()

    # Phase 1: exact pattern matches.
    for inferred_pat, inferred_rule in inferred_by_pattern.items():
        existing = existing_by_pattern.get(inferred_pat)
        if existing is None:
            continue
        matched_existing.add(inferred_pat)
        matched_inferred.add(inferred_pat)
        _classify_pair(existing, inferred_rule, diff, teams_by_handle)

    # Phase 2: container-based fallback.
    unmatched_existing = [
        (pat, existing_by_pattern[pat])
        for pat in existing_by_pattern
        if pat not in matched_existing
    ]
    unmatched_inferred = [
        (pat, inferred_by_pattern[pat])
        for pat in inferred_by_pattern
        if pat not in matched_inferred
    ]
    inferred_containers = {pat: _pattern_directory_container(pat) for pat, _ in unmatched_inferred}

    for ex_pat, existing in sorted(unmatched_existing):
        ex_container = _pattern_directory_container(existing.pattern)
        best_inf_pat: str | None = None
        best_strength = -1
        for inf_pat, _ in unmatched_inferred:
            inf_container = inferred_containers[inf_pat]
            if not _containers_overlap(ex_container, inf_container):
                continue
            strength = min(len(ex_container), len(inf_container))
            if strength > best_strength:
                best_strength = strength
                best_inf_pat = inf_pat
        if best_inf_pat is not None:
            matched_existing.add(ex_pat)
            matched_inferred.add(best_inf_pat)
            _classify_pair(existing, inferred_by_pattern[best_inf_pat], diff, teams_by_handle)

    # Phase 3: bucket what remains.
    for inf_pat, inferred_rule in inferred_by_pattern.items():
        if inf_pat not in matched_inferred:
            diff.new_rules.append(inferred_rule)
    for ex_pat, existing in existing_by_pattern.items():
        if ex_pat not in matched_existing:
            diff.existing_with_no_inference.append(existing)

    return diff


# ---- Rich report ----


def render_report(diff: CodeownersDiff, codeowners_path: str | None):
    """Build a Rich-renderable terminal report from a CodeownersDiff.

    Returns a ``rich.console.Group`` of tables — callers print it via
    ``rich.console.Console.print(report)``. Sections with zero rows are
    omitted so the output stays readable on small repos.
    """
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    summary = diff.summary
    header = Panel(
        Text(
            f"Compared inferred ownership against "
            f"`{codeowners_path or 'no CODEOWNERS found'}`.\n"
            f"Confirmed: {summary['confirmed']}    "
            f"Drifted: {summary['drifted']}    "
            f"Inferred-only: {summary['new']}    "
            f"Declared-only: {summary['existing_with_no_inference']}"
        ),
        title="tend diff — CODEOWNERS drift",
        border_style="cyan",
    )

    confirmed = Table(title="Confirmed (declared matches inference)", show_lines=False)
    confirmed.add_column("Pattern", style="green")
    confirmed.add_column("Declared owners")
    confirmed.add_column("Top inferred")
    for existing, inferred in diff.confirmed_rules:
        top = inferred.owners[0].github_username if inferred.owners else "(none)"
        confirmed.add_row(existing.pattern, " ".join(existing.owners), f"@{top.lstrip('@')}")

    drifted = Table(title="Drifted (declared owner ≠ top contributor)", show_lines=False)
    drifted.add_column("Pattern", style="yellow")
    drifted.add_column("Declared owners")
    drifted.add_column("Top inferred")
    for existing, inferred in diff.drifted_rules:
        top = inferred.owners[0].github_username if inferred.owners else "(none)"
        drifted.add_row(existing.pattern, " ".join(existing.owners), f"@{top.lstrip('@')}")

    new_rules = Table(title="Inferred-only (tend would propose)", show_lines=False)
    new_rules.add_column("Pattern", style="blue")
    new_rules.add_column("Inferred owner")
    for r in diff.new_rules:
        top = r.owners[0].github_username if r.owners else "(none)"
        new_rules.add_row(r.path_pattern, f"@{top.lstrip('@')}")

    declared_only = Table(
        title="Declared-only (no inference signal for this path)", show_lines=False
    )
    declared_only.add_column("Pattern", style="dim")
    declared_only.add_column("Declared owners")
    for r in diff.existing_with_no_inference:
        declared_only.add_row(r.pattern, " ".join(r.owners))

    sections = [header]
    for t in (confirmed, drifted, new_rules, declared_only):
        if t.row_count > 0:
            sections.append(t)
    return Group(*sections)
