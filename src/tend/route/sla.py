"""SLA-aging report and nudge comments for open Dependabot PRs.

Walks open PRs in a repo, filters to Dependabot authors, classifies
severity by label, computes age against per-severity SLA thresholds,
renders a markdown report to ``$GITHUB_STEP_SUMMARY`` (or stdout), and
optionally posts nudge comments on aging PRs.

Nudge comments are idempotent: a marker HTML comment per (PR, state)
prevents duplicate pings on repeat runs. A PR transitioning
``breach → double_breach`` *will* receive a second comment — that's
intended: the escalation pings the fallback owner.

The SLA clock starts at ``pr.created_at`` and is stable across
Dependabot rebases / force-pushes (which don't update ``created_at``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from tend.github.client import GitHubClient
from tend.output.tend_yaml import OwnershipFile
from tend.route import DEPENDABOT_LOGINS
from tend.route.matcher import find_owners_for_files
from tend.route.skip import DEFAULT_SKIP_PATHS, filter_changed_files

logger = logging.getLogger(__name__)

SLAState = Literal["ok", "breach", "double_breach"]
SeverityName = Literal["security-advisory", "default"]

# Nudge marker format: an HTML comment in the body. Format:
#   <!-- tend-nudge:<level>:<mention> -->
# Idempotency: ``post_nudges`` checks existing comments for a matching
# (level, mention) marker before posting a new one. A breach→double_breach
# state transition produces two markers (different levels), which is by
# design — the escalation pings the fallback owner explicitly.
NUDGE_MARKER_PREFIX = "<!-- tend-nudge:"
NUDGE_MARKER_RE = re.compile(r"<!--\s*tend-nudge:(breach|double_breach):([^\s>]+)\s*-->")


@dataclass(frozen=True)
class AgingPR:
    """A single Dependabot PR's SLA aging state."""

    number: int
    title: str
    url: str
    author: str
    created_at: datetime
    severity: SeverityName
    primary_owner: str | None
    age_hours: float
    sla_hours: int
    sla_state: SLAState
    next_owner: str | None
    fallback_owner: str | None
    changed_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgingReport:
    """Aggregated SLA report for a repo's open Dependabot PRs."""

    generated_at: datetime
    prs: list[AgingPR]
    by_owner: dict[str, list[AgingPR]]
    by_severity: dict[str, list[AgingPR]]


def _classify_severity_from_labels(labels: Sequence[dict[str, Any]]) -> SeverityName:
    """Label-based severity classifier. Same heuristic as ``cli.route``."""
    names = {(lbl or {}).get("name", "") for lbl in labels or []}
    if "security" in names or "security-advisory" in names:
        return "security-advisory"
    return "default"


def _sla_state(age_hours: float, sla_hours: int) -> SLAState:
    """Bucket an age into ok / breach / double_breach.

    Boundaries are inclusive of the *next* bucket: at exactly ``sla_hours``
    the PR is in ``breach``; at exactly ``2 * sla_hours`` it's in
    ``double_breach``. Matches the principle that SLA expiry "starts now"."""
    if age_hours < sla_hours:
        return "ok"
    if age_hours < 2 * sla_hours:
        return "breach"
    return "double_breach"


