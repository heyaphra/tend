"""Typer CLI entrypoint for tend.

Subcommands:

- ``tend analyze`` — run inference, write ``.tend/owners.yml``, optionally PR.
- ``tend diff --against-codeowners`` — diagnostic CODEOWNERS drift report.
  Read-only; never writes to CODEOWNERS.
- ``tend explain`` — per-contributor scoring breakdown for a single path.
- ``tend route`` — Actions-runner entrypoint; reads ``EVENT_NAME`` /
  ``EVENT_PATH`` and routes Dependabot-authored PRs to ``route_pr`` (v1).
  Non-Dependabot PRs, non-PR events, and ``dependabot_alert`` (v1.1 planned)
  are logged and skipped.
- ``tend create-pr`` — convenience wrapper for ``analyze --create-pr``.

The CLI itself is intentionally thin: each subcommand parses flags,
constructs the necessary objects, and delegates to the analyze /
output / route / diagnose packages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, cast

import typer
from rich.console import Console

from tend import __version__
from tend.analyze.commits import analyze_contributions
from tend.analyze.inference import infer, suppress_individual_wildcard
from tend.analyze.team_substitution import teams_by_handle
from tend.config import SENSITIVITY_PRESETS, Sensitivity, resolve_config
from tend.diagnose.codeowners_diff import (
    diff_codeowners,
    load_local_codeowners,
    parse_codeowners,
    render_report,
)
from tend.github.associations import Association, fetch_associations
from tend.github.auth import get_auth_from_env, token_auth
from tend.github.client import GitHubClient
from tend.github.pr import create_or_update_tend_pr
from tend.github.teams import TeamMembership, fetch_org_teams
from tend.output.explain import explain_directory
from tend.output.strength import annotate_rules
from tend.output.tend_yaml import dump_full
from tend.route import DEPENDABOT_LOGINS as _ROUTE_DEPENDABOT_LOGINS
from tend.route.parser import load_owners_yml
from tend.route.router import VALID_MODES, RoutingMode, route_pr
from tend.route.skip import DEFAULT_SKIP_PATHS, filter_changed_files
from tend.route.sla import collect_aging_prs, post_nudges, render_markdown

logger = logging.getLogger(__name__)
console = Console()

app = typer.Typer(
    name="tend",
    help="Infer code ownership and route security findings.",
    no_args_is_help=True,
)

DEFAULT_CODEOWNERS_PROBE = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
# Canonical home is ``tend.route.__init__``; re-exported here for back-compat
# so anything still importing ``tend.cli.DEPENDABOT_LOGINS`` keeps working.
DEPENDABOT_LOGINS = _ROUTE_DEPENDABOT_LOGINS


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"tend {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(  # noqa: ARG001
        False,
        "--version",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _split_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        raise typer.BadParameter("--repo must be 'owner/name'")
    owner, name = repo.split("/", 1)
    return owner, name


def _auth_from_token_or_env(token: str | None):
    return token_auth(token) if token else get_auth_from_env()


def _validate_sensitivity(value: str) -> Sensitivity:
    if value not in SENSITIVITY_PRESETS:
        raise typer.BadParameter(f"--sensitivity must be one of: {', '.join(SENSITIVITY_PRESETS)}")
    return cast("Sensitivity", value)


# -------- analyze --------


@app.command()
def analyze(
    repo: str = typer.Option(..., "--repo", help="GitHub repo as owner/name"),
    sensitivity: str = typer.Option(
        "balanced",
        "--sensitivity",
        help="strict | balanced | permissive",
    ),
    lookback_days: int = typer.Option(180, "--lookback-days"),
    advanced: str = typer.Option("{}", "--advanced", help="JSON overrides for InferenceConfig"),
    output_path: Path = typer.Option(
        Path(".tend/owners.yml"), "--output-path", help="Where to write the YAML"
    ),
    create_pr: bool = typer.Option(False, "--create-pr", help="Open a PR if drift detected"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print result to stdout instead of writing or opening a PR",
    ),
    github_token: str | None = typer.Option(None, "--github-token", envvar="TEND_GITHUB_TOKEN"),
    no_associations: bool = typer.Option(
        False,
        "--no-associations",
        help="Skip the GraphQL fetch for authorAssociation weighting "
        "(useful for tokens lacking the required scope).",
    ),
    no_team_resolution: bool = typer.Option(
        False,
        "--no-team-resolution",
        help="Skip the org team fetch and emit individual handles only "
        "(useful for tokens lacking read:org / members:read).",
    ),
) -> None:
    """Run ownership inference and write ``.tend/owners.yml``."""
    owner, name = _split_repo(repo)
    sens = _validate_sensitivity(sensitivity)
    overrides = json.loads(advanced) if advanced and advanced != "{}" else None
    config = resolve_config(sens, lookback_days=lookback_days, overrides=overrides)
    auth = _auth_from_token_or_env(github_token)

    async def _run() -> int:
        async with GitHubClient(auth=auth) as gh:
            contribution_result = await analyze_contributions(gh, owner, name, config)
            associations: dict[str, Association] | None = None
            if not no_associations:
                associations = await fetch_associations(gh, owner, name)
            teams: list[TeamMembership] | None = None
            if not no_team_resolution:
                teams = await fetch_org_teams(gh, owner)
            inferred = infer(contribution_result.contributions, config, associations, teams)
            inferred.rules = suppress_individual_wildcard(inferred.rules)
            annotate_rules(
                inferred.rules,
                min_confidence=config.min_confidence,
                lookback_days=config.lookback_days,
            )
            yaml_content = dump_full(inferred.rules, config, sens)

            if dry_run:
                console.print(f"# Dry run: would write to {output_path}\n")
                console.print(yaml_content)
                return 0

            if create_pr:
                result = await create_or_update_tend_pr(
                    gh=gh,
                    owner=owner,
                    repo=name,
                    rules=inferred.rules,
                    config=config,
                    sensitivity=sens,
                    file_path=str(output_path),
                )
                console.print(f"[green]PR action:[/green] {result.action}")
                if result.url:
                    console.print(f"  URL: {result.url}")
                return 0

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(yaml_content, encoding="utf-8")
            console.print(f"[green]Wrote[/green] {output_path}")
            return 0

    raise typer.Exit(asyncio.run(_run()))


# -------- diff --------


@app.command()
def diff(
    against_codeowners: Path = typer.Option(
        None,
        "--against-codeowners",
        help="Path to existing CODEOWNERS file (default: probe .github/CODEOWNERS, ./CODEOWNERS)",
    ),
    repo: str = typer.Option(..., "--repo", help="GitHub repo as owner/name"),
    sensitivity: str = typer.Option("balanced", "--sensitivity"),
    lookback_days: int = typer.Option(180, "--lookback-days"),
    github_token: str | None = typer.Option(None, "--github-token", envvar="TEND_GITHUB_TOKEN"),
    no_associations: bool = typer.Option(
        False, "--no-associations", help="Skip the GraphQL authorAssociation fetch."
    ),
    no_team_resolution: bool = typer.Option(
        False,
        "--no-team-resolution",
        help="Skip the org team fetch and emit individual handles only "
        "(useful for tokens lacking read:org / members:read).",
    ),
) -> None:
    """Compare inferred ownership against an existing CODEOWNERS file.

    Read-only diagnostic. Prints a Rich-formatted drift report to the
    terminal. Never modifies CODEOWNERS.
    """
    owner, name = _split_repo(repo)
    sens = _validate_sensitivity(sensitivity)
    config = resolve_config(sens, lookback_days=lookback_days)
    auth = _auth_from_token_or_env(github_token)

    codeowners_path, existing_rules = _load_codeowners(against_codeowners)

    async def _run() -> int:
        async with GitHubClient(auth=auth) as gh:
            contribution_result = await analyze_contributions(gh, owner, name, config)
            associations: dict[str, Association] | None = None
            if not no_associations:
                associations = await fetch_associations(gh, owner, name)
            teams: list[TeamMembership] | None = None
            if not no_team_resolution:
                teams = await fetch_org_teams(gh, owner)
            inferred = infer(contribution_result.contributions, config, associations, teams)
            inferred.rules = suppress_individual_wildcard(inferred.rules)

        team_map = teams_by_handle(teams) if teams else None
        d = diff_codeowners(existing_rules, inferred.rules, teams_by_handle=team_map)
        console.print(render_report(d, codeowners_path))
        return 0

    raise typer.Exit(asyncio.run(_run()))


def _load_codeowners(
    explicit_path: Path | None,
) -> tuple[str | None, list]:
    if explicit_path is not None:
        if not explicit_path.exists():
            raise typer.BadParameter(f"--against-codeowners path does not exist: {explicit_path}")
        return str(explicit_path), parse_codeowners(explicit_path.read_text(encoding="utf-8"))
    return load_local_codeowners()


# -------- explain --------


@app.command()
def explain(
    path: str = typer.Option(..., "--path", help="Directory or path to explain"),
    repo: str = typer.Option(..., "--repo", help="GitHub repo as owner/name"),
    sensitivity: str = typer.Option("balanced", "--sensitivity"),
    lookback_days: int = typer.Option(180, "--lookback-days"),
    github_token: str | None = typer.Option(None, "--github-token", envvar="TEND_GITHUB_TOKEN"),
    no_associations: bool = typer.Option(
        False, "--no-associations", help="Skip the GraphQL authorAssociation fetch."
    ),
) -> None:
    """Show the per-contributor scoring breakdown for a single directory."""
    owner, name = _split_repo(repo)
    sens = _validate_sensitivity(sensitivity)
    config = resolve_config(sens, lookback_days=lookback_days)
    auth = _auth_from_token_or_env(github_token)

    async def _run() -> int:
        async with GitHubClient(auth=auth) as gh:
            result = await analyze_contributions(gh, owner, name, config)
            associations: dict[str, Association] | None = None
            if not no_associations:
                associations = await fetch_associations(gh, owner, name)
        rows = explain_directory(result.contributions, path, config, associations)
        if not rows:
            console.print(f"[yellow]No contributions found for {path}[/yellow]")
            return 0
        from rich.table import Table

        table = Table(title=f"Contributors to {path}")
        table.add_column("Contributor")
        table.add_column("Assoc.")
        table.add_column("Weight", justify="right")
        table.add_column("Commits", justify="right")
        table.add_column("Lines+", justify="right")
        table.add_column("Lines-", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Share lower", justify="right")
        table.add_column("95% CI", justify="right")
        table.add_column("Base")
        table.add_column("Days stale", justify="right")
        table.add_column("Final")
        table.add_column("Status")
        for row in rows:
            share_lower = row.get("share_lower")
            share_upper = row.get("share_upper")
            lower_cell = f"{share_lower:.2f}" if share_lower is not None else "—"
            ci_cell = (
                f"[{share_lower:.2f}, {share_upper:.2f}]"
                if share_lower is not None and share_upper is not None
                else "—"
            )
            base_cell = row.get("base_strength") or "—"
            final_cell = row.get("final_strength") or "—"
            assoc_cell = str(row["association"].value) if row.get("association") else "—"
            weight_cell = (
                f"{row['association_weight']:.2f}"
                if row.get("association_weight") is not None
                else "—"
            )
            table.add_row(
                str(row["username"]),
                assoc_cell,
                weight_cell,
                f"{row['commits']:g}",
                f"{row['lines_added']:g}",
                f"{row['lines_removed']:g}",
                f"{row['score']:.0f}",
                lower_cell,
                ci_cell,
                base_cell,
                f"{row['days_stale']:d}",
                final_cell,
                row["status"],
            )
        console.print(table)
        return 0

    raise typer.Exit(asyncio.run(_run()))


# -------- route --------


@app.command()
def route(
    owners_file: Path = typer.Option(
        Path(".tend/owners.yml"),
        "--owners-file",
        help="Path to the ownership file",
    ),
    mode: str = typer.Option("assign", "--mode", help=" | ".join(VALID_MODES)),
    repo: str = typer.Option(..., "--repo", help="GitHub repo as owner/name"),
    github_token: str | None = typer.Option(None, "--github-token", envvar="TEND_GITHUB_TOKEN"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be routed without calling GitHub",
    ),
    # ---- v0.3 routing extensions ----
    security_cc: str | None = typer.Option(
        None,
        "--security-cc",
        help=(
            "Extra reviewer (e.g. '@org/appsec') added when the PR has a "
            "'security' label. CC'd in addition to per-file owners."
        ),
    ),
    fallback_owner: str | None = typer.Option(
        None,
        "--fallback-owner",
        help=(
            "Handle to route to when no per-file rule in owners.yml matches "
            "(e.g. '@org/appsec'). Useful for catching coverage gaps."
        ),
    ),
    extra_skip_path: list[str] = typer.Option(
        [],
        "--extra-skip-path",
        help=(
            "Additional skip pattern (repeatable). Same shapes as the matcher: "
            "'foo/' prefix, exact path, or '*.ext' basename glob. Added to "
            f"the built-in defaults: {', '.join(DEFAULT_SKIP_PATHS)}."
        ),
    ),
    # ---- v0.3 dev velocity flags ----
    event_file: Path | None = typer.Option(
        None,
        "--event-file",
        help=(
            "Override EVENT_PATH with a local JSON file (dev/test). "
            "Capture a real payload via `cat $GITHUB_EVENT_PATH > fixture.json`."
        ),
    ),
    changed_files: str | None = typer.Option(
        None,
        "--changed-files",
        help=(
            "Comma-separated changed paths. Overrides the GitHub API call to "
            "fetch PR files. With --dry-run, no GitHub API call is made at all."
        ),
    ),
) -> None:
    """Route a Dependabot-authored PR to its owners.

    Reads the GitHub event from ``EVENT_NAME`` / ``EVENT_PATH`` env vars
    (set by the Actions runner) — or from ``--event-file`` for local
    testing. v1 only routes PRs whose author is ``dependabot[bot]`` (or
    the legacy ``dependabot-preview[bot]``); other PRs, non-PR events,
    and ``dependabot_alert`` (v1.1 planned) are logged and skipped.

    Bot PRs are routed as review requests, not assignments: assignees
    imply ownership of a work item, which doesn't apply to a bot. When
    ``--mode=assign`` is passed for a bot PR, tend downgrades to
    ``request-review``.

    Files under ``node_modules/``, ``vendor/``, ``dist/``, ``build/``, and
    ``*.lock`` basenames are stripped before owner matching — they have
    no meaningful owner. If every file is stripped and ``--fallback-owner``
    is set, the PR routes to the fallback.
    """
    if mode not in VALID_MODES:
        raise typer.BadParameter(f"--mode must be one of: {', '.join(VALID_MODES)}")
    owner, name = _split_repo(repo)

    # ---- Resolve event source: --event-file overrides env vars. ----
    event_name = os.environ.get("EVENT_NAME", "")
    event_path = str(event_file) if event_file else os.environ.get("EVENT_PATH", "")
    if event_file and not event_name:
        # Local fixture: assume pull_request unless caller is explicit.
        event_name = "pull_request"

    if not event_name:
        console.print("[yellow]No EVENT_NAME set — nothing to route.[/yellow]")
        raise typer.Exit(0)

    if event_name.startswith("dependabot_alert"):
        console.print(
            "[yellow]dependabot_alert events are not yet routed (v1.1 planned). Skipping.[/yellow]"
        )
        if event_path:
            console.print(f"  Payload: {event_path}", style="dim")
        raise typer.Exit(0)

    if not event_name.startswith("pull_request"):
        console.print(f"[yellow]Event {event_name!r} not routed by v1 — skipping.[/yellow]")
        raise typer.Exit(0)

    if not event_path or not Path(event_path).exists():
        console.print("[red]EVENT_PATH not set or missing.[/red]")
        raise typer.Exit(1)

    payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pr_payload = payload.get("pull_request") or {}
    author_login = (pr_payload.get("user") or {}).get("login", "")
    if author_login not in DEPENDABOT_LOGINS:
        console.print(
            f"[dim]PR #{pr_payload.get('number') or '?'} by "
            f"@{author_login or '<unknown>'} is not a Dependabot PR — "
            f"skipping (MVP routes Dependabot only).[/dim]"
        )
        raise typer.Exit(0)

    pr_number = pr_payload.get("number") or payload.get("number")
    if not pr_number:
        console.print("[red]Could not find PR number in event payload.[/red]")
        raise typer.Exit(1)

    # ---- Security awareness: label-based detection. ----
    label_names = {(lbl or {}).get("name", "") for lbl in pr_payload.get("labels") or []}
    is_security = "security" in label_names or "security-advisory" in label_names

    extra_reviewers: list[str] | None = None
    if is_security and security_cc:
        extra_reviewers = [security_cc]

    fallback_handles: list[str] | None = [fallback_owner] if fallback_owner else None

    ownership = load_owners_yml(owners_file)

    # ---- Dev velocity: pre-parse --changed-files; skip token when fully offline. ----
    changed_files_override: list[str] | None = None
    if changed_files:
        changed_files_override = [f.strip() for f in changed_files.split(",") if f.strip()]
        # When the file list is supplied locally and we're dry-running, no
        # GitHub API call is ever issued (the bot override gives us
        # request-review, which doesn't fetch the PR; dry-run blocks writes).
        # In that case allow a missing token — improves dev DX.
        if dry_run and not github_token and not os.environ.get("TEND_GITHUB_TOKEN"):
            github_token = "dev-only-fake-token"

    auth = _auth_from_token_or_env(github_token)

    skip_paths = (*DEFAULT_SKIP_PATHS, *extra_skip_path)

    async def _run() -> int:
        async with GitHubClient(auth=auth) as gh:
            if changed_files_override is not None:
                raw_files = changed_files_override
            else:
                changed = await gh.get_pr_files(owner, name, pr_number)
                raw_files = [f["filename"] for f in changed if f.get("filename")]

            kept, filtered = filter_changed_files(raw_files, skip_paths)

            result = await route_pr(
                gh=gh,
                owner=owner,
                repo=name,
                pr_number=pr_number,
                changed_files=kept,
                ownership=ownership,
                mode=cast("RoutingMode", mode),
                dry_run=dry_run,
                is_bot_author=True,  # we already filtered to DEPENDABOT_LOGINS above
                extra_reviewers=extra_reviewers,
                fallback_handles=fallback_handles,
            )
        _print_routing_result(result, filtered_files=filtered, is_security=is_security)
        return 0

    raise typer.Exit(asyncio.run(_run()))


def _print_routing_result(
    result: Any,
    *,
    filtered_files: list[str] | None = None,
    is_security: bool = False,
) -> None:
    prefix = "[dim](dry-run)[/dim] " if result.dry_run else ""
    if is_security:
        console.print("[red]Security-advisory PR detected (label-based).[/red]")
    if getattr(result, "effective_mode", None):
        console.print(f"[dim]Effective mode: {result.effective_mode}[/dim]")
    if getattr(result, "fallback_used", False):
        console.print(f"{prefix}[yellow]Fallback used:[/yellow] no per-file rule matched.")
    if result.assigned:
        console.print(f"{prefix}[green]Assigned:[/green] {', '.join(result.assigned)}")
    if result.removed_assignees:
        console.print(
            f"{prefix}[yellow]Removed prior assignees:[/yellow] "
            f"{', '.join(result.removed_assignees)}"
        )
    if result.reviewers_requested or result.team_reviewers_requested:
        all_revs = [
            *result.reviewers_requested,
            *(f"@org/{t}" for t in result.team_reviewers_requested),
        ]
        console.print(f"{prefix}[green]Reviewers requested:[/green] {', '.join(all_revs)}")
    if getattr(result, "cc_handles", None):
        console.print(f"[dim]CC: {', '.join(result.cc_handles)}[/dim]")
    if result.comment_posted:
        console.print(f"{prefix}[green]Comment posted.[/green]")
    if filtered_files:
        console.print(
            f"[dim]Skipped {len(filtered_files)} vendored/generated file(s): "
            f"{', '.join(filtered_files[:3])}"
            + (f", … (+{len(filtered_files) - 3} more)" if len(filtered_files) > 3 else "")
            + "[/dim]"
        )
    if result.skipped_files:
        console.print(
            f"[dim]No matching rule for {len(result.skipped_files)} files: "
            f"{', '.join(result.skipped_files[:3])}"
            + (
                f", … (+{len(result.skipped_files) - 3} more)"
                if len(result.skipped_files) > 3
                else ""
            )
            + "[/dim]"
        )


# -------- sla --------


@app.command()
def sla(
    repo: str = typer.Option(..., "--repo", help="GitHub repo as owner/name"),
    owners_file: Path = typer.Option(
        Path(".tend/owners.yml"),
        "--owners-file",
        help="Path to the ownership file",
    ),
    github_token: str | None = typer.Option(None, "--github-token", envvar="TEND_GITHUB_TOKEN"),
    sla_hours: int = typer.Option(
        72, "--sla-hours", help="Default SLA threshold in hours (regular bumps)."
    ),
    security_sla_hours: int = typer.Option(
        24,
        "--security-sla-hours",
        help="SLA threshold (hours) for PRs labeled 'security' or 'security-advisory'.",
    ),
    fallback_owner: str | None = typer.Option(
        None,
        "--fallback-owner",
        help="Handle to mention at escalation when owners.yml has no match.",
    ),
    extra_skip_path: list[str] = typer.Option(
        [],
        "--extra-skip-path",
        help="Additional skip pattern (repeatable). Added to defaults.",
    ),
    nudge: bool = typer.Option(
        False,
        "--nudge",
        help=(
            "Post nudge comments on PRs in breach / double_breach state. "
            "Idempotent via HTML-comment markers."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="With --nudge, print intended comments without posting them.",
    ),
    fixture: Path | None = typer.Option(
        None,
        "--fixture",
        help=(
            "JSON file with {open_pulls: [...], changed_files: {n: [...]}}. "
            "Replaces live list_open_pulls calls. Offline dev/test."
        ),
    ),
) -> None:
    """Report Dependabot PR aging vs SLA; optionally post nudge comments.

    Walks open PRs, filters to Dependabot authors, classifies severity by
    label (``security`` / ``security-advisory`` → ``--security-sla-hours``,
    everything else → ``--sla-hours``), and renders a markdown table to
    ``$GITHUB_STEP_SUMMARY`` if set, else stdout.

    SLA clock starts at ``pull_request.created_at`` (stable across
    Dependabot rebases / force-pushes).

    Always exits 0 — aging is informational, not a build failure.
    """
    owner_name, repo_name = _split_repo(repo)
    ownership = load_owners_yml(owners_file)

    fixture_data: dict[str, Any] | None = None
    if fixture:
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        # Offline: no API calls if fixture covers everything; allow a fake token.
        if not github_token and not os.environ.get("TEND_GITHUB_TOKEN"):
            github_token = "dev-only-fake-token"

    auth = _auth_from_token_or_env(github_token)
    skip_paths = (*DEFAULT_SKIP_PATHS, *extra_skip_path)

    async def _run() -> None:
        async with GitHubClient(auth=auth) as gh:
            report = await collect_aging_prs(
                gh=gh,
                owner=owner_name,
                repo=repo_name,
                ownership=ownership,
                default_sla_hours=sla_hours,
                security_sla_hours=security_sla_hours,
                fallback_owner=fallback_owner,
                skip_paths=skip_paths,
                fixture=fixture_data,
            )
            markdown = render_markdown(report)

            summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as fh:
                    fh.write(markdown + "\n")
                console.print(
                    f"[green]Wrote SLA report to $GITHUB_STEP_SUMMARY "
                    f"({len(report.prs)} PR(s)).[/green]"
                )
            else:
                console.print(markdown)

            if nudge:
                # In fixture mode, use the fixture's existing_comments
                # (if provided) instead of hitting the live API.
                existing_map: dict[int, list[Any]] | None = None
                if fixture_data is not None:
                    raw_map = fixture_data.get("existing_comments") or {}
                    existing_map = {int(k): list(v) for k, v in raw_map.items()}
                posted = await post_nudges(
                    gh=gh,
                    owner=owner_name,
                    repo=repo_name,
                    report=report,
                    dry_run=dry_run,
                    existing_comments_map=existing_map,
                )
                verb = "Would post" if dry_run else "Posted"
                if posted:
                    console.print(
                        f"[green]{verb} {len(posted)} nudge(s): "
                        f"{', '.join(f'#{n} ({lvl})' for n, lvl in posted)}[/green]"
                    )
                else:
                    console.print("[dim]No nudges needed (or all already posted).[/dim]")

    asyncio.run(_run())


# -------- create-pr (convenience wrapper) --------


@app.command("create-pr")
def create_pr(
    repo: str = typer.Option(..., "--repo", help="GitHub repo as owner/name"),
    sensitivity: str = typer.Option("balanced", "--sensitivity"),
    lookback_days: int = typer.Option(180, "--lookback-days"),
    output_path: Path = typer.Option(Path(".tend/owners.yml"), "--output-path"),
    github_token: str | None = typer.Option(None, "--github-token", envvar="TEND_GITHUB_TOKEN"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Convenience wrapper for ``tend analyze --create-pr``."""
    sys.argv = [
        sys.argv[0],
        "analyze",
        "--repo",
        repo,
        "--sensitivity",
        sensitivity,
        "--lookback-days",
        str(lookback_days),
        "--output-path",
        str(output_path),
        "--create-pr",
    ]
    if dry_run:
        sys.argv.append("--dry-run")
    if github_token:
        sys.argv.extend(["--github-token", github_token])
    app()
