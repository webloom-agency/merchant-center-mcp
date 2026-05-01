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
import difflib
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse

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

_SERVER_INSTRUCTIONS = """\
Google Merchant Center MCP server (Merchant API v1).

Tool-selection rules (follow these before any analytics or product tool):

1. Every Merchant Center tool except `list_accounts` and `find_accounts`
   requires a NUMERIC account_id (e.g. 123456789), NOT a domain, brand name
   or URL.

2. If the user gives you a brand name or website (e.g. "supersmart.com",
   "Webloom") instead of a numeric ID, call `find_accounts(query="...")`
   FIRST to resolve it to the matching numeric account_id, then call the
   real tool. Do NOT ask the human for the ID before trying `find_accounts`.

3. If the user gives no account context at all and `DEFAULT_MERCHANT_ACCOUNT_ID`
   is not configured, call `list_accounts` to enumerate what they can see,
   then ask which one to use.

4. All write operations may be disabled server-side via
   `GOOGLE_MERCHANT_READ_ONLY=1`; if a write fails for that reason, do not
   retry — surface the message to the user.

5. Reports query language is the v1 MCQL dialect: snake_case table names
   (`product_performance_view`, `price_competitiveness_product_view`, …) and
   BARE field names (no `segments.` / `metrics.` qualifiers — those are
   v1beta and now rejected). Date filter is `date BETWEEN 'YYYY-MM-DD'
   AND 'YYYY-MM-DD'` or `date DURING LAST_30_DAYS`. Read the
   `merchant-reports://reference` resource for the full schema before
   building custom queries with `run_merchant_query`.

6. If you get a 401 UNAUTHENTICATED with a message saying the GCP project
   "is not registered with the merchant account", the GCP project used for
   OAuth has never been registered with this Merchant Center via the
   one-time `registerGcp` Merchant API method. Recovery: call the
   `register_gcp_developer` tool on this server (requires
   `GOOGLE_MERCHANT_READ_ONLY=0` since it's a setup write call) with the
   target Merchant Center account_id and a real Google-Account email
   (NOT a service account) to act as the developer contact. Wait ~5
   minutes after registration before retrying. See:
   https://developers.google.com/merchant/api/guides/quickstart/registration
"""

