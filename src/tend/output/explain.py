"""``tend explain <path>`` — per-contributor breakdown for one directory.

Returns one row per contributor that touched the directory, sorted by
raw score. Each row carries the scoring inputs, Wilson confidence
bounds, derived strength label, and a ``status`` field explaining why
the contributor would (or wouldn't) make it through ``min_commits`` /
``min_confidence`` / volume-floor filtering.

The function is owner-agnostic — it works the same whether the path made
it into ``.tend/owners.yml`` or was dropped by a threshold. Use it to
debug "why isn't @alice listed for /src/auth/?".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tend.analyze._models import FileContribution
from tend.analyze.confidence import wilson_bounds, z_from_level
from tend.analyze.scoring import apply_association_weight, score
from tend.config import InferenceConfig
from tend.github.associations import Association, association_weight
from tend.output.strength import adjust_strength_for_recency, strength_label


def explain_directory(
    contributions: list[FileContribution],
    directory: str,
    config: InferenceConfig,
    associations: dict[str, Association] | None = None,
) -> list[dict[str, Any]]:
    """Pre-filter per-author breakdown for a single directory.

    ``directory`` is matched as a *subtree* — ``/src/auth/`` covers any
    file path starting with ``src/auth/``. This mirrors how the inference
    pipeline emits collapsed parent rules whose contributions live entirely
    in child directories (``lib/``, ``test/``, ...). Without subtree
    matching, explain would return empty for exactly the rules users ask
    about.
    """
    key = directory.strip("/")
    if key == "" or directory == "*":
        contribs = list(contributions)
    else:
        prefix = key + "/"
        contribs = [c for c in contributions if c.file_path.startswith(prefix)]
    if not contribs:
        return []

    now = datetime.now(UTC)
    z = z_from_level(config.confidence_level)
    per_author: dict[str, dict[str, Any]] = {}
    for c in contribs:
        last = c.last_commit_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        days = max(0, (now - last).days)
        lines = c.lines_added + c.lines_removed
        raw = score(lines, days, c.commit_count, config.halflife_days)

        assoc: Association | None = None
        if associations is not None:
            handle_key = (c.github_username or "").lower()
            assoc = associations.get(handle_key, Association.UNKNOWN)
            raw = apply_association_weight(raw, assoc)

        akey = (c.github_username or c.author_email).lower()
        entry = per_author.setdefault(
            akey,
            {
                "github_username": c.github_username,
                "email": c.author_email,
                "score": 0.0,
                "commits": 0.0,
                "lines_added": 0.0,
                "lines_removed": 0.0,
                "last_active": last,
                "association": assoc,
            },
        )
        entry["score"] += raw
        entry["commits"] += c.commit_count
        entry["lines_added"] += c.lines_added
        entry["lines_removed"] += c.lines_removed
        if last > entry["last_active"]:
            entry["last_active"] = last
        if entry["github_username"] is None and c.github_username:
            entry["github_username"] = c.github_username
        if entry.get("association") is None and assoc is not None:
            entry["association"] = assoc

    total_score = sum(e["score"] for e in per_author.values())
    qualified = [e for e in per_author.values() if e["commits"] >= config.min_commits]
    qualified_total = sum(e["score"] for e in qualified)
    total_volume = sum(e["commits"] for e in per_author.values())
    volume_passes = total_volume >= config.volume_floor

    rows: list[dict[str, Any]] = []
    for entry in per_author.values():
        raw_share = entry["score"] / total_score if total_score > 0 else 0.0
        share_lower: float | None = None
        share_upper: float | None = None
        base_label: str | None = None
        final_label: str | None = None
        days_stale = max(0, (now - entry["last_active"]).days)
        if entry["commits"] < config.min_commits:
            status = f"dropped (commits<{config.min_commits})"
        elif not entry["github_username"]:
            status = "dropped (unresolved handle)"
        else:
            share_lower, share_upper = wilson_bounds(entry["score"], qualified_total, z=z)
            base_label = strength_label(share_lower, min_confidence=config.min_confidence)
            final_label = adjust_strength_for_recency(
                base_label, days_stale, lookback_days=config.lookback_days
            )
            if share_lower < config.min_confidence:
                status = f"dropped (share_lower<{config.min_confidence:.2f})"
            elif not volume_passes:
                status = f"dropped (dir volume<{config.volume_floor})"
            else:
                status = "owner"
        assoc_for_row: Association | None = entry.get("association")
        weight_for_row = association_weight(assoc_for_row) if assoc_for_row is not None else None
        rows.append(
            {
                "username": entry["github_username"] or f"({entry['email']})",
                "commits": entry["commits"],
                "lines_added": entry["lines_added"],
                "lines_removed": entry["lines_removed"],
                "last_active": entry["last_active"],
                "days_stale": days_stale,
                "score": entry["score"],
                "raw_share": raw_share,
                "share_lower": share_lower,
                "share_upper": share_upper,
                "base_strength": base_label,
                "final_strength": final_label,
                "association": assoc_for_row,
                "association_weight": weight_for_row,
                "status": status,
            }
        )
    rows.sort(key=lambda r: -r["score"])
    return rows
