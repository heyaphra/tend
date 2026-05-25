"""Longest-prefix path matching against ``.tend/owners.yml`` rules.

Three pattern shapes are supported:

- ``*`` — fallback that matches every file. Lowest priority.
- ``/foo/`` — directory rule. Matches any file under ``foo/``.
- ``/foo/bar.py`` — file rule. Matches exactly that path.

No globbing. Security routing is unambiguous by construction: if you can
read the rule, you know exactly which files it will fire on. Match
precedence is by pattern specificity (longest pattern wins); ``*`` is
always the lowest-priority fallback.
"""

from __future__ import annotations

from tend.analyze._models import InferredOwner
from tend.output.tend_yaml import OwnershipFile


def _matches(pattern: str, file_path: str) -> bool:
    if pattern == "*":
        return True
    stripped = pattern.strip("/")
    if pattern.endswith("/"):
        # Directory rule: match exact dir or any file under it.
        return file_path == stripped or file_path.startswith(stripped + "/")
    return file_path == stripped


def _specificity(pattern: str) -> int:
    """Higher = more specific. Wildcard is always the least specific."""
    if pattern == "*":
        return -1
    return len(pattern.rstrip("/"))


def find_owner(ownership: OwnershipFile, file_path: str) -> InferredOwner | None:
    """Return the most specific matching rule, or ``None`` if no match."""
    best: InferredOwner | None = None
    best_specificity = -2
    for rule in ownership.paths:
        if not _matches(rule.path_pattern, file_path):
            continue
        spec = _specificity(rule.path_pattern)
        if spec > best_specificity:
            best = rule
            best_specificity = spec
    return best


def find_owners_for_files(
    ownership: OwnershipFile, file_paths: list[str]
) -> dict[str, InferredOwner | None]:
    """Bulk version: ``{file_path: matching_rule_or_None}``."""
    return {fp: find_owner(ownership, fp) for fp in file_paths}


__all__ = ["find_owner", "find_owners_for_files"]
