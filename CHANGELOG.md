# Changelog

All notable changes to Tend are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Tend adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — v0.2.0

### Changed
- **Breaking: `.tend/owners.yml` per-path shape simplified.** Each path
  now maps to a *list* of contributor records (`handle`, `strength`,
  `commits`, `last_touched`) instead of the v0.1 owner-dict with
  `confidence` / `evidence` / `runner_ups` sub-blocks. Runners-up are
  just additional list entries with weaker strength labels.
- **Strength labels replace confidence floats in YAML.** Each contributor
  record carries one of `strong` / `moderate` / `suggestive` derived from
  the underlying confidence bound. The numeric confidence is now
  internal-only; use `tend explain` to inspect it.
- **`config` block trimmed.** Only `sensitivity` and `lookback_days`
  appear on disk; the `resolved:` sub-block (halflife, alpha, etc.) is
  gone — those parameters are derived from `sensitivity` and visible via
  `tend explain`.
- **`last_touched` uses `YYYY-MM-DD`.** Was full ISO datetime; shorter
  and more human-readable.
- `tend` version bumped to **0.2.0**.

### Migration notes
- The first `tend analyze` run after upgrading to v0.2.x will refresh
  any open `tend/update` PR even if the logical ownership is unchanged.
  The content hash includes the schema shape, so a one-time PR refresh
  is expected.
- v0.1.x `.tend/owners.yml` files are rejected on load with a clear
  error pointing to the migration. Run `tend analyze` to regenerate.
- The `--advanced` JSON escape hatch on `tend analyze` still accepts the
  old field names for forward-compatibility with custom tuning.

## [0.1.0] — Initial release

### Added
- Initial bootstrap of the Tend v1 project structure.
- `.tend/owners.yml` versioned schema (`version: 1`) — the single
  output format produced by `tend analyze`.
- Sensitivity presets (`strict` / `balanced` / `permissive`) translating
  user intent into resolved inference parameters.
- `tend route` subcommand with `route_pr()` for PR routing
  (assign / request-review / comment / all modes).
- `tend diff --against-codeowners <path>` diagnostic command that
  compares Tend's inference against an existing CODEOWNERS file and
  prints a Rich terminal report. Read-only — never writes to CODEOWNERS.
- GitHub composite Actions `actions/analyze` and `actions/route` for
  customer adoption without hosted infrastructure.

### Notes for users of the prototype
- Tend does **not** manage your `CODEOWNERS` file. If you previously
  ran the prototype (`codeowners-infer`), its open suggestion PR
  remains on the `codeowners-infer/update` branch and is unaffected;
  close it manually before adopting Tend.
- Tend writes only `.tend/owners.yml`. The two files coexist.
- Routing for `dependabot_alert` events is planned for v1.1.
