"""Auth construction and header generation tests.

The App-mode token refresh path makes real HTTP calls; ``pytest-httpx``
mocks the access-token endpoint.
"""

from __future__ import annotations

import time

import pytest
from pytest_httpx import HTTPXMock

from tend.github.auth import (
    Auth,
    app_auth,
    get_auth_from_env,
    token_auth,
)

VALID_PEM = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAtest_placeholder_will_be_swapped_in_tests_that_need_signing
-----END RSA PRIVATE KEY-----
"""


@pytest.mark.asyncio
async def test_token_auth_returns_static_bearer_header():
    auth = token_auth("ghp_testtoken")
    assert auth.mode == "pat"
    headers = await auth.headers()
    assert headers == {"Authorization": "Bearer ghp_testtoken"}


@pytest.mark.asyncio
async def test_app_auth_constructs_app_mode():
    """Setting expiry to the future skips the refresh path so we don't try
    to actually sign a JWT against the placeholder PEM."""
    auth = app_auth(app_id=123, private_key="pem-text", installation_id=456)
    auth.token = "cached-installation-token"
    auth._expires_at = time.time() + 3600
    assert auth.mode == "app"
    headers = await auth.headers()
    assert headers == {"Authorization": "Bearer cached-installation-token"}


@pytest.mark.asyncio
async def test_app_auth_refreshes_when_token_near_expiry(monkeypatch, httpx_mock: HTTPXMock):
    """Token within the refresh window triggers a POST to the access-token
    endpoint; the new token is then used for subsequent ``headers()`` calls."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/app/installations/789/access_tokens",
        json={
            "token": "fresh-installation-token",
            "expires_at": "2099-12-31T23:59:59Z",
        },
    )
    monkeypatch.setattr(
        "tend.github.auth._build_app_jwt",
        lambda _app_id, _private_key: "fake.jwt.token",
    )

    auth = app_auth(app_id=123, private_key="pem-text", installation_id=789)
    # Token is empty and expires_at is 0 → forces refresh on first call.
    headers = await auth.headers()
    assert headers == {"Authorization": "Bearer fresh-installation-token"}
    assert auth.token == "fresh-installation-token"


def test_get_auth_from_env_prefers_pat_when_no_app_config(monkeypatch):
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "ghp_pat")
    monkeypatch.delenv("TEND_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("TEND_GITHUB_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("TEND_GITHUB_INSTALLATION_ID", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    auth = get_auth_from_env()
    assert auth.mode == "pat"
    assert auth.token == "ghp_pat"


def test_get_auth_from_env_falls_back_to_github_token(monkeypatch):
    """Actions runners set ``GITHUB_TOKEN`` automatically; tend should pick
    it up when ``TEND_GITHUB_TOKEN`` isn't explicitly set."""
    monkeypatch.delenv("TEND_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "runner-token")
    monkeypatch.delenv("TEND_GITHUB_APP_ID", raising=False)
    auth = get_auth_from_env()
    assert auth.mode == "pat"
    assert auth.token == "runner-token"


def test_get_auth_from_env_uses_app_when_private_key_exists(monkeypatch, tmp_path):
    key_file = tmp_path / "private.pem"
    key_file.write_text("pem-contents")
    monkeypatch.setenv("TEND_GITHUB_APP_ID", "42")
    monkeypatch.setenv("TEND_GITHUB_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setenv("TEND_GITHUB_INSTALLATION_ID", "99")
    auth = get_auth_from_env()
    assert auth.mode == "app"
    assert auth.app_id == 42
    assert auth.installation_id == 99
    assert auth.private_key == "pem-contents"


def test_get_auth_from_env_falls_back_to_pat_if_key_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("TEND_GITHUB_APP_ID", "42")
    monkeypatch.setenv("TEND_GITHUB_PRIVATE_KEY_PATH", str(tmp_path / "nope.pem"))
    monkeypatch.setenv("TEND_GITHUB_INSTALLATION_ID", "99")
    monkeypatch.setenv("TEND_GITHUB_TOKEN", "fallback-pat")
    auth = get_auth_from_env()
    assert auth.mode == "pat"
    assert auth.token == "fallback-pat"


def test_get_auth_from_env_raises_when_nothing_configured(monkeypatch):
    for var in (
        "TEND_GITHUB_TOKEN",
        "GITHUB_TOKEN",
        "TEND_GITHUB_APP_ID",
        "TEND_GITHUB_PRIVATE_KEY_PATH",
        "TEND_GITHUB_INSTALLATION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="No GitHub credentials"):
        get_auth_from_env()


def test_auth_dataclass_default_api_base():
    auth = Auth(mode="pat", token="x")
    assert auth.api_base == "https://api.github.com"
