from __future__ import annotations

import hmac
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import create_public_id, create_registration_key, flash, hash_password, is_admin, pop_flash
from app.config import BASE_DIR, SCHEDULE_DAYS, SCHEDULE_STATUSES, UPLOAD_DIR
from app.database import get_db
from app.models import (
    Announcement,
    ChatMessage,
    RegistrationKey,
    RegistrationRequest,
    User,
    WashSlot,
)
from app.services import clear_reservation, week_start

router = APIRouter(prefix="/Admin", tags=["admin"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def require_admin(request: Request):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    return None


async def unique_public_id(db: AsyncSession) -> str:
    while True:
        value = create_public_id()
        exists = (await db.execute(select(User).where(User.PublicId == value))).scalar_one_or_none()
        if exists is None:
            return value


@router.get("", response_class=HTMLResponse)
@router.get("/Index", response_class=HTMLResponse)
async def admin_index(request: Request, tab: str | None = None, db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)

    week = week_start(datetime.now())
    users = list((await db.execute(select(User).order_by(User.CreatedAt.desc()))).scalars().all())
    slots = list((await db.execute(select(WashSlot).order_by(WashSlot.Id))).scalars().all())

    changed = False
    for slot in slots:
        if (
            slot.ReservationWeekStart
            and slot.ReservationWeekStart.date() == week.date()
            and slot.ReservedByUserId is None
            and (slot.ReservedBy or "").strip()
        ):
            user = next(
                (
                    u
                    for u in users
                    if f"{u.FirstName} {u.LastName} (комната {u.RoomNumber})" == slot.ReservedBy
                ),
                None,
            )
            if user is not None:
                slot.ReservedByUserId = user.Id
                changed = True
    if changed:
        await db.commit()

    counts: dict[int, int] = {}
    for slot in slots:
        if (
            slot.ReservationWeekStart
            and slot.ReservationWeekStart.date() == week.date()
            and slot.ReservedByUserId is not None
        ):
            counts[slot.ReservedByUserId] = counts.get(slot.ReservedByUserId, 0) + 1

    registration_keys = list(
        (await db.execute(select(RegistrationKey).order_by(RegistrationKey.CreatedAt.desc()))).scalars().all()
    )
    registration_requests = list(
        (
            await db.execute(
                select(RegistrationRequest)
                .options(selectinload(RegistrationRequest.RegistrationKey))
                .where(RegistrationRequest.Status == "Pending")
                .order_by(RegistrationRequest.CreatedAt)
            )
        ).scalars().all()
    )
    chat_messages = list(
        (
            await db.execute(
                select(ChatMessage)
                .options(
                    selectinload(ChatMessage.User),
                    selectinload(ChatMessage.RegistrationRequest).selectinload(RegistrationRequest.RegistrationKey),
                )
                .order_by(ChatMessage.CreatedAt)
            )
        ).scalars().all()
    )
    unread_chat_count = sum(1 for m in chat_messages if m.SenderType == "User" and not m.IsRead)
    announcements = list(
        (await db.execute(select(Announcement).order_by(Announcement.CreatedAt.desc()))).scalars().all()
    )

    threads: list[dict] = []
    grouped: dict[tuple, list[ChatMessage]] = {}
    for message in chat_messages:
        grouped.setdefault((message.UserId, message.RegistrationRequestId), []).append(message)
    for (uid, rid), group in grouped.items():
        first = group[0]
        user = first.User or next((u for u in users if u.Id == uid), None)
        req = first.RegistrationRequest or next((r for r in registration_requests if r.Id == rid), None)
        if user is not None:
            title = f"{user.FirstName} {user.LastName} · комната {user.RoomNumber}"
        elif req is not None:
            title = f"Заявка #{req.Id} · {req.FirstName} {req.LastName} · комната {req.RoomNumber}"
        else:
            title = "Пользователь"
        threads.append({"title": title, "messages": group, "user": user, "request": req})

    return templates.TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "users": users,
            "slots": slots,
            "weekly_counts": counts,
            "week_start": week,
            "registration_keys": registration_keys,
            "registration_requests": registration_requests,
            "chat_messages": chat_messages,
            "chat_threads": threads,
            "unread_chat_count": unread_chat_count,
            "announcements": announcements,
            "active_admin_tab": (tab or "accounts").strip().lower(),
            "admin_message": pop_flash(request, "AdminMessage"),
            "days": SCHEDULE_DAYS,
            "schedule_statuses": SCHEDULE_STATUSES,
        },
    )


