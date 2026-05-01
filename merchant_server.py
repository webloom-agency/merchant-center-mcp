"""
Google Merchant Center MCP Server.

Exposes Google Merchant API (https://merchantapi.googleapis.com) tools over MCP
with the same OAuth 2.1 / FastMCP / Render scaffolding used by mcp-google-ads.

Multi-user OAuth 2.1: each MCP client performs DCR, the user consents on Google,
and the server stores Google credentials per-user on disk so calls are fully
isolated between tenants.
"""

from typing import Any, Dict, List, Optional
from pydantic import Field
import os
import json
import re
import time
import logging
from datetime import datetime, timedelta

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

# ----------------------------------------------------------------------------
# Patch MCP transport security BEFORE importing FastMCP.
# This must happen before any MCP modules are loaded so that hosted deployments
# behind reverse proxies (Render, Fly, etc.) don't reject Host headers.
# ----------------------------------------------------------------------------
import sys
import types

if 'mcp.server.transport_security' not in sys.modules:
    mock_module = types.ModuleType('mcp.server.transport_security')

    class _PermissiveTransportSecurity:
        def __init__(self, allowed_hosts=None):
            self.allowed_hosts = None

        async def validate_request(self, request, is_post=False):
            host = request.headers.get('host', 'unknown')
            print(f"\U0001f513 Transport security: allowing Host {host} (Bearer token validates)")
            return None

    from pydantic import BaseModel as _BaseModel, Field as _Field
    from typing import Optional as _Optional, List as _List

    class _PermissiveSecuritySettings(_BaseModel):
        allowed_hosts: _Optional[_List[str]] = _Field(default=None)

        class Config:
            extra = "allow"

    mock_module.TransportSecurityMiddleware = _PermissiveTransportSecurity
    mock_module.TransportSecuritySettings = _PermissiveSecuritySettings
    mock_module.logger = None
    mock_module.logging = logging
    sys.modules['mcp.server.transport_security'] = mock_module
    print("\u2713 Pre-patched transport_security module before MCP import")

from fastmcp import FastMCP

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger('merchant_server')

logger.info("=" * 80)
logger.info("\U0001f680 Google Merchant MCP Server starting up...")
logger.info("=" * 80)

# Best-effort: also patch transport security at the module level after MCP import.
try:
    from mcp.server import transport_security
    from mcp.server import streamable_http_manager

    if hasattr(transport_security, 'TransportSecurityMiddleware'):
        OriginalMiddleware = transport_security.TransportSecurityMiddleware

        class PatchedMiddleware(OriginalMiddleware):
            async def dispatch(self, request, call_next):
                logger.info(
                    f"\U0001f513 Bypassing Host validation for: {request.headers.get('host', 'unknown')}"
                )
                return await call_next(request)

        transport_security.TransportSecurityMiddleware = PatchedMiddleware

    if hasattr(streamable_http_manager, 'TransportSecurityMiddleware'):
        streamable_http_manager.TransportSecurityMiddleware = transport_security.TransportSecurityMiddleware

    if hasattr(transport_security, 'TransportSecuritySettings'):
        OriginalSettings = transport_security.TransportSecuritySettings

        class PatchedSettings(OriginalSettings):
            def __init__(self, *args, **kwargs):
                kwargs['allowed_hosts'] = None
                super().__init__(*args, **kwargs)

        transport_security.TransportSecuritySettings = PatchedSettings

    if hasattr(streamable_http_manager, 'TransportSecuritySettings'):
        streamable_http_manager.TransportSecuritySettings = transport_security.TransportSecuritySettings

    logger.info("\u2705 Transport security patch block completed")
except Exception as e:
    logger.warning(f"Could not patch transport_security at module level: {e}")

# ----------------------------------------------------------------------------
# Per-user OAuth 2.1 (opt-in via MCP_ENABLE_OAUTH21)
# ----------------------------------------------------------------------------
_OAUTH21_ENABLED = os.getenv("MCP_ENABLE_OAUTH21", "").lower() in ("1", "true", "yes")
_auth_provider_instance = None


