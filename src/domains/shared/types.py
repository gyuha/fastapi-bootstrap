"""Shared domain type aliases.

Strongly-typed, UUID-backed identifiers for the major aggregate roots.  Using
``NewType`` makes it impossible to accidentally pass a ``ConversationId`` where
a ``UserId`` is expected — mypy/pyright will catch the mismatch at check time.

Usage::

    from domains.shared.types import UserId, ConversationId

    async def get_conversations(user_id: UserId) -> list[Conversation]:
        ...

Notes
-----
* These are *type-level* aliases.  At runtime they are plain :class:`uuid.UUID`
  values (``NewType`` has zero runtime overhead).
* Domain services and repositories should annotate IDs with these types instead
  of bare ``uuid.UUID`` to improve call-site clarity.
* SQLAlchemy ORM models still use plain ``uuid.UUID`` columns — the semantic
  layer is added by the service / use-case layer.
"""

from __future__ import annotations

import uuid
from typing import NewType

# ---------------------------------------------------------------------------
# Aggregate identity types
# ---------------------------------------------------------------------------

#: Identity of a :class:`~app.domains.auth.models.User` aggregate.
UserId = NewType("UserId", uuid.UUID)

#: Identity of a :class:`~app.domains.auth.models.Role`.
RoleId = NewType("RoleId", uuid.UUID)

#: Identity of a :class:`~app.domains.auth.models.Permission`.
PermissionId = NewType("PermissionId", uuid.UUID)


#: Identity of a :class:`~app.domains.chat.models.Conversation` aggregate.
ConversationId = NewType("ConversationId", uuid.UUID)

#: Identity of a :class:`~app.domains.chat.models.Message`.
MessageId = NewType("MessageId", uuid.UUID)


# ---------------------------------------------------------------------------
# Primitive value-type aliases
# ---------------------------------------------------------------------------

#: A permission key string, e.g. ``"chat:write"``, ``"admin:users"``.
PermissionKey = NewType("PermissionKey", str)

#: OAuth provider name, e.g. ``"google"``, ``"kakao"``, ``"naver"``.
OAuthProvider = NewType("OAuthProvider", str)

#: JWT ID claim — uniquely identifies a token for blacklisting.
JtiStr = NewType("JtiStr", str)
