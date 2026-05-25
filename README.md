# tend

Infer code ownership from contribution patterns and route security findings
to the people who actually work in the affected paths.

Tend ships as two GitHub composite Actions:

- `actions/analyze` — runs ownership inference and opens a PR proposing
  changes to `.tend/owners.yml`.
- `actions/route` — reads `.tend/owners.yml` and assigns reviewers /
  posts mentions on incoming PRs (and, in v1.1, on Dependabot alerts).

Tend never writes to your `CODEOWNERS` file. The two coexist:
`CODEOWNERS` is GitHub's general-purpose review mechanism;
`.tend/owners.yml` is a structured ownership record consumed by Tend
(and any other tool you point at it) for security routing.

## Quickstart

Add the analyze workflow to your repo:

```yaml
# .github/workflows/tend-analyze.yml
name: tend analyze
on:
  schedule: [{ cron: "0 12 * * 1" }]
  workflow_dispatch:

jobs:
  analyze:
    runs-on: ubuntu-latest
    permissions: { contents: write, pull-requests: write }
    steps:
      - uses: actions/checkout@v4
      - uses: <org>/tend/actions/analyze@v1
        with:
          sensitivity: balanced
```

Add the routing workflow:

```yaml
# .github/workflows/tend-route.yml
name: tend route
on:
  pull_request_target:
    types: [opened, synchronize]

jobs:
  route:
    runs-on: ubuntu-latest
    permissions: { pull-requests: write, issues: write }
    steps:
      - uses: actions/checkout@v4
      - uses: <org>/tend/actions/route@v1
        with:
          routing-mode: request-review
```

## CLI

```bash
uv tool install tend

tend analyze --sensitivity balanced --dry-run
tend diff --against-codeowners ./.github/CODEOWNERS
tend explain --path src/security/
```

See `docs/` for the full reference.

## Status

Alpha (v0.1.0). API and schema may evolve before v1.0.
