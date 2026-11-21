# api/auth.py
"""API-key authentication, role checks and tenant resolution. All comparisons are constant-time."""
import hashlib
import hmac
from enum import Enum
from typing import List, Optional

from fastapi import Header, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from api.config import settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class UserRole(str, Enum):
    OPERATOR = "OPERATOR"
    APPROVER = "APPROVER"
    AUDITOR = "AUDITOR"
    ADMIN = "ADMIN"


class AuthenticatedUser(BaseModel):
    user_id: str
    tenant_id: str
    role: UserRole


def _sha(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).digest()


def _build_registry() -> List[tuple]:
    t = settings.DEFAULT_TENANT_ID
    users = [
        (settings.ADMIN_API_KEY, AuthenticatedUser(user_id="usr-admin", tenant_id=t, role=UserRole.ADMIN)),
        (settings.APPROVER_API_KEY, AuthenticatedUser(user_id="usr-approver-01", tenant_id=t, role=UserRole.APPROVER)),
        (settings.OPERATOR_API_KEY, AuthenticatedUser(user_id="usr-operator-01", tenant_id=t, role=UserRole.OPERATOR)),
        (settings.AUDITOR_API_KEY, AuthenticatedUser(user_id="usr-auditor-01", tenant_id=t, role=UserRole.AUDITOR)),
    ]
    for u in settings.extra_users():
        users.append((u["key"], AuthenticatedUser(user_id=u["user_id"], tenant_id=u["tenant_id"],
                                                  role=UserRole(u["role"]))))
    # Only digests are kept in memory; raw keys are not retained by the registry.
    return [(_sha(k), user) for k, user in users]


_REGISTRY = _build_registry()


def authenticate(api_key: Optional[str]) -> AuthenticatedUser:
    """Compares against EVERY key (no early exit) using fixed-length digests: constant time, unicode safe."""
    if not api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing required 'X-API-Key' header.")
    digest = _sha(api_key)
    match: Optional[AuthenticatedUser] = None
    for stored, user in _REGISTRY:
        if hmac.compare_digest(stored, digest):
            match = user
    if match is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid API key.")
    return match


def get_current_user(api_key: Optional[str] = Security(api_key_header)) -> AuthenticatedUser:
    return authenticate(api_key)


def require_roles(allowed: List[UserRole]):
    def checker(user: AuthenticatedUser = Security(get_current_user)) -> AuthenticatedUser:
        if user.role != UserRole.ADMIN and user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"Role '{user.role.value}' lacks permission for this operation.")
        return user
    return checker


def verify_internal_service_token(x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")) -> None:
    if not x_internal_token or not hmac.compare_digest(_sha(x_internal_token), _sha(settings.ERP_INTERNAL_API_KEY)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Valid 'X-Internal-Token' required.")
