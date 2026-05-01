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

    def test_domain_input_suggests_find_accounts(self):
        """A domain like 'supersmart.com' must trigger a recovery hint that
        names the `find_accounts` tool with the right query, so the LLM
        retries automatically instead of asking the human for an ID."""
        with mock.patch.object(merchant_server, "DEFAULT_MERCHANT_ACCOUNT_ID", None):
            with self.assertRaises(ValueError) as ctx:
                merchant_server.normalize_account_id("supersmart.com")
        msg = str(ctx.exception)
        self.assertIn("find_accounts", msg)
        self.assertIn("supersmart.com", msg)

    def test_url_input_suggests_find_accounts_with_hostname(self):
        with mock.patch.object(merchant_server, "DEFAULT_MERCHANT_ACCOUNT_ID", None):
            with self.assertRaises(ValueError) as ctx:
                merchant_server.normalize_account_id("https://www.webloom.fr/")
        msg = str(ctx.exception)
        self.assertIn("find_accounts", msg)
        self.assertIn("www.webloom.fr", msg)

    def test_brand_name_suggests_find_accounts(self):
        with mock.patch.object(merchant_server, "DEFAULT_MERCHANT_ACCOUNT_ID", None):
            with self.assertRaises(ValueError) as ctx:
                merchant_server.normalize_account_id("SuperSmart")
        msg = str(ctx.exception)
        self.assertIn("find_accounts", msg)
        self.assertIn("SuperSmart", msg)

    def test_empty_input_also_suggests_find_accounts(self):
        with mock.patch.object(merchant_server, "DEFAULT_MERCHANT_ACCOUNT_ID", None):
            with self.assertRaises(ValueError) as ctx:
                merchant_server.normalize_account_id(None)
        self.assertIn("find_accounts", str(ctx.exception))


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
        "find_accounts",
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

        def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
            captured["method"] = method
            captured["url"] = url
            captured["timeout"] = timeout

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


class TestFindAccounts(unittest.TestCase):
    """Unit tests for find_accounts: index building, scoring, caching."""

    def setUp(self):
        # Reset the per-user cache before each test
        merchant_server._accounts_index_cache.clear()

    def _sample_index(self):
        return [
            {
                "account_id": "111111111",
                "account_name": "Webloom Shop",
                "homepage_uri": "https://www.webloom.fr/",
                "homepage_host": "webloom.fr",
                "is_subaccount": False,
                "parent_id": None,
                "test_account": False,
                "adult_content": False,
                "language_code": "fr",
                "time_zone": "Europe/Paris",
            },
            {
                "account_id": "222222222",
                "account_name": "Acme Inc",
                "homepage_uri": "https://acme.example.com/",
                "homepage_host": "acme.example.com",
                "is_subaccount": False,
                "parent_id": None,
                "test_account": False,
                "adult_content": False,
                "language_code": "en",
                "time_zone": "America/New_York",
            },
            {
                "account_id": "333333333",
                "account_name": "Webloom EU (sub)",
                "homepage_uri": "https://eu.webloom.fr",
                "homepage_host": "eu.webloom.fr",
                "is_subaccount": True,
                "parent_id": "111111111",
                "test_account": False,
                "adult_content": False,
                "language_code": "fr",
                "time_zone": "Europe/Paris",
            },
        ]

    def test_score_exact_id_match_dominates(self):
        idx = self._sample_index()
        scores = {
            a["account_id"]: merchant_server._score_account("222222222", a)
            for a in idx
        }
        self.assertEqual(max(scores, key=scores.get), "222222222")

    def test_score_domain_query_finds_homepage_host(self):
        idx = self._sample_index()
        scores = {
            a["account_id"]: merchant_server._score_account("webloom.fr", a)
            for a in idx
        }
        # Both Webloom rows should outscore Acme
        self.assertGreater(scores["111111111"], scores["222222222"])
        self.assertGreater(scores["333333333"], scores["222222222"])

    def test_score_brand_name_query(self):
        idx = self._sample_index()
        scores = {
            a["account_id"]: merchant_server._score_account("webloom", a)
            for a in idx
        }
        self.assertGreater(scores["111111111"], scores["222222222"])

    def test_normalize_homepage_strips_scheme_and_www(self):
        self.assertEqual(merchant_server._normalize_homepage("https://www.webloom.fr/"), "webloom.fr")
        self.assertEqual(merchant_server._normalize_homepage("http://acme.example.com"), "acme.example.com")
        self.assertEqual(merchant_server._normalize_homepage(""), "")

    def test_account_id_extraction(self):
        self.assertEqual(merchant_server._account_id_from_resource("accounts/123456"), "123456")
        self.assertEqual(merchant_server._account_id_from_resource("accounts/123-456-789"), "123456789")
        self.assertEqual(merchant_server._account_id_from_resource(""), "")

    def test_find_accounts_uses_cache(self):
        builds = []

        def fake_build(*, include_subaccounts, include_homepages):
            builds.append((include_subaccounts, include_homepages))
            return self._sample_index()

        with mock.patch.object(merchant_server, "_build_accounts_index", side_effect=fake_build):
            result1 = asyncio.run(
                merchant_server.find_accounts.fn(
                    query="webloom",
                    top_k=5,
                    include_subaccounts=True,
                    include_homepage=True,
                    force_refresh=False,
                )
            )
            result2 = asyncio.run(
                merchant_server.find_accounts.fn(
                    query="acme",
                    top_k=5,
                    include_subaccounts=True,
                    include_homepage=True,
                    force_refresh=False,
                )
            )

        # Index should have been built only once thanks to the cache
        self.assertEqual(len(builds), 1)
        self.assertIn("111111111", result1)
        self.assertIn("222222222", result2)

    def test_find_accounts_force_refresh_rebuilds(self):
        builds = []

        def fake_build(*, include_subaccounts, include_homepages):
            builds.append(True)
            return self._sample_index()

        with mock.patch.object(merchant_server, "_build_accounts_index", side_effect=fake_build):
            asyncio.run(
                merchant_server.find_accounts.fn(
                    query="webloom",
                    top_k=5,
                    include_subaccounts=True,
                    include_homepage=True,
                    force_refresh=False,
                )
            )
            asyncio.run(
                merchant_server.find_accounts.fn(
                    query="webloom",
                    top_k=5,
                    include_subaccounts=True,
                    include_homepage=True,
                    force_refresh=True,
                )
            )

        self.assertEqual(len(builds), 2)

    def test_find_accounts_direct_id_hit_short_circuits_scoring(self):
        with mock.patch.object(
            merchant_server, "_build_accounts_index", return_value=self._sample_index()
        ):
            result = asyncio.run(
                merchant_server.find_accounts.fn(
                    query="222222222",
                    top_k=5,
                    include_subaccounts=True,
                    include_homepage=False,
                    force_refresh=False,
                )
            )

        parsed = json.loads(result)
        self.assertEqual(len(parsed["matches"]), 1)
        self.assertEqual(parsed["matches"][0]["account_id"], "222222222")


if __name__ == "__main__":
    unittest.main()
