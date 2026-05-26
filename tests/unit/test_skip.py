"""Tests for ``tend.route.skip`` — skip-path filtering."""

from __future__ import annotations

import pytest

from tend.route.skip import DEFAULT_SKIP_PATHS, filter_changed_files


def test_directory_prefix_strips_files_under_it() -> None:
    kept, skipped = filter_changed_files(
        ["node_modules/lodash/index.js", "src/app.py"],
        ["node_modules/"],
    )
    assert kept == ["src/app.py"]
    assert skipped == ["node_modules/lodash/index.js"]


def test_directory_prefix_does_not_strip_partial_name_match() -> None:
    """``node_modules_test/x.js`` is NOT under ``node_modules/``."""
    kept, skipped = filter_changed_files(
        ["node_modules_test/x.js"],
        ["node_modules/"],
    )
    assert kept == ["node_modules_test/x.js"]
    assert skipped == []


@pytest.mark.parametrize(
    ("filename", "should_skip"),
    [
        ("Cargo.lock", True),
        ("Gemfile.lock", True),
        ("yarn.lock", True),
        ("poetry.lock", True),
        ("Pipfile.lock", True),
        ("composer.lock", True),
        ("package-lock.json", False),  # ends in .json
        ("pnpm-lock.yaml", False),  # ends in .yaml
        ("go.sum", False),  # not a .lock
    ],
)
def test_lock_glob_matches_basename_ending_in_dot_lock(filename: str, should_skip: bool) -> None:
    """``*.lock`` matches basename only when filename literally ends in
    ``.lock``. ``package-lock.json`` and ``pnpm-lock.yaml`` are NOT caught.
    Documented behavior — users can add ``--extra-skip-path
    package-lock.json`` if they need npm/pnpm coverage."""
    kept, skipped = filter_changed_files([f"services/api/{filename}"], ["*.lock"])
    if should_skip:
        assert skipped == [f"services/api/{filename}"]
        assert kept == []
    else:
        assert kept == [f"services/api/{filename}"]
        assert skipped == []


def test_exact_path_match() -> None:
    kept, skipped = filter_changed_files(["LICENSE", "README.md"], ["LICENSE"])
    assert kept == ["README.md"]
    assert skipped == ["LICENSE"]


def test_empty_skip_paths_keeps_everything() -> None:
    files = ["a.py", "node_modules/x.js"]
    kept, skipped = filter_changed_files(files, [])
    assert kept == files
    assert skipped == []


def test_all_files_filtered_returns_empty_kept_list() -> None:
    kept, skipped = filter_changed_files(
        ["node_modules/x.js", "vendor/y.go"],
        ["node_modules/", "vendor/"],
    )
    assert kept == []
    assert skipped == ["node_modules/x.js", "vendor/y.go"]


def test_default_skip_paths_covers_common_vendored_locations() -> None:
    files = [
        "node_modules/lodash/index.js",
        "vendor/aws-sdk/client.go",
        "dist/bundle.js",
        "build/output/index.html",
        "Cargo.lock",
        "src/app.py",
    ]
    kept, skipped = filter_changed_files(files, DEFAULT_SKIP_PATHS)
    assert kept == ["src/app.py"]
    assert len(skipped) == 5


def test_order_preserved_within_each_partition() -> None:
    files = ["b/x.py", "node_modules/a.js", "a/y.py", "node_modules/b.js"]
    kept, skipped = filter_changed_files(files, ["node_modules/"])
    assert kept == ["b/x.py", "a/y.py"]
    assert skipped == ["node_modules/a.js", "node_modules/b.js"]


def test_multiple_patterns_or_together() -> None:
    files = ["node_modules/a.js", "vendor/b.go", "src/c.py"]
    kept, skipped = filter_changed_files(files, ["node_modules/", "vendor/"])
    assert kept == ["src/c.py"]
    assert set(skipped) == {"node_modules/a.js", "vendor/b.go"}
