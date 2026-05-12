"""Auth domain HTTP router.

Routes
------
POST   /auth/signup                     Register + send verification email
POST   /auth/verify-email/{token}       Verify email address
POST   /auth/login                      Login → JWT pair
POST   /auth/refresh                    Rotate refresh token
POST   /auth/logout                     Revoke tokens
POST   /auth/password-reset             Request password-reset email
POST   /auth/password-reset/confirm     Apply reset token + new password
GET    /auth/me                         Current authenticated user

GET    /auth/oauth/{provider}/login     OAuth2 authorization URL
GET    /auth/oauth/{provider}/callback  OAuth2 callback — exchange code

"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings, get_settings
from core.database import get_async_session
from core.exceptions import AppError
from core.redis import get_redis_dep
from domains.auth.email import AuthEmailSender, get_auth_email_service
from domains.auth.models import User
from domains.auth.repository import AuthRepository
from domains.auth.schemas import (
    LoginRequest,
    LogoutRequest,
    OAuthLoginURLResponse,
    PasswordResetConfirmRequest,
    PasswordResetConfirmResponse,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    RefreshRequest,
    SignupRequest,
    SignupResponse,
    TokenResponse,
    UserResponse,
    VerifyEmailResponse,
)
from domains.auth.security import (
    AccessTokenContext,
    get_current_access_token_context,
    get_current_user,
)
from domains.auth.service import AuthService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


async def _get_service(
    session: AsyncSession = Depends(get_async_session),
    redis: Redis = Depends(get_redis_dep),
    mail_service: AuthEmailSender = Depends(get_auth_email_service),
) -> AuthService:
    """FastAPI dependency — build an :class:`AuthService` per request."""
    repo = AuthRepository(session)
    return AuthService(repo, redis, mail_service=mail_service)


def _app_error_to_http(exc: AppError) -> HTTPException:
    headers = None
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        headers = {"WWW-Authenticate": "Bearer"}
    return HTTPException(status_code=exc.status_code, detail=exc.message, headers=headers)


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
)
async def signup(
    payload: SignupRequest,
    service: AuthService = Depends(_get_service),
) -> SignupResponse:
    """Register a new user account and send a verification email.

    FastAPI validates and normalizes ``payload`` before this handler delegates
    registration to the auth application service.  The response does NOT
    include a JWT — the user must verify their email first (or the calling
    client skips verification in non-prod envs).

    Raises
    ------
    409
        If a user with the given email already exists.
    """
    try:
        user = await service.signup_and_send_email(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
        )
    except AppError as exc:
        raise _app_error_to_http(exc) from exc

    return SignupResponse(
        user=UserResponse.model_validate(user),
        message="Verification email sent. Please check your inbox.",
    )


@router.post(
    "/verify-email/{token}",
    response_model=VerifyEmailResponse,
    summary="Verify email address",
)
async def verify_email(
    token: str,
    service: AuthService = Depends(_get_service),
) -> VerifyEmailResponse:
    """Mark a user's email as verified using the token from the email link."""
    try:
        user = await service.verify_email(token)
    except AppError as exc:
        raise _app_error_to_http(exc) from exc

    return VerifyEmailResponse(user=UserResponse.model_validate(user))


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login and receive JWT pair",
)
async def login(
    body: LoginRequest,
    service: AuthService = Depends(_get_service),
) -> TokenResponse:
    """Authenticate with email + password and receive an access + refresh JWT pair."""
    try:
        tokens = await service.login(body.email, body.password)
    except AppError as exc:
        raise _app_error_to_http(exc) from exc

    return TokenResponse(**tokens)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate refresh token",
)
async def refresh(
    body: RefreshRequest,
    service: AuthService = Depends(_get_service),
) -> TokenResponse:
    """Exchange a refresh token for a new access + refresh token pair.

    The old refresh token is immediately revoked.  Reuse of an already-rotated
    token triggers family revocation (all sessions for the user are terminated).

    Raises
    ------
    401
        If the token is invalid, expired, revoked, or a reuse attack is detected.
    """
    try:
        tokens = await service.refresh(body.refresh_token)
    except AppError as exc:
        raise _app_error_to_http(exc) from exc

    return TokenResponse(**tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Logout and revoke tokens",
)
async def logout(
    body: LogoutRequest,
    access_token: AccessTokenContext = Depends(get_current_access_token_context),
    _current_user: User = Depends(get_current_user),
    service: AuthService = Depends(_get_service),
) -> None:
    """Revoke the refresh token and blacklist the current access token.

    The client should discard both tokens after calling this endpoint.
    """
    await service.logout(
        body.refresh_token,
        access_jti=access_token.jti,
        access_expires_at=access_token.expires_at,
    )


