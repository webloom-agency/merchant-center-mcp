"""
Persist FastMCP / MCP OAuth authorization-server state to disk.

Survives process restarts (e.g. Render deploys) so registered clients,
in-flight Google OAuth state, short-lived MCP auth codes, and MCP
access/refresh tokens remain valid while Google user credentials are
stored separately by the credential store.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any

from mcp.server.auth.provider import AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from fastmcp.server.auth.auth import AccessToken

logger = logging.getLogger(__name__)

STATE_VERSION = 1
SUBDIR_NAME = "mcp_oauth"
STATE_FILENAME = "server_state.json"

# Pending Google OAuth round-trips and MCP auth codes are short-lived.
DEFAULT_PENDING_TTL_SECONDS = 15 * 60


def mcp_oauth_state_path(base_dir: str) -> str:
    return os.path.join(base_dir, SUBDIR_NAME, STATE_FILENAME)


def write_mcp_oauth_state_atomic(path: str, payload: dict[str, Any]) -> None:
    """Write JSON atomically with restrictive permissions."""
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".mcp_oauth_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_mcp_oauth_state(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read MCP OAuth state file %s: %s", path, e)
        return None


def deserialize_state(
    raw: dict[str, Any],
) -> tuple[
    dict[str, OAuthClientInformationFull],
    dict[str, AccessToken],
    dict[str, RefreshToken],
    dict[str, str],
    dict[str, dict],
    dict[str, AuthorizationCode],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    clients: dict[str, OAuthClientInformationFull] = {}
    access_tokens: dict[str, AccessToken] = {}
    refresh_tokens: dict[str, RefreshToken] = {}
    token_to_email: dict[str, str] = {}
    pending_authorizations: dict[str, dict] = {}
    auth_codes: dict[str, AuthorizationCode] = {}
    auth_code_to_email: dict[str, str] = {}
    auth_code_to_id_token: dict[str, str] = {}
    user_id_tokens: dict[str, str] = {}

    if raw.get("version") != STATE_VERSION:
        logger.warning(
            "Ignoring MCP OAuth state file: unsupported version %r",
            raw.get("version"),
        )
        return (
            clients,
            access_tokens,
            refresh_tokens,
            token_to_email,
            pending_authorizations,
            auth_codes,
            auth_code_to_email,
            auth_code_to_id_token,
            user_id_tokens,
        )

    for cid, cdata in (raw.get("clients") or {}).items():
        try:
            clients[cid] = OAuthClientInformationFull.model_validate(cdata)
        except Exception as e:
            logger.warning("Skipping invalid OAuth client %s: %s", cid, e)

    for tok, tdata in (raw.get("access_tokens") or {}).items():
        try:
            access_tokens[tok] = AccessToken.model_validate(tdata)
        except Exception as e:
            logger.warning("Skipping invalid access token entry: %s", e)

    for tok, tdata in (raw.get("refresh_tokens") or {}).items():
        try:
            refresh_tokens[tok] = RefreshToken.model_validate(tdata)
        except Exception as e:
            logger.warning("Skipping invalid refresh token entry: %s", e)

    token_to_email = dict(raw.get("token_to_email") or {})

    now = time.time()
    saved_at = raw.get("saved_at")
    pending_fallback_created_at = (
        saved_at if isinstance(saved_at, (int, float)) else now
    )
    for state, pdata in (raw.get("pending_authorizations") or {}).items():
        if not isinstance(pdata, dict):
            continue
        entry = dict(pdata)
        created_at = entry.get("created_at")
        if not isinstance(created_at, (int, float)):
            entry["created_at"] = pending_fallback_created_at
            created_at = pending_fallback_created_at
        if created_at + DEFAULT_PENDING_TTL_SECONDS < now:
            continue
        pending_authorizations[state] = entry

    for code, cdata in (raw.get("auth_codes") or {}).items():
        try:
            ac = AuthorizationCode.model_validate(cdata)
            if ac.expires_at < now:
                continue
            auth_codes[code] = ac
        except Exception as e:
            logger.warning("Skipping invalid auth code entry: %s", e)

    auth_code_to_email = {
        k: v
        for k, v in dict(raw.get("auth_code_to_email") or {}).items()
        if k in auth_codes
    }
    auth_code_to_id_token = {
        k: v
        for k, v in dict(raw.get("auth_code_to_id_token") or {}).items()
        if k in auth_codes
    }
    user_id_tokens = {
        k: v for k, v in dict(raw.get("user_id_tokens") or {}).items() if isinstance(v, str)
    }

    return (
        clients,
        access_tokens,
        refresh_tokens,
        token_to_email,
        pending_authorizations,
        auth_codes,
        auth_code_to_email,
        auth_code_to_id_token,
        user_id_tokens,
    )


def serialize_state(
    clients: dict[str, OAuthClientInformationFull],
    access_tokens: dict[str, AccessToken],
    refresh_tokens: dict[str, RefreshToken],
    token_to_email: dict[str, str],
    pending_authorizations: dict[str, dict] | None = None,
    auth_codes: dict[str, AuthorizationCode] | None = None,
    auth_code_to_email: dict[str, str] | None = None,
    auth_code_to_id_token: dict[str, str] | None = None,
    user_id_tokens: dict[str, str] | None = None,
) -> dict[str, Any]:
    pending = dict(pending_authorizations or {})
    codes = dict(auth_codes or {})
    code_emails = dict(auth_code_to_email or {})
    code_id_tokens = dict(auth_code_to_id_token or {})
    id_tokens = dict(user_id_tokens or {})

    auth_codes_snap: dict[str, Any] = {}
    for k, v in codes.items():
        dumped = v.model_dump(mode="json")
        # AnyHttpUrl serializes fine via model_dump; keep explicit for older shapes.
        if "redirect_uri" in dumped and not isinstance(dumped["redirect_uri"], str):
            dumped["redirect_uri"] = str(v.redirect_uri)
        auth_codes_snap[k] = dumped

    return {
        "version": STATE_VERSION,
        "saved_at": time.time(),
        "clients": {
            k: v.model_dump(mode="json") for k, v in dict(clients).items()
        },
        "access_tokens": {
            k: v.model_dump(mode="json") for k, v in dict(access_tokens).items()
        },
        "refresh_tokens": {
            k: v.model_dump(mode="json") for k, v in dict(refresh_tokens).items()
        },
        "token_to_email": dict(token_to_email),
        "pending_authorizations": pending,
        "auth_codes": auth_codes_snap,
        "auth_code_to_email": code_emails,
        "auth_code_to_id_token": code_id_tokens,
        "user_id_tokens": id_tokens,
    }


def prune_expired(
    access_tokens: dict[str, AccessToken],
    refresh_tokens: dict[str, RefreshToken],
    token_to_email: dict[str, str],
    pending_authorizations: dict[str, dict] | None = None,
    auth_codes: dict[str, AuthorizationCode] | None = None,
    auth_code_to_email: dict[str, str] | None = None,
    auth_code_to_id_token: dict[str, str] | None = None,
    now: float | None = None,
) -> None:
    """Remove expired MCP tokens / auth codes / pending Google OAuth states."""
    t = now if now is not None else time.time()
    for key, at in list(access_tokens.items()):
        if at.expires_at is not None and at.expires_at < t:
            access_tokens.pop(key, None)
            token_to_email.pop(key, None)
    for key, rt in list(refresh_tokens.items()):
        if rt.expires_at is not None and rt.expires_at < t:
            refresh_tokens.pop(key, None)
            token_to_email.pop(key, None)
    valid_keys = set(access_tokens) | set(refresh_tokens)
    for key in list(token_to_email.keys()):
        if key not in valid_keys:
            token_to_email.pop(key, None)

    if pending_authorizations is not None:
        for state, pdata in list(pending_authorizations.items()):
            created_at = pdata.get("created_at") if isinstance(pdata, dict) else None
            if not isinstance(created_at, (int, float)):
                pending_authorizations.pop(state, None)
                continue
            if created_at + DEFAULT_PENDING_TTL_SECONDS < t:
                pending_authorizations.pop(state, None)

    if auth_codes is not None:
        for code, ac in list(auth_codes.items()):
            if ac.expires_at < t:
                auth_codes.pop(code, None)
                if auth_code_to_email is not None:
                    auth_code_to_email.pop(code, None)
                if auth_code_to_id_token is not None:
                    auth_code_to_id_token.pop(code, None)
        if auth_code_to_email is not None:
            for code in list(auth_code_to_email.keys()):
                if code not in auth_codes:
                    auth_code_to_email.pop(code, None)
        if auth_code_to_id_token is not None:
            for code in list(auth_code_to_id_token.keys()):
                if code not in auth_codes:
                    auth_code_to_id_token.pop(code, None)
