#!/usr/bin/env python3
"""Unit tests for MCP OAuth disk state (including in-flight login state)."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp.server.auth.provider import AuthorizationCode
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl

from auth import mcp_oauth_state_store as store


class McpOauthStateStoreTests(unittest.TestCase):
    def test_roundtrip_pending_and_auth_codes(self):
        clients = {
            "cid": OAuthClientInformationFull(
                client_id="cid",
                client_secret="secret",
                redirect_uris=[AnyHttpUrl("http://localhost/cb")],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_post",
            )
        }
        pending = {
            "gstate": {
                "client_id": "cid",
                "redirect_uri": "http://localhost/cb",
                "redirect_uri_provided_explicitly": True,
                "state": "client-state",
                "code_challenge": "challenge",
                "scopes": ["https://www.googleapis.com/auth/content"],
                "created_at": time.time(),
            }
        }
        code = "mcp-code"
        auth_codes = {
            code: AuthorizationCode(
                code=code,
                client_id="cid",
                redirect_uri=AnyHttpUrl("http://localhost/cb"),
                redirect_uri_provided_explicitly=True,
                scopes=["https://www.googleapis.com/auth/content"],
                expires_at=time.time() + 300,
                code_challenge="challenge",
            )
        }
        payload = store.serialize_state(
            clients,
            {},
            {},
            {},
            pending,
            auth_codes,
            {code: "user@example.com"},
            {code: "id.token"},
            {"user@example.com": "id.token"},
        )
        (
            out_clients,
            _access,
            _refresh,
            _emails,
            out_pending,
            out_codes,
            out_code_emails,
            out_code_id,
            out_user_id,
        ) = store.deserialize_state(payload)

        self.assertIn("cid", out_clients)
        self.assertEqual(out_pending["gstate"]["client_id"], "cid")
        self.assertIn(code, out_codes)
        self.assertEqual(out_code_emails[code], "user@example.com")
        self.assertEqual(out_code_id[code], "id.token")
        self.assertEqual(out_user_id["user@example.com"], "id.token")

    def test_expired_pending_is_dropped(self):
        payload = store.serialize_state(
            {},
            {},
            {},
            {},
            {
                "old": {
                    "client_id": "cid",
                    "created_at": time.time() - store.DEFAULT_PENDING_TTL_SECONDS - 10,
                }
            },
            {},
            {},
            {},
            {},
        )
        *_, pending, auth_codes, _, _, _ = store.deserialize_state(payload)
        self.assertEqual(pending, {})
        self.assertEqual(auth_codes, {})

    def test_atomic_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = store.mcp_oauth_state_path(tmp)
            payload = store.serialize_state(
                {},
                {},
                {},
                {},
                {
                    "s": {
                        "client_id": "cid",
                        "created_at": time.time(),
                    }
                },
            )
            store.write_mcp_oauth_state_atomic(path, payload)
            raw = store.read_mcp_oauth_state(path)
            self.assertIsNotNone(raw)
            *_, pending, _, _, _, _ = store.deserialize_state(raw)
            self.assertIn("s", pending)


if __name__ == "__main__":
    unittest.main()
