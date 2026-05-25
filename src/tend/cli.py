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
from tend.route.parser import load_owners_yml
from tend.route.router import VALID_MODES, RoutingMode, route_pr

logger = logging.getLogger(__name__)
console = Console()

app = typer.Typer(
    name="tend",
    help="Infer code ownership and route security findings.",
    no_args_is_help=True,
)

DEFAULT_CODEOWNERS_PROBE = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
DEPENDABOT_LOGINS = frozenset({"dependabot[bot]", "dependabot-preview[bot]"})


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
) -> None:
    """Route a Dependabot-authored PR to its owners.

    Reads the GitHub event from ``EVENT_NAME`` / ``EVENT_PATH`` env vars
    (set by the Actions runner). v1 only routes PRs whose author is
    ``dependabot[bot]`` (or the legacy ``dependabot-preview[bot]``);
    other PRs, non-PR events, and ``dependabot_alert`` (v1.1 planned)
    are logged and skipped.

    When ``--mode=assign``/``all`` the resulting assignee list is
    canonical: pre-existing assignees not in tend's picks are removed
    before tend's picks are added. GitHub does not expose assignee
    provenance, so this override is unconditional.
    """
    if mode not in VALID_MODES:
        raise typer.BadParameter(f"--mode must be one of: {', '.join(VALID_MODES)}")
    owner, name = _split_repo(repo)
    auth = _auth_from_token_or_env(github_token)

    event_name = os.environ.get("EVENT_NAME", "")
    event_path = os.environ.get("EVENT_PATH", "")

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

    ownership = load_owners_yml(owners_file)

    async def _run() -> int:
        async with GitHubClient(auth=auth) as gh:
            changed = await gh.get_pr_files(owner, name, pr_number)
            changed_files = [f["filename"] for f in changed if f.get("filename")]
            result = await route_pr(
                gh=gh,
                owner=owner,
                repo=name,
                pr_number=pr_number,
                changed_files=changed_files,
                ownership=ownership,
                mode=cast("RoutingMode", mode),
                dry_run=dry_run,
            )
        _print_routing_result(result)
        return 0

    raise typer.Exit(asyncio.run(_run()))


def _print_routing_result(result: Any) -> None:
    prefix = "[dim](dry-run)[/dim] " if result.dry_run else ""
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
    if result.comment_posted:
        console.print(f"{prefix}[green]Comment posted.[/green]")
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
