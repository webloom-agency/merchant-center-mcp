"""
Google Merchant Center OAuth Scopes

Centralized scope definitions for the Google Merchant Center MCP.
"""

import logging

logger = logging.getLogger(__name__)

# Base OAuth scopes required for user identification
USERINFO_EMAIL_SCOPE = 'https://www.googleapis.com/auth/userinfo.email'
USERINFO_PROFILE_SCOPE = 'https://www.googleapis.com/auth/userinfo.profile'
OPENID_SCOPE = 'openid'

BASE_SCOPES = [
    USERINFO_EMAIL_SCOPE,
    USERINFO_PROFILE_SCOPE,
    OPENID_SCOPE,
]

# Google Merchant API scope (covers both Merchant API and the legacy Content API
# for Shopping; granted via the Google Cloud OAuth consent screen).
MERCHANT_API_SCOPE = 'https://www.googleapis.com/auth/content'

# All scopes needed for the Google Merchant MCP.
SCOPES = BASE_SCOPES + [MERCHANT_API_SCOPE]


def get_current_scopes():
    """Returns all scopes required by the Google Merchant MCP."""
    return list(set(SCOPES))
