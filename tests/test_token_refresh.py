#!/usr/bin/env python3
"""
Smoke test for the Google Merchant MCP token refresh path.

This script verifies that:
  * `get_credentials()` returns a usable Credentials object in legacy mode.
  * `get_headers(creds)` produces the expected Bearer-only header set.
  * If the OAuth credentials advertise `expired=True` and a refresh token, a
    refresh attempt is triggered.

Run from the project root:

    python -m tests.test_token_refresh
"""

import os
import sys
from pathlib import Path
from datetime import datetime
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

os.environ.setdefault("MCP_ENABLE_OAUTH21", "false")
os.environ.setdefault("GOOGLE_MERCHANT_AUTH_TYPE", "oauth")

import merchant_server  # noqa: E402


def _mock_creds(*, expired: bool = False, has_refresh: bool = True):
    creds = mock.MagicMock()
    creds.token = "ya29.fake-token"
    creds.valid = not expired
    creds.expired = expired
    creds.refresh_token = "1//refresh-token" if has_refresh else None
    creds.scopes = ["https://www.googleapis.com/auth/content"]
    creds.expiry = datetime.utcnow()
    return creds


def test_headers_only_have_bearer_and_content_type():
    print("\n=== get_headers() shape ===")
    creds = _mock_creds(expired=False)
    headers = merchant_server.get_headers(creds)
    print(headers)
    assert headers["Authorization"].startswith("Bearer "), "Bearer header missing"
    assert headers["Content-Type"] == "application/json"
    # The Merchant API does not use developer-token / login-customer-id.
    assert "developer-token" not in {k.lower() for k in headers.keys()}
    assert "login-customer-id" not in {k.lower() for k in headers.keys()}
    print("PASSED")


def test_headers_refresh_when_expired():
    print("\n=== get_headers() refreshes expired creds ===")
    creds = _mock_creds(expired=True)

    def _refresh(_):
        creds.valid = True
        creds.expired = False
        creds.token = "ya29.refreshed-token"

    creds.refresh.side_effect = _refresh
    headers = merchant_server.get_headers(creds)
    assert creds.refresh.called, "Expected creds.refresh() to be called"
    assert headers["Authorization"] == "Bearer ya29.refreshed-token"
    print("PASSED")


def test_get_credentials_legacy_env_path():
    """If env-based refresh token is set, get_credentials should construct creds."""
    print("\n=== get_credentials() legacy refresh-token path ===")

    fake_creds = _mock_creds(expired=False)

    def fake_legacy():
        return fake_creds

    with (
        mock.patch.object(merchant_server, "_OAUTH21_ENABLED", False),
        mock.patch.object(merchant_server, "_get_legacy_oauth_credentials", side_effect=fake_legacy),
    ):
        creds = merchant_server.get_credentials()

    assert creds is fake_creds
    print("PASSED")


def main() -> int:
    try:
        test_headers_only_have_bearer_and_content_type()
        test_headers_refresh_when_expired()
        test_get_credentials_legacy_env_path()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        return 1
    print("\nAll token-refresh smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
