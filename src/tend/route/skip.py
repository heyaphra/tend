"""Skip-path filtering for changed files before owner matching.

A Dependabot PR that touches ``node_modules/foo/package.json`` shouldn't
route to whoever last bumped that vendored package — there is no
meaningful owner. The skip filter strips such files before
``find_owners_for_files`` runs; if every file in a PR gets skipped, the
caller (``route_pr`` via ``fallback_handles``) can route to a configured
default owner instead.

Patterns supported (intentionally narrow, matching the matcher's
"unambiguous by construction" posture):

- ``node_modules/`` — directory prefix (same shape as the matcher's dir rule).
- ``LICENSE``       — exact path match.
- ``*.ext``         — basename glob; the only glob form we accept.

Any pattern starting with ``*.`` is treated as a basename glob via
``fnmatch.fnmatchcase``. Everything else is prefix-or-exact. We
deliberately do not support ``**/*.test.ts`` style globs — the matcher's
"if you can read the rule, you know exactly what it fires on" principle
applies here too.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

# Default skip list shipped with tend. Users can add more via
# ``tend route --extra-skip-path`` (repeatable).
#
# Note on ``*.lock``: this catches Cargo/Gemfile/yarn/poetry/Pipfile/composer
# lockfiles (basename ends in ``.lock``). It does NOT match
# ``package-lock.json`` (ends in ``.json``) or ``pnpm-lock.yaml`` (ends in
# ``.yaml``). In practice this is harmless because Dependabot PRs typically
# touch the lockfile in the same directory as a manifest with the same owner;
# the manifest match resolves ownership and the dedupe in ``route_pr``
# collapses the duplicate handles.
DEFAULT_SKIP_PATHS: tuple[str, ...] = (
    "node_modules/",
    "vendor/",
    "dist/",
    "build/",
    "*.lock",
)


def _matches(pattern: str, file_path: str) -> bool:
    if pattern.startswith("*."):
        return fnmatchcase(PurePosixPath(file_path).name, pattern)
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/")
        return file_path == prefix or file_path.startswith(prefix + "/")
    return file_path == pattern


def filter_changed_files(
    changed_files: Sequence[str],
    skip_paths: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Partition ``changed_files`` into ``(kept, skipped)`` by skip rules.

    Order is preserved within each partition. Skip rules are OR'd: a file
    matching any pattern goes to ``skipped``. Empty ``skip_paths`` is a
    no-op (everything kept).
    """
    if not skip_paths:
        return list(changed_files), []
    kept: list[str] = []
    skipped: list[str] = []
    for fp in changed_files:
        if any(_matches(pat, fp) for pat in skip_paths):
            skipped.append(fp)
        else:
            kept.append(fp)
    return kept, skipped


__all__ = ["DEFAULT_SKIP_PATHS", "filter_changed_files"]
