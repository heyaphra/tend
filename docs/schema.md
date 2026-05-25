# `.tend/owners.yml` schema (v1)

Tend's only output format. Versioned at the top level so consumers can
detect schema changes.

## Example

```yaml
version: 1
generated_at: "2026-05-25T12:34:56+00:00"
generated_by: "tend/0.2.0"

config:
  sensitivity: balanced
  lookback_days: 180

paths:
  /src/auth/:
    - handle: schiller-manuel
      strength: strong
      commits: 24
      last_touched: 2026-05-22
    - handle: Sheraff
      strength: moderate
      commits: 8
      last_touched: 2026-04-30
  /docs/:
    - handle: acme/docs
      strength: strong
      commits: 0
      last_touched: 2026-05-25
```

## Field reference

### Top level

| Field | Type | Required | Notes |
|---|---|---|---|
| `version` | `int` | yes | Schema version. v1 today. |
| `generated_at` | RFC 3339 datetime | optional | Wall-clock timestamp. Excluded from the content hash. |
| `generated_by` | `str` | optional | Tool + version stamp. Excluded from the content hash. |
| `config` | `mapping` | optional | What sensitivity Tend was run with. See below. |
| `paths` | `mapping` | yes | Pattern → ordered list of contributor records. |

### `config` block

Thin by design — only what a human reviewer needs to interpret the file.
Internal parameters (thresholds, decay, breadth, etc.) are derived from
`sensitivity` and not stored in YAML; use `tend explain` to inspect them.

| Field | Type | Notes |
|---|---|---|
| `sensitivity` | `"strict" \| "balanced" \| "permissive"` | The user-facing dial. |
| `lookback_days` | `int` | History window. |

### `paths.{pattern}` entries

Patterns follow three shapes:

- `*` — matches every file. Lowest priority.
- `/foo/` — directory rule. Matches any file under `foo/`.
- `/foo/bar.py` — file rule. Matches exactly that path.

No globbing — patterns are unambiguous by construction.

Each path maps to a **list of contributor records**, ordered by strength
(primary owner first). Records below the configured threshold are not
emitted at all — Tend never writes a contributor whose claim doesn't
meet the sensitivity's bar.

| Field | Type | Required | Notes |
|---|---|---|---|
| `handle` | `str` | yes | GitHub handle without `@` prefix. Can be `user` or `org/team`. |
| `strength` | `"strong" \| "moderate" \| "suggestive"` | yes | Qualitative confidence label. See thresholds below. |
| `commits` | `int` | yes | Contributor's commit count within the lookback window (rounded). |
| `last_touched` | `YYYY-MM-DD` | yes | Date of the contributor's most recent commit under this path. |

## Strength labels

The label is derived from the Wilson lower bound on the contributor's
per-path share:

| Label | Wilson lower bound |
|---|---|
| `strong` | ≥ 0.60 |
| `moderate` | ≥ 0.35 |
| `suggestive` | ≥ `min_confidence` (sensitivity-dependent) |
| (excluded) | below `min_confidence` |

`min_confidence` defaults to 0.30 (`balanced`); see
[`docs/configuration.md`](configuration.md) for the per-sensitivity
values. The thresholds are intentionally coarse — labels are easier to
review than percentages, and routing decisions rarely benefit from
sub-tier precision. Power users can see the underlying confidence
intervals via `tend explain`.

## Versioning policy

- **Backwards-compatible additions** (new optional top-level keys, new
  optional fields inside a contributor record) bump the patch version of
  `tend` but not the schema. Consumers should ignore unknown fields.
- **Breaking changes** (renamed fields, removed required fields,
  semantically different patterns) bump `version` and the `tend` major
  version. Consumers must check `version` before parsing.

The current schema version is `1`. `tend` v0.2.x produces `version: 1`
output. (Note: `tend` v0.1.x also produced `version: 1` but with a
different per-path shape. See `CHANGELOG.md` for the v0.2 migration —
old files are rejected with a clear error on load.)

## Hand-editing

`.tend/owners.yml` is meant to be reviewed and edited by humans. If you
change `handle:` for a path, Tend will respect your edit on the next
run (the content-hash check no-ops when nothing has changed at the data
level). Removing a path tells Tend "I disagree with this inference"; it
will reappear in the next analyze PR if the underlying contribution data
still warrants it.

To pin an owner regardless of inference, add the entry by hand with
`strength: strong` and a current `last_touched`. Tend's regeneration
won't second-guess a human-pinned entry (idempotency hash treats the
edited file as authoritative).
