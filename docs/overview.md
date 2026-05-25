# Overview

Tend infers code ownership from contribution patterns and routes
security findings (PRs today; vulnerability alerts in v1.1) to the
people who actually work in the affected paths.

It ships as two GitHub composite Actions:

- `tend/actions/analyze` — runs ownership inference and opens a PR
  proposing `.tend/owners.yml`.
- `tend/actions/route` — reads `.tend/owners.yml` and dispatches
  incoming PRs to the relevant owners.

## Why a separate file?

Tend writes its ownership data to `.tend/owners.yml`. It does not touch
your `CODEOWNERS` file. Two reasons:

1. **CODEOWNERS is too noisy for security routing.** It fires GitHub's
   reviewer mechanism on every PR, regardless of whether the change
   needs a security review. Mixing those two responsibilities means
   either CODEOWNERS gets pruned (and the general code-review value
   suffers) or every owner gets paged on every PR (and the security
   value suffers). Keeping them separate lets each file do its job.
2. **`.tend/owners.yml` is richer.** It carries confidence scores,
   evidence (commit counts, last-touched dates, runner-up contributors),
   and configuration metadata that a CODEOWNERS file can't represent.
   Downstream tools — `tend route`, dashboards, audit scripts — read
   this richer shape directly.

The two files coexist. CODEOWNERS keeps doing GitHub's review routing;
Tend's file backs security routing and any other downstream tool you
point at it.

## Three things Tend cares about

- **Trust the data, not the file.** Ownership is inferred from
  observable behavior (commits, line counts, recency) rather than asked.
- **Suggest, don't assign.** Tend opens PRs. Humans merge them.
  `.tend/owners.yml` only changes via a reviewed PR.
- **Be conservative about the wildcard.** A `* @one-person` rule that
  ends up in a security routing file is almost always wrong. Tend
  drops wildcards owned by individuals unless they're a team handle
  or explicitly pinned.

## What's next

- `docs/quickstart.md` — five-minute install of both Actions.
- `docs/schema.md` — the `.tend/owners.yml` v1 schema.
- `docs/configuration.md` — every flag, every input.
