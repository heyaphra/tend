"""Schema round-trip + hash stability + diff correctness for .tend/owners.yml.

v1.1 schema: per-path is a list of contributor records, each with
``handle`` / ``strength`` / ``commits`` / ``last_touched``. No floats
in YAML; ``strength`` is the qualitative label.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import yaml

from tend.analyze._models import InferredOwner, OwnerCandidate
from tend.config import InferenceConfig
from tend.output.strength import annotate_rules
from tend.output.tend_yaml import (
    SCHEMA_VERSION,
    OwnershipFile,
    content_hash,
    diff,
    dump_full,
    loads,
)

RECENT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _owner(handle: str, conf: float, commits: int = 10) -> OwnerCandidate:
    return OwnerCandidate(
        email=f"{handle}@example.com",
        name=handle,
        github_username=handle,
        confidence=conf,
        commit_count=commits,
        last_active=RECENT,
    )


def _rule(pattern: str, owners: list[OwnerCandidate]) -> InferredOwner:
    return InferredOwner(path_pattern=pattern, owners=owners)


def _config(min_confidence: float = 0.30) -> InferenceConfig:
    return InferenceConfig(min_confidence=min_confidence)


def test_dump_full_includes_version_and_generated_at():
    rules = [_rule("/src/", [_owner("alice", 0.7)])]
    text = dump_full(rules, _config(), "balanced", now=RECENT)
    data = yaml.safe_load(text)
    assert data["version"] == SCHEMA_VERSION
    assert "generated_at" in data
    assert data["generated_by"].startswith("tend/")
    # Per-path is a list; first entry's handle is alice (no @ prefix).
    assert data["paths"]["/src/"][0]["handle"] == "alice"


def test_dump_full_metadata_keys_precede_paths():
    """Rendered YAML lists metadata (version, generated_at, generated_by,
    config) in that order before the ``paths`` block — a reviewer should
    see the timestamp at the top, not buried below 50+ rules."""
    rules = [_rule("/src/", [_owner("alice", 0.7)])]
    text = dump_full(rules, _config(), "balanced", now=RECENT)
    positions = {
        key: text.index(f"{key}:")
        for key in ("version", "generated_at", "generated_by", "config", "paths")
    }
    assert positions["version"] < positions["generated_at"]
    assert positions["generated_at"] < positions["generated_by"]
    assert positions["generated_by"] < positions["config"]
    assert positions["config"] < positions["paths"]


def test_content_hash_stable_across_now_values():
    """Hash is computed over content minus generated_at; identical owners
    with identical last_active values produce identical hashes regardless
    of when render is invoked."""
    rules = [_rule("/src/", [_owner("alice", 0.7)])]
    cfg = _config()
    h1 = content_hash(rules, cfg, "balanced")
    h2 = content_hash(rules, cfg, "balanced")
    assert h1 == h2


def test_dump_full_per_path_is_list_of_records():
    """Each path entry is ``list[{handle, strength, commits, last_touched}]``."""
    rules = [_rule("/src/", [_owner("alice", 0.7, commits=42)])]
    text = dump_full(rules, _config(), "balanced", now=RECENT)
    data = yaml.safe_load(text)
    entry = data["paths"]["/src/"]
    assert isinstance(entry, list)
    record = entry[0]
    assert set(record.keys()) == {"handle", "strength", "commits", "last_touched"}
    assert record["commits"] == 42
    assert record["last_touched"] == "2026-05-01"  # YYYY-MM-DD per spec
    assert record["strength"] == "strong"  # 0.7 >= 0.60


def test_dump_full_strength_thresholds():
    """strong >= 0.60, moderate >= 0.35, suggestive >= min_confidence (0.30)."""
    rules = [
        _rule(
            "/src/",
            [
                _owner("strong-one", 0.70),
                _owner("moderate-one", 0.40),
                _owner("suggestive-one", 0.32),
                _owner("excluded", 0.10),  # below min_confidence — dropped
            ],
        )
    ]
    text = dump_full(rules, _config(min_confidence=0.30), "balanced", now=RECENT)
    data = yaml.safe_load(text)
    entries = data["paths"]["/src/"]
    handles = {e["handle"]: e["strength"] for e in entries}
    assert handles == {
        "strong-one": "strong",
        "moderate-one": "moderate",
        "suggestive-one": "suggestive",
    }
    assert "excluded" not in handles  # below floor → excluded entirely


def test_dump_full_renders_multiple_moderate_owners():
    """Two co-owners both clearing min_confidence must both appear in the
    YAML output, ordered by confidence descending.

    Regression for the .tend/owners.yml co-owner-drop bug observed on
    tanstack/router's /packages/router-core/: schiller-manuel and
    Sheraff both at strength ``moderate`` but only Sheraff was emitted.
    The renderer was already correct — this test guards against a
    rendering-side regression and documents the expected shape.
    """
    rules = [
        _rule(
            "/packages/router-core/",
            [
                _owner("schiller-manuel", 0.50),
                _owner("Sheraff", 0.35),
            ],
        )
    ]
    text = dump_full(rules, _config(min_confidence=0.30), "balanced", now=RECENT)
    data = yaml.safe_load(text)
    entries = data["paths"]["/packages/router-core/"]
    assert len(entries) == 2
    assert [e["handle"] for e in entries] == ["schiller-manuel", "Sheraff"]
    assert all(e["strength"] == "moderate" for e in entries)


def test_dump_full_config_block_drops_resolved():
    """v1.1 config block is just sensitivity + lookback_days; no resolved internals."""
    cfg = InferenceConfig(min_confidence=0.40)
    text = dump_full([], cfg, "strict")
    data = yaml.safe_load(text)
    assert data["config"]["sensitivity"] == "strict"
    assert "lookback_days" in data["config"]
    assert "resolved" not in data["config"]


def test_content_hash_is_stable_across_generated_at_changes():
    rules = [_rule("/src/", [_owner("alice", 0.7)])]
    cfg = _config()
    h1 = content_hash(rules, cfg, "balanced")
    h2 = content_hash(rules, cfg, "balanced")
    assert h1 == h2
    different_time_text = dump_full(rules, cfg, "balanced", now=datetime(2099, 1, 1, tzinfo=UTC))
    same_time_text = dump_full(rules, cfg, "balanced", now=RECENT)
    assert different_time_text != same_time_text  # files differ on timestamp
    assert content_hash(rules, cfg, "balanced") == h1  # but hash is identical


def test_content_hash_changes_when_rules_change():
    cfg = _config()
    h1 = content_hash([_rule("/src/", [_owner("alice", 0.7)])], cfg, "balanced")
    h2 = content_hash([_rule("/src/", [_owner("bob", 0.7)])], cfg, "balanced")
    assert h1 != h2


def test_loads_round_trips_rendered_yaml():
    rules = [_rule("/src/", [_owner("alice", 0.7, commits=15)])]
    text = dump_full(rules, _config(), "balanced", now=RECENT)
    parsed = loads(text)
    assert parsed.version == SCHEMA_VERSION
    assert parsed.paths[0].path_pattern == "/src/"
    top = parsed.paths[0].owners[0]
    assert top.github_username == "alice"
    assert top.commit_count == 15
    assert top.strength == "strong"
    # Confidence floats are NOT round-tripped — they're internal-only now.
    assert top.confidence == 0.0


def test_loads_rejects_legacy_v1_dict_shape():
    """v1 dict-shaped entries should fail loudly with a migration pointer."""
    legacy = """version: 1
