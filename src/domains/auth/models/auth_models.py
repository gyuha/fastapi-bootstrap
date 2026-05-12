"""Auth domain SQLAlchemy ORM models.

Tables
------
* users           — registered accounts (email + argon2 password)
* roles           — named roles (admin, user, moderator, …)
* permissions     — fine-grained permission keys (chat:write, admin:users, …)
* role_permissions — M:N join table
* user_roles      — M:N join table
* refresh_tokens  — one row per active refresh token (rotation + reuse detection)
* email_verifications — pending email verification tokens
* password_resets — pending password-reset tokens

* oauth_accounts  — linked federated-provider identities


All UUIDs are server-generated (``uuid4``) and stored as native UUID columns on
PostgreSQL via ``postgresql.UUID(as_uuid=True)``.

Import pattern::

    from domains.auth.models import User, RefreshToken, ...
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from core.database import Base

# ---------------------------------------------------------------------------
# M:N association tables (no ORM class needed — pure join tables)
# ---------------------------------------------------------------------------

role_permissions: Table = Table(
    "role_permissions",
    Base.metadata,
    Column(
        "role_id", UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "permission_id",
        UUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

user_roles: Table = Table(
    "user_roles",
    Base.metadata,
    Column(
        "user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "role_id", UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    ),
)


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------


class Permission(Base):
    """A fine-grained permission key, e.g. ``chat:write``, ``admin:users``."""

    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # relationships
    roles: Mapped[list[Role]] = relationship(
        "Role", secondary=role_permissions, back_populates="permissions"
    )

    def __repr__(self) -> str:
        return f"<Permission key={self.key!r}>"


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------


class Role(Base):
    """A named role that groups permissions, e.g. ``admin``, ``user``."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # relationships
    permissions: Mapped[list[Permission]] = relationship(
        "Permission", secondary=role_permissions, back_populates="roles"
    )
    users: Mapped[list[User]] = relationship("User", secondary=user_roles, back_populates="roles")

    def __repr__(self) -> str:
        return f"<Role name={self.name!r}>"


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class User(Base):
    """Registered user account.

    Fields
    ------
    email:          Unique email address (lowercased on save).
    display_name:   Optional human-readable name.
    hashed_password: argon2 hash — ``None`` for OAuth-only accounts.
    is_verified:    ``True`` after the user clicks the verification link.
    is_active:      ``False`` = soft-deleted / banned.
    created_at:     Row creation timestamp (server-side).
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hashed_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # relationships
    roles: Mapped[list[Role]] = relationship(
        "Role", secondary=user_roles, back_populates="users", lazy="selectin"
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    email_verifications: Mapped[list[EmailVerification]] = relationship(
        "EmailVerification", back_populates="user", cascade="all, delete-orphan"
    )
    password_resets: Mapped[list[PasswordReset]] = relationship(
        "PasswordReset", back_populates="user", cascade="all, delete-orphan"
    )

    oauth_accounts: Mapped[list[OAuthAccount]] = relationship(
        "OAuthAccount", back_populates="user", cascade="all, delete-orphan"
    )

    def has_permission(self, key: str) -> bool:
        """Return True if any of the user's roles grant *key*."""
        for role in self.roles:
            for perm in role.permissions:
                if perm.key == key:
                    return True
        return False

    def __repr__(self) -> str:
        return f"<User email={self.email!r} verified={self.is_verified}>"


# ---------------------------------------------------------------------------
# RefreshToken
# ---------------------------------------------------------------------------


class RefreshToken(Base):
    """Active refresh token row — one per issued token.

    On rotation:
    1. The old row is deleted (or ``revoked=True``).
    2. A new row is inserted.

    On reuse detection (revoked token used again):
    * All refresh tokens for the user are invalidated (family revocation).

    ``token_hash`` stores SHA-256(raw_token) — never the raw token itself.
    ``jti`` is the JWT claim shared between the access token and its refresh
    token — used for Redis blacklisting.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # family_id groups tokens issued in the same rotation chain
    family_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Rotation metadata: when this token was replaced and which refresh jti replaced it.
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_jti: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="refresh_tokens")

    def __repr__(self) -> str:
        return f"<RefreshToken jti={self.jti!r} revoked={self.revoked}>"


# ---------------------------------------------------------------------------
# EmailVerification
# ---------------------------------------------------------------------------


class EmailVerification(Base):
    """Pending email verification record.

    The ``token`` field is a random URL-safe token (not a JWT).  It is stored
    as a SHA-256 hash for security.  The raw token is emailed to the user.
    """

    __tablename__ = "email_verifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="email_verifications")

    def __repr__(self) -> str:
        return f"<EmailVerification user_id={self.user_id!r} used={self.used}>"


# ---------------------------------------------------------------------------
# PasswordReset
# ---------------------------------------------------------------------------


class PasswordReset(Base):
    """Pending password-reset record.

    Same token lifecycle as :class:`EmailVerification`.
    """

    __tablename__ = "password_resets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="password_resets")

    def __repr__(self) -> str:
        return f"<PasswordReset user_id={self.user_id!r} used={self.used}>"


# ---------------------------------------------------------------------------
# OAuthAccount
# ---------------------------------------------------------------------------


class OAuthAccount(Base):
    """Linked OAuth / federated-identity account.

    One user can have multiple OAuth accounts (one per provider).
    ``provider_user_id`` is the stable identifier from the provider.
    """

    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_oauth_provider_uid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # "google" | "kakao" | "naver"
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped[User] = relationship("User", back_populates="oauth_accounts")

    def __repr__(self) -> str:
        return f"<OAuthAccount provider={self.provider!r} uid={self.provider_user_id!r}>"
