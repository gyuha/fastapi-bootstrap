"""Refresh HTTP route contract tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from domains.auth.router import _get_service, router


class FakeRefreshService:
    """Minimal auth application-service fake for route-level refresh validation tests."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        self.calls.append(refresh_token)
        return {
            "access_token": "new.access.jwt",
            "refresh_token": "new.refresh.jwt",
            "token_type": "bearer",
            "expires_in": 900,
        }


@pytest.fixture
def refresh_service() -> FakeRefreshService:
    return FakeRefreshService()


@pytest.fixture
def app(refresh_service: FakeRefreshService) -> FastAPI:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[_get_service] = lambda: refresh_service
    return application


@pytest.mark.parametrize("payload", [{}, {"refresh_token": ""}, {"refresh_token": "   "}])
async def test_refresh_rejects_malformed_payload_before_service_call(
    app: FastAPI,
    refresh_service: FakeRefreshService,
    payload: dict[str, Any],
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/v1/auth/refresh", json=payload)

    assert response.status_code == 422
    assert refresh_service.calls == []


async def test_refresh_delegates_refresh_token_to_auth_service(
    app: FastAPI,
    refresh_service: FakeRefreshService,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "refresh.jwt"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "access_token": "new.access.jwt",
        "refresh_token": "new.refresh.jwt",
        "token_type": "bearer",
        "expires_in": 900,
    }
    assert refresh_service.calls == ["refresh.jwt"]