config: {}
paths:
  /src/:
    owners: ["@alice"]
    confidence: 0.7
    evidence:
      commits: 10
      last_touched: "2026-05-01T00:00:00+00:00"
"""
    with pytest.raises(ValueError, match="legacy v1 dict shape"):
        loads(legacy)


def test_loads_rejects_wrong_version():
    text = "version: 99\nconfig: {}\npaths: {}\n"
    with pytest.raises(ValueError, match="schema version"):
        loads(text)


def test_loads_rejects_non_mapping():
    with pytest.raises(ValueError, match="must be a mapping"):
        loads("- not a mapping\n")


# -------- diff --------


def _annotated_file(rules: list[InferredOwner]) -> OwnershipFile:
    """Strength must be populated for diff comparison."""
    annotate_rules(rules, min_confidence=0.30, lookback_days=180, now=RECENT)
    return OwnershipFile(version=1, generated_at=None, config={}, paths=rules)


def test_diff_first_time_returns_all_as_added():
    rules = [_rule("/src/", [_owner("alice", 0.7)])]
    d = diff(None, _annotated_file(rules))
    assert len(d.paths_added) == 1
    assert d.paths_owner_changed == []
    assert d.paths_removed == []


def test_diff_detects_new_paths():
    old = _annotated_file([_rule("/a/", [_owner("alice", 0.7)])])
    new = _annotated_file(
        [
            _rule("/a/", [_owner("alice", 0.7)]),
            _rule("/b/", [_owner("bob", 0.6)]),
        ]
    )
    d = diff(old, new)
    assert [r.path_pattern for r in d.paths_added] == ["/b/"]


def test_diff_detects_owner_change():
    old = _annotated_file([_rule("/a/", [_owner("alice", 0.7)])])
    new = _annotated_file([_rule("/a/", [_owner("bob", 0.7)])])
    d = diff(old, new)
    assert len(d.paths_owner_changed) == 1
    old_rule, new_rule = d.paths_owner_changed[0]
    assert old_rule.owners[0].github_username == "alice"
    assert new_rule.owners[0].github_username == "bob"


def test_diff_detects_strength_change():
    """Strength tier change (strong → moderate, etc.) is the only quantitative
    drift the schema represents — within-label drift is invisible by design."""
    old = _annotated_file([_rule("/a/", [_owner("alice", 0.40)])])  # moderate
    new = _annotated_file([_rule("/a/", [_owner("alice", 0.70)])])  # strong
    d = diff(old, new)
    assert len(d.paths_strength_changed) == 1


def test_diff_ignores_within_label_drift():
    """Two strong owners with different confidences still diff as empty."""
    old = _annotated_file([_rule("/a/", [_owner("alice", 0.65)])])  # strong
    new = _annotated_file([_rule("/a/", [_owner("alice", 0.75)])])  # also strong
    d = diff(old, new)
    assert d.is_empty


def test_diff_detects_removed_paths():
    old = _annotated_file(
        [
            _rule("/a/", [_owner("alice", 0.7)]),
            _rule("/b/", [_owner("bob", 0.6)]),
        ]
    )
    new = _annotated_file([_rule("/a/", [_owner("alice", 0.7)])])
    d = diff(old, new)
    assert [r.path_pattern for r in d.paths_removed] == ["/b/"]


def test_diff_is_empty_when_nothing_changed():
    rules = [_rule("/a/", [_owner("alice", 0.7)])]
    annotated = annotate_rules(rules, min_confidence=0.30, lookback_days=180, now=RECENT)
    f = OwnershipFile(version=1, generated_at=None, config={}, paths=annotated)
    d = diff(f, f)
    assert d.is_empty
