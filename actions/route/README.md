# `tend/actions/route`

Composite GitHub Action that routes PR (and, in v1.1, Dependabot alert)
events to the owners declared in `.tend/owners.yml`.

## Usage (published)

```yaml
on:
  pull_request_target:
    types: [opened, synchronize, reopened]

jobs:
  route:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      issues: write
    steps:
      - uses: actions/checkout@v4
      - uses: <your-org>/tend/actions/route@v1
        with:
          routing-mode: request-review   # assign | request-review | comment | all
```

## Usage (in-repo dev)

```yaml
- uses: ./actions/route
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `owners-file` | `.tend/owners.yml` | Where to read the ownership rules from. |
| `routing-mode` | `request-review` | What to do with matched owners (see below). |
| `github-token` | `${{ github.token }}` | Token used for the assignment / review-request / comment API. |

## Modes

| Mode | Effect |
|---|---|
| `assign` | Adds owners as PR assignees. |
| `request-review` | Adds owners (individuals and teams) as requested reviewers. |
| `comment` | Posts a PR comment mentioning owners and listing which files they cover. |
| `all` | All three. |

## v1.1: Dependabot alerts

`dependabot_alert` event types are recognized but currently logged and
skipped. Full routing of vulnerability alerts is planned for v1.1.