mcp = FastMCP(
    "google-merchant-server",
    instructions=_SERVER_INSTRUCTIONS,
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

logger.info(
    "Targeting Merchant API v1. Reminder: each Google Cloud project used for "
    "OAuth must be registered ONCE per Merchant Center via `registerGcp` "
    "before v1 calls work for that project (https://developers.google.com/"
    "merchant/api/guides/quickstart/registration). If the very first v1 call "
    "returns 401 UNAUTHENTICATED with 'GCP project ... is not registered', "
    "use the `register_gcp_developer` tool on this server (requires "
    "GOOGLE_MERCHANT_READ_ONLY=0) to perform that one-time setup."
)

API_HOST = "https://merchantapi.googleapis.com"

# Google discontinued every v1beta sub-API of the Merchant API on
# 2026-02-28; everything is now on v1. Override per-call via the
# *_API_VERSION env vars below if you ever need to pin a different surface
# (e.g. v1alpha for experimental features).
ACCOUNTS_API_VERSION = os.getenv("MERCHANT_ACCOUNTS_API_VERSION", "v1")
PRODUCTS_API_VERSION = os.getenv("MERCHANT_PRODUCTS_API_VERSION", "v1")
DATASOURCES_API_VERSION = os.getenv("MERCHANT_DATASOURCES_API_VERSION", "v1")
ISSUERESOLUTION_API_VERSION = os.getenv("MERCHANT_ISSUERESOLUTION_API_VERSION", "v1")
REPORTS_API_VERSION = os.getenv("MERCHANT_REPORTS_API_VERSION", "v1")
PROMOTIONS_API_VERSION = os.getenv("MERCHANT_PROMOTIONS_API_VERSION", "v1")
QUOTA_API_VERSION = os.getenv("MERCHANT_QUOTA_API_VERSION", "v1")
INVENTORIES_API_VERSION = os.getenv("MERCHANT_INVENTORIES_API_VERSION", "v1")
NOTIFICATIONS_API_VERSION = os.getenv("MERCHANT_NOTIFICATIONS_API_VERSION", "v1")
CONVERSIONS_API_VERSION = os.getenv("MERCHANT_CONVERSIONS_API_VERSION", "v1")

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

    If the caller passed something that looks like a brand name, domain or URL
    (e.g. "supersmart.com"), raise an error whose message is a self-recovering
    instruction so the LLM client knows to call `find_accounts` first instead
    of asking the human for a numeric ID.
    """
    if value is None or str(value).strip() == "":
        if DEFAULT_MERCHANT_ACCOUNT_ID:
            value = DEFAULT_MERCHANT_ACCOUNT_ID
        else:
            raise ValueError(
                "account_id is required (Merchant Center numeric ID). "
                "If you only have a brand name or website domain, call "
                "`find_accounts(query=\"...\")` first to resolve it, then retry "
                "with the numeric `account_id` from the result."
            )
    raw = str(value).strip()
    digits = re.sub(r"\D", "", raw)

    # Heuristic: looks like a domain / URL / brand name → guide the LLM to find_accounts.
    looks_like_domain_or_brand = (
        not digits
        or "." in raw
        or "/" in raw
        or any(c.isalpha() for c in raw)
    )
    if not digits or (looks_like_domain_or_brand and len(digits) < 6):
        # Strip a leading scheme so the suggested query is clean.
        suggested_query = raw
        if "://" in suggested_query:
            try:
                suggested_query = urlparse(suggested_query).hostname or raw
            except Exception:
                pass
        raise ValueError(
            f"Invalid Merchant Center account_id: {value!r}. "
            f"This looks like a domain or brand name, not a numeric Merchant "
            f"Center ID. RECOVERY: call "
            f"`find_accounts(query=\"{suggested_query}\")` to look up the "
            f"matching numeric account ID, then retry this tool with the "
            f"`account_id` from the response. Do NOT ask the human for the ID "
            f"unless `find_accounts` returns no matches."
        )
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


_HTTP_TIMEOUT_SECONDS = float(os.getenv("MERCHANT_HTTP_TIMEOUT_SECONDS", "30"))


def _request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Centralized HTTP wrapper with read-only gating, timeouts, and error surfacing."""
    if GOOGLE_MERCHANT_READ_ONLY and not _is_readonly_method(method, path):
        raise PermissionError(
            "Write operations are disabled (GOOGLE_MERCHANT_READ_ONLY=1)."
        )
    url = f"{API_HOST}{path}"
    creds = get_credentials(_get_user_email())
    headers = get_headers(creds)
    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=body,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except requests.Timeout as e:
        raise RuntimeError(
            f"Merchant API timeout after {_HTTP_TIMEOUT_SECONDS}s on {method} {path}"
        ) from e
    except requests.ConnectionError as e:
        raise RuntimeError(f"Merchant API connection error on {method} {path}: {e}") from e
    if resp.status_code >= 400:
        # Surface only the API status + a truncated body to avoid leaking very large payloads
        body_text = (resp.text or "")[:1000]
        raise RuntimeError(
            f"Merchant API {resp.status_code} {method} {path}: {body_text}"
        )
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
) -> str:
    """Get a single Merchant Center account."""
    aid = normalize_account_id(account_id)
    data = _request("GET", f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}")
    return _dump(data)


@mcp.tool()
async def list_users(
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
) -> str:
    """Get the business info (address, phone, customer service) for an account."""
    aid = normalize_account_id(account_id)
    data = _request(
        "GET",
        f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}/businessInfo",
    )
    return _dump(data)


