""".tend/owners.yml schema (v1.1) — render, load, hash, diff.

This is tend's only output format. Schema layout::

    version: 1
    generated_at: 2026-05-25T18:30:00+00:00
    generated_by: tend/0.2.0
    config:
      sensitivity: balanced
      lookback_days: 180
    paths:
      /src/auth/:
        - handle: schiller-manuel
          strength: strong
          commits: 24
          last_touched: 2026-05-22

Each path entry is a list of contributor records ordered by strength.
Runners-up are just additional list entries with weaker labels — there
is no separate ``runner_ups`` block. ``confidence`` floats are internal;
``strength`` is the qualitative label exposed in the file.

``dump_full`` produces the file Tend writes. ``content_hash`` is the
generated_at-independent SHA-256 used for PR idempotency. ``load``
parses a file back into ``OwnershipFile``; ``diff`` computes the
``OwnersDiff`` the PR body composer renders.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.config import InferenceConfig, Sensitivity
from tend.output.strength import adjust_strength_for_recency, strength_label

SCHEMA_VERSION = 1
GENERATED_BY = "tend/0.2.0"


@dataclass
class OwnershipFile:
    """In-memory representation of a parsed ``.tend/owners.yml``."""

    version: int
    generated_at: datetime | None
    config: dict[str, Any]
    paths: list[InferredOwner]


@dataclass
class OwnersDiff:
    """Difference between two ownership files. Used to compose PR bodies."""

    paths_added: list[InferredOwner] = field(default_factory=list)
    paths_owner_changed: list[tuple[InferredOwner, InferredOwner]] = field(
        default_factory=list
    )  # (old, new)
    paths_strength_changed: list[tuple[InferredOwner, InferredOwner]] = field(default_factory=list)
    paths_removed: list[InferredOwner] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.paths_added,
                self.paths_owner_changed,
                self.paths_strength_changed,
                self.paths_removed,
            )
        )


# ---- Render ----


def _bare_handle(handle: str) -> str:
    """YAML stores handles without the ``@`` prefix; the file's purpose is
    machine-readable ownership data, not a Markdown mention."""
    return handle.lstrip("@")


def _last_touched_str(when: datetime) -> str:
    """YYYY-MM-DD per the spec — short enough to scan, precise enough for
    'how stale is this'."""
    return when.date().isoformat() if isinstance(when, datetime) else str(when)


def _rule_to_path_entry(
    rule: InferredOwner,
    *,
    min_confidence: float,
    lookback_days: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """Render one rule as a list of contributor records.

    Each owner gets a share-based label adjusted for recency: an owner
    who hasn't touched the path within ``lookback_days // 2`` days is
    demoted one tier. Owners that fall below the ``min_confidence`` floor
    are excluded entirely — they have no place in a file that's also the
    routing input.
    """
    out: list[dict[str, Any]] = []
    for owner in rule.owners:
        base = strength_label(owner.confidence, min_confidence=min_confidence)
        if base is None:
            continue
        last = owner.last_active
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        days = max(0, (now - last).days)
        label = adjust_strength_for_recency(base, days, lookback_days=lookback_days)
        if label is None:
            continue
        out.append(
            {
                "handle": _bare_handle(owner.github_username),
                "strength": label,
                # Post-aggregation attributed count for this contributor on
                # this specific path rule. ``tend explain`` shows the raw
                # subtree commit count, which can be 5-9× larger after
                # ancestor/sibling collapse merges multiple directories.
                # Different units, both correct for their purposes.
                "commits": round(owner.commit_count),
                "last_touched": _last_touched_str(owner.last_active),
            }
        )
    return out


def _config_block(sensitivity: Sensitivity, config: InferenceConfig) -> dict[str, Any]:
    """The on-disk config block is intentionally thin — just what a human
    reviewer needs to interpret the file. Internal parameters live in
    ``InferenceConfig`` and are visible via ``tend explain``."""
    return {
        "sensitivity": sensitivity,
        "lookback_days": config.lookback_days,
    }


def _to_dict(
    rules: list[InferredOwner],
    config: InferenceConfig,
    sensitivity: Sensitivity,
    *,
    include_generated_at: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference = now or datetime.now(UTC)
    paths: dict[str, list[dict[str, Any]]] = {}
    for r in rules:
        entry = _rule_to_path_entry(
            r,
            min_confidence=config.min_confidence,
            lookback_days=config.lookback_days,
            now=reference,
        )
        if entry:
            paths[r.path_pattern] = entry
    data: dict[str, Any] = {"version": SCHEMA_VERSION}
    if include_generated_at:
        data["generated_at"] = reference.isoformat()
    data["generated_by"] = GENERATED_BY
    data["config"] = _config_block(sensitivity, config)
    data["paths"] = paths
    return data


def dump_full(
    rules: list[InferredOwner],
    config: InferenceConfig,
    sensitivity: Sensitivity = "balanced",
    *,
    now: datetime | None = None,
) -> str:
    """Render the full ``.tend/owners.yml`` content, with ``generated_at``."""
    data = _to_dict(rules, config, sensitivity, include_generated_at=True, now=now)
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def content_hash(
    rules: list[InferredOwner],
    config: InferenceConfig,
    sensitivity: Sensitivity = "balanced",
) -> str:
    """Stable SHA-256 of the YAML content excluding ``generated_at``.

    Sorted keys + default flow style off → identical bytes for identical
    inputs, regardless of dict ordering.
    """
    data = _to_dict(rules, config, sensitivity, include_generated_at=False)
    canonical = yaml.safe_dump(data, sort_keys=True, default_flow_style=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---- Load ----


def load(path: str | Path) -> OwnershipFile:
    """Parse a ``.tend/owners.yml`` from disk into an ``OwnershipFile``."""
    text = Path(path).read_text(encoding="utf-8")
    return loads(text)


def loads(text: str) -> OwnershipFile:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Invalid .tend/owners.yml: top-level must be a mapping")
    version = data.get("version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported .tend/owners.yml schema version {version!r}; expected {SCHEMA_VERSION}"
        )
    generated_at_raw = data.get("generated_at")
    generated_at = (
        datetime.fromisoformat(generated_at_raw) if isinstance(generated_at_raw, str) else None
    )
    config = data.get("config") or {}
    if not isinstance(config, dict):
        raise ValueError("Invalid .tend/owners.yml: 'config' must be a mapping")

    raw_paths = data.get("paths") or {}
    if not isinstance(raw_paths, dict):
        raise ValueError("Invalid .tend/owners.yml: 'paths' must be a mapping")

    paths: list[InferredOwner] = []
    for pattern, entry in raw_paths.items():
        paths.append(_path_entry_to_rule(pattern, entry))

    return OwnershipFile(version=version, generated_at=generated_at, config=config, paths=paths)


def _path_entry_to_rule(pattern: str, entry: Any) -> InferredOwner:
    """Parse one path entry into an ``InferredOwner``.

    v1.1 shape is ``list[dict]``. v1 (dict with ``owners``/``confidence``/
    ``evidence``) is rejected with a migration pointer rather than silently
    accepted, so customers know to re-run analyze after upgrading.
    """
    if isinstance(entry, dict):
        raise ValueError(
            f"Path {pattern!r} uses the legacy v1 dict shape; this version expects a "
            "list of contributor records. Re-run `tend analyze` to regenerate the file."
        )
    if not isinstance(entry, list):
        raise ValueError(f"Invalid path entry for {pattern!r}: must be a list of mappings")

    candidates: list[OwnerCandidate] = []
    for raw in entry:
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid contributor record under {pattern!r}: must be a mapping")
        handle = raw.get("handle")
        if not isinstance(handle, str) or not handle:
            raise ValueError(f"Missing/invalid 'handle' under {pattern!r}")
        strength = raw.get("strength")
        if strength is not None and not isinstance(strength, str):
            raise ValueError(f"Invalid 'strength' for {handle!r} under {pattern!r}")
        commits = int(raw.get("commits", 0))
        last_touched = _parse_last_touched(raw.get("last_touched"))
        candidates.append(
            OwnerCandidate(
                email="",
                name=handle,
                github_username=_strip_at(handle),
                confidence=0.0,  # internal-only; not round-tripped through YAML
                commit_count=commits,
                last_active=last_touched,
                strength=strength,
            )
        )

    return InferredOwner(path_pattern=pattern, owners=candidates)


def _parse_last_touched(value: Any) -> datetime:
    """Accept YYYY-MM-DD (v1.1) or full ISO datetime (legacy). Returns
    a timezone-aware ``datetime`` so downstream date math stays consistent."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _strip_at(handle: str) -> str:
    return handle[1:] if handle.startswith("@") else handle


