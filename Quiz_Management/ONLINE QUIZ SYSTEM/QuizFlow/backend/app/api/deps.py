"""
API Dependencies for Database Sessions, Authentication, and Multi-Role Authorization.
Re-exports the single canonical get_db from app.database.session so dependency overrides
and connection pooling remain unified across the entire application.
"""
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.database.session import get_db
from app.models.user import User, UserRole

security_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    token_auth: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Validate bearer token and return authenticated user."""
    if not token_auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(token_auth.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token subject.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User associated with this token no longer exists.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended or deactivated. Please contact an administrator.",
        )

    return user


def get_optional_current_user(
    token_auth: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Safely return the current user if a valid bearer token is present, or None if anonymous."""
    if not token_auth:
        return None
    try:
        payload = decode_access_token(token_auth.credentials)
        if not payload or "sub" not in payload:
            return None
        user_id = int(payload["sub"])
        user = db.get(User, user_id)
        return user if (user and user.is_active) else None
    except Exception:
        return None


def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure that the authenticated user possesses administrative privileges."""
    if current_user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative privileges required to access this resource.",
        )
    return current_user


def get_current_organizer(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure that the authenticated user is an Organizer or Admin."""
    if current_user.role not in (UserRole.ORGANIZER.value, UserRole.ADMIN.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organizer privileges required to access this resource.",
        )
    return current_user


def get_current_participant(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure that the authenticated user is a Participant (or legacy Student)."""
    if current_user.role not in (UserRole.PARTICIPANT.value, UserRole.STUDENT.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Participant privileges required to access this resource.",
        )
    return current_user


# Backward compatibility alias
get_current_student = get_current_participant