def _init_oauth21():
    """Initialize OAuth 2.1 auth provider if enabled and configured."""
    global _auth_provider_instance
    if not _OAUTH21_ENABLED:
        logger.info(
            "OAuth 2.1 disabled (set MCP_ENABLE_OAUTH21=true to enable per-user auth)"
        )
        return None
    try:
        from auth.oauth_config import get_oauth_config
        from auth.google_oauth_provider import GoogleOAuthProvider

        config = get_oauth_config()
        if not config.is_configured():
            logger.warning(
                "OAuth 2.1 enabled but GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET not set"
            )
            return None

        base_url = config.get_oauth_base_url()
        _auth_provider_instance = GoogleOAuthProvider(base_url=base_url)
        logger.info("OAuth 2.1 per-user authentication initialized (OAuthProvider)")
        return _auth_provider_instance
    except Exception as e:
        logger.error(f"Failed to initialize OAuth 2.1: {e}", exc_info=True)
        return None


_oauth21_provider = _init_oauth21()

mcp = FastMCP(
    "google-merchant-server",
    dependencies=[
        "google-auth-oauthlib",
        "google-auth",
        "requests",
        "python-dotenv",
    ],
    auth=_oauth21_provider,
)

if _OAUTH21_ENABLED and _oauth21_provider:
    try:
        from auth.auth_info_middleware import AuthInfoMiddleware
        mcp.add_middleware(AuthInfoMiddleware())
        logger.info("AuthInfoMiddleware registered for per-user auth")
    except Exception as e:
        logger.warning(f"Could not register AuthInfoMiddleware: {e}")

# ----------------------------------------------------------------------------
# Environment / configuration
# ----------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
    logger.info("Environment variables loaded from .env file")
except ImportError:
    logger.warning("python-dotenv not installed; skipping .env file loading")

API_HOST = "https://merchantapi.googleapis.com"

# All Merchant sub-APIs ship a v1beta surface today; some have promoted v1.
# Override per-call via the *_API_VERSION env vars below if you need v1.
ACCOUNTS_API_VERSION = os.getenv("MERCHANT_ACCOUNTS_API_VERSION", "v1beta")
PRODUCTS_API_VERSION = os.getenv("MERCHANT_PRODUCTS_API_VERSION", "v1beta")
DATASOURCES_API_VERSION = os.getenv("MERCHANT_DATASOURCES_API_VERSION", "v1beta")
ISSUERESOLUTION_API_VERSION = os.getenv("MERCHANT_ISSUERESOLUTION_API_VERSION", "v1beta")
REPORTS_API_VERSION = os.getenv("MERCHANT_REPORTS_API_VERSION", "v1beta")
PROMOTIONS_API_VERSION = os.getenv("MERCHANT_PROMOTIONS_API_VERSION", "v1beta")
QUOTA_API_VERSION = os.getenv("MERCHANT_QUOTA_API_VERSION", "v1beta")
INVENTORIES_API_VERSION = os.getenv("MERCHANT_INVENTORIES_API_VERSION", "v1beta")
NOTIFICATIONS_API_VERSION = os.getenv("MERCHANT_NOTIFICATIONS_API_VERSION", "v1beta")
CONVERSIONS_API_VERSION = os.getenv("MERCHANT_CONVERSIONS_API_VERSION", "v1beta")

DEFAULT_MERCHANT_ACCOUNT_ID = os.environ.get("DEFAULT_MERCHANT_ACCOUNT_ID")
GOOGLE_MERCHANT_READ_ONLY = (
    os.environ.get("GOOGLE_MERCHANT_READ_ONLY", "1").lower() not in ("0", "false", "no")
)
GOOGLE_MERCHANT_AUTH_TYPE = os.environ.get("GOOGLE_MERCHANT_AUTH_TYPE", "oauth")
GOOGLE_MERCHANT_CREDENTIALS_PATH = os.environ.get("GOOGLE_MERCHANT_CREDENTIALS_PATH")

# ----------------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------------
from auth.scopes import SCOPES


def _get_user_email() -> Optional[str]:
    """
    Extract the authenticated user's email from FastMCP context.
    Returns None when OAuth 2.1 is not active or no user is authenticated.
    """
    if not _OAUTH21_ENABLED:
        return None
    try:
        from fastmcp.server.dependencies import get_context
        ctx = get_context()
        if ctx:
            email = ctx.get_state("authenticated_user_email")
            if email:
                return email
    except Exception:
        pass
    return None