# ---- Diff ----


def diff(old: OwnershipFile | None, new: OwnershipFile) -> OwnersDiff:
    """Compute the change-set between two ownership files.

    - ``paths_added`` — new path patterns.
    - ``paths_owner_changed`` — primary owner handle differs.
    - ``paths_strength_changed`` — primary owner same, strength label
      differs (e.g. ``moderate`` → ``strong``). Quantitative drift within
      the same label is invisible by design — the file traffics in
      qualitative labels.
    - ``paths_removed`` — old path no longer present.

    First-time runs (``old is None``) return everything in ``paths_added``.
    """
    out = OwnersDiff()
    if old is None:
        out.paths_added = list(new.paths)
        return out

    old_by_pattern = {r.path_pattern: r for r in old.paths}
    new_by_pattern = {r.path_pattern: r for r in new.paths}

    for pattern, new_rule in new_by_pattern.items():
        old_rule = old_by_pattern.get(pattern)
        if old_rule is None:
            out.paths_added.append(new_rule)
            continue
        if not old_rule.owners or not new_rule.owners:
            continue
        old_top = old_rule.owners[0]
        new_top = new_rule.owners[0]
        if old_top.github_username != new_top.github_username:
            out.paths_owner_changed.append((old_rule, new_rule))
            continue
        if old_top.strength != new_top.strength:
            out.paths_strength_changed.append((old_rule, new_rule))

    for pattern, old_rule in old_by_pattern.items():
        if pattern not in new_by_pattern:
            out.paths_removed.append(old_rule)

    return out
