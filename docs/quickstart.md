# Quickstart

Get Tend running against one of your repos in about five minutes.

## 1. Add the analyze workflow

Create `.github/workflows/tend-analyze.yml`:

```yaml
name: tend analyze
on:
  schedule:
    - cron: '0 12 * * 1'        # Mondays at noon UTC
  workflow_dispatch:

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
          sensitivity: balanced
```

Trigger it once via the Actions tab → "Run workflow". The first run
opens a PR that proposes the initial `.tend/owners.yml`. Review the
evidence in the PR body and merge if it looks right.

## 2. Add the routing workflow

Create `.github/workflows/tend-route.yml`:

```yaml
name: tend route
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
          routing-mode: request-review
```

Once `.tend/owners.yml` is on `main`, incoming PRs that touch owned
paths will get their listed owners auto-requested for review.

## 3. (Optional) Try the CLI locally

```bash
uv tool install tend

# Dry-run inference against a repo you have read access to:
tend analyze --repo <your-org>/<repo> --dry-run

# See per-contributor scoring for a directory:
tend explain --repo <your-org>/<repo> --path src/auth/

# Drift report against an existing CODEOWNERS file (read-only):
tend diff --repo <your-org>/<repo> --against-codeowners .github/CODEOWNERS
```

## What you get

- A single source of structured ownership data (`.tend/owners.yml`)
  that downstream tools can read.
- Auto-routed security review on incoming PRs.
- A weekly recomputation that keeps ownership in sync with reality
  (you can change the cron in step 1).
