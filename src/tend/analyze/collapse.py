"""Recursive sibling collapse and ancestor-absorbs-descendant passes.

Two passes, iterated to fixpoint by ``infer()``:

- ``collapse()`` drops a child rule whose nearest existing ancestor has the
  same primary owner and a similar confidence (within ``tolerance``). The
  ancestor already covers the path.
- ``collapse_siblings()`` synthesizes a parent rule when a *supermajority*
  of immediate children share the same primary owner. The dominant owner
  must hold at least ``SIBLING_COLLAPSE_MIN_DOMINANCE`` of the children
  (with a floor of two matching siblings); dissenters are left in place
  as explicit overrides. The new parent's confidence is the min across
  the dominant subset so we don't inflate strength.

Wildcard ``*`` is never created or absorbed — it's a separate concern
handled by ``suppress_individual_wildcard`` post-collapse.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.config import InferenceConfig

SIBLING_COLLAPSE_MIN_DOMINANCE = 0.75


def directory_of(file_path: str) -> str:
    parts = file_path.rsplit("/", 1)
    return parts[0] if len(parts) > 1 else "."


def dir_to_pattern(directory: str) -> str:
    """Convert ``pkg/storage`` to the CODEOWNERS-shaped pattern ``/pkg/storage/``.

    Kept stable in the tend YAML output too — paths in ``.tend/owners.yml``
    follow the same shape so a future ``tend export --as-codeowners`` is
    trivial if anyone ever wants it.
    """
    if directory == ".":
        return "*"
    return "/" + directory.strip("/") + "/"


def pattern_depth(pattern: str) -> int:
    """``*`` → 0; ``/foo/`` → 1; ``/foo/bar/`` → 2."""
    if pattern == "*":
        return 0
    return pattern.strip("/").count("/") + 1


def parent_pattern(pattern: str) -> str | None:
    """Immediate parent pattern, or None at top level. Wildcard never escalates."""
    if pattern == "*":
        return None
    base = pattern.strip("/")
    if "/" not in base:
        return None
    return "/" + base.rsplit("/", 1)[0] + "/"


def nearest_existing_ancestor(
    pattern: str, by_pattern: dict[str, InferredOwner]
) -> InferredOwner | None:
    current = pattern
    while True:
        parent = parent_pattern(current)
        if parent is None:
            return None
        if parent in by_pattern:
            return by_pattern[parent]
        current = parent


def _aggregate_owners(
    owner_lists: list[list[OwnerCandidate]],
    *,
    min_confidence: float,
    max_owners: int,
    min_appearances: int = 1,
) -> list[OwnerCandidate]:
    """Merge multiple owners lists into one, deduped by ``github_username``.

    Aggregation across a contributor's appearances:
    ``confidence = min(...)`` (conservative floor; idempotent under
    repeated aggregation), ``commit_count = sum(...)``,
    ``last_active = max(...)``. Drops contributors below
    ``min_confidence`` or appearing in fewer than ``min_appearances``
    of the input lists; sorts by confidence descending; caps at
    ``max_owners``.
    """
    by_handle: dict[str, list[OwnerCandidate]] = defaultdict(list)
    for owners in owner_lists:
        for owner in owners:
            by_handle[owner.github_username].append(owner)

    aggregated: list[OwnerCandidate] = []
    for handle, candidates in by_handle.items():
        if len(candidates) < min_appearances:
            continue
        confidence = round(min(c.confidence for c in candidates), 3)
        if confidence < min_confidence:
            continue
        seed = candidates[0]
        aggregated.append(
            OwnerCandidate(
                email=seed.email,
                name=seed.name,
                github_username=handle,
                confidence=confidence,
                commit_count=sum(c.commit_count for c in candidates),
                last_active=max(c.last_active for c in candidates),
            )
        )
    aggregated.sort(key=lambda c: c.confidence, reverse=True)
    return aggregated[:max_owners]


def collapse(
    rules: list[InferredOwner],
    config: InferenceConfig,
    tolerance: float = 0.15,
) -> list[InferredOwner]:
    """Drop child rules whose nearest ancestor already covers them.

    Iterates to a fixpoint so chained patterns collapse in stages
    (a → b → c where each generation matches the previous).

    ``config`` is accepted for signature symmetry with
    ``collapse_siblings`` and reserved for the deferred fix that would
    propagate a dropped child's secondary owners into its ancestor's
    owners list. Currently unused — the function still drops children
    without merging.
    """
    _ = config  # see docstring; intentionally unused at this layer for now
    rules = list(rules)
    while True:
        by_pattern = {r.path_pattern: r for r in rules}
        to_drop: set[str] = set()
        for child in sorted(rules, key=lambda r: -pattern_depth(r.path_pattern)):
            if child.path_pattern == "*" or child.path_pattern in to_drop:
                continue
            ancestor = nearest_existing_ancestor(child.path_pattern, by_pattern)
            if ancestor is None or ancestor.path_pattern in to_drop:
                continue
            if not ancestor.owners or not child.owners:
                continue
            if ancestor.owners[0].github_username != child.owners[0].github_username:
                continue
            if abs(ancestor.owners[0].confidence - child.owners[0].confidence) > tolerance:
                continue
            to_drop.add(child.path_pattern)
        if not to_drop:
            break
        rules = [r for r in rules if r.path_pattern not in to_drop]
    return rules


def collapse_siblings(
    rules: list[InferredOwner],
    config: InferenceConfig,
) -> list[InferredOwner]:
    """Create a parent rule when a supermajority of children agree on primary owner.

    The dominant primary owner must hold at least
    ``SIBLING_COLLAPSE_MIN_DOMINANCE`` of the children (with a floor of two
    matching siblings) for the parent to be synthesized. Only dominant-owner
    children are consumed; dissenters stay in the rules list and, on the
    next fixpoint pass, fall through ``collapse()`` as explicit overrides
    against the synthesized parent (different primary owner → preserved).

    Synthesized parents inherit ``min(child.confidence)`` per owner across
    the dominant subset so we don't inflate strength. Secondary owners
    (anyone other than the dominant primary) also propagate if they
    appear in at least ``ceil(N/2)`` of the dominant children — gated so
    a one-off secondary doesn't leak into the parent. ``commit_count``
    and ``last_active`` aggregate per owner across that subset; the
    dissenter's evidence stays on the dissenter's own rule.

    Iterates to a fixpoint; the outer ``infer()`` loop also re-feeds these
    into ``collapse()`` so chains cascade.
    """
    rules = list(rules)
    while True:
        existing_patterns = {r.path_pattern for r in rules}
        groups: dict[str, list[InferredOwner]] = defaultdict(list)
        for rule in rules:
            if rule.path_pattern == "*":
                continue
            parent = parent_pattern(rule.path_pattern)
            if parent is None:
                continue  # depth-1 rule; we never synthesize `*` as a parent
            groups[parent].append(rule)

        new_parents: list[InferredOwner] = []
        consumed: set[str] = set()
        for parent_pat, children in groups.items():
            if parent_pat in existing_patterns:
                continue  # let collapse() handle absorbing into the existing rule
            if len(children) < 2:
                continue

            owned_children = [c for c in children if c.owners]
            if len(owned_children) < 2:
                continue

            handle_counts = Counter(c.owners[0].github_username for c in owned_children)
            dominant_handle, dominant_count = handle_counts.most_common(1)[0]
            if dominant_count < 2:
                continue
            if dominant_count / len(owned_children) < SIBLING_COLLAPSE_MIN_DOMINANCE:
                continue

            dominant_children = [
                c for c in owned_children if c.owners[0].github_username == dominant_handle
            ]
            # ceil(N/2) with a floor of 2: a secondary joins the synthesized
            # parent only if present in at least half the dominant children
            # (and at minimum two of them). The dominant primary trivially
            # clears this — it appears at owners[0] of every dominant child
            # by construction.
            min_appearances = max(2, (len(dominant_children) + 1) // 2)
            parent_owners = _aggregate_owners(
                [c.owners for c in dominant_children],
                min_confidence=config.min_confidence,
                max_owners=config.max_owners_per_path,
                min_appearances=min_appearances,
            )
            if not parent_owners:
                continue
            new_parents.append(
                InferredOwner(path_pattern=parent_pat, owners=parent_owners)
            )
            for c in dominant_children:
                consumed.add(c.path_pattern)

        if not new_parents:
            return rules
        rules = [r for r in rules if r.path_pattern not in consumed] + new_parents