@router.post(
    "/password-reset",
    response_model=PasswordResetRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request password-reset email",
)
async def request_password_reset(
    body: PasswordResetRequest,
    service: AuthService = Depends(_get_service),
) -> PasswordResetRequestResponse:
    """Send a password-reset link to the given email.

    Always returns the same 202 response regardless of whether the email exists
    (prevents user enumeration).
    """
    await service.request_password_reset(body.email)
    return PasswordResetRequestResponse()


@router.post(
    "/password-reset/confirm",
    response_model=PasswordResetConfirmResponse,
    summary="Apply password-reset token",
)
async def confirm_password_reset(
    body: PasswordResetConfirmRequest,
    service: AuthService = Depends(_get_service),
) -> PasswordResetConfirmResponse:
    """Apply a password-reset token and set a new password."""
    try:
        await service.confirm_password_reset(body.token, body.new_password)
    except AppError as exc:
        raise _app_error_to_http(exc) from exc

    return PasswordResetConfirmResponse()


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Current authenticated user",
)
async def get_me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return the profile of the currently authenticated user."""
    return UserResponse.model_validate(current_user)


# ---------------------------------------------------------------------------
# OAuth endpoints
# ---------------------------------------------------------------------------

_OAUTH_STATE_PREFIX = "oauth:state:"
_OAUTH_STATE_TTL = 600  # 10 minutes


@router.get(
    "/oauth/{provider}/login",
    response_model=OAuthLoginURLResponse,
    summary="OAuth2 authorization URL",
)
async def oauth_login(
    provider: str,
    redis: Redis = Depends(get_redis_dep),
) -> OAuthLoginURLResponse:
    """Generate an OAuth2 authorization URL for the given provider.

    The returned ``state`` nonce is stored in Redis for CSRF validation in the
    callback.  Supported providers: google,kakao,naver.

    Raises
    ------
    400
        If *provider* is not configured or unsupported.
    """
    s = get_settings()
    adapter: Any = _get_oauth_adapter(provider, s)

    authorization_url, state = adapter.get_authorization_url()

    # Store state nonce in Redis for CSRF verification
    await redis.set(f"{_OAUTH_STATE_PREFIX}{state}", provider, ex=_OAUTH_STATE_TTL)

    return OAuthLoginURLResponse(authorization_url=authorization_url, state=state)


@router.get(
    "/oauth/{provider}/callback",
    response_model=TokenResponse,
    summary="OAuth2 callback — exchange code for JWT",
)
async def oauth_callback(
    provider: str,
    code: Annotated[str, Query(description="Authorization code from provider")],
    state: Annotated[str, Query(description="CSRF state nonce")],
    redis: Redis = Depends(get_redis_dep),
    service: AuthService = Depends(_get_service),
) -> TokenResponse:
    """Complete the OAuth2 flow by exchanging the code for user tokens.

    Validates the ``state`` nonce against Redis to prevent CSRF attacks.

    Raises
    ------
    400
        If the state is invalid or expired.
    502
        If the OAuth provider returns an error.
    """
    # CSRF state validation
    stored_provider = await redis.get(f"{_OAUTH_STATE_PREFIX}{state}")
    if stored_provider != provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OAuth state.",
        )
    await redis.delete(f"{_OAUTH_STATE_PREFIX}{state}")

    s = get_settings()
    adapter: Any = _get_oauth_adapter(provider, s)

    try:
        if provider == "naver":
            user_info = await adapter.exchange_code(code, state)
        else:
            user_info = await adapter.exchange_code(code)

    except Exception as exc:
        logger.error("oauth_exchange_failed", provider=provider, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OAuth provider error: {exc!s}",
        ) from exc

    try:
        from datetime import UTC, timedelta

        expires_at = None
        if user_info.get("expires_in"):
            from datetime import datetime

            expires_at = datetime.now(UTC) + timedelta(seconds=int(user_info["expires_in"]))

        _user, tokens = await service.oauth_provision_user(
            provider=provider,
            provider_user_id=user_info["provider_user_id"],
            email=user_info["email"],
            display_name=user_info.get("display_name"),
            access_token=user_info.get("access_token"),
            refresh_token=user_info.get("refresh_token"),
            expires_at=expires_at,
        )
    except AppError as exc:
        raise _app_error_to_http(exc) from exc

    return TokenResponse(**tokens)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_oauth_adapter(provider: str, settings: Settings) -> object:
    """Return the OAuth adapter for *provider*.

    Supported providers: google,kakao,naver.

    Raises
    ------
    HTTPException 400
        If *provider* is unsupported or not included in this project.
    """

    if provider == "google":
        from domains.auth.oauth.google import GoogleOAuthAdapter

        return GoogleOAuthAdapter(settings)

    if provider == "kakao":
        from domains.auth.oauth.kakao import KakaoOAuthAdapter

        return KakaoOAuthAdapter(settings)

    if provider == "naver":
        from domains.auth.oauth.naver import NaverOAuthAdapter

        return NaverOAuthAdapter(settings)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported OAuth provider: '{provider}'. Configured: google,kakao,naver.",
    )