def _get_credentials_for_user(user_email: str) -> Optional[Credentials]:
    """Per-user Google credentials from the on-disk credential store."""
    try:
        from auth.credential_store import get_credential_store
        store = get_credential_store()
        creds = store.get_credential(user_email)
        if creds:
            if (
                not creds.valid
                and getattr(creds, "expired", False)
                and getattr(creds, "refresh_token", None)
            ):
                try:
                    creds.refresh(Request())
                    store.store_credential(user_email, creds)
                except Exception as e:
                    logger.warning(
                        f"Failed to refresh stored credentials for {user_email}: {e}"
                    )
            if creds.valid or creds.token:
                return creds
    except Exception as e:
        logger.debug(f"Credential store lookup failed for {user_email}: {e}")
    return None


def _get_legacy_oauth_credentials() -> Credentials:
    """
    Legacy (single-user) OAuth path for stdio dev usage.

    Resolution order:
      1. GOOGLE_OAUTH_CLIENT_ID/SECRET + GOOGLE_MERCHANT_REFRESH_TOKEN env vars.
      2. Token file at GOOGLE_MERCHANT_CREDENTIALS_PATH.
      3. InstalledAppFlow (interactive, requires a local browser).
    """
    env_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    env_client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    env_refresh_token = os.environ.get("GOOGLE_MERCHANT_REFRESH_TOKEN")

    if env_client_id and env_client_secret and env_refresh_token:
        logger.info("Building OAuth credentials from environment (refresh token).")
        creds = Credentials(
            token=None,
            refresh_token=env_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=env_client_id,
            client_secret=env_client_secret,
            scopes=SCOPES,
        )
        creds.refresh(Request())
        return creds

    token_path = GOOGLE_MERCHANT_CREDENTIALS_PATH
    if token_path and os.path.exists(token_path):
        if os.path.isdir(token_path):
            token_path = os.path.join(token_path, "google_merchant_token.json")
        try:
            logger.info(f"Loading OAuth credentials from file: {token_path}")
            with open(token_path, "r") as f:
                creds_data = json.load(f)
                if "refresh_token" in creds_data or "access_token" in creds_data:
                    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
                    if not creds.valid:
                        creds.refresh(Request())
                    return creds
                else:
                    client_config = creds_data
        except Exception as e:
            logger.warning(f"Could not load token from file: {e}")
            client_config = None
    else:
        client_config = None

    logger.info("Falling back to interactive OAuth flow (local dev only).")
    if not client_config:
        if not env_client_id or not env_client_secret:
            raise ValueError(
                "Legacy OAuth requires GOOGLE_OAUTH_CLIENT_ID/SECRET + "
                "GOOGLE_MERCHANT_REFRESH_TOKEN, or GOOGLE_MERCHANT_CREDENTIALS_PATH "
                "to a token file, or local InstalledAppFlow with CLIENT_ID/SECRET."
            )
        client_config = {
            "installed": {
                "client_id": env_client_id,
                "client_secret": env_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [
                    "urn:ietf:wg:oauth:2.0:oob",
                    "http://localhost",
                ],
            }
        }

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    try:
        out_path = GOOGLE_MERCHANT_CREDENTIALS_PATH
        if out_path:
            if os.path.isdir(out_path):
                out_path = os.path.join(out_path, "google_merchant_token.json")
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w") as f:
                f.write(creds.to_json())
            logger.info(f"Saved OAuth token to {out_path}")
    except Exception as e:
        logger.warning(f"Could not save OAuth token to file: {e}")

    return creds


def get_credentials(user_email: Optional[str] = None) -> Credentials:
    """
    Resolve Google credentials for the current request.

    - When OAuth 2.1 is active and a user email is in context, returns the
      per-user credentials from the on-disk store (refreshing if expired).
    - Otherwise falls back to the legacy single-user path.
    """
    if user_email and _OAUTH21_ENABLED:
        creds = _get_credentials_for_user(user_email)
        if creds:
            return creds
        raise ValueError(
            f"No credentials found for user {user_email}. "
            "Please authenticate via the OAuth 2.1 flow first."
        )

    if GOOGLE_MERCHANT_AUTH_TYPE.lower() == "oauth":
        return _get_legacy_oauth_credentials()

    raise ValueError(
        "Unsupported GOOGLE_MERCHANT_AUTH_TYPE; only 'oauth' is supported. "
        "Service-account auth is not exposed by the Merchant API."
    )


def get_headers(creds: Credentials) -> Dict[str, str]:
    """Build request headers (Bearer auth — no developer-token / login-customer-id)."""
    if not creds.valid:
        if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise ValueError(f"Failed to refresh OAuth token: {e}")
        elif not creds.token:
            raise ValueError("OAuth credentials are invalid and cannot be refreshed.")
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }


