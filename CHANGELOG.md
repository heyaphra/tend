# Changelog

All notable changes to Tend are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Tend adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — v0.3.0

The Dependabot vulnerability-management MVP. Tend goes from "routes
Dependabot PRs to their owners" to "operationalizes Dependabot triage
with severity-aware routing, vendored-path skipping, and SLA tracking
with nudges." All additions are layered on top of the existing
``route_pr`` / ``owners.yml`` flow — no schema changes, no new config
file. Settings vary via CLI flags / Action inputs.

### Added
- **Bot-author override.** ``--mode assign`` on a Dependabot-authored PR
  is downgraded to ``request-review``: assignees imply ownership of a
  work item, which doesn't apply to a bot. Other modes are unchanged.
- **Security-label awareness.** PRs with a ``security`` or
  ``security-advisory`` label are detected at routing time. Combine with
  ``--security-cc @org/appsec`` to always CC the security team on those
  PRs (additive — does not replace per-file owners).
- **Skip-paths + fallback owner.** Files under ``node_modules/``,
  ``vendor/``, ``dist/``, ``build/``, and ``*.lock`` basenames are
  stripped before owner matching. ``--extra-skip-path`` (repeatable)
  adds more; ``--fallback-owner @org/appsec`` routes the PR there when
  no per-file rule matches (or every file was skipped).
- **``tend sla`` subcommand.** Walks open Dependabot PRs, classifies
  severity by label, computes age vs. per-severity SLA, renders a
  grouped markdown table to ``$GITHUB_STEP_SUMMARY`` (else stdout).
  Flags: ``--sla-hours`` (default 72), ``--security-sla-hours``
  (default 24). Always exits 0.
- **Nudge / escalation comments.** ``tend sla --nudge`` posts comments
  on PRs past SLA, mentioning the next-strongest owner. At 2× SLA the
  comment escalates to ``--fallback-owner``. Idempotent via HTML-comment
  markers (``<!-- tend-nudge:breach:@handle -->``); a re-run posts
  nothing. A breach→double-breach transition produces a second comment
  with the escalation marker — by design.
- **Dev velocity flags on ``tend route``.** ``--event-file`` overrides
  ``EVENT_PATH`` with a local JSON fixture; ``--changed-files`` skips
  the GitHub API call to fetch PR files. With ``--dry-run`` you can run
  the full routing logic offline in under a second — no fork, no PR.
- **``--fixture`` flag on ``tend sla``.** JSON file with
  ``{open_pulls: [...], changed_files: {n: [...]}}`` replaces live
  GitHub calls — same offline workflow as ``tend route``.
- **New composite action ``actions/report-sla``.** Wraps ``tend sla``
  with cron-friendly inputs. Recommended consumer cron:
  ``0 13 * * 1-5`` (weekdays, 1pm UTC).
- **Event fixtures.** ``tests/fixtures/events/`` ships four hand-crafted
  payloads (security PR, regular bump, vendored-only, human author)
  and ``tests/fixtures/sla-snapshot.json`` for offline test/dev work.

### Changed
- ``DEPENDABOT_LOGINS`` moved from ``cli.py`` to
  ``tend.route.__init__`` so the SLA collector can import it without a
  circular dependency. Still re-exported from ``tend.cli`` for
  back-compat.
- ``RoutingResult`` gains ``fallback_used``, ``effective_mode``, and
  ``cc_handles`` fields. All new fields default to safe values; v0.2.x
  callers and tests work unchanged.
- ``actions/route/action.yml`` gains inputs ``security-cc``,
  ``fallback-owner``, ``extra-skip-paths``. All optional; behavior
  unchanged when omitted.
- ``tend`` version bumped to **0.3.0**.

### Deferred (NOT in v0.3)
- ``.tend/route.yml`` config file. Settings live as CLI flags / Action
  inputs for now; we'll revisit if a customer hits the limits.
- Dependabot-alert routing (still v1.1).
- Slack / email destinations for the SLA report.
- ``--fail-on-double-breach`` (reserved flag space, currently a no-op).
- GraphQL Security Advisories API for ground-truth severity (label
  heuristic is sufficient for MVP).

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
