"""Tests for the routing-specific loader and handle validation.

v1.1 schema: per-path is a list of contributor records.
"""

from __future__ import annotations

import pytest

from tend.route.parser import load_owners_yml, loads_owners_yml

VALID_YAML = """version: 1
config:
  sensitivity: balanced
  lookback_days: 180
paths:
  /src/:
    - handle: alice
      strength: strong
      commits: 24
      last_touched: 2026-05-01
"""


def test_loads_round_trips_valid_yaml():
    f = loads_owners_yml(VALID_YAML)
    assert f.paths[0].path_pattern == "/src/"
    assert f.paths[0].owners[0].github_username == "alice"
    assert f.paths[0].owners[0].strength == "strong"


def test_load_from_disk(tmp_path):
    path = tmp_path / "owners.yml"
    path.write_text(VALID_YAML, encoding="utf-8")
    f = load_owners_yml(path)
    assert f.paths[0].path_pattern == "/src/"


def test_rejects_path_with_no_owners():
    """An empty list of contributors → routing PRs to nobody."""
    bad = """version: 1
config: {}
paths:
  /src/: []
"""
    with pytest.raises(ValueError, match="no owners"):
        loads_owners_yml(bad)


def test_rejects_malformed_handle():
    bad = """version: 1
config: {}
paths:
  /src/:
    - handle: "alice with spaces!"
      strength: strong
      commits: 10
      last_touched: 2026-05-01
"""
    with pytest.raises(ValueError, match="Invalid owner handle"):
        loads_owners_yml(bad)


def test_accepts_team_handle():
    yaml_with_team = """version: 1
config: {}
paths:
  /src/:
    - handle: acme/backend
      strength: strong
      commits: 10
      last_touched: 2026-05-01
"""
    f = loads_owners_yml(yaml_with_team)
    assert f.paths[0].owners[0].github_username == "acme/backend"


def test_rejects_legacy_v1_dict_shape():
    """A v1 file fails at the tend_yaml.loads layer with a migration hint."""
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
        loads_owners_yml(legacy)
