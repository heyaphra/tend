"""Authentication for the GitHub API.

Two modes:

- **PAT** — a static ``Authorization: Bearer <token>`` header. Used for the
  CLI surface and any GitHub Actions runner that passes ``GITHUB_TOKEN``
  through the action input.
- **App** — a GitHub App's RS256 JWT exchanged for an installation token,
  refreshed transparently ~5 minutes before expiry.

The constructors are explicit (no hidden environment reads in the library
core); ``get_auth_from_env`` is the seam the CLI uses to read
``TEND_GITHUB_TOKEN`` and friends.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.github.com"
_REFRESH_WINDOW_SECONDS = 300


@dataclass
class Auth:
    """Auth strategy. ``mode='pat'`` returns a static header; ``mode='app'`` refreshes."""

    mode: str  # "pat" or "app"
    token: str = ""
    app_id: int = 0
    private_key: str = ""
    installation_id: int = 0
    api_base: str = DEFAULT_API_BASE
    _expires_at: float = 0.0
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    async def headers(self) -> dict[str, str]:
        if self.mode == "pat":
            return {"Authorization": f"Bearer {self.token}"}
        if self._lock is None:
            self._lock = asyncio.Lock()
        if time.time() > self._expires_at - _REFRESH_WINDOW_SECONDS:
            async with self._lock:
                if time.time() > self._expires_at - _REFRESH_WINDOW_SECONDS:
                    await self._refresh_installation_token()
        return {"Authorization": f"Bearer {self.token}"}

    async def _refresh_installation_token(self) -> None:
        app_jwt = _build_app_jwt(self.app_id, self.private_key)
        async with httpx.AsyncClient(base_url=self.api_base, timeout=30.0) as client:
            resp = await client.post(
                f"/app/installations/{self.installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        self.token = data["token"]
        expires_iso = data["expires_at"].replace("Z", "+00:00")
        self._expires_at = datetime.fromisoformat(expires_iso).timestamp()


def _build_app_jwt(app_id: int, private_key: str) -> str:
    """Build a 9-minute RS256 JWT for GitHub App authentication.

    9 minutes (vs GitHub's 10-minute max) gives a safety margin; ``iat - 60``
    backdates the issued-at to allow modest clock skew.
    """
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": str(app_id)}
    return jwt.encode(payload, private_key, algorithm="RS256")


def token_auth(token: str, *, api_base: str = DEFAULT_API_BASE) -> Auth:
    """Construct a PAT-mode ``Auth``."""
    return Auth(mode="pat", token=token, api_base=api_base)


def app_auth(
    app_id: int,
    private_key: str,
    installation_id: int,
    *,
    api_base: str = DEFAULT_API_BASE,
) -> Auth:
    """Construct a GitHub-App-mode ``Auth``. ``private_key`` is the PEM contents."""
    return Auth(
        mode="app",
        app_id=app_id,
        private_key=private_key,
        installation_id=installation_id,
        api_base=api_base,
    )


def get_auth_from_env() -> Auth:
    """Read auth credentials from ``TEND_*`` / ``GITHUB_TOKEN`` env vars.

    Precedence: App credentials (if private key file exists) → PAT. The
    Actions runner sets ``GITHUB_TOKEN`` automatically; ``TEND_GITHUB_TOKEN``
    overrides if set.
    """
    app_id_str = os.environ.get("TEND_GITHUB_APP_ID", "")
    key_path = os.environ.get("TEND_GITHUB_PRIVATE_KEY_PATH", "")
    install_str = os.environ.get("TEND_GITHUB_INSTALLATION_ID", "")
    api_base = os.environ.get("TEND_GITHUB_API_BASE", DEFAULT_API_BASE)

    if app_id_str and key_path and install_str:
        key_file = Path(key_path).expanduser()
        if key_file.exists():
            return app_auth(
                app_id=int(app_id_str),
                private_key=key_file.read_text(),
                installation_id=int(install_str),
                api_base=api_base,
            )
        logger.warning(
            "GitHub App private key not found at %s; falling back to PAT auth.", key_file
        )

    token = os.environ.get("TEND_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token_auth(token, api_base=api_base)

    raise RuntimeError(
        "No GitHub credentials configured. Set TEND_GITHUB_TOKEN (or GITHUB_TOKEN), "
        "or TEND_GITHUB_APP_ID + TEND_GITHUB_PRIVATE_KEY_PATH + TEND_GITHUB_INSTALLATION_ID."
    )


def _now_utc() -> datetime:
    """Module-level helper kept for tests that need to assert against a clock."""
    return datetime.now(UTC)