# ----------------------------------------------------------------------------
# Account helpers
# ----------------------------------------------------------------------------
def normalize_account_id(value: Optional[str]) -> str:
    """
    Normalize a Merchant Center account ID. Accepts numeric strings or full
    resource names like 'accounts/123456789' and returns digits only.
    """
    if value is None or str(value).strip() == "":
        if DEFAULT_MERCHANT_ACCOUNT_ID:
            value = DEFAULT_MERCHANT_ACCOUNT_ID
        else:
            raise ValueError("account_id is required (Merchant Center ID).")
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        raise ValueError(f"Invalid account_id: {value!r}")
    return digits


def _is_readonly_method(method: str, path: str) -> bool:
    """Decide whether an HTTP request counts as read-only for the gate below."""
    method_u = (method or "").upper()
    if method_u == "GET":
        return True
    if path.endswith(":search"):
        return True
    if path.endswith(":renderaccountissues") or path.endswith(":renderproductissues"):
        return True
    if path.endswith(":listSubaccounts"):
        return True
    if path.endswith(":aggregateProductStatuses"):
        return True
    return False


def _request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Centralized HTTP wrapper with read-only gating and error surfacing."""
    if GOOGLE_MERCHANT_READ_ONLY and not _is_readonly_method(method, path):
        raise PermissionError(
            "Write operations are disabled (GOOGLE_MERCHANT_READ_ONLY=1)."
        )
    url = f"{API_HOST}{path}"
    creds = get_credentials(_get_user_email())
    headers = get_headers(creds)
    resp = requests.request(method, url, headers=headers, params=params, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"Merchant API {resp.status_code} {method} {path}: {resp.text}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def _paginate(
    method: str,
    path: str,
    *,
    items_key: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    max_items: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Iterate through nextPageToken pages and accumulate items."""
    items: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    while True:
        if method.upper() == "POST":
            payload = dict(body or {})
            if next_token:
                payload["pageToken"] = next_token
            data = _request(method, path, body=payload)
        else:
            qp = dict(params or {})
            if next_token:
                qp["pageToken"] = next_token
            data = _request(method, path, params=qp)
        items.extend(data.get(items_key, []))
        next_token = data.get("nextPageToken")
        if not next_token or (max_items is not None and len(items) >= max_items):
            break
    if max_items is not None:
        return items[:max_items]
    return items


def _dump(value: Any) -> str:
    """JSON-dump helper that tolerates non-serializable bits."""
    try:
        return json.dumps(value, indent=2, default=str)
    except Exception:
        return str(value)


# ============================================================================
# TOOLS — Account management
# ============================================================================

@mcp.tool()
async def list_accounts() -> str:
    """List Merchant Center accounts the authenticated user can access."""
    data = _request("GET", f"/accounts/{ACCOUNTS_API_VERSION}/accounts")
    return _dump(data.get("accounts", []) or data)


@mcp.tool()
async def list_subaccounts(
    account_id: str = Field(
        description="Advanced (provider) Merchant Center account ID"
    ),
) -> str:
    """List sub-accounts of an advanced (provider) Merchant Center account."""
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}:listSubaccounts",
    )
    return _dump(data)


@mcp.tool()
async def get_account(
    account_id: str = Field(description="Merchant Center account ID"),
) -> str:
    """Get a single Merchant Center account."""
    aid = normalize_account_id(account_id)
    data = _request("GET", f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}")
    return _dump(data)


@mcp.tool()
async def list_users(
    account_id: str = Field(description="Merchant Center account ID"),
) -> str:
    """List users with access to a Merchant Center account."""
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}/users",
    )
    return _dump(data)


@mcp.tool()
async def list_programs(
    account_id: str = Field(description="Merchant Center account ID"),
) -> str:
    """List the Merchant Center programs (Free Listings, Shopping Ads, etc.) on an account."""
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}/programs",
    )
    return _dump(data)


@mcp.tool()
async def list_regions(
    account_id: str = Field(description="Merchant Center account ID"),
) -> str:
    """List shipping regions configured for a Merchant Center account."""
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}/regions",
    )
    return _dump(data)


@mcp.tool()
async def get_shipping_settings(
    account_id: str = Field(description="Merchant Center account ID"),
) -> str:
    """Get the shipping settings (services + rate groups) for an account."""
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}/shippingSettings",
    )
    return _dump(data)


