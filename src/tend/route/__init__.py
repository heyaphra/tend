"""Routing: consume ``.tend/owners.yml`` to dispatch PRs to owners.

This package owns the read-side of Tend's pipeline. ``analyze`` produces
``owners.yml``; the modules in here consume it.

Public surface:

- ``router.route_pr`` — dispatch a single Dependabot PR.
- ``matcher.find_owners_for_files`` — longest-prefix path matching.
- ``parser.load_owners_yml`` — load + validate the owners file.
- ``skip.filter_changed_files`` — strip vendored / generated paths.
- ``DEPENDABOT_LOGINS`` — recognized Dependabot author logins.

``DEPENDABOT_LOGINS`` lives at the package root so both ``cli.py``
(routing decisions on a single PR) and ``sla`` (the open-PR scan) can
import it without a circular dependency.
"""

from __future__ import annotations

DEPENDABOT_LOGINS = frozenset({"dependabot[bot]", "dependabot-preview[bot]"})

__all__ = ["DEPENDABOT_LOGINS"]
