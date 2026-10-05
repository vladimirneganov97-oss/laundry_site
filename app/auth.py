from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request, Response
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PersistentLogin, User, WardenAccount

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


def create_public_id() -> str:
    return uuid.uuid4().hex[:12].upper()


def create_registration_key() -> str:
    raw = secrets.token_hex(12).upper()
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:24]}"


def is_letters_only(value: str) -> bool:
    return bool(value) and value.isalpha()


def reservation_text(user: User) -> str:
    return f"{user.FirstName} {user.LastName} (комната {user.RoomNumber})"


def is_admin(request: Request) -> bool:
    return request.session.get("Admin") == "true"


def clear_user_session(request: Request) -> None:
    for key in ("UserId", "UserPublicId", "UserLogin", "UserName", "Room"):
        request.session.pop(key, None)


def clear_warden_session(request: Request) -> None:
    for key in ("WardenId", "WardenPublicId", "WardenName", "WardenMessage"):
        request.session.pop(key, None)


def get_session_warden_id(request: Request) -> int | None:
    value = request.session.get("WardenId")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_session_user_id(request: Request) -> int | None:
    value = request.session.get("UserId")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def sign_in(request: Request, response: Response, db: AsyncSession, user: User) -> None:
    request.session["UserId"] = user.Id
    request.session["UserPublicId"] = user.PublicId
    request.session["UserLogin"] = user.Login
    request.session["UserName"] = f"{user.FirstName} {user.LastName}"
    request.session["Room"] = user.RoomNumber
    request.session.pop("PendingRegistrationKey", None)

    token = secrets.token_urlsafe(48)
    db.add(
        PersistentLogin(
            UserId=user.Id,
            Token=token,
            CreatedAt=datetime.utcnow(),
            ExpiresAt=datetime.utcnow() + timedelta(days=365),
        )
    )
    await db.commit()

    response.set_cookie(
        key="LaundryRememberMe",
        value=token,
        httponly=True,
        max_age=365 * 24 * 60 * 60,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


async def restore_remember_me(request: Request, db: AsyncSession) -> None:
    if get_session_user_id(request) is not None:
        return

    token = request.cookies.get("LaundryRememberMe")
    if not token:
        return

    result = await db.execute(
        select(PersistentLogin).where(
            PersistentLogin.Token == token,
            PersistentLogin.ExpiresAt > datetime.utcnow(),
        )
    )
    login = result.scalar_one_or_none()
    if login is None:
        return

    user = await db.get(User, login.UserId)
    if user is None:
        return

    request.session["UserId"] = user.Id
    request.session["UserPublicId"] = user.PublicId
    request.session["UserLogin"] = user.Login
    request.session["UserName"] = f"{user.FirstName} {user.LastName}"
    request.session["Room"] = user.RoomNumber


async def validate_session_user(request: Request, db: AsyncSession) -> None:
    user_id = get_session_user_id(request)
    if user_id is None:
        return

    user = await db.get(User, user_id)
    session_public_id = request.session.get("UserPublicId")
    if user is None or user.PublicId != session_public_id or user.IsBlocked:
        clear_user_session(request)


async def validate_session_warden(request: Request, db: AsyncSession) -> None:
    warden_id = get_session_warden_id(request)
    if warden_id is None:
        return
    warden = await db.get(WardenAccount, warden_id)
    if (
        warden is None
        or warden.PublicId != request.session.get("WardenPublicId")
        or warden.IsBlocked
    ):
        clear_warden_session(request)


def flash(request: Request, key: str, value: Any) -> None:
    flashes = request.session.setdefault("_flashes", {})
    flashes[key] = value


def pop_flash(request: Request, key: str, default: Any = None) -> Any:
    flashes = request.session.get("_flashes") or {}
    if key not in flashes:
        return default
    value = flashes.pop(key)
    if not flashes:
        request.session.pop("_flashes", None)
    else:
        request.session["_flashes"] = flashes
    return value