@mcp.tool()
async def get_business_info(
    account_id: str = Field(description="Merchant Center account ID"),
) -> str:
    """Get the business info (address, phone, customer service) for an account."""
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}/businessInfo",
    )
    return _dump(data)


# ============================================================================
# TOOLS — Products & data sources
# ============================================================================

@mcp.tool()
async def list_products(
    account_id: str = Field(description="Merchant Center account ID"),
    page_size: int = Field(default=50, description="Max products to return per page (1-1000)"),
    page_token: Optional[str] = Field(default=None, description="Pagination token"),
) -> str:
    """List products in a Merchant Center account (one page)."""
    aid = normalize_account_id(account_id)
    params: Dict[str, Any] = {"pageSize": max(1, min(int(page_size), 1000))}
    if page_token:
        params["pageToken"] = page_token
    data = _request(
        "GET",
        f"/products/{PRODUCTS_API_VERSION}/accounts/{aid}/products",
        params=params,
    )
    return _dump(data)


@mcp.tool()
async def get_product(
    account_id: str = Field(description="Merchant Center account ID"),
    product_name: str = Field(
        description=(
            "Product name. Either a full resource name "
            "('accounts/{aid}/products/{name}') or just the product name segment."
        )
    ),
) -> str:
    """Fetch a single product by its Merchant API resource name."""
    aid = normalize_account_id(account_id)
    name = product_name
    if name.startswith("accounts/"):
        path = f"/products/{PRODUCTS_API_VERSION}/{name}"
    else:
        path = f"/products/{PRODUCTS_API_VERSION}/accounts/{aid}/products/{name}"
    data = _request("GET", path)
    return _dump(data)


@mcp.tool()
async def list_data_sources(
    account_id: str = Field(description="Merchant Center account ID"),
) -> str:
    """List the product data sources (feeds) configured on a Merchant Center account."""
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        f"/datasources/{DATASOURCES_API_VERSION}/accounts/{aid}/dataSources",
    )
    return _dump(data)


@mcp.tool()
async def get_data_source(
    account_id: str = Field(description="Merchant Center account ID"),
    data_source_id: str = Field(description="Data source ID"),
) -> str:
    """Get a single data source by ID."""
    aid = normalize_account_id(account_id)
    dsid = re.sub(r"\D", "", str(data_source_id)) or data_source_id
    data = _request(
        "GET",
        f"/datasources/{DATASOURCES_API_VERSION}/accounts/{aid}/dataSources/{dsid}",
    )
    return _dump(data)


@mcp.tool()
async def list_file_uploads(
    account_id: str = Field(description="Merchant Center account ID"),
    data_source_id: str = Field(description="Data source ID"),
) -> str:
    """
    Get the latest file upload status for a data source.

    The Merchant API exposes the most recent upload as the resource named
    'fileUploads/latest' under the data source.
    """
    aid = normalize_account_id(account_id)
    dsid = re.sub(r"\D", "", str(data_source_id)) or data_source_id
    data = _request(
        "GET",
        (
            f"/datasources/{DATASOURCES_API_VERSION}/accounts/{aid}"
            f"/dataSources/{dsid}/fileUploads/latest"
        ),
    )
    return _dump(data)


# ============================================================================
# TOOLS — Issue resolution / status
# ============================================================================

@mcp.tool()
async def aggregate_product_statuses(
    account_id: str = Field(description="Merchant Center account ID"),
) -> str:
    """
    Aggregate counts of product statuses (approved / pending / disapproved)
    across reporting contexts and countries.
    """
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        (
            f"/issueresolution/{ISSUERESOLUTION_API_VERSION}/accounts/{aid}"
            "/aggregateProductStatuses"
        ),
    )
    return _dump(data)


@mcp.tool()
async def render_account_issues(
    account_id: str = Field(description="Merchant Center account ID"),
    language_code: str = Field(default="en-US", description="BCP-47 language code"),
    time_zone: str = Field(default="UTC", description="IANA time zone identifier"),
) -> str:
    """Render account-level issues (human-readable titles + descriptions)."""
    aid = normalize_account_id(account_id)
    body = {"languageCode": language_code, "timeZone": time_zone}
    data = _request(
        "POST",
        (
            f"/issueresolution/{ISSUERESOLUTION_API_VERSION}/accounts/{aid}"
            ":renderaccountissues"
        ),
        body=body,
    )
    return _dump(data)


