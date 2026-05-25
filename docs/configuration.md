# Configuration reference

Every flag on every Action and CLI command.

## Sensitivity presets

The `sensitivity` knob is the high-level dial. It maps to resolved
algorithm parameters as follows:

| Preset | `min_confidence` | `alpha` (shrinkage) | `min_commits` |
|---|---|---|---|
| `strict` | 0.40 | 8.0 | 5 |
| `balanced` (default) | 0.30 | 5.0 | 3 |
| `permissive` | 0.20 | 3.0 | 2 |

Higher `alpha` punishes low-commit-count owners more. Higher
`min_confidence` excludes more contributors per path. Higher
`min_commits` excludes contributors who only touched the path once or
twice.

## Power-user overrides (`--advanced`)

The CLI's `--advanced` flag takes a JSON object whose keys are
`InferenceConfig` field names. Any field not listed falls back to the
preset / default.

```bash
tend analyze --repo o/r \
  --sensitivity balanced \
  --advanced '{"halflife_days": 60, "max_depth": 4}'
```

| Field | Default | Notes |
|---|---|---|
| `halflife_days` | 90.0 | Recency decay. 60 weights newer activity more. |
| `min_confidence` | 0.30 (balanced) | Per-path inclusion threshold. |
| `alpha` | 5.0 (balanced) | Bayesian shrinkage on lone survivors. |
| `min_commits` | 3 (balanced) | Per-contributor commit count gate. |
| `volume_floor` | 10 | Skip directories with fewer total commits. |
| `bot_filter_enabled` | true | Set false to keep bots in scoring (don't do this). |
| `lookback_days` | 180 | History window. |
| `max_commits` | 2000 | Stop after this many commits to bound API cost. |
| `max_depth` | 3 | Max directory depth for emitted rules. |
| `max_owners_per_path` | 3 | Cap on owners listed per path. |
| `max_files_per_commit` | 500 | Skip giant merges / regenerations. |

## `tend analyze`

```
tend analyze --repo OWNER/NAME
  [--sensitivity strict|balanced|permissive]
  [--lookback-days N]
  [--advanced JSON]
  [--output-path PATH]
  [--create-pr]
  [--dry-run]
  [--github-token TOKEN]
```

The default writes `.tend/owners.yml` directly. `--create-pr` instead
opens (or updates) a suggestion PR on the `tend/update` branch.
`--dry-run` writes nothing.

## `tend diff`

```
tend diff --repo OWNER/NAME --against-codeowners PATH
  [--sensitivity ...]
  [--lookback-days N]
```

Read-only diagnostic. Compares Tend's inference against an existing
CODEOWNERS file and prints a Rich-formatted drift report. **Never
modifies CODEOWNERS.** If `--against-codeowners` is omitted, Tend
probes `./.github/CODEOWNERS` and `./CODEOWNERS`.

## `tend explain`

```
tend explain --repo OWNER/NAME --path STR
  [--sensitivity ...]
  [--lookback-days N]
```

Prints the per-contributor scoring breakdown for a single path. Use to
debug "why isn't @alice listed for `/src/auth/`?".

## `tend route`

```
tend route --repo OWNER/NAME
  [--owners-file PATH]
  [--mode assign|request-review|comment|all]
  [--dry-run]
```

Reads `EVENT_NAME` and `EVENT_PATH` env vars (set by the GitHub Actions
runner) and dispatches the event:

- `pull_request*` → routed via `route_pr`.
- `dependabot_alert` → logged and exit 0 (v1.1 will route these).
- Anything else → logged and exit 0.

## Authentication

Two modes; precedence is App credentials → PAT.

### PAT (CLI, Actions runner)

```bash
export TEND_GITHUB_TOKEN=ghp_...
# or, on Actions runners, GITHUB_TOKEN is picked up automatically
```

### GitHub App (installed apps, future managed offering)

```bash
export TEND_GITHUB_APP_ID=123456
export TEND_GITHUB_PRIVATE_KEY_PATH=~/private-key.pem
export TEND_GITHUB_INSTALLATION_ID=987654
```

If the private key path doesn't resolve, Tend falls back to PAT
(with a warning) if `TEND_GITHUB_TOKEN` is set.

## Note: HTTP caching

The prototype shipped with a hishel-based response cache. Tend v1
removes it for simplicity. If you run analyze on a hot schedule and
hit GitHub rate limits, lower `--lookback-days` or run less often.