@router.get("/Login", response_class=HTMLResponse)
async def admin_login_get(request: Request):
    return templates.TemplateResponse("admin/login.html", {"request": request, "error": None})


@router.post("/Login", response_class=HTMLResponse)
async def admin_login_post(request: Request, password: str = Form("")):
    admin_password = os.getenv("LAUNDRY_ADMIN_PASSWORD", "")
    if not admin_password:
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Пароль администратора не настроен. Задайте LAUNDRY_ADMIN_PASSWORD."},
        )

    supplied = (password or "").encode("utf-8")
    expected = admin_password.encode("utf-8")
    valid = len(supplied) == len(expected) and hmac.compare_digest(supplied, expected)
    if valid:
        request.session["Admin"] = "true"
        return RedirectResponse("/Admin", status_code=303)

    return templates.TemplateResponse("admin/login.html", {"request": request, "error": "Неверный пароль."})


@router.get("/Logout")
async def admin_logout(request: Request):
    request.session.pop("Admin", None)
    return RedirectResponse("/Admin/Login", status_code=303)


@router.post("/DeleteUser")
async def delete_user(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    user = await db.get(User, id)
    if user is not None:
        slots = (await db.execute(select(WashSlot))).scalars().all()
        identity = f"{user.FirstName} {user.LastName} (комната {user.RoomNumber})"
        for slot in slots:
            if slot.ReservedByUserId == user.Id or slot.ReservedBy == identity:
                clear_reservation(slot)
        await db.delete(user)
        await db.commit()
    return RedirectResponse("/Admin", status_code=303)


@router.post("/UpdateUserLimit")
async def update_user_limit(
    request: Request,
    id: int = Form(...),
    weeklyBookingLimit: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    user = await db.get(User, id)
    if user is not None:
        user.WeeklyBookingLimit = max(0, min(50, weeklyBookingLimit))
        await db.commit()
    return RedirectResponse("/Admin", status_code=303)


@router.post("/ResetUserPassword")
async def reset_user_password(
    request: Request,
    id: int = Form(...),
    newPassword: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    user = await db.get(User, id)
    if user is not None and newPassword and len(newPassword) >= 8:
        user.PasswordHash = hash_password(newPassword)
        await db.commit()
        flash(request, "AdminMessage", f"Пароль аккаунта {user.PublicId} изменён.")
    else:
        flash(request, "AdminMessage", "Новый пароль должен содержать минимум 8 символов.")
    return RedirectResponse("/Admin", status_code=303)


@router.post("/ClearReservation")
async def clear_reservation_action(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    slot = await db.get(WashSlot, id)
    if slot is not None:
        clear_reservation(slot)
        await db.commit()
    return RedirectResponse("/Admin", status_code=303)


@router.post("/UpdateSchedule")
async def update_schedule(
    request: Request,
    id: int = Form(...),
    day: str = Form(""),
    time: str = Form(""),
    status: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    slot = await db.get(WashSlot, id)
    if slot is None:
        return RedirectResponse("/Admin", status_code=303)
    day = (day or "").strip()
    time = (time or "").strip()
    status = (status or "").strip()
    if day in SCHEDULE_DAYS and 0 < len(time) <= 30 and status in SCHEDULE_STATUSES:
        slot.Day = day
        slot.Time = time
        slot.Status = status
        if status != "Свободно":
            clear_reservation(slot)
        await db.commit()
        flash(request, "AdminMessage", f"Расписание изменено: {day}, {time}, статус «{status}».")
    return RedirectResponse("/Admin", status_code=303)


@router.post("/AddSchedule")
async def add_schedule(
    request: Request,
    day: str = Form(""),
    time: str = Form(""),
    status: str = Form("Свободно"),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    day = (day or "").strip()
    time = (time or "").strip()
    status = (status or "Свободно").strip()
    if day in SCHEDULE_DAYS and 0 < len(time) <= 30 and status in SCHEDULE_STATUSES:
        db.add(WashSlot(Day=day, Time=time, Status=status))
        await db.commit()
        flash(request, "AdminMessage", f"Добавлена стирка: {day}, {time}.")
    return RedirectResponse("/Admin", status_code=303)


@router.post("/DeleteSchedule")
async def delete_schedule(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    slot = await db.get(WashSlot, id)
    if slot is not None:
        await db.delete(slot)
        await db.commit()
        flash(request, "AdminMessage", "Стирка удалена из расписания.")
    return RedirectResponse("/Admin", status_code=303)


@router.post("/AddRegistrationKeys")
async def add_registration_keys(
    request: Request,
    count: int = Form(10),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    count = max(1, min(500, count))
    existing = {k.Key for k in (await db.execute(select(RegistrationKey))).scalars().all()}
    keys = []
    while len(keys) < count:
        value = create_registration_key()
        if value in existing:
            continue
        existing.add(value)
        keys.append(RegistrationKey(Key=value, CreatedAt=datetime.utcnow()))
    db.add_all(keys)
    await db.commit()
    flash(request, "AdminMessage", f"Добавлено ключей регистрации: {count}.")
    return RedirectResponse("/Admin?tab=keys", status_code=303)


@router.post("/ApproveRegistration")
async def approve_registration(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)

    req = (
        await db.execute(
            select(RegistrationRequest)
            .options(selectinload(RegistrationRequest.RegistrationKey))
            .where(RegistrationRequest.Id == id, RegistrationRequest.Status == "Pending")
        )
    ).scalar_one_or_none()
    if req is None or req.RegistrationKey is None:
        flash(request, "AdminMessage", "Заявка уже обработана или не найдена.")
        return RedirectResponse("/Admin?tab=requests", status_code=303)

    existing = (
        await db.execute(
            select(User).where(
                func.lower(User.FirstName) == req.FirstName.lower(),
                func.lower(User.LastName) == req.LastName.lower(),
                User.RoomNumber == req.RoomNumber,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        req.Status = "Rejected"
        req.ReviewedAt = datetime.utcnow()
        req.RegistrationKey.IsUsed = False
        req.RegistrationKey.UsedByRequestId = None
        req.RegistrationKey.UsedAt = None
        db.add(
            ChatMessage(
                RegistrationRequestId=req.Id,
                SenderType="System",
                Message="Заявка отклонена: аккаунт с такими данными уже существует.",
                IsRead=True,
                CreatedAt=datetime.utcnow(),
            )
        )
        await db.commit()
        flash(request, "AdminMessage", "Заявка отклонена: аккаунт с такими данными уже существует.")
        return RedirectResponse("/Admin?tab=requests", status_code=303)

    user = User(
        PublicId=await unique_public_id(db),
        FirstName=req.FirstName,
        LastName=req.LastName,
        RoomNumber=req.RoomNumber,
        PasswordHash=req.PasswordHash,
        WeeklyBookingLimit=2,
        CreatedAt=datetime.utcnow(),
    )
    db.add(user)
    await db.flush()
    req.Status = "Approved"
    req.ReviewedAt = datetime.utcnow()
    req.ApprovedUserId = user.Id
    req.RegistrationKey.IsUsed = True
    req.RegistrationKey.UsedAt = datetime.utcnow()
    await db.flush()

    pending_chat = (
        await db.execute(
            select(ChatMessage).where(
                ChatMessage.RegistrationRequestId == req.Id,
                ChatMessage.UserId.is_(None),
            )
        )
    ).scalars().all()
    for chat in pending_chat:
        chat.UserId = user.Id
    db.add(
        ChatMessage(
            UserId=user.Id,
            RegistrationRequestId=req.Id,
            SenderType="System",
            Message=(
                f"Администратор подтвердил вашу регистрацию. Ваш ID аккаунта: {user.PublicId}. "
                "Теперь вы можете войти в аккаунт."
            ),
            IsRead=False,
            CreatedAt=datetime.utcnow(),
        )
    )
    await db.commit()
    flash(request, "AdminMessage", f"Регистрация подтверждена. Создан ID аккаунта: {user.PublicId}.")
    return RedirectResponse("/Admin?tab=requests", status_code=303)


@router.post("/RejectRegistration")
async def reject_registration(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    req = (
        await db.execute(
            select(RegistrationRequest)
            .options(selectinload(RegistrationRequest.RegistrationKey))
            .where(RegistrationRequest.Id == id, RegistrationRequest.Status == "Pending")
        )
    ).scalar_one_or_none()
    if req is not None:
        req.Status = "Rejected"
        req.ReviewedAt = datetime.utcnow()
        if req.RegistrationKey is not None:
            req.RegistrationKey.IsUsed = False
            req.RegistrationKey.UsedByRequestId = None
            req.RegistrationKey.UsedAt = None
        db.add(
            ChatMessage(
                RegistrationRequestId=req.Id,
                SenderType="System",
                Message="Администратор отклонил вашу заявку. Ключ снова доступен для новой регистрации.",
                IsRead=False,
                CreatedAt=datetime.utcnow(),
            )
        )
        await db.commit()
        flash(request, "AdminMessage", "Заявка отклонена. Ключ снова доступен для регистрации.")
    return RedirectResponse("/Admin?tab=requests", status_code=303)


@router.post("/ReplyToUser")
async def reply_to_user(
    request: Request,
    message: str = Form(""),
    userId: str = Form(""),
    requestId: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    message = (message or "").strip()
    if not message or len(message) > 2000:
        return RedirectResponse("/Admin?tab=chat", status_code=303)

    uid = int(userId) if str(userId).isdigit() else None
    rid = int(requestId) if str(requestId).isdigit() else None
    user = await db.get(User, uid) if uid else None
    req = await db.get(RegistrationRequest, rid) if rid else None
    if user is None and req is not None and req.ApprovedUserId:
        user = await db.get(User, req.ApprovedUserId)
    if req is None and user is not None:
        req = (
            await db.execute(
                select(RegistrationRequest).where(RegistrationRequest.ApprovedUserId == user.Id)
            )
        ).scalars().first()
    if user is None and req is None:
        return RedirectResponse("/Admin?tab=chat", status_code=303)

    db.add(
        ChatMessage(
            UserId=user.Id if user else None,
            RegistrationRequestId=req.Id if req else None,
            SenderType="Admin",
            Message=message,
            IsRead=False,
            CreatedAt=datetime.utcnow(),
        )
    )
    await db.commit()
    return RedirectResponse("/Admin?tab=chat", status_code=303)


@router.post("/MarkChatRead")
async def mark_chat_read(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    message = await db.get(ChatMessage, id)
    if message is not None and message.SenderType == "User":
        message.IsRead = True
        await db.commit()
    return RedirectResponse("/Admin?tab=chat", status_code=303)


@router.get("/UnreadChatCount")
async def unread_chat_count(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"count": 0})
    messages = (
        await db.execute(
            select(ChatMessage).where(ChatMessage.SenderType == "User", ChatMessage.IsRead.is_(False))
        )
    ).scalars().all()
    return JSONResponse({"count": len(messages)})


@router.post("/AddAnnouncement")
async def add_announcement(
    request: Request,
    text: str = Form(""),
    image: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)

    path = None
    if image is not None and image.filename:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        suffix = Path(image.filename).suffix
        name = uuid.uuid4().hex + suffix
        dest = UPLOAD_DIR / name
        dest.write_bytes(await image.read())
        path = "/uploads/" + name

    db.add(Announcement(Text=text or "", ImagePath=path, CreatedAt=datetime.utcnow()))
    await db.commit()
    return RedirectResponse("/Admin?tab=announcements", status_code=303)


@router.post("/DeleteAnnouncement")
async def delete_announcement(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    item = await db.get(Announcement, id)
    if item is not None:
        await db.delete(item)
        await db.commit()
    return RedirectResponse("/Admin?tab=announcements", status_code=303)
