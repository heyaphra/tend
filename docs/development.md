# Developing Tend

## The dev loop you want

Touch a routing/severity/skip rule, see the effect end-to-end in under a
second — no fork, no real PR, no GitHub API call.

```bash
# Edit src/tend/route/{router,skip,sla}.py, then:
uv run pytest tests/unit/ -q
uv run tend route \
  --event-file tests/fixtures/events/dependabot-security.json \
  --changed-files services/api/Pipfile,services/api/Pipfile.lock \
  --owners-file tests/fixtures/owners-example.yml \
  --security-cc @acme/appsec \
  --fallback-owner @acme/appsec \
  --repo acme/widgets --mode assign --dry-run
```

Output should show:

- `Security-advisory PR detected (label-based).`
- `Effective mode: request-review` (bot override fired)
- `Reviewers requested: alice, bob, @org/appsec`
- `CC: @acme/appsec`
- `Skipped 1 vendored/generated file(s): services/api/Pipfile.lock`

Roundtrip: ~1 second.

## Two dev-velocity flags

Both live on `tend route` and `tend sla`:

| Flag | What it overrides | Why |
|---|---|---|
| `--event-file <path>` | `EVENT_PATH` env var | Replace the GitHub event payload with a local JSON fixture. |
| `--changed-files a,b,c` | `gh.get_pr_files()` | Skip the API call to fetch a PR's changed files. Combined with `--dry-run`, no GitHub call happens at all. |

`tend sla` has the same idea via `--fixture <path>`, which provides
both `open_pulls` and per-PR `changed_files` in one JSON file.

When both `--changed-files` and `--dry-run` are set and no token is in
env, `tend route` substitutes a fake token so you don't have to provide
one. Same for `tend sla --fixture --dry-run`.

## Fixtures shipped in the repo

```
tests/fixtures/
├── events/
│   ├── dependabot-bump.json       Regular bump (no security label)
│   ├── dependabot-security.json   Has 'security' label + GHSA ref
│   ├── dependabot-vendored.json   Touches only vendor/ — hits fallback
│   └── human.json                 Non-bot author — should skip
├── owners-example.yml             Three owner rules over /services/* and /docs/
└── sla-snapshot.json              4 open PRs (3 bot, 1 human)
```

To capture a new fixture from a real workflow run, the GitHub event payload
is available at `$GITHUB_EVENT_PATH`:

```bash
# Inside a debugging Actions run:
cat $GITHUB_EVENT_PATH > my-fixture.json
```

The shape is documented in [GitHub's webhook event reference](https://docs.github.com/en/webhooks/webhook-events-and-payloads).

## Try every scenario

```bash
# Routine bump — bot override + standard owners, no security CC.
uv run tend route \
  --event-file tests/fixtures/events/dependabot-bump.json \
  --changed-files services/api/package.json,services/api/package-lock.json \
  --owners-file tests/fixtures/owners-example.yml \
  --security-cc @acme/appsec \
  --repo acme/widgets --dry-run

# Vendored-only PR — all files filtered, falls back to AppSec.
uv run tend route \
  --event-file tests/fixtures/events/dependabot-vendored.json \
  --changed-files vendor/foo/bar.go,vendor/foo/baz.go \
  --owners-file tests/fixtures/owners-example.yml \
  --fallback-owner @acme/appsec \
  --repo acme/widgets --dry-run

# Human PR — skipped with a "not Dependabot" notice.
uv run tend route \
  --event-file tests/fixtures/events/human.json \
  --changed-files services/api/auth.py \
  --owners-file tests/fixtures/owners-example.yml \
  --repo acme/widgets --dry-run

# SLA report against a captured snapshot.
GITHUB_STEP_SUMMARY=/tmp/tend-sla.md uv run tend sla \
  --fixture tests/fixtures/sla-snapshot.json \
  --owners-file tests/fixtures/owners-example.yml \
  --fallback-owner @acme/appsec \
  --repo acme/widgets
cat /tmp/tend-sla.md

# SLA + nudge dry-run — see who would get pinged.
uv run tend sla \
  --fixture tests/fixtures/sla-snapshot.json \
  --owners-file tests/fixtures/owners-example.yml \
  --fallback-owner @acme/appsec \
  --repo acme/widgets --nudge --dry-run
```

## Running tests

```bash
uv run pytest                 # All tests
uv run pytest tests/unit/ -q  # Unit tests only — fast
uv run pytest tests/unit/test_router.py tests/unit/test_skip.py tests/unit/test_sla.py -v
```

The CI integration tests (`tests/integration/test_cli.py`) drive the
CLI end-to-end through Typer's test runner with `pytest-httpx`
mocking the GitHub API.

## Where things live

| Concern | File |
|---|---|
| Owner inference (analyze pipeline) | `src/tend/analyze/` |
| Wilson bounds, severity math | `src/tend/analyze/confidence.py`, `scoring.py` |
| Routing dispatch (per-PR) | `src/tend/route/router.py` — `route_pr` |
| Path matching | `src/tend/route/matcher.py` — `find_owners_for_files` |
| Skip-paths filter | `src/tend/route/skip.py` — `filter_changed_files` |
| SLA report + nudges | `src/tend/route/sla.py` |
| Owners.yml parse / dump | `src/tend/output/tend_yaml.py`, `src/tend/route/parser.py` |
| GitHub API client | `src/tend/github/client.py` |
| CLI surface | `src/tend/cli.py` |
| Composite actions | `actions/{analyze,route,report-sla}/action.yml` |

## A note on idempotency

Two places in v0.3 use the marker-comment idempotency pattern:

1. **`tend analyze --create-pr`** — re-running with unchanged inference
   no-ops via a content-hash marker (`HASH_MARKER` in `github/pr.py`).
2. **`tend sla --nudge`** — re-running on a PR that already has a
   nudge comment at its current SLA state no-ops via
   `<!-- tend-nudge:<level>:<mention> -->` (`NUDGE_MARKER_RE` in
   `route/sla.py`).

A PR transitioning `breach → double_breach` *does* get a second comment
(different marker level) — by design. The escalation pings the fallback
owner explicitly.

## Common gotchas

- **`*.lock` does not match `package-lock.json` or `pnpm-lock.yaml`.**
  These end in `.json` / `.yaml`, not `.lock`. Add them via
  `--extra-skip-path` if you need npm/pnpm coverage.
- **Bot-author override only downgrades `assign`.** `comment`, `all`,
  and `request-review` are explicit choices and respected.
- **SLA clock starts at `pr.created_at`.** Stable across Dependabot
  rebases / force-pushes.
- **Severity is label-based for v0.3.** Title `[security]` prefix and
  body `GHSA-` reference aren't currently checked — keep an eye on
  Dependabot's labeling stability, easy to add a regex fallback in
  `cli.py` if needed.