@mcp.tool()
async def register_gcp_developer(
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID to register the calling GCP "
            "project against. Use your PRIMARY (advanced/MCA) account ID — "
            "Google says do NOT register against subaccounts. The signed-in "
            "user must have ADMIN access on this account. If you only have "
            "a brand name or website domain, call `find_accounts(query=\"...\")` "
            "first to resolve the numeric ID."
        )
    ),
    developer_email: Optional[str] = Field(
        default=None,
        description=(
            "Real Google-Account email address that will become the developer "
            "technical contact for Merchant API service announcements (NOT a "
            "service account — service accounts can't receive emails). If "
            "omitted, the call still links the GCP project to the Merchant "
            "Center account but no contact is added."
        ),
    ),
) -> str:
    """One-time registration of the calling Google Cloud project with a Merchant
    Center account, required since Merchant API v1 GA.

    When to call: only if a v1 request returned `401 UNAUTHENTICATED` with a
    message saying "GCP project with id ... is not registered with the merchant
    account". Otherwise this is a no-op (and re-running may return
    ALREADY_REGISTERED — that's fine, it confirms you're already set up).

    Important:
      - Run ONCE per Google Cloud project, against your PRIMARY Merchant Center
        account only. The registration covers all linked subaccounts.
      - The Merchant API must be enabled on the GCP project (APIs & Services →
        Library → "Merchant API" → Enable). One-time, in the Cloud Console.
      - This is a write call. Set `GOOGLE_MERCHANT_READ_ONLY=0` server-side
        before invoking, then flip it back afterwards.
      - Each Google Cloud project can be registered to only ONE Merchant Center
        at a time. Trying to register a second time returns ALREADY_REGISTERED.
      - Wait ~5 minutes after a successful registration before retrying real
        API calls — propagation isn't instant.

    Returns the resulting `DeveloperRegistration` resource (or the API error
    surfaced unchanged if registration failed).
    """
    aid = normalize_account_id(account_id)
    body: Dict[str, Any] = {}
    if developer_email and str(developer_email).strip():
        body["developerEmail"] = str(developer_email).strip()
    data = _request(
        "POST",
        f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{aid}"
        f"/developerRegistration:registerGcp",
        body=body,
    )
    return _dump(data)


# ----------------------------------------------------------------------------
# Account index (per-user cached) — used by find_accounts
# ----------------------------------------------------------------------------
ACCOUNTS_CACHE_TTL_SECONDS = int(os.getenv("MERCHANT_ACCOUNTS_CACHE_TTL_SECONDS", "900"))
_accounts_index_cache: Dict[str, Dict[str, Any]] = {}


def _accounts_cache_key() -> str:
    """Per-user cache key, isolates results in multi-tenant mode."""
    return _get_user_email() or "__default__"


def _account_id_from_resource(name_or_id: str) -> str:
    """Extract digits from 'accounts/123456' / 'accounts/123/products/...' / '123-456'."""
    if not name_or_id:
        return ""
    last = str(name_or_id).split("/")[-1]
    return re.sub(r"\D", "", last)


