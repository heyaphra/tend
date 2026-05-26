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
| `dry-run` | `'false'` | When `'true'`, log the routing plan without calling any write API. Use during setup. |
| `github-token` | `${{ github.token }}` | Token used for the assignment / review-request / comment API. |

## Modes

| Mode | Effect |
|---|---|
| `assign` | Adds owners as PR assignees. |
| `request-review` | Adds owners (individuals and teams) as requested reviewers. |
| `comment` | Posts a PR comment mentioning owners and listing which files they cover. |
| `all` | All three. |

## Testing safely

Before enabling routing on a real repo (especially a fork of a busy
upstream like `tanstack/router`, where `owners.yml` will contain dozens
of contributors you don't want to spam), test with `dry-run: 'true'`:

```yaml
- uses: <your-org>/tend/actions/route@v1
  with:
    routing-mode: request-review
    dry-run: 'true'
```

In dry-run, the action loads `owners.yml`, fetches the PR's changed
files, computes the routing plan, prints it, and exits. **No GitHub
write API is called** — no assignments, no review requests, no
comments, no notifications to anyone.

Other ways to test without spamming:

- **Edit `.tend/owners.yml` on your fork** so every owner is just you
  (or a sock-puppet account). The action still runs end-to-end against
  real APIs, but only you get notified.
- **Use a personal scratch repo** instead of the upstream fork. Push a
  few files, hand-write a tiny `owners.yml`, simulate a Dependabot PR
  with `pull_request` trigger and a fake author.

Flip `dry-run` back to `'false'` once you've seen the plan look right.

## v1.1: Dependabot alerts

`dependabot_alert` event types are recognized but currently logged and
skipped. Full routing of vulnerability alerts is planned for v1.1.