def _parse_iso8601_utc(value: str) -> datetime:
    """Parse an RFC3339 datetime string into a UTC-aware datetime.
    Tolerates the ``Z`` suffix (GitHub's canonical form)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def collect_aging_prs(
    *,
    gh: GitHubClient,
    owner: str,
    repo: str,
    ownership: OwnershipFile,
    default_sla_hours: int = 72,
    security_sla_hours: int = 24,
    fallback_owner: str | None = None,
    skip_paths: Sequence[str] = DEFAULT_SKIP_PATHS,
    now: datetime | None = None,
    fixture: dict[str, Any] | None = None,
) -> AgingReport:
    """Walk open PRs and compute SLA-aging state for each Dependabot one.

    When ``fixture`` is provided, its ``open_pulls`` field is used in
    place of ``gh.list_open_pulls``, and per-PR ``changed_files`` are
    pulled from ``fixture["changed_files"]`` keyed by PR number. Enables
    offline testing without GitHub.

    ``now`` is injectable for testability (matches the pattern in
    ``output/strength.py``). Defaults to ``datetime.now(UTC)``.
    """
    now = now or datetime.now(UTC)
    pr_files_map: dict[int, list[str]] = {}
    if fixture is not None:
        open_prs = fixture.get("open_pulls", [])
        pr_files_map = {int(k): list(v) for k, v in (fixture.get("changed_files") or {}).items()}
    else:
        open_prs = await gh.list_open_pulls(owner, repo)

    prs: list[AgingPR] = []
    for pr_data in open_prs:
        author = (pr_data.get("user") or {}).get("login") or ""
        if author not in DEPENDABOT_LOGINS:
            continue

        number = int(pr_data["number"])
        severity = _classify_severity_from_labels(pr_data.get("labels") or [])
        sla_hours = security_sla_hours if severity == "security-advisory" else default_sla_hours

        created_str = pr_data.get("created_at", "")
        try:
            created_at = _parse_iso8601_utc(created_str)
        except (ValueError, AttributeError):
            logger.warning("PR #%s has unparseable created_at %r; skipping", number, created_str)
            continue

        age_hours = (now - created_at).total_seconds() / 3600.0
        state = _sla_state(age_hours, sla_hours)

        # Changed files: from fixture if provided, else live fetch.
        if number in pr_files_map:
            raw_files = pr_files_map[number]
        elif fixture is not None:
            raw_files = []
        else:
            files = await gh.get_pr_files(owner, repo, number)
            raw_files = [f["filename"] for f in files if f.get("filename")]
        kept, _ = filter_changed_files(raw_files, skip_paths)

        # Owner resolution: primary (first match's first owner), next (second
        # match's owner or second owner of the same rule) for nudge fallback.
        matches = find_owners_for_files(ownership, kept)
        primary_owner: str | None = None
        next_owner: str | None = None
        for _fp, rule in matches.items():
            if rule is None or not rule.owners:
                continue
            handles = [
                o.github_username if o.github_username.startswith("@") else f"@{o.github_username}"
                for o in rule.owners
            ]
            if primary_owner is None:
                primary_owner = handles[0]
                if len(handles) > 1:
                    next_owner = handles[1]
            elif next_owner is None and handles[0] != primary_owner:
                next_owner = handles[0]

        prs.append(
            AgingPR(
                number=number,
                title=pr_data.get("title", ""),
                url=pr_data.get("html_url", ""),
                author=author,
                created_at=created_at,
                severity=severity,
                primary_owner=primary_owner,
                age_hours=age_hours,
                sla_hours=sla_hours,
                sla_state=state,
                next_owner=next_owner,
                fallback_owner=fallback_owner,
                changed_files=list(kept),
            )
        )

    # Sort: worst SLA state first, then oldest within state.
    state_order: dict[SLAState, int] = {"double_breach": 0, "breach": 1, "ok": 2}
    prs.sort(key=lambda p: (state_order[p.sla_state], -p.age_hours))

    by_owner: dict[str, list[AgingPR]] = {}
    by_severity: dict[str, list[AgingPR]] = {}
    for pr in prs:
        owner_key = pr.primary_owner or pr.fallback_owner or "(unowned)"
        by_owner.setdefault(owner_key, []).append(pr)
        by_severity.setdefault(pr.severity, []).append(pr)

    return AgingReport(generated_at=now, prs=prs, by_owner=by_owner, by_severity=by_severity)


def _fmt_age(hours: float) -> str:
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


_STATE_EMOJI: dict[SLAState, str] = {
    "ok": ":large_green_circle:",
    "breach": ":large_yellow_circle:",
    "double_breach": ":red_circle:",
}


def render_markdown(report: AgingReport) -> str:
    """Render the report as ``$GITHUB_STEP_SUMMARY``-friendly markdown.

    Uses GitHub-flavored emoji shortcodes (rendered in the Actions UI)
    rather than literal emoji codepoints — keeps the file ASCII for
    diff-friendliness.
    """
    lines: list[str] = []
    lines.append("# Tend SLA aging report")
    lines.append("")
    lines.append(f"_Generated at {report.generated_at.isoformat()}_")
    lines.append("")

    counts = {"ok": 0, "breach": 0, "double_breach": 0}
    for pr in report.prs:
        counts[pr.sla_state] += 1
    lines.append(
        f"**Open Dependabot PRs:** {len(report.prs)} "
        f"({_STATE_EMOJI['ok']} {counts['ok']} ok, "
        f"{_STATE_EMOJI['breach']} {counts['breach']} breach, "
        f"{_STATE_EMOJI['double_breach']} {counts['double_breach']} escalate)"
    )
    lines.append("")

    if not report.prs:
        lines.append("_No open Dependabot PRs._")
        return "\n".join(lines)

    lines.append("## By severity")
    lines.append("")
    lines.append("| State | PR | Age | SLA | Severity | Owner | Title |")
    lines.append("|---|---|---|---|---|---|---|")
    for pr in report.prs:
        owner_display = pr.primary_owner or pr.fallback_owner or "_(no owner)_"
        title = pr.title[:60] + ("…" if len(pr.title) > 60 else "")
        lines.append(
            f"| {_STATE_EMOJI[pr.sla_state]} {pr.sla_state} "
            f"| [#{pr.number}]({pr.url}) "
            f"| {_fmt_age(pr.age_hours)} "
            f"| {pr.sla_hours}h "
            f"| {pr.severity} "
            f"| {owner_display} "
            f"| {title} |"
        )
    lines.append("")

    lines.append("## By owner")
    lines.append("")
    for owner_key in sorted(report.by_owner.keys()):
        owner_prs = report.by_owner[owner_key]
        lines.append(f"### {owner_key}")
        lines.append("")
        for pr in owner_prs:
            lines.append(
                f"- {_STATE_EMOJI[pr.sla_state]} "
                f"[#{pr.number}]({pr.url}) — {_fmt_age(pr.age_hours)} "
                f"(SLA {pr.sla_hours}h, {pr.severity})"
            )
        lines.append("")

    return "\n".join(lines)


def _format_nudge_comment(pr: AgingPR, mention: str, level: SLAState) -> str:
    """Build the body of a nudge comment, terminated with the marker."""
    marker = f"{NUDGE_MARKER_PREFIX}{level}:{mention} -->"
    age = _fmt_age(pr.age_hours)
    sev_note = " (security advisory)" if pr.severity == "security-advisory" else ""
    if level == "breach":
        body = (
            f"{mention} — this Dependabot PR has been open for **{age}**, "
            f"past the {pr.sla_hours}h SLA{sev_note}. Could you take a look?"
        )
    else:  # double_breach
        body = (
            f"{mention} — escalating: this Dependabot PR has been open for "
            f"**{age}**, past **2x the {pr.sla_hours}h SLA**{sev_note}. "
            f"Please prioritize."
        )
    return f"{body}\n\n{marker}"


def _has_marker_at_level(body: str, level: str) -> bool:
    """True if comment body contains a tend-nudge marker for ``level``."""
    return any(m.group(1) == level for m in NUDGE_MARKER_RE.finditer(body))


def _pick_mention(pr: AgingPR) -> str | None:
    """Pick whom to mention based on SLA state.

    - ``breach``: next_owner if available, else primary, else fallback.
      The intent is to widen visibility — the primary owner already knew
      about the PR when it was assigned to them.
    - ``double_breach``: fallback first (AppSec / escalation), then any
      remaining owner. The intent is to escalate beyond the team.
    """
    if pr.sla_state == "breach":
        return pr.next_owner or pr.primary_owner or pr.fallback_owner
    if pr.sla_state == "double_breach":
        return pr.fallback_owner or pr.next_owner or pr.primary_owner
    return None


async def post_nudges(
    *,
    gh: GitHubClient,
    owner: str,
    repo: str,
    report: AgingReport,
    dry_run: bool = False,
    existing_comments_map: dict[int, list[dict[str, Any]]] | None = None,
) -> list[tuple[int, SLAState]]:
    """Post nudge comments on PRs in breach / double_breach state.

    Idempotency: each PR is checked for an existing
    ``<!-- tend-nudge:<level>:... -->`` marker at its current level
    before posting; if one exists, the post is skipped. A PR transitioning
    breach → double_breach receives a second comment (different marker
    level) — by design, to escalate.

    When ``existing_comments_map`` is provided (offline / fixture mode),
    its ``{pr_number: [comment_dicts]}`` mapping replaces the live
    ``list_issue_comments`` calls. Missing PR numbers are treated as
    empty (no prior nudges) so a fixture without that field still
    behaves correctly under dry-run.

    Returns ``[(pr_number, level), ...]`` for the posts actually made
    (or *would* have been made under ``dry_run``).
    """
    posted: list[tuple[int, SLAState]] = []
    for pr in report.prs:
        if pr.sla_state == "ok":
            continue

        mention = _pick_mention(pr)
        if mention is None:
            logger.info("PR #%s: no one to nudge — skipping", pr.number)
            continue

        if existing_comments_map is not None:
            existing = existing_comments_map.get(pr.number, [])
        else:
            existing = await gh.list_issue_comments(owner, repo, pr.number)
        if any(_has_marker_at_level(c.get("body", ""), pr.sla_state) for c in existing):
            logger.debug("PR #%s: %s nudge already posted; skipping", pr.number, pr.sla_state)
            continue

        body = _format_nudge_comment(pr, mention, pr.sla_state)
        if not dry_run:
            await gh.post_issue_comment(owner, repo, pr.number, body)
        posted.append((pr.number, pr.sla_state))

    return posted


__all__ = [
    "NUDGE_MARKER_PREFIX",
    "NUDGE_MARKER_RE",
    "AgingPR",
    "AgingReport",
    "collect_aging_prs",
    "post_nudges",
    "render_markdown",
]
