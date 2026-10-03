from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import (
    flash,
    hash_password,
    is_letters_only,
    pop_flash,
    sign_in,
    verify_password,
)
from app.config import BASE_DIR
from app.database import get_db
from app.models import ChatMessage, RegistrationKey, RegistrationRequest, User

router = APIRouter(prefix="/Account", tags=["account"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@router.get("/Register", response_class=HTMLResponse)
async def register_get(request: Request):
    return templates.TemplateResponse(
        "account/register.html",
        {
            "request": request,
            "errors": [],
            "status": None,
            "registration_pending": pop_flash(request, "RegistrationPending"),
        },
    )


@router.post("/Register", response_class=HTMLResponse)
async def register_post(
    request: Request,
    firstName: str = Form(""),
    lastName: str = Form(""),
    roomNumber: str = Form(""),
    password: str = Form(""),
    passwordConfirm: str = Form(""),
    registrationKey: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
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

    if errors:
        return templates.TemplateResponse(
            "account/register.html",
            {"request": request, "errors": errors, "status": None, "registration_pending": None},
        )

    key = (
        await db.execute(
            select(RegistrationKey).where(
                RegistrationKey.Key == registration_key,
                RegistrationKey.IsUsed.is_(False),
                RegistrationKey.UsedByRequestId.is_(None),
            )
        )
    ).scalar_one_or_none()
    if key is None:
        return templates.TemplateResponse(
            "account/register.html",
            {
                "request": request,
                "errors": ["Ключ регистрации недействителен или уже использован."],
                "status": None,
                "registration_pending": None,
            },
        )

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
            {
                "request": request,
                "errors": ["Аккаунт с такими именем, фамилией и номером комнаты уже существует."],
                "status": None,
                "registration_pending": None,
            },
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
            {
                "request": request,
                "errors": ["Заявка с такими данными уже отправлена и ожидает подтверждения администратора."],
                "status": None,
                "registration_pending": None,
            },
        )

    request_row = RegistrationRequest(
        FirstName=first_name,
        LastName=last_name,
        RoomNumber=room_number,
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
                "Заявка на регистрацию отправлена администратору. "
                "Вы можете написать администратору в чате «Связь с админом». Ожидайте подтверждения."
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
        "Заявка отправлена. Дождитесь подтверждения администратора. Чат с администратором открыт ниже — повторно вводить ключ не нужно.",
    )
    flash(request, "OpenRegistrationChat", True)
    return RedirectResponse("/Account/Login", status_code=303)


@router.post("/CheckRegistrationStatus", response_class=HTMLResponse)
async def check_registration_status(
    request: Request,
    registrationKey: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
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
        status = "Заявка ещё ожидает подтверждения администратора."
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
        {"request": request, "errors": [], "status": status, "registration_pending": None},
    )


@router.get("/Login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse(
        "account/login.html",
        {
            "request": request,
            "errors": [],
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
    if not public_id or not password:
        return templates.TemplateResponse(
            "account/login.html",
            {
                "request": request,
                "errors": ["Введите ID аккаунта и пароль."],
                "registration_pending": None,
                "open_registration_chat": False,
            },
        )

    user = (
        await db.execute(select(User).where(User.PublicId == public_id))
    ).scalar_one_or_none()
    if user is None or not user.PasswordHash or not verify_password(password, user.PasswordHash):
        return templates.TemplateResponse(
            "account/login.html",
            {
                "request": request,
                "errors": ["Неверный ID или пароль."],
                "registration_pending": None,
                "open_registration_chat": False,
            },
        )

    redirect = RedirectResponse("/", status_code=303)
    await sign_in(request, redirect, db, user)
    return redirect


@router.get("/Logout")
async def logout(request: Request, response: Response):
    request.session.clear()
    redirect = RedirectResponse("/", status_code=303)
    redirect.delete_cookie("LaundryRememberMe")
    return redirect