@mcp.tool()
async def render_product_issues(
    account_id: str = Field(description="Merchant Center account ID"),
    product_name: str = Field(
        description="Product name (segment after '/products/') or a full resource name"
    ),
    language_code: str = Field(default="en-US", description="BCP-47 language code"),
    time_zone: str = Field(default="UTC", description="IANA time zone identifier"),
) -> str:
    """Render rich, localized issues for a single product."""
    aid = normalize_account_id(account_id)
    if product_name.startswith("accounts/"):
        path = f"/issueresolution/{ISSUERESOLUTION_API_VERSION}/{product_name}:renderproductissues"
    else:
        path = (
            f"/issueresolution/{ISSUERESOLUTION_API_VERSION}/accounts/{aid}"
            f"/products/{product_name}:renderproductissues"
        )
    body = {"languageCode": language_code, "timeZone": time_zone}
    data = _request("POST", path, body=body)
    return _dump(data)


# ============================================================================
# TOOLS — Reports (the analytics surface)
# ============================================================================

@mcp.tool()
async def run_merchant_query(
    account_id: str = Field(description="Merchant Center account ID"),
    query: str = Field(
        description=(
            "Merchant Reports query. SQL-flavored DSL: "
            "SELECT … FROM <view> WHERE … LIMIT …. Common views: "
            "productPerformanceView, nonProductPerformanceView, productView, "
            "priceCompetitivenessProductView, priceInsightsProductView, "
            "bestSellersProductClusterView, bestSellersBrandView, "
            "competitiveVisibilityCompetitorView."
        )
    ),
    page_size: int = Field(default=1000, description="Max rows per page (1-10000)"),
    page_token: Optional[str] = Field(default=None, description="Pagination token"),
) -> str:
    """Run an arbitrary Merchant Reports query (analogous to GAQL on the Ads side)."""
    aid = normalize_account_id(account_id)
    body: Dict[str, Any] = {
        "query": query,
        "pageSize": max(1, min(int(page_size), 10000)),
    }
    if page_token:
        body["pageToken"] = page_token
    data = _request(
        "POST",
        f"/reports/{REPORTS_API_VERSION}/accounts/{aid}/reports:search",
        body=body,
    )
    return _dump(data)


def _date_range_clause(days: int) -> str:
    """Build a 'segments.date BETWEEN ...' clause for the last N days (UTC)."""
    today = datetime.utcnow().date()
    start = today - timedelta(days=max(1, int(days)))
    end = today - timedelta(days=1)
    return f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"


@mcp.tool()
async def get_top_products(
    account_id: str = Field(description="Merchant Center account ID"),
    days: int = Field(default=30, description="Lookback window in days"),
    limit: int = Field(default=50, description="Max rows to return"),
) -> str:
    """Top products by clicks over the last N days (productPerformanceView)."""
    aid = normalize_account_id(account_id)
    where = _date_range_clause(days)
    query = (
        "SELECT segments.offer_id, "
        "metrics.clicks, metrics.impressions, metrics.click_through_rate, "
        "metrics.conversions, metrics.conversion_value_micros "
        "FROM productPerformanceView "
        f"WHERE {where} "
        f"ORDER BY metrics.clicks DESC "
        f"LIMIT {max(1, min(int(limit), 1000))}"
    )
    data = _request(
        "POST",
        f"/reports/{REPORTS_API_VERSION}/accounts/{aid}/reports:search",
        body={"query": query, "pageSize": max(1, min(int(limit), 10000))},
    )
    return _dump(data)


@mcp.tool()
async def get_price_competitiveness(
    account_id: str = Field(description="Merchant Center account ID"),
    country: Optional[str] = Field(
        default=None, description="2-letter country code filter (e.g. 'US')"
    ),
    limit: int = Field(default=100, description="Max rows to return"),
) -> str:
    """Price competitiveness benchmarks for products (priceCompetitivenessProductView)."""
    aid = normalize_account_id(account_id)
    where = ""
    if country:
        where = f"WHERE price_competitiveness_product_view.report_country_code = '{country.upper()}'"
    query = (
        "SELECT price_competitiveness_product_view.id, "
        "price_competitiveness_product_view.title, "
        "price_competitiveness_product_view.brand, "
        "price_competitiveness_product_view.price, "
        "price_competitiveness_product_view.benchmark_price, "
        "price_competitiveness_product_view.report_country_code "
        "FROM priceCompetitivenessProductView "
        f"{where} "
        f"LIMIT {max(1, min(int(limit), 10000))}"
    )
    data = _request(
        "POST",
        f"/reports/{REPORTS_API_VERSION}/accounts/{aid}/reports:search",
        body={"query": query, "pageSize": max(1, min(int(limit), 10000))},
    )
    return _dump(data)


