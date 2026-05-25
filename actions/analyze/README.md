# `tend/actions/analyze`

Composite GitHub Action that runs `tend analyze --create-pr` against the
repo that calls it. Produces (or updates) a PR proposing changes to
`.tend/owners.yml`.

## Usage (published)

```yaml
- uses: <your-org>/tend/actions/analyze@v1
  with:
    sensitivity: balanced       # strict | balanced | permissive
    lookback-days: '180'
    output-path: '.tend/owners.yml'
```

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
