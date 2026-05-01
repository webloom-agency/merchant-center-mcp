"""
Smoke + unit tests for the Google Merchant MCP server.

These tests deliberately avoid any live Merchant API call: they only validate
that the server module imports, that the read-only gate behaves correctly,
that account-ID / pagination helpers work, and that the registered MCP tool
surface matches the blueprint.

To run a tiny live exercise (requires real credentials and a Merchant Center
account), set RUN_LIVE_MERCHANT_TESTS=1 in your env and uncomment the live
section at the bottom.
"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# Make the project importable when running tests directly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Disable OAuth 2.1 + read-only mode before importing the server so the module
# initialization doesn't try to spin up the auth provider during tests.
os.environ.setdefault("MCP_ENABLE_OAUTH21", "false")
os.environ.setdefault("GOOGLE_MERCHANT_READ_ONLY", "1")

import merchant_server  # noqa: E402


class TestNormalizeAccountId(unittest.TestCase):
    def test_strips_non_digits(self):
        self.assertEqual(merchant_server.normalize_account_id("123-456-789"), "123456789")
        self.assertEqual(merchant_server.normalize_account_id("accounts/123456"), "123456")
        self.assertEqual(merchant_server.normalize_account_id(123456), "123456")

    def test_falls_back_to_default(self):
        with mock.patch.object(merchant_server, "DEFAULT_MERCHANT_ACCOUNT_ID", "987654321"):
            self.assertEqual(merchant_server.normalize_account_id(None), "987654321")
            self.assertEqual(merchant_server.normalize_account_id(""), "987654321")

    def test_raises_when_empty(self):
        with mock.patch.object(merchant_server, "DEFAULT_MERCHANT_ACCOUNT_ID", None):
            with self.assertRaises(ValueError):
                merchant_server.normalize_account_id(None)
            with self.assertRaises(ValueError):
                merchant_server.normalize_account_id("---")


class TestReadOnlyGate(unittest.TestCase):
    def test_get_is_readonly(self):
        self.assertTrue(merchant_server._is_readonly_method("GET", "/accounts/v1beta/accounts"))

    def test_search_is_readonly(self):
        path = "/reports/v1beta/accounts/123/reports:search"
        self.assertTrue(merchant_server._is_readonly_method("POST", path))

    def test_render_is_readonly(self):
        self.assertTrue(
            merchant_server._is_readonly_method(
                "POST", "/issueresolution/v1beta/accounts/1:renderaccountissues"
            )
        )
        self.assertTrue(
            merchant_server._is_readonly_method(
                "POST", "/issueresolution/v1beta/accounts/1/products/2:renderproductissues"
            )
        )

    def test_listsubaccounts_is_readonly(self):
        self.assertTrue(
            merchant_server._is_readonly_method(
                "GET", "/accounts/v1beta/accounts/1:listSubaccounts"
            )
        )

    def test_post_insert_blocked_in_readonly_mode(self):
        with mock.patch.object(merchant_server, "GOOGLE_MERCHANT_READ_ONLY", True):
            with self.assertRaises(PermissionError):
                merchant_server._request("POST", "/products/v1beta/accounts/1/products")
            with self.assertRaises(PermissionError):
                merchant_server._request("DELETE", "/products/v1beta/accounts/1/products/x")
            with self.assertRaises(PermissionError):
                merchant_server._request("PATCH", "/promotions/v1beta/accounts/1/promotions/x")


class TestPagination(unittest.TestCase):
    def test_paginate_collects_all_pages(self):
        pages = [
            {"items": [{"id": 1}, {"id": 2}], "nextPageToken": "p2"},
            {"items": [{"id": 3}], "nextPageToken": None},
        ]
        calls = []

        def fake_request(method, path, *, params=None, body=None):
            calls.append((method, path, dict(params or {}), dict(body or {})))
            return pages[len(calls) - 1]

        with mock.patch.object(merchant_server, "_request", side_effect=fake_request):
            items = merchant_server._paginate(
                "GET", "/x", items_key="items", params={"pageSize": 2}
            )

        self.assertEqual(items, [{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], {"pageSize": 2})
        self.assertEqual(calls[1][2], {"pageSize": 2, "pageToken": "p2"})

    def test_paginate_respects_max_items(self):
        pages = [
            {"items": [{"id": 1}, {"id": 2}], "nextPageToken": "p2"},
            {"items": [{"id": 3}, {"id": 4}], "nextPageToken": None},
        ]

        def fake_request(method, path, *, params=None, body=None):
            return pages.pop(0)

        with mock.patch.object(merchant_server, "_request", side_effect=fake_request):
            items = merchant_server._paginate(
                "GET", "/x", items_key="items", max_items=3
            )

        self.assertEqual(items, [{"id": 1}, {"id": 2}, {"id": 3}])


class TestToolSurface(unittest.TestCase):
    """Verify all tools required by the Merchant MCP blueprint are registered."""

    EXPECTED_TOOLS = {
        "list_accounts",
        "list_subaccounts",
        "get_account",
        "list_users",
        "list_programs",
        "list_regions",
        "get_shipping_settings",
        "get_business_info",
        "list_products",
        "get_product",
        "list_data_sources",
        "get_data_source",
        "list_file_uploads",
        "aggregate_product_statuses",
        "render_account_issues",
        "render_product_issues",
        "run_merchant_query",
        "get_top_products",
        "get_price_competitiveness",
        "get_best_sellers",
        "list_promotions",
        "get_promotion",
        "list_quotas",
    }

    def test_all_tools_registered(self):
        registered = asyncio.run(merchant_server.mcp.get_tools())
        registered_names = set(registered.keys())
        missing = self.EXPECTED_TOOLS - registered_names
        self.assertFalse(missing, f"Missing tools: {sorted(missing)}")

    def test_resources_and_prompts_registered(self):
        async def _collect():
            resources = await merchant_server.mcp.get_resources()
            prompts = await merchant_server.mcp.get_prompts()
            return resources, prompts

        resources, prompts = asyncio.run(_collect())
        self.assertIn("merchant-reports://reference", resources)
        self.assertIn("google_merchant_workflow", prompts)
        self.assertIn("merchant_reports_help", prompts)


class TestMockRequest(unittest.TestCase):
    """End-to-end tool exercise using a mocked HTTP layer."""

    def test_list_accounts_calls_correct_endpoint(self):
        captured = {}

        def fake_request(method, url, headers=None, params=None, json=None):
            captured["method"] = method
            captured["url"] = url

            class _Resp:
                status_code = 200
                content = b'{"accounts":[{"name":"accounts/1","accountName":"Test"}]}'

                def json(self_):
                    return {"accounts": [{"name": "accounts/1", "accountName": "Test"}]}

                @property
                def text(self_):
                    return self_.content.decode()

            return _Resp()

        fake_creds = mock.MagicMock()
        fake_creds.valid = True
        fake_creds.token = "fake-token"

        with (
            mock.patch.object(merchant_server, "get_credentials", return_value=fake_creds),
            mock.patch("merchant_server.requests.request", side_effect=fake_request),
        ):
            # FastMCP wraps tool functions; call the underlying coroutine via .fn
            result = asyncio.run(merchant_server.list_accounts.fn())

        self.assertIn("accounts/1", result)
        self.assertEqual(captured["method"], "GET")
        self.assertTrue(captured["url"].endswith("/accounts/v1beta/accounts"))


if __name__ == "__main__":
    unittest.main()