@mcp.tool()
async def get_best_sellers(
    account_id: str = Field(description="Merchant Center account ID"),
    country: str = Field(description="2-letter country code (e.g. 'US')"),
    category_id: Optional[str] = Field(
        default=None, description="Google product category ID to filter by"
    ),
    limit: int = Field(default=50, description="Max rows to return"),
) -> str:
    """Best-selling product clusters in a country (bestSellersProductClusterView)."""
    aid = normalize_account_id(account_id)
    clauses = [
        f"best_sellers_product_cluster_view.report_country_code = '{country.upper()}'",
    ]
    if category_id:
        clauses.append(
            f"best_sellers_product_cluster_view.report_category_id = {int(category_id)}"
        )
    where = " AND ".join(clauses)
    query = (
        "SELECT best_sellers_product_cluster_view.title, "
        "best_sellers_product_cluster_view.brand, "
        "best_sellers_product_cluster_view.rank, "
        "best_sellers_product_cluster_view.previous_rank, "
        "best_sellers_product_cluster_view.relative_demand, "
        "best_sellers_product_cluster_view.report_country_code, "
        "best_sellers_product_cluster_view.report_category_id "
        "FROM bestSellersProductClusterView "
        f"WHERE {where} "
        "ORDER BY best_sellers_product_cluster_view.rank "
        f"LIMIT {max(1, min(int(limit), 10000))}"
    )
    data = _request(
        "POST",
        f"/reports/{REPORTS_API_VERSION}/accounts/{aid}/reports:search",
        body={"query": query, "pageSize": max(1, min(int(limit), 10000))},
    )
    return _dump(data)


# ============================================================================
# TOOLS — Promotions & quota
# ============================================================================

@mcp.tool()
async def list_promotions(
    account_id: str = Field(description="Merchant Center account ID"),
    page_size: int = Field(default=100, description="Max promotions per page"),
    page_token: Optional[str] = Field(default=None, description="Pagination token"),
) -> str:
    """List promotions on a Merchant Center account."""
    aid = normalize_account_id(account_id)
    params: Dict[str, Any] = {"pageSize": max(1, min(int(page_size), 1000))}
    if page_token:
        params["pageToken"] = page_token
    data = _request(
        "GET",
        f"/promotions/{PROMOTIONS_API_VERSION}/accounts/{aid}/promotions",
        params=params,
    )
    return _dump(data)


@mcp.tool()
async def get_promotion(
    account_id: str = Field(description="Merchant Center account ID"),
    promotion_name: str = Field(
        description="Promotion ID or full resource name 'accounts/{aid}/promotions/{id}'"
    ),
) -> str:
    """Get a single promotion by ID or resource name."""
    aid = normalize_account_id(account_id)
    if promotion_name.startswith("accounts/"):
        path = f"/promotions/{PROMOTIONS_API_VERSION}/{promotion_name}"
    else:
        path = f"/promotions/{PROMOTIONS_API_VERSION}/accounts/{aid}/promotions/{promotion_name}"
    data = _request("GET", path)
    return _dump(data)


@mcp.tool()
async def list_quotas(
    account_id: str = Field(description="Merchant Center account ID"),
) -> str:
    """List API quota usage and limits per method group for an account."""
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        f"/quota/{QUOTA_API_VERSION}/accounts/{aid}/quotas",
    )
    return _dump(data)


# ============================================================================
# RESOURCES & PROMPTS
# ============================================================================

