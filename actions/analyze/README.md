# `tend/actions/analyze`

Composite GitHub Action that runs `tend analyze --create-pr` against the
repo that calls it. Produces (or updates) a PR proposing changes to
`.tend/owners.yml`.

## Usage (published)

```yaml
name: tend
on:
  workflow_dispatch:           # manual trigger from the Actions tab
  schedule:
    - cron: '0 12 * * 1'       # Mondays at noon UTC

jobs:
  analyze:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: <your-org>/tend/actions/analyze@v1
        with:
          sensitivity: balanced       # strict | balanced | permissive
          lookback-days: '180'
          output-path: '.tend/owners.yml'
```

### When to run it

Run on a cron, **not on every PR**. Tend does aggregate inference over
months of commit history, so a per-PR trigger would burn API quota for
no signal change and churn the suggestion PR every merge. Weekly
(Mondays) is a reasonable default; bump to daily only for very active
monorepos.

The suggestion PR is opened on a fixed branch (`tend/update`), so each
scheduled run **updates the existing PR** rather than opening a new
one. You'll only ever see one open Tend PR at a time.

### Fork / schedule gotchas

- Scheduled workflows only fire from the **default branch** — the file
  must be on `main` (or whatever your default is) to be picked up.
- On forks, GitHub **pauses scheduled workflows after 60 days of no
  repo activity**. Pushing any commit re-arms the cron.
- The default `GITHUB_TOKEN` works for reading the calling repo and
  opening PRs against it. To read history from a *different* repo,
  pass a PAT via `github-token`.

## Usage (in-repo dev)

When developing against a local checkout of the Tend repo:

```yaml
- uses: ./actions/analyze    # path relative to the workflow's repo
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `sensitivity` | `balanced` | `strict` raises thresholds, `permissive` lowers them. |
| `lookback-days` | `180` | Commit history window. |
| `output-path` | `.tend/owners.yml` | Where Tend writes the YAML. Default is the conventional path. |
| `advanced` | `{}` | JSON overrides for raw `InferenceConfig` fields. Power-user. |
| `github-token` | `${{ github.token }}` | Token used for API reads and PR creation. |

## Permissions required

```yaml
permissions:
  contents: write       # to commit .tend/owners.yml on the tend/update branch
  pull-requests: write  # to open / update the suggestion PR
```

## What does not get touched

This action writes only `.tend/owners.yml`. Your repo's `CODEOWNERS` file
is never read or modified.
