"""Google OAuth 2.0 adapter.

Handles the authorization URL generation and callback token exchange for
Google's OAuth 2.0 / OpenID Connect flow.

Usage::

    from domains.auth.oauth.google import GoogleOAuthAdapter

    adapter = GoogleOAuthAdapter(settings)
    url, state = adapter.get_authorization_url()
    user_info = await adapter.exchange_code(code, state)
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from core.config import Settings

logger = structlog.get_logger(__name__)

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


class GoogleOAuthAdapter:
    """Google OAuth 2.0 adapter."""

    PROVIDER = "google"

    def __init__(self, settings: Settings) -> None:
        self._client_id = settings.google_client_id
        self._client_secret = settings.google_client_secret.get_secret_value()
        self._redirect_uri = settings.google_redirect_uri

    def get_authorization_url(self) -> tuple[str, str]:
        """Return (authorization_url, state) — state is a random CSRF nonce."""
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        url = f"{_AUTH_URL}?{urlencode(params)}"
        return url, state

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange an authorization code for user info.

        Returns
        -------
        dict with keys: provider_user_id, email, display_name, access_token,
                        refresh_token, expires_at
        """
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": self._redirect_uri,
                },
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()

            userinfo_resp = await client.get(
                _USERINFO_URL,
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            userinfo_resp.raise_for_status()
            user_data = userinfo_resp.json()

        return {
            "provider_user_id": user_data["sub"],
            "email": user_data.get("email", ""),
            "display_name": user_data.get("name"),
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": token_data.get("expires_in"),
        }