def _normalize_homepage(url: str) -> str:
    """Strip scheme + 'www.' for fuzzy matching against domain queries."""
    if not url:
        return ""
    try:
        host = urlparse(url).netloc or url
    except Exception:
        host = url
    host = host.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _list_all_accounts_pages() -> List[Dict[str, Any]]:
    """Iterate through accounts.list pagination."""
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"pageSize": 250}
        if page_token:
            params["pageToken"] = page_token
        data = _request(
            "GET",
            f"/accounts/{ACCOUNTS_API_VERSION}/accounts",
            params=params,
        )
        items.extend(data.get("accounts", []) or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def _list_subaccounts_safe(parent_id: str) -> List[Dict[str, Any]]:
    """List subaccounts of an advanced account, swallowing 'not advanced' errors."""
    try:
        data = _request(
            "GET",
            f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{parent_id}:listSubaccounts",
        )
        return data.get("accounts", []) or []
    except Exception as e:
        logger.debug(f"listSubaccounts({parent_id}) failed (likely standalone): {e}")
        return []


def _fetch_homepage_safe(account_id: str) -> str:
    """Fetch the homepage URI for an account; '' if unset or inaccessible."""
    try:
        data = _request(
            "GET",
            f"/accounts/{ACCOUNTS_API_VERSION}/accounts/{account_id}/homepage",
        )
        return data.get("uri", "") or ""
    except Exception as e:
        logger.debug(f"homepage({account_id}) fetch failed: {e}")
        return ""


def _build_accounts_index(
    *, include_subaccounts: bool, include_homepages: bool
) -> List[Dict[str, Any]]:
    """
    Build a flat index of every Merchant Center account the user can access,
    optionally walking sub-accounts under advanced (provider) accounts and
    optionally fetching each account's homepage URL for domain-based matching.
    """
    raw = _list_all_accounts_pages()
    seen_ids: set = set()
    index: List[Dict[str, Any]] = []

    def _emit(acc: Dict[str, Any], *, parent_id: Optional[str] = None) -> None:
        aid = _account_id_from_resource(acc.get("name", ""))
        if not aid or aid in seen_ids:
            return
        seen_ids.add(aid)
        index.append({
            "account_id": aid,
            "account_name": acc.get("accountName") or "",
            "test_account": bool(acc.get("testAccount", False)),
            "adult_content": bool(acc.get("adultContent", False)),
            "language_code": acc.get("languageCode") or "",
            "time_zone": (acc.get("timeZone") or {}).get("id", ""),
            "is_subaccount": parent_id is not None,
            "parent_id": parent_id,
            "homepage_uri": "",
            "homepage_host": "",
        })

    for acc in raw:
        _emit(acc)

    if include_subaccounts:
        # Snapshot the IDs *before* expanding so we don't iterate while extending.
        roots = [a["account_id"] for a in list(index)]
        for parent_id in roots:
            for sub in _list_subaccounts_safe(parent_id):
                _emit(sub, parent_id=parent_id)

    if include_homepages:
        for entry in index:
            uri = _fetch_homepage_safe(entry["account_id"])
            entry["homepage_uri"] = uri
            entry["homepage_host"] = _normalize_homepage(uri)

    return index


def _get_accounts_index(
    *,
    include_subaccounts: bool,
    include_homepages: bool,
    force_refresh: bool,
) -> List[Dict[str, Any]]:
    """Cached accessor for the accounts index (per-user, TTL-bound)."""
    key = _accounts_cache_key()
    cache = _accounts_index_cache.get(key)
    now = int(time.time())
    cache_valid = (
        cache
        and not force_refresh
        and (now - cache.get("at", 0)) < ACCOUNTS_CACHE_TTL_SECONDS
        and cache.get("include_subaccounts") == include_subaccounts
        and cache.get("include_homepages") == include_homepages
    )
    if cache_valid:
        return cache["items"]

    items = _build_accounts_index(
        include_subaccounts=include_subaccounts,
        include_homepages=include_homepages,
    )
    _accounts_index_cache[key] = {
        "at": now,
        "items": items,
        "include_subaccounts": include_subaccounts,
        "include_homepages": include_homepages,
    }
    return items


def _score_account(query: str, entry: Dict[str, Any]) -> float:
    """
    Relevance score: higher = better match. Combines exact-substring and
    fuzzy similarity over accountName, accountId and homepage host.
    """
    q = (query or "").strip().lower()
    if not q:
        return 0.0

    name = (entry.get("account_name") or "").lower()
    aid = entry.get("account_id") or ""
    host = entry.get("homepage_host") or ""
    full_uri = (entry.get("homepage_uri") or "").lower()

    # Exact ID hit dominates everything else
    if q == aid:
        return 100.0

    # Numeric query that matches an ID prefix/suffix is very strong
    if q.isdigit() and (aid.startswith(q) or aid.endswith(q)):
        return 50.0 + min(len(q), 10) / 10.0

    # Substring hits on host/name/uri
    substring_score = 0.0
    if host and q in host:
        substring_score = max(substring_score, 5.0 + (len(q) / max(len(host), 1)))
    if name and q in name:
        substring_score = max(substring_score, 4.0 + (len(q) / max(len(name), 1)))
    if full_uri and q in full_uri:
        substring_score = max(substring_score, 3.0 + (len(q) / max(len(full_uri), 1)))

    # Fuzzy similarity (handles typos / partial names)
    fuzzy = max(
        difflib.SequenceMatcher(None, q, name).ratio() if name else 0.0,
        difflib.SequenceMatcher(None, q, host).ratio() if host else 0.0,
    )

    return substring_score + fuzzy


@mcp.tool()
async def find_accounts(
    query: str = Field(
        description=(
            "Free-text search: account name fragment ('Webloom'), domain "
            "('webloom.fr'), full URL ('https://www.webloom.fr/'), or a partial "
            "Merchant Center account ID."
        )
    ),
    top_k: int = Field(default=5, description="Max number of results to return (1-25)"),
    include_subaccounts: bool = Field(
        default=True,
        description=(
            "Also walk sub-accounts of every advanced (provider) account you can "
            "see. Slower on big agencies but lets you find a sub-account by name."
        ),
    ),
    include_homepage: bool = Field(
        default=True,
        description=(
            "Also fetch each account's registered homepage URL and match the query "
            "against it (lets you search by domain). Adds one extra API call per "
            "account; cached for 15 minutes per user."
        ),
    ),
    force_refresh: bool = Field(
        default=False,
        description="Bypass the 15-minute per-user index cache and rebuild from scratch.",
    ),
) -> str:
    """
    Fuzzy-search the Merchant Center accounts the authenticated user has access to.

    Useful when you only know a client's brand name or website domain and want
    the corresponding Merchant Center account ID — e.g. "find me the account
    for webloom.fr". Matches against account name, account ID, and registered
    homepage host.
    """
    top_k = max(1, min(int(top_k), 25))

    try:
        items = _get_accounts_index(
            include_subaccounts=include_subaccounts,
            include_homepages=include_homepage,
            force_refresh=force_refresh,
        )
    except Exception as e:
        return f"Error building accounts index: {e}"

    if not items:
        return "No Merchant Center accounts accessible to this user."

    # Direct ID hit (digits-only query) → bypass scoring entirely
    digits_only = re.sub(r"\D", "", query or "")
    if digits_only and len(digits_only) >= 6:
        exact = [a for a in items if a["account_id"] == digits_only]
        if exact:
            return _dump({"query": query, "matches": exact[:top_k]})

    scored = [(_score_account(query, a), a) for a in items]
    scored = [(s, a) for s, a in scored if s > 0.3]
    scored.sort(key=lambda x: -x[0])

    matches = [a for _, a in scored[:top_k]]
    if not matches:
        return _dump({
            "query": query,
            "matches": [],
            "hint": (
                "No matches above threshold. Try a shorter query, set "
                "include_homepage=true, or call list_accounts to see everything."
            ),
            "total_indexed": len(items),
        })

    return _dump({
        "query": query,
        "total_indexed": len(items),
        "matches": [
            {
                **a,
                "score": round(s, 3),
            }
            for (s, a) in scored[:top_k]
        ],
    })


# ============================================================================
# TOOLS — Products & data sources
# ============================================================================

@mcp.tool()
async def list_products(
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
    query: str = Field(
        description=(
            "Merchant Reports query (Merchant API v1 MCQL). SQL-flavored DSL: "
            "SELECT field1, field2 FROM <view> WHERE <conditions> "
            "[ORDER BY field [DESC]] [LIMIT n]. "
            "Use snake_case views and BARE field names (no `segments.` / "
            "`metrics.` qualifiers — those were v1beta and are now rejected). "
            "Common views: product_performance_view, "
            "non_product_performance_view, product_view, "
            "price_competitiveness_product_view, price_insights_product_view, "
            "best_sellers_product_cluster_view, best_sellers_brand_view, "
            "competitive_visibility_competitor_view. "
            "Date filter is `date BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD'` or "
            "`date DURING LAST_30_DAYS`. See the `merchant-reports://reference` "
            "resource for the full schema."
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
    """Build a `date BETWEEN ...` clause for the last N days (UTC).

    Note: the v1 Merchant Reports query language drops the `segments.` /
    `metrics.` qualifier prefixes used by v1beta — fields are bare snake_case.
    """
    today = datetime.utcnow().date()
    start = today - timedelta(days=max(1, int(days)))
    end = today - timedelta(days=1)
    return f"date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"


@mcp.tool()
async def get_top_products(
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
    days: int = Field(default=30, description="Lookback window in days"),
    limit: int = Field(default=50, description="Max rows to return"),
) -> str:
    """Top products by clicks over the last N days (product_performance_view).

    Uses Merchant API v1 query syntax: snake_case table name, bare field names
    (no `segments.` / `metrics.` prefix), and `conversion_value` as a Price
    object instead of `conversion_value_micros`.
    """
    aid = normalize_account_id(account_id)
    where = _date_range_clause(days)
    query = (
        "SELECT offer_id, title, brand, "
        "clicks, impressions, click_through_rate, "
        "conversions, conversion_value "
        "FROM product_performance_view "
        f"WHERE {where} "
        f"ORDER BY clicks DESC "
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
    country: Optional[str] = Field(
        default=None, description="2-letter country code filter (e.g. 'US')"
    ),
    limit: int = Field(default=100, description="Max rows to return"),
) -> str:
    """Price competitiveness benchmarks for products (price_competitiveness_product_view).

    Uses Merchant API v1 query syntax: snake_case table name and bare
    field names (no `<view>.` qualifier prefix).
    """
    aid = normalize_account_id(account_id)
    where = ""
    if country:
        where = f"WHERE report_country_code = '{country.upper()}'"
    query = (
        "SELECT id, offer_id, title, brand, "
        "price, benchmark_price, report_country_code "
        "FROM price_competitiveness_product_view "
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
    country: str = Field(description="2-letter country code (e.g. 'US')"),
    category_id: Optional[str] = Field(
        default=None, description="Google product category ID to filter by"
    ),
    limit: int = Field(default=50, description="Max rows to return"),
) -> str:
    """Best-selling product clusters in a country (best_sellers_product_cluster_view).

    Uses Merchant API v1 query syntax: snake_case table name and bare
    field names (no `<view>.` qualifier prefix).
    """
    aid = normalize_account_id(account_id)
    clauses = [f"report_country_code = '{country.upper()}'"]
    if category_id:
        clauses.append(f"report_category_id = {int(category_id)}")
    where = " AND ".join(clauses)
    query = (
        "SELECT title, brand, rank, previous_rank, "
        "relative_demand, report_country_code, report_category_id "
        "FROM best_sellers_product_cluster_view "
        f"WHERE {where} "
        "ORDER BY rank "
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    account_id: str = Field(
        description=(
            "Numeric Merchant Center account ID (e.g. '123456789'). "
            "If you only have a brand name or website domain, call "
            "`find_accounts(query=\"...\")` first to resolve it."
        )
    ),
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
    """Quick reference for the Merchant Reports query DSL (v1 syntax)."""
    return """
    # Merchant Reports Query Language — MCQL (v1 syntax)

    IMPORTANT: This is the post-2026 v1 syntax. v1beta is sunset.
    Differences from v1beta:
      - Table names are snake_case   (`product_performance_view`, NOT `productPerformanceView`)
      - Fields are bare snake_case   (`offer_id`, NOT `segments.offer_id`)
      - Metrics are bare snake_case  (`clicks`, NOT `metrics.clicks`)
      - `conversion_value` is a Price object  (replaces `conversion_value_micros`)
      - Date filter is just `date`   (NOT `segments.date`)

    Shape:
      SELECT field1, field2 FROM <view>
      WHERE <conditions>
      ORDER BY <field> [DESC]
      LIMIT <n>

    Common views (use exactly these snake_case names in FROM):
      product_performance_view              - clicks/impressions/conversions per product
                                              (offer_id, date, marketing_method, customer_country_code, ...)
      non_product_performance_view          - performance unattributed to a single product
      product_view                          - product catalog snapshot (title, brand, price, item issues)
      price_competitiveness_product_view    - price benchmarks vs. competitors (report_country_code)
      price_insights_product_view           - suggested price + projected uplift per product
      best_sellers_product_cluster_view     - best-selling product clusters per country/category
                                              (rank, previous_rank, relative_demand)
      best_sellers_brand_view               - best-selling brands per country/category
      competitive_visibility_competitor_view - relative impression share vs. top competitors

    Date filters (date is an ISO-8601 day in the merchant timezone):
      WHERE date BETWEEN '2026-04-01' AND '2026-04-30'
      WHERE date DURING LAST_30_DAYS              -- relative
      WHERE date DURING THIS_MONTH                -- relative

    Examples:
      SELECT offer_id, clicks, impressions
      FROM product_performance_view
      WHERE date BETWEEN '2026-04-01' AND '2026-04-30'
      ORDER BY clicks DESC
      LIMIT 50

      SELECT id, title, price, benchmark_price
      FROM price_competitiveness_product_view
      WHERE report_country_code = 'FR'
      LIMIT 100

    Notes:
      - Each view exposes its own set of selectable fields; mixing fields across
        views is not allowed.
      - Selecting any segment requires also selecting at least one metric.
      - Pagination is via `pageToken` + `nextPageToken`; max page size is 100,000.
      - For free-form analytics use `run_merchant_query`; for canned reports use
        `get_top_products`, `get_price_competitiveness`, `get_best_sellers`.
    """


@mcp.prompt("google_merchant_workflow")
def google_merchant_workflow() -> str:
    return """
    Suggested workflow:

      0) RESOLVE THE ACCOUNT FIRST.
         If the user gave a brand name or website (e.g. "supersmart.com",
         "Webloom") instead of a numeric Merchant Center ID, call
         `find_accounts(query="<brand-or-domain>")` and use the numeric
         `account_id` from the response in every subsequent call.
         Only call `list_accounts()` if the user gave no context at all
         (no name, no domain, no ID).

      1) list_accounts()                       — enumerate accessible accounts
      2) list_subaccounts(account_id=...)      — for advanced (provider) accounts
      3) list_data_sources(account_id=...)     — see configured feeds
      4) aggregate_product_statuses(account_id=...) — check feed health
      5) render_account_issues(account_id=...) — human-readable issues
      6) get_top_products(account_id=..., days=30) — analytics quick win
      7) run_merchant_query(account_id=..., query="SELECT ... FROM productPerformanceView ...")

    Reminder: every tool except `list_accounts` and `find_accounts` requires a
    numeric account_id. Never ask the human for the ID before trying
    `find_accounts` first.
    """


@mcp.prompt("merchant_reports_help")
def merchant_reports_help() -> str:
    return """
    Merchant Reports Query Language — examples (v1 syntax)

    Reminder: post-2026 v1 syntax uses snake_case table names, bare field
    names with NO `segments.` / `metrics.` prefixes, and `conversion_value`
    in place of `conversion_value_micros`. v1beta syntax will be rejected.

    # Top 10 products by clicks, last 30 days
    SELECT offer_id, title, brand, clicks, impressions, click_through_rate
    FROM product_performance_view
    WHERE date BETWEEN '2026-04-01' AND '2026-04-30'
    ORDER BY clicks DESC
    LIMIT 10

    # Same query using a relative date range
    SELECT offer_id, clicks, impressions
    FROM product_performance_view
    WHERE date DURING LAST_30_DAYS
    ORDER BY clicks DESC
    LIMIT 10

    # Pricing benchmarks for the US
    SELECT id, offer_id, title, price, benchmark_price
    FROM price_competitiveness_product_view
    WHERE report_country_code = 'US'
    LIMIT 50

    # Conversions and revenue per offer this month (FREE traffic)
    SELECT offer_id, conversions, conversion_value
    FROM product_performance_view
    WHERE date DURING THIS_MONTH
      AND marketing_method = 'ORGANIC'
    ORDER BY conversion_value DESC
    LIMIT 25
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

# CORS: lock down to a comma-separated allow-list when MCP_CORS_ORIGINS is set;
# otherwise default to "*" for ease of MCP client onboarding.
_cors_env = os.getenv("MCP_CORS_ORIGINS", "").strip()
if _cors_env and _cors_env != "*":
    _allowed_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
    logger.info(f"CORS allow-list: {_allowed_origins}")
else:
    _allowed_origins = ["*"]
    logger.info("CORS allow-list: * (set MCP_CORS_ORIGINS to restrict)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("merchant_server:app", host="0.0.0.0", port=port, reload=False)