@mcp.resource("merchant-reports://reference")
def merchant_reports_reference() -> str:
    """Quick reference for the Merchant Reports query DSL."""
    return """
    # Merchant Reports Query Language (short reference)
    Shape:
      SELECT field1, field2 FROM <view>
      WHERE <conditions>
      ORDER BY <field> [DESC]
      LIMIT <n>

    Common views:
      productPerformanceView         - clicks/impressions/conversions per product (segments.offer_id, segments.date, segments.program, segments.country_code)
      nonProductPerformanceView      - performance unattributed to a single product
      productView                    - product catalog snapshot (title, brand, price, item issues)
      priceCompetitivenessProductView- price benchmarks vs. competitors (report_country_code)
      priceInsightsProductView       - suggested price + projected uplift per product
      bestSellersProductClusterView  - best-selling product clusters per country/category (rank, previous_rank, relative_demand)
      bestSellersBrandView           - best-selling brands per country/category
      competitiveVisibilityCompetitorView - relative impression share vs. top competitors

    Date filters (segments.date is an ISO-8601 day):
      WHERE segments.date BETWEEN '2025-01-01' AND '2025-01-31'

    Notes:
      - Each view exposes its own set of selectable fields; mixing fields across
        views is not allowed.
      - Pagination is via `pageToken` + `nextPageToken`; max page size is 10,000.
      - For free-form analytics use `run_merchant_query`; for canned reports use
        `get_top_products`, `get_price_competitiveness`, `get_best_sellers`.
    """


@mcp.prompt("google_merchant_workflow")
def google_merchant_workflow() -> str:
    return """
    Suggested workflow:
      1) list_accounts()                       — pick the Merchant Center account
      2) list_subaccounts(account_id=...)      — for advanced (provider) accounts
      3) list_data_sources(account_id=...)     — see configured feeds
      4) aggregate_product_statuses(account_id=...) — check feed health
      5) render_account_issues(account_id=...) — get human-readable issues
      6) get_top_products(account_id=..., days=30) — analytics quick win
      7) run_merchant_query(account_id=..., query="SELECT ... FROM productPerformanceView ...")
    """


@mcp.prompt("merchant_reports_help")
def merchant_reports_help() -> str:
    return """
    Examples:

    # Top 10 products by clicks last 30 days
    SELECT segments.offer_id, metrics.clicks, metrics.impressions
    FROM productPerformanceView
    WHERE segments.date BETWEEN '2026-04-01' AND '2026-04-30'
    ORDER BY metrics.clicks DESC
    LIMIT 10

    # Pricing benchmarks for US
    SELECT price_competitiveness_product_view.id,
           price_competitiveness_product_view.price,
           price_competitiveness_product_view.benchmark_price
    FROM priceCompetitivenessProductView
    WHERE price_competitiveness_product_view.report_country_code = 'US'
    LIMIT 50
    """


# ============================================================================
# HTTP / ASGI app for Render
# ============================================================================

MCP_HTTP_PATH = os.getenv("MCP_HTTP_PATH", "/mcp")
try:
    mcp.settings.streamable_http_path = MCP_HTTP_PATH  # type: ignore[attr-defined]
except Exception:
    pass


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Single-secret transport guard used when OAuth 2.1 is disabled."""

    async def dispatch(self, request, call_next):
        if getattr(request, "method", "").upper() == "OPTIONS":
            return await call_next(request)

        token_expected = os.getenv("MCP_BEARER_TOKEN")
        if not token_expected:
            return await call_next(request)

        try:
            path = request.url.path
        except Exception:
            path = ""

        base_path = MCP_HTTP_PATH.rstrip("/")
        if path == base_path or path.startswith(base_path + "/"):
            auth_header = request.headers.get("authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                return PlainTextResponse("Unauthorized", status_code=401)
            token = auth_header.split(" ", 1)[1]
            if token != token_expected:
                return PlainTextResponse("Forbidden", status_code=403)

        return await call_next(request)


app = mcp.http_app()

try:
    if hasattr(app, 'state'):
        for attr_name in dir(app.state):
            attr = getattr(app.state, attr_name, None)
            if attr and hasattr(attr, 'validate_host'):
                attr.validate_host = lambda host: True
                logger.info(f"\u2713 Disabled Host validation on {attr_name}")
            if attr and hasattr(attr, '_transport_security'):
                ts = attr._transport_security
                if ts and hasattr(ts, 'validate_host'):
                    ts.validate_host = lambda host: True
                    ts.allowed_hosts = None
                    logger.info(
                        f"\u2713 Disabled Host validation on {attr_name}._transport_security"
                    )
except Exception as e:
    logger.warning(f"Could not patch app.state: {e}")

if not (_OAUTH21_ENABLED and _oauth21_provider):
    app.add_middleware(BearerAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("merchant_server:app", host="0.0.0.0", port=port, reload=False)
