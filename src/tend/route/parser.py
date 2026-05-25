"""Load and validate ``.tend/owners.yml`` for routing.

Thin wrapper over ``tend.output.tend_yaml.load`` that adds a routing-
specific validation pass: every path entry must have at least one owner,
and every owner handle must look like ``@user`` or ``@org/team``. Routing
calls fail noisily on malformed owner files rather than silently routing
PRs to nobody.
"""

from __future__ import annotations

import re
from pathlib import Path

from tend.output.tend_yaml import OwnershipFile, load, loads

_HANDLE_RE = re.compile(r"^@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:/[A-Za-z0-9_-]+)?$")


def load_owners_yml(path: str | Path) -> OwnershipFile:
    """Load ``.tend/owners.yml`` from disk and validate it for routing."""
    file = load(path)
    _validate_for_routing(file)
    return file


def loads_owners_yml(text: str) -> OwnershipFile:
    """Parse a ``.tend/owners.yml`` body and validate for routing."""
    file = loads(text)
    _validate_for_routing(file)
    return file


def _validate_for_routing(file: OwnershipFile) -> None:
    """Reject files that would route PRs to nobody or to malformed handles."""
    for rule in file.paths:
        if not rule.owners:
            raise ValueError(f"Invalid owners.yml: path {rule.path_pattern!r} has no owners")
        for owner in rule.owners:
            handle = owner.github_username
            display = handle if handle.startswith("@") else f"@{handle}"
            if not _HANDLE_RE.match(display):
                raise ValueError(
                    f"Invalid owner handle {display!r} for path {rule.path_pattern!r}; "
                    "expected @user or @org/team"
                )


__all__ = ["OwnershipFile", "load_owners_yml", "loads_owners_yml"]
