from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import flash, get_session_user_id, pop_flash
from app.config import BASE_DIR
from app.database import get_db
from app.models import Announcement, User, WashSlot
from app.services import cleanup_expired_reservations, clear_reservation, get_slot_end_server_time, week_start
from app.auth import reservation_text

router = APIRouter(tags=["home"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


async def current_user_booking_count(db: AsyncSession, user_id: int, week: datetime) -> int:
    now = datetime.now()
    result = await db.execute(
        select(WashSlot).where(
            WashSlot.ReservationWeekStart.is_not(None),
            WashSlot.ReservedByUserId == user_id,
            WashSlot.ReservationExpiresAt.is_not(None),
            WashSlot.ReservationExpiresAt > now,
        )
    )
    slots = result.scalars().all()
    return sum(1 for s in slots if s.ReservationWeekStart and s.ReservationWeekStart.date() == week.date())


@router.get("/", response_class=HTMLResponse)
@router.get("/Home/Index", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    server_now = datetime.now()
    await cleanup_expired_reservations(db, server_now)

    result = await db.execute(select(WashSlot).order_by(WashSlot.Id))
    slots = list(result.scalars().all())
    week = week_start(server_now)

    changed = False
    for slot in slots:
        if slot.ReservationWeekStart is None or slot.ReservationWeekStart.date() != week.date():
            if slot.ReservedBy or slot.ReservedByUserId is not None:
                clear_reservation(slot)
                changed = True
    if changed:
        await db.commit()

    user_id = get_session_user_id(request)
    user = await db.get(User, user_id) if user_id else None

    announcements = (
        await db.execute(select(Announcement).order_by(Announcement.CreatedAt.desc()))
    ).scalars().all()

    return templates.TemplateResponse(
        "home/index.html",
        {
            "request": request,
            "slots": slots,
            "user_name": None if user is None else f"{user.FirstName} {user.LastName}",
            "room": None if user is None else user.RoomNumber,
            "user_public_id": None if user is None else user.PublicId,
            "weekly_bookings": 0 if user is None else await current_user_booking_count(db, user.Id, week),
            "weekly_limit": 2 if user is None else user.WeeklyBookingLimit,
            "week_start": week,
            "new_account_id": pop_flash(request, "AccountId"),
            "error": pop_flash(request, "Error"),
            "announcements": announcements,
        },
    )


@router.post("/Home/Reserve")
async def reserve(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse("/Account/Login", status_code=303)

    user = await db.get(User, user_id)
    if user is None:
        request.session.clear()
        return RedirectResponse("/Account/Login", status_code=303)

    server_now = datetime.now()
    await cleanup_expired_reservations(db, server_now)
    week = week_start(server_now)

    current_bookings = await current_user_booking_count(db, user.Id, week)
    limit = max(0, user.WeeklyBookingLimit)
    if current_bookings >= limit:
        flash(request, "Error", f"Ваш недельный лимит исчерпан: {limit} стирки(ок).")
        return RedirectResponse("/", status_code=303)

    slot = await db.get(WashSlot, id)
    if slot is None:
        return RedirectResponse("/", status_code=303)

    if slot.ReservationExpiresAt is not None and slot.ReservationExpiresAt <= server_now:
        clear_reservation(slot)
        await db.commit()

    if slot.ReservationWeekStart is None or slot.ReservationWeekStart.date() != week.date():
        clear_reservation(slot)

    if slot.Status != "Свободно":
        flash(request, "Error", f"Эта стирка недоступна: статус «{slot.Status}».")
        return RedirectResponse("/", status_code=303)

    if not (slot.ReservedBy or "").strip() and slot.ReservedByUserId is None:
        expires_at = get_slot_end_server_time(slot, server_now)
        if expires_at is None or expires_at <= server_now:
            flash(request, "Error", "Эта стирка уже закончилась и недоступна для записи.")
            return RedirectResponse("/", status_code=303)

        slot.ReservedBy = reservation_text(user)
        slot.ReservedByUserId = user.Id
        slot.ReservationWeekStart = week
        slot.ReservationExpiresAt = expires_at
        await db.commit()

    return RedirectResponse("/", status_code=303)


@router.post("/Home/Cancel")
async def cancel(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse("/Account/Login", status_code=303)

    server_now = datetime.now()
    await cleanup_expired_reservations(db, server_now)
    week = week_start(server_now)

    slot = await db.get(WashSlot, id)
    if (
        slot is not None
        and slot.ReservationWeekStart is not None
        and slot.ReservationWeekStart.date() == week.date()
        and slot.ReservedByUserId == user_id
    ):
        clear_reservation(slot)
        await db.commit()

    return RedirectResponse("/", status_code=303)
