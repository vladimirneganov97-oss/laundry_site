from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import (
    clear_user_session,
    flash,
    hash_password,
    is_letters_only,
    pop_flash,
    sign_in,
    verify_password,
)
from app.config import BASE_DIR
from app.database import get_db
from app.models import (
    ChatMessage,
    LaundryRoom,
    LoginAttempt,
    RegistrationKey,
    RegistrationKeyAttempt,
    RegistrationRequest,
    User,
    WardenAccount,
    WardenLaundryRoom,
)
from app.registration_services import wardens_for_laundry_room
from app.security_limits import check_rate_limit

router = APIRouter(prefix="/Account", tags=["account"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
KEY_COOLDOWN = timedelta(seconds=5)
REGISTRATION_LOCKOUT = timedelta(hours=3)
LOGIN_LOCKOUT = timedelta(minutes=15)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _registration_context(
    request: Request,
    db: AsyncSession,
    *,
    errors: list[str] | None = None,
    status: str | None = None,
    registration_pending: str | None = None,
    key_lock_seconds: int = 0,
    registration_blocked: bool = False,
) -> dict:
    rooms = list(
        (
            await db.execute(
                select(LaundryRoom).where(LaundryRoom.IsActive.is_(True)).order_by(
                    LaundryRoom.Floor, LaundryRoom.RoomNumber
                )
            )
        ).scalars().all()
    )
    wardens = list(
        (
            await db.execute(
                select(WardenAccount).where(WardenAccount.IsBlocked.is_(False)).order_by(WardenAccount.DisplayName)
            )
        ).scalars().all()
    )
    assignments = (
        await db.execute(select(WardenLaundryRoom.WardenId, WardenLaundryRoom.LaundryRoomId))
    ).all()
    room_ids_by_warden: dict[int, list[int]] = {}
    for warden_id, room_id in assignments:
        room_ids_by_warden.setdefault(warden_id, []).append(room_id)
    return {
        "request": request,
        "errors": errors or [],
        "status": status,
        "registration_pending": registration_pending,
        "key_lock_seconds": key_lock_seconds,
        "registration_blocked": registration_blocked,
        "registration_rooms": rooms,
        "registration_floors": sorted({room.Floor for room in rooms}),
        "registration_wardens": [
            {
                "Id": warden.Id,
                "DisplayName": warden.DisplayName,
                "IsUniversal": warden.IsUniversal,
                "RoomIds": room_ids_by_warden.get(warden.Id, []),
            }
            for warden in wardens
        ],
    }


async def _get_key_attempt(db: AsyncSession, client_ip: str) -> RegistrationKeyAttempt | None:
    attempt = await db.get(RegistrationKeyAttempt, client_ip)
    now = datetime.utcnow()
    if attempt is not None and attempt.BlockedUntil is not None and attempt.BlockedUntil <= now:
        attempt.BlockedUntil = None
        if attempt.FailedAttempts >= 3:
            attempt.FailedAttempts = 0
        await db.commit()
    return attempt


def _lock_seconds(attempt: RegistrationKeyAttempt | None) -> int:
    if attempt is None or attempt.BlockedUntil is None:
        return 0
    return max(0, ceil((attempt.BlockedUntil - datetime.utcnow()).total_seconds()))


def _attempt_message(attempt: RegistrationKeyAttempt, seconds: int) -> str:
    if attempt.FailedAttempts >= 3:
        return "После трёх неверных ключей регистрация временно заблокирована на 3 часа. Попробуйте снова после окончания блокировки."
    if seconds:
        return (
            "Введённый ключ не подошёл уже дважды. Осталась одна попытка. "
            f"Повторный ввод будет доступен через {seconds} сек.; третья ошибка заблокирует регистрацию на 3 часа."
        )
    return "Ключ регистрации неверный или уже использован. Осталось две попытки."


async def _record_failed_key(db: AsyncSession, client_ip: str) -> RegistrationKeyAttempt:
    attempt = await _get_key_attempt(db, client_ip)
    if attempt is None:
        attempt = RegistrationKeyAttempt(ClientIp=client_ip, FailedAttempts=0)
        db.add(attempt)
    attempt.FailedAttempts += 1
    if attempt.FailedAttempts == 2:
        attempt.BlockedUntil = datetime.utcnow() + KEY_COOLDOWN
    elif attempt.FailedAttempts >= 3:
        attempt.BlockedUntil = datetime.utcnow() + REGISTRATION_LOCKOUT
    await db.commit()
    return attempt


async def _get_login_attempt(db: AsyncSession, client_ip: str) -> LoginAttempt | None:
    attempt = await db.get(LoginAttempt, client_ip)
    if attempt is not None and attempt.BlockedUntil is not None and attempt.BlockedUntil <= datetime.now():
        attempt.BlockedUntil = None
        attempt.FailedAttempts = 0
        await db.commit()
    return attempt


async def _record_failed_login(db: AsyncSession, client_ip: str) -> LoginAttempt:
    attempt = await _get_login_attempt(db, client_ip)
    if attempt is None:
        attempt = LoginAttempt(ClientIp=client_ip, FailedAttempts=0)
        db.add(attempt)
    attempt.FailedAttempts += 1
    if attempt.FailedAttempts >= 5:
        attempt.BlockedUntil = datetime.now() + LOGIN_LOCKOUT
    await db.commit()
    return attempt


@router.get("/Register", response_class=HTMLResponse)
async def register_get(request: Request, db: AsyncSession = Depends(get_db)):
    attempt = await _get_key_attempt(db, _client_ip(request))
    seconds = _lock_seconds(attempt)
    return templates.TemplateResponse(
        "account/register.html",
        await _registration_context(
            request,
            db,
            errors=[_attempt_message(attempt, seconds)] if seconds and attempt else [],
            registration_pending=pop_flash(request, "RegistrationPending"),
            key_lock_seconds=seconds,
            registration_blocked=bool(attempt and attempt.FailedAttempts >= 3 and seconds),
        ),
    )


@router.post("/Register", response_class=HTMLResponse)
async def register_post(
    request: Request,
    firstName: str = Form(""),
    lastName: str = Form(""),
    roomNumber: str = Form(""),
    floor: int = Form(0),
    laundryRoomId: int = Form(0),
    wardenId: int = Form(0),
    password: str = Form(""),
    passwordConfirm: str = Form(""),
    registrationKey: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    client_ip = _client_ip(request)
    allowed, retry_after = await check_rate_limit(db, f"registration:{client_ip}", 5, timedelta(hours=1))
    if not allowed:
        return templates.TemplateResponse(
            "account/register.html",
            await _registration_context(
                request,
                db,
                errors=[f"Слишком много попыток регистрации. Повторите через {ceil(retry_after / 60)} мин."],
                key_lock_seconds=retry_after,
                registration_blocked=True,
            ),
        )
    attempt = await _get_key_attempt(db, client_ip)
    seconds = _lock_seconds(attempt)
    if seconds and attempt:
        return templates.TemplateResponse(
            "account/register.html",
            await _registration_context(
                request,
                db,
                errors=[_attempt_message(attempt, seconds)],
                key_lock_seconds=seconds,
                registration_blocked=attempt.FailedAttempts >= 3,
            ),
        )

    first_name = (firstName or "").strip()
    last_name = (lastName or "").strip()
    room_number = (roomNumber or "").strip()
    registration_key = (registrationKey or "").strip().upper()
    password = password or ""
    password_confirm = passwordConfirm or ""
    errors: list[str] = []

    if not first_name or not last_name or not room_number or len(password) < 8 or not registration_key:
        errors.append("Заполните все поля. Пароль должен содержать минимум 8 символов.")
    if len(first_name) > 30 or len(last_name) > 30:
        errors.append("Имя и фамилия должны содержать не более 30 символов.")
    if not is_letters_only(first_name):
        errors.append("Имя может содержать только буквы.")
    if not is_letters_only(last_name):
        errors.append("Фамилия может содержать только буквы.")
    try:
        room = int(room_number)
        if room < 1 or room > 1000 or room_number != str(room):
            raise ValueError
    except ValueError:
        errors.append("Номер комнаты должен быть целым положительным числом от 1 до 1000.")
    if password != password_confirm:
        errors.append("Пароли не совпадают.")

    laundry_room = await db.get(LaundryRoom, laundryRoomId) if laundryRoomId else None
    if (
        laundry_room is None
        or not laundry_room.IsActive
        or laundry_room.Floor != floor
    ):
        errors.append("Выберите действующую постирочную на выбранном этаже.")
        available_wardens: list[WardenAccount] = []
        room_has_specific_warden = False
    else:
        available_wardens = await wardens_for_laundry_room(db, laundry_room.Id)
        room_has_specific_warden = await db.scalar(
            select(WardenAccount.Id)
            .join(WardenLaundryRoom, WardenLaundryRoom.WardenId == WardenAccount.Id)
            .where(
                WardenLaundryRoom.LaundryRoomId == laundry_room.Id,
                WardenAccount.IsBlocked.is_(False),
                WardenAccount.IsUniversal.is_(False),
            )
            .limit(1)
        ) is not None
        selected_warden_is_valid = any(warden.Id == wardenId for warden in available_wardens)
        if wardenId == 0 and room_has_specific_warden:
            errors.append("Администратора можно выбрать, только если к постирочной не прикреплён староста.")
        elif wardenId != 0 and not selected_warden_is_valid:
            errors.append("Выбранный староста не обслуживает эту постирочную.")
    if errors:
        return templates.TemplateResponse(
            "account/register.html",
            await _registration_context(request, db, errors=errors),
        )

    if laundry_room is None:
        return templates.TemplateResponse(
            "account/register.html",
            await _registration_context(
                request, db, errors=["Выберите действующую постирочную на выбранном этаже."]
            ),
        )
    selected_warden = next((warden for warden in available_wardens if warden.Id == wardenId), None)

    key_warden_filter = (
        RegistrationKey.WardenId == selected_warden.Id
        if selected_warden is not None
        else RegistrationKey.WardenId.is_(None)
    )
    key = (
        await db.execute(
            select(RegistrationKey).where(
                RegistrationKey.Key == registration_key,
                RegistrationKey.IsUsed.is_(False),
                RegistrationKey.IsDeleted.is_(False),
                RegistrationKey.UsedByRequestId.is_(None),
                RegistrationKey.LaundryRoomId == laundry_room.Id,
                key_warden_filter,
            )
        )
    ).scalar_one_or_none()
    if key is None:
        attempt = await _record_failed_key(db, client_ip)
        seconds = _lock_seconds(attempt)
        return templates.TemplateResponse(
            "account/register.html",
            await _registration_context(
                request,
                db,
                errors=[
                    "Ключ не подходит выбранной постирочной и получателю либо уже использован.",
                    _attempt_message(attempt, seconds),
                ],
                key_lock_seconds=seconds,
                registration_blocked=attempt.FailedAttempts >= 3,
            ),
        )

    attempt = await db.get(RegistrationKeyAttempt, client_ip)
    if attempt is not None:
        await db.delete(attempt)
        await db.commit()

    existing = (
        await db.execute(
            select(User).where(
                func.lower(User.FirstName) == first_name.lower(),
                func.lower(User.LastName) == last_name.lower(),
                User.RoomNumber == room_number,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return templates.TemplateResponse(
            "account/register.html",
            await _registration_context(
                request, db, errors=["Аккаунт с такими именем, фамилией и номером комнаты уже существует."]
            ),
        )

    pending_duplicate = (
        await db.execute(
            select(RegistrationRequest).where(
                RegistrationRequest.Status == "Pending",
                func.lower(RegistrationRequest.FirstName) == first_name.lower(),
                func.lower(RegistrationRequest.LastName) == last_name.lower(),
                RegistrationRequest.RoomNumber == room_number,
            )
        )
    ).scalar_one_or_none()
    if pending_duplicate is not None:
        return templates.TemplateResponse(
            "account/register.html",
            await _registration_context(
                request,
                db,
                errors=["Заявка с такими данными уже отправлена и ожидает проверки."],
            ),
        )

    request_row = RegistrationRequest(
        FirstName=first_name,
        LastName=last_name,
        RoomNumber=room_number,
        LaundryRoomId=laundry_room.Id,
        WardenId=selected_warden.Id if selected_warden is not None else None,
        PasswordHash=hash_password(password),
        RegistrationKeyId=key.Id,
        Status="Pending",
        CreatedAt=datetime.utcnow(),
    )
    db.add(request_row)
    await db.flush()

    key.IsUsed = True
    key.UsedByRequestId = request_row.Id
    db.add(
        ChatMessage(
            RegistrationRequestId=request_row.Id,
            SenderType="System",
            Message=(
                f"Заявка отправлена старосте {selected_warden.DisplayName}. Вы можете написать ему в чате."
                if selected_warden is not None
                else "Заявка отправлена администратору. Вы можете написать ему в чате."
            ),
            IsRead=True,
            CreatedAt=datetime.utcnow(),
        )
    )
    await db.commit()

    request.session["PendingRegistrationKey"] = key.Key
    flash(
        request,
        "RegistrationPending",
        (
            "Заявка отправлена выбранному старосте. Ожидайте ответа; чат с ним доступен ниже."
            if selected_warden is not None
            else "Заявка отправлена администратору. Ожидайте ответа; чат с ним доступен ниже."
        ),
    )
    flash(request, "OpenRegistrationChat", True)
    return RedirectResponse("/Account/Login", status_code=303)


@router.post("/CheckRegistrationStatus", response_class=HTMLResponse)
async def check_registration_status(
    request: Request,
    registrationKey: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    client_ip = _client_ip(request)
    allowed, retry_after = await check_rate_limit(db, f"registration-status:{client_ip}", 10, timedelta(minutes=10))
    if not allowed:
        return templates.TemplateResponse(
            "account/register.html",
            await _registration_context(
                request,
                db,
                errors=[f"Слишком много проверок статуса. Повторите через {ceil(retry_after / 60)} мин."],
            ),
        )
    registration_key = (registrationKey or "").strip().upper()
    req = (
        await db.execute(
            select(RegistrationRequest)
            .options(selectinload(RegistrationRequest.RegistrationKey))
            .join(RegistrationKey)
            .where(RegistrationKey.Key == registration_key)
        )
    ).scalar_one_or_none()

    status = None
    if req is None:
        status = "Заявка с таким ключом не найдена."
    elif req.Status == "Pending":
        status = (
            "Заявка ещё ожидает подтверждения выбранного старосты."
            if req.WardenId is not None
            else "Заявка ещё ожидает рассмотрения администратором."
        )
    elif req.Status == "Rejected":
        status = "Заявка была отклонена. Ключ снова можно использовать для новой регистрации."
    elif req.Status == "Approved" and req.ApprovedUserId:
        user = await db.get(User, req.ApprovedUserId)
        status = (
            "Регистрация подтверждена, но аккаунт не найден."
            if user is None
            else f"Регистрация подтверждена. Ваш ID аккаунта: {user.PublicId}"
        )

    return templates.TemplateResponse(
        "account/register.html",
        await _registration_context(request, db, status=status),
    )


@router.get("/Login", response_class=HTMLResponse)
async def login_get(request: Request, publicId: str = ""):
    return templates.TemplateResponse(
        "account/login.html",
        {
            "request": request,
            "errors": [],
            "public_id": publicId.strip(),
            "registration_pending": pop_flash(request, "RegistrationPending"),
            "open_registration_chat": bool(pop_flash(request, "OpenRegistrationChat")),
        },
    )


@router.post("/Login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    publicId: str = Form(""),
    password: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    public_id = (publicId or "").strip()
    password = password or ""
    client_ip = _client_ip(request)
    login_attempt = await _get_login_attempt(db, client_ip)
    if login_attempt is not None and login_attempt.BlockedUntil is not None and login_attempt.BlockedUntil > datetime.now():
        seconds = max(0, ceil((login_attempt.BlockedUntil - datetime.now()).total_seconds()))
        return templates.TemplateResponse(
            "account/login.html",
            {
                "request": request,
                "errors": [f"Слишком много неверных попыток. Повторите через {ceil(seconds / 60)} мин."],
                "public_id": public_id,
                "registration_pending": None,
                "open_registration_chat": False,
            },
        )
    if not public_id or not password:
        return templates.TemplateResponse(
            "account/login.html",
            {
                "request": request,
                "errors": ["Введите ID аккаунта и пароль."],
                "public_id": public_id,
                "registration_pending": None,
                "open_registration_chat": False,
            },
        )

    user = (
        await db.execute(select(User).where(User.PublicId == public_id))
    ).scalar_one_or_none()
    if user is not None and user.IsBlocked:
        return templates.TemplateResponse(
            "account/login.html",
            {
                "request": request,
                "errors": ["Этот аккаунт заблокирован администратором."],
                "public_id": public_id,
                "registration_pending": None,
                "open_registration_chat": False,
            },
        )
    if user is None or not user.PasswordHash or not verify_password(password, user.PasswordHash):
        attempt = await _record_failed_login(db, client_ip)
        error = (
            "Слишком много неверных попыток. Вход заблокирован на 15 минут."
            if attempt.BlockedUntil is not None
            else f"Неверный ID или пароль. Осталось попыток: {5 - attempt.FailedAttempts}."
        )
        return templates.TemplateResponse(
            "account/login.html",
            {
                "request": request,
                "errors": [error],
                "public_id": public_id,
                "registration_pending": None,
                "open_registration_chat": False,
            },
        )

    attempt = await db.get(LoginAttempt, client_ip)
    if attempt is not None:
        await db.delete(attempt)
        await db.commit()
    redirect = RedirectResponse("/", status_code=303)
    await sign_in(request, redirect, db, user)
    return redirect


@router.get("/Logout")
async def logout(request: Request, response: Response):
    clear_user_session(request)
    redirect = RedirectResponse("/", status_code=303)
    redirect.delete_cookie("LaundryRememberMe")
    return redirect
