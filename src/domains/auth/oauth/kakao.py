"""Kakao OAuth 2.0 adapter.

Implements the Kakao login flow using the Kakao REST API.

Reference: https://developers.kakao.com/docs/latest/ko/kakaologin/rest-api
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from core.config import Settings

logger = structlog.get_logger(__name__)

_AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
_USERINFO_URL = "https://kapi.kakao.com/v2/user/me"


class KakaoOAuthAdapter:
    """Kakao OAuth 2.0 adapter."""

    PROVIDER = "kakao"

    def __init__(self, settings: Settings) -> None:
        self._client_id = settings.kakao_client_id
        self._client_secret = settings.kakao_client_secret.get_secret_value()
        self._redirect_uri = settings.kakao_redirect_uri

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

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange code for Kakao user info."""
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": self._redirect_uri,
                    "code": code,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()

            userinfo_resp = await client.get(
                _USERINFO_URL,
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            userinfo_resp.raise_for_status()
            user_data = userinfo_resp.json()

        kakao_account = user_data.get("kakao_account", {})
        profile = kakao_account.get("profile", {})

        return {
            "provider_user_id": str(user_data["id"]),
            "email": kakao_account.get("email", ""),
            "display_name": profile.get("nickname"),
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": token_data.get("expires_in"),
        }
