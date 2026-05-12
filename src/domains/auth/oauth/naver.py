"""Naver OAuth 2.0 adapter.

Implements the Naver login flow using the Naver OAuth 2.0 API.

Reference: https://developers.naver.com/docs/login/api/api.md
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from core.config import Settings

logger = structlog.get_logger(__name__)

_AUTH_URL = "https://nid.naver.com/oauth2.0/authorize"
_TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
_USERINFO_URL = "https://openapi.naver.com/v1/nid/me"


class NaverOAuthAdapter:
    """Naver OAuth 2.0 adapter."""

    PROVIDER = "naver"

    def __init__(self, settings: Settings) -> None:
        self._client_id = settings.naver_client_id
        self._client_secret = settings.naver_client_secret.get_secret_value()
        self._redirect_uri = settings.naver_redirect_uri

    def get_authorization_url(self) -> tuple[str, str]:
        """Return (authorization_url, state)."""
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "state": state,
        }
        url = f"{_AUTH_URL}?{urlencode(params)}"
        return url, state

    async def exchange_code(self, code: str, state: str) -> dict[str, Any]:
        """Exchange code for Naver user info."""
        async with httpx.AsyncClient() as client:
            token_resp = await client.get(
                _TOKEN_URL,
                params={
                    "grant_type": "authorization_code",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "state": state,
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

        response = user_data.get("response", {})

        return {
            "provider_user_id": response.get("id", ""),
            "email": response.get("email", ""),
            "display_name": response.get("name") or response.get("nickname"),
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": token_data.get("expires_in"),
        }
