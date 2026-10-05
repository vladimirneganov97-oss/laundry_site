from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import clear_user_session, flash, get_session_user_id, pop_flash
from app.config import BASE_DIR, as_local, now_local
from app.database import get_db
from app.models import Announcement, Booking, LaundryRoom, User, UserNotification, WaitlistEntry, WashSlot
from app.booking_services import add_notification, booking_window_end, machine_for_laundry_room, notify_booking_change, offer_next_waitlist, slot_is_bookable, slot_start_end
from app.security_limits import check_rate_limit
from app.services import cleanup_expired_reservations, clear_reservation, get_slot_end_server_time, week_start
from app.auth import reservation_text

router = APIRouter(tags=["home"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


async def current_user_booking_count(db: AsyncSession, user_id: int, week: datetime) -> int:
    now = now_local()
    return await db.scalar(
        select(func.count(Booking.Id)).where(
            Booking.UserId == user_id,
            Booking.WeekStart == week,
            Booking.Status.in_(("Забронировано", "В работе")),
            Booking.EndsAt > now,
        )
    ) or 0


def recurrence_dates(
    slot: WashSlot,
    now: datetime,
    repeat: str,
    selected_date: date | None = None,
) -> list[date]:
    day_index = next(
        (
            index
            for index, day in enumerate(
                ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
            )
            if day == slot.Day
        ),
        None,
    )
    if day_index is None:
        return []

    first_date = selected_date or (now.date() + timedelta(days=(day_index - now.weekday()) % 7))
    horizon_end = booking_window_end(now).date()
    if first_date.weekday() != day_index or not now.date() <= first_date < horizon_end:
        return []
    if repeat == "weekly":
        candidates = [first_date + timedelta(days=7 * week_offset) for week_offset in range(3)]
    elif repeat == "daily":
        candidates = [first_date + timedelta(days=offset) for offset in range(3)]
    else:
        weekday_names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        if repeat in weekday_names:
            target_weekday = weekday_names.index(repeat)
            candidates = [
                first_date + timedelta(days=offset)
                for offset in range(3)
                if (first_date + timedelta(days=offset)).weekday() == target_weekday
            ]
        else:
            candidates = [first_date]
    return [candidate for candidate in candidates if first_date <= candidate < horizon_end]


@router.get("/", response_class=HTMLResponse)
@router.get("/Home/Index", response_class=HTMLResponse)
async def index(
    request: Request,
    roomId: int | None = None,
    week: int = 0,
    db: AsyncSession = Depends(get_db),
):
    server_now = now_local()
    await cleanup_expired_reservations(db, server_now)

    rooms = list(
        (
            await db.execute(
                select(LaundryRoom)
                .where(LaundryRoom.IsActive.is_(True))
                .order_by(LaundryRoom.Floor, LaundryRoom.RoomNumber)
            )
        ).scalars().all()
    )
    user_id = get_session_user_id(request)
    user = await db.get(User, user_id) if user_id else None
    user_room_id = (user.LaundryRoomId or 1) if user is not None else None
    default_room_id = user_room_id if any(room.Id == user_room_id for room in rooms) else None
    if default_room_id is None and rooms:
        default_room_id = rooms[0].Id
    selected_room = next((room for room in rooms if room.Id == roomId), None)
    if selected_room is None:
        selected_room = next((room for room in rooms if room.Id == default_room_id), None)
    selected_room_id = selected_room.Id if selected_room is not None else None
    selected_week_index = 1 if week >= 1 else 0
    current_week = week_start(server_now)
    selected_week = current_week + timedelta(days=7 * selected_week_index)

    all_slots = list((await db.execute(select(WashSlot).order_by(WashSlot.Id))).scalars().all())
    slots = [slot for slot in all_slots if slot.LaundryRoomId == selected_room_id]
    booking_horizon_end = booking_window_end(server_now)
    schedule_dates = [
        (selected_week + timedelta(days=offset)).date()
        for offset in range(7)
    ]
    booking_weeks = {selected_week}
    slot_ids = [slot.Id for slot in slots]
    current_bookings = (
        await db.execute(
            select(Booking).where(
                Booking.SlotId.in_(slot_ids),
                Booking.WeekStart == selected_week,
                Booking.Status.in_(("Забронировано", "В работе")),
            )
        )
    ).scalars().all() if slot_ids else []
    bookings_by_occurrence = {
        (booking.SlotId, booking.WeekStart.date()): booking for booking in current_bookings
    }
    waitlist_counts = {
        (row[0], row[1]): row[2]
        for row in (
            await db.execute(
                select(WaitlistEntry.SlotId, WaitlistEntry.WeekStart, func.count(WaitlistEntry.Id))
                .where(
                    WaitlistEntry.SlotId.in_(slot_ids),
                    WaitlistEntry.WeekStart == selected_week,
                    WaitlistEntry.Status.in_(("Ожидает", "Предложено")),
                )
                .group_by(WaitlistEntry.SlotId, WaitlistEntry.WeekStart)
            )
        ).all()
    } if slot_ids else {}

    changed = False
    for slot in all_slots:
        if slot.ReservationWeekStart is None or slot.ReservationWeekStart.date() != current_week.date():
            if slot.ReservedBy or slot.ReservedByUserId is not None:
                clear_reservation(slot)
                changed = True
    if changed:
        await db.commit()

    machine = await machine_for_laundry_room(db, selected_room_id) if selected_room_id is not None else None
    can_book_selected_room = user is not None and selected_room_id == user_room_id
    schedule_days = []
    bookable_occurrences: set[tuple[int, date]] = set()
    past_occurrences: set[tuple[int, date]] = set()
    for schedule_date in schedule_dates:
        day_name = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")[schedule_date.weekday()]
        day_slot_order: list[tuple[datetime, WashSlot]] = []
        for slot in slots:
            if slot.Day != day_name:
                continue
            interval = slot_start_end(slot, selected_week)
            if interval is None:
                continue
            day_slot_order.append((interval[0], slot))
            if interval[1] <= server_now:
                past_occurrences.add((slot.Id, schedule_date))
            if (
                can_book_selected_room
                and slot.Status == "Свободно"
                and slot_is_bookable(*interval, server_now, booking_horizon_end)
            ):
                bookable_occurrences.add((slot.Id, schedule_date))
        day_slots = [slot for _, slot in sorted(day_slot_order, key=lambda item: (item[0], item[1].Id))]
        schedule_days.append((schedule_date, day_name, selected_week, day_slots))
    booking_counts_by_week = {
        schedule_week: (
            await current_user_booking_count(db, user.Id, schedule_week)
            if user is not None
            else 0
        )
        for schedule_week in booking_weeks
    }

    announcement_query = select(Announcement).order_by(
        Announcement.CreatedByWardenId.is_not(None),
        Announcement.CreatedAt.desc(),
    )
    if selected_room_id is not None:
        announcement_query = announcement_query.where(
            (Announcement.LaundryRoomId.is_(None))
            | (Announcement.LaundryRoomId == selected_room_id)
        )
    else:
        announcement_query = announcement_query.where(Announcement.LaundryRoomId.is_(None))
    announcements = (await db.execute(announcement_query)).scalars().all()
    notifications = []
    unread_notifications = 0
    offered_waitlist = []
    if user is not None:
        notifications = (
            await db.execute(
                select(UserNotification)
                .where(UserNotification.UserId == user.Id)
                .order_by(UserNotification.CreatedAt.desc())
                .limit(10)
            )
        ).scalars().all()
        unread_notifications = await db.scalar(
            select(func.count(UserNotification.Id)).where(
                UserNotification.UserId == user.Id,
                UserNotification.ReadAt.is_(None),
            )
        ) or 0
        offered_waitlist = (
            await db.execute(
                select(WaitlistEntry, WashSlot)
                .join(WashSlot, WaitlistEntry.SlotId == WashSlot.Id)
                .where(
                    WaitlistEntry.UserId == user.Id,
                    WaitlistEntry.Status == "Предложено",
                    WaitlistEntry.OfferedUntil > server_now,
                )
                .order_by(WaitlistEntry.CreatedAt)
            )
        ).all()

    return templates.TemplateResponse(
        "home/index.html",
        {
            "request": request,
            "slots": slots,
            "rooms": rooms,
            "selected_room": selected_room,
            "selected_room_id": selected_room_id,
            "user_room_id": user_room_id,
            "can_book_selected_room": can_book_selected_room,
            "bookable_occurrences": bookable_occurrences,
            "past_occurrences": past_occurrences,
            "today": server_now.date(),
            "server_now": server_now.replace(tzinfo=None),
            "cancellation_deadline": (server_now + timedelta(hours=1)).replace(tzinfo=None),
            "booking_horizon_end_date": booking_horizon_end.date(),
            "selected_week_index": selected_week_index,
            "schedule_days": schedule_days,
            "machine": machine,
            "bookings_by_occurrence": bookings_by_occurrence,
            "waitlist_counts": waitlist_counts,
            "booking_counts_by_week": booking_counts_by_week,
            "user_name": None if user is None else f"{user.FirstName} {user.LastName}",
            "room": None if user is None else user.RoomNumber,
            "user_public_id": None if user is None else user.PublicId,
            "weekly_bookings": 0 if user is None else await current_user_booking_count(db, user.Id, current_week),
            "weekly_limit": 2 if user is None else user.WeeklyBookingLimit,
            "week_start": selected_week,
            "week_dates": [selected_week + timedelta(days=index) for index in range(7)],
            "new_account_id": pop_flash(request, "AccountId"),
            "error": pop_flash(request, "Error"),
            "booking_message": pop_flash(request, "BookingMessage"),
            "announcements": announcements,
            "notifications": notifications,
            "unread_notifications": unread_notifications,
            "offered_waitlist": offered_waitlist,
        },
    )


@router.post("/Home/Reserve")
async def reserve(
    request: Request,
    id: int = Form(...),
    targetDate: str = Form(""),
    repeat: str = Form("once"),
    db: AsyncSession = Depends(get_db),
):
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse("/Account/Login", status_code=303)

    user = await db.get(User, user_id)
    if user is None:
        clear_user_session(request)
        return RedirectResponse("/Account/Login", status_code=303)

    slot = await db.get(WashSlot, id)
    if slot is None:
        flash(request, "Error", "Слот не найден.")
        return RedirectResponse("/", status_code=303)
    room_id = user.LaundryRoomId or 1
    if slot.LaundryRoomId not in (None, room_id):
        flash(request, "Error", "У вас нет доступа к этой постирочной.")
        return RedirectResponse("/", status_code=303)

    ip = request.client.host if request.client else "unknown"
    for rate_key in (f"booking:ip:{ip}", f"booking:user:{user.Id}"):
        allowed, retry_after = await check_rate_limit(db, rate_key, 10, timedelta(seconds=10))
        if not allowed:
            flash(request, "Error", f"Слишком много запросов на бронирование. Повторите через {retry_after} сек.")
            return RedirectResponse("/", status_code=303)

    server_now = now_local()
    await cleanup_expired_reservations(db, server_now)
    week = week_start(server_now)
    machine = await machine_for_laundry_room(db, room_id)
    if machine is not None and machine.Status in {"Неисправна", "На обслуживании"}:
        flash(request, "Error", "Машина №1 временно недоступна.")
        return RedirectResponse("/", status_code=303)

    limit = max(0, user.WeeklyBookingLimit)
    slot = await db.get(WashSlot, id)
    if slot is None:
        return RedirectResponse("/", status_code=303)

    if slot.Status != "Свободно":
        flash(request, "Error", f"Эта стирка недоступна: статус «{slot.Status}».")
        return RedirectResponse("/", status_code=303)

    if repeat not in {"once", "weekly", "daily", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}:
        repeat = "once"

    try:
        selected_date = date.fromisoformat(targetDate)
    except ValueError:
        flash(request, "Error", "Выберите корректную дату стирки.")
        return RedirectResponse("/", status_code=303)
    booking_horizon_end = booking_window_end(server_now)
    if not server_now.date() <= selected_date < booking_horizon_end.date():
        flash(request, "Error", "Записаться можно только на ближайшие три дня.")
        return RedirectResponse("/", status_code=303)

    created = 0
    skipped_reasons: set[str] = set()
    attempted_bookings: set[tuple[int, datetime]] = set()
    for target_date in recurrence_dates(slot, server_now, repeat, selected_date):
        target_week = week_start(datetime.combine(target_date, datetime.min.time()))
        target_day = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")[target_date.weekday()]
        target_slot = slot
        if target_day != slot.Day:
            target_slot = (
                await db.execute(
                    select(WashSlot).where(
                        WashSlot.Day == target_day,
                        WashSlot.Time == slot.Time,
                        WashSlot.LaundryRoomId == room_id,
                    )
                )
            ).scalars().first()
        if target_slot is None or target_slot.Status != "Свободно":
            skipped_reasons.add("unavailable")
            continue

        interval = slot_start_end(target_slot, target_week)
        if interval is None:
            skipped_reasons.add("invalid_time")
            continue
        starts_at, ends_at = interval
        if not slot_is_bookable(starts_at, ends_at, server_now, booking_horizon_end):
            skipped_reasons.add("past")
            continue

        existing = await db.execute(
            select(Booking.Id).where(
                Booking.SlotId == target_slot.Id,
                Booking.WeekStart == target_week,
                Booking.Status.in_(("Забронировано", "В работе")),
            )
        )
        if existing.scalar_one_or_none() is not None:
            skipped_reasons.add("occupied")
            continue

        active_offer = await db.execute(
            select(WaitlistEntry.UserId).where(
                WaitlistEntry.SlotId == target_slot.Id,
                WaitlistEntry.WeekStart == target_week,
                WaitlistEntry.Status == "Предложено",
                WaitlistEntry.OfferedUntil > server_now,
            )
        )
        offer_user_id = active_offer.scalar_one_or_none()
        if offer_user_id is not None and offer_user_id != user.Id:
            skipped_reasons.add("waitlist")
            continue

        if await current_user_booking_count(db, user.Id, target_week) >= limit:
            skipped_reasons.add("limit")
            continue

        attempted_bookings.add((target_slot.Id, target_week))
        db.add(
            Booking(
                SlotId=target_slot.Id,
                UserId=user.Id,
                WeekStart=target_week,
                StartsAt=starts_at,
                EndsAt=ends_at,
                Status="Забронировано",
                CreatedAt=server_now,
            )
        )
        if target_week == week:
            target_slot.ReservedBy = reservation_text(user)
            target_slot.ReservedByUserId = user.Id
            target_slot.ReservationWeekStart = week
            target_slot.ReservationExpiresAt = ends_at
        created += 1

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        active_conflict = await db.scalar(
            select(Booking.Id)
            .where(
                Booking.Status.in_(("Забронировано", "В работе")),
                tuple_(Booking.SlotId, Booking.WeekStart).in_(attempted_bookings),
            )
            .limit(1)
        ) if attempted_bookings else None
        if active_conflict is None:
            raise
        flash(request, "Error", "Это место только что заняли. Обновите расписание и выберите другое время.")
        return RedirectResponse("/", status_code=303)
    if created == 0:
        if "occupied" in skipped_reasons or "waitlist" in skipped_reasons:
            message = "Этот слот уже занят или ожидает подтверждения участником очереди."
        elif "limit" in skipped_reasons:
            message = f"Достигнут недельный лимит бронирований: {limit}."
        elif "past" in skipped_reasons:
            message = "Время этого слота уже прошло. Выберите будущий слот."
        elif "unavailable" in skipped_reasons:
            message = "Слот больше недоступен. Обновите страницу и выберите другое время."
        elif "invalid_time" in skipped_reasons:
            message = "В расписании указано некорректное время. Сообщите администратору."
        else:
            message = "Не удалось найти подходящий слот для выбранного повторения."
        flash(request, "Error", message)
    else:
        flash(
            request,
            "BookingMessage",
            f"Создано бронирований: {created}.",
        )

    return RedirectResponse("/", status_code=303)


@router.post("/Home/Cancel")
async def cancel(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse("/Account/Login", status_code=303)

    server_now = now_local()
    await cleanup_expired_reservations(db, server_now)
    week = week_start(server_now)

    slot = await db.get(WashSlot, id)
    booking = (
        await db.execute(
            select(Booking).where(
                Booking.SlotId == id,
                Booking.UserId == user_id,
                Booking.WeekStart == week,
                Booking.Status.in_(("Забронировано", "В работе")),
            )
        )
    ).scalar_one_or_none()
    if booking is not None:
        if (as_local(booking.StartsAt) or booking.StartsAt) < server_now + timedelta(hours=1):
            flash(request, "Error", "Нельзя отменить бронирование, если до начала стирки осталось меньше часа.")
            return RedirectResponse("/", status_code=303)
        booking.Status = "Отменено"
        booking.CancelledAt = server_now
        await notify_booking_change(db, booking, "Ваше бронирование отменено.", "Отмена")
        await offer_next_waitlist(db, id, week, server_now)
        if slot is not None and slot.ReservedByUserId == user_id:
            clear_reservation(slot)
        await db.commit()

    return RedirectResponse("/", status_code=303)


@router.get("/Home/History", response_class=HTMLResponse)
async def history(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse("/Account/Login", status_code=303)

    query = select(Booking, WashSlot).join(WashSlot, Booking.SlotId == WashSlot.Id).where(Booking.UserId == user_id)
    if date_from:
        try:
            query = query.where(Booking.StartsAt >= datetime.fromisoformat(date_from))
        except ValueError:
            date_from = None
    if date_to:
        try:
            query = query.where(Booking.StartsAt < datetime.fromisoformat(date_to) + timedelta(days=1))
        except ValueError:
            date_to = None
    rows = (await db.execute(query.order_by(Booking.StartsAt.desc()))).all()
    server_now = now_local()
    return templates.TemplateResponse(
        "home/history.html",
        {
            "request": request,
            "rows": rows,
            "date_from": date_from,
            "date_to": date_to,
            "server_now": server_now.replace(tzinfo=None),
            "cancellation_deadline": (server_now + timedelta(hours=1)).replace(tzinfo=None),
        },
    )


@router.post("/Home/History/Cancel")
async def cancel_future_booking(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse("/Account/Login", status_code=303)
    now = now_local()
    booking = await db.get(Booking, id)
    if (
        booking is not None
        and booking.UserId == user_id
        and booking.Status == "Забронировано"
        and (as_local(booking.StartsAt) or booking.StartsAt) > now
    ):
        if (as_local(booking.StartsAt) or booking.StartsAt) < now + timedelta(hours=1):
            flash(request, "Error", "Нельзя отменить бронирование, если до начала стирки осталось меньше часа.")
            return RedirectResponse("/", status_code=303)
        booking.Status = "Отменено"
        booking.CancelledAt = now
        await notify_booking_change(db, booking, "Ваше будущее бронирование отменено.", "Отмена")
        await offer_next_waitlist(db, booking.SlotId, booking.WeekStart, now)
        slot = await db.get(WashSlot, booking.SlotId)
        if (
            slot is not None
            and slot.ReservedByUserId == user_id
            and slot.ReservationWeekStart == booking.WeekStart
        ):
            clear_reservation(slot)
        await db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/Home/Notifications/Read")
async def mark_notification_read(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse("/Account/Login", status_code=303)
    notification = await db.get(UserNotification, id)
    if notification is not None and notification.UserId == user_id and notification.ReadAt is None:
        notification.ReadAt = now_local()
        await db.commit()
    return RedirectResponse("/#notifications", status_code=303)


@router.post("/Home/Waitlist")
async def join_waitlist(
    request: Request,
    id: int = Form(...),
    targetDate: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse("/Account/Login", status_code=303)
    user = await db.get(User, user_id)
    if user is None:
        return RedirectResponse("/Account/Login", status_code=303)
    slot = await db.get(WashSlot, id)
    now = now_local()
    try:
        selected_date = date.fromisoformat(targetDate)
    except ValueError:
        flash(request, "Error", "Выберите корректную дату стирки.")
        return RedirectResponse("/", status_code=303)
    booking_horizon_end = booking_window_end(now)
    if not now.date() <= selected_date < booking_horizon_end.date():
        flash(request, "Error", "Встать в очередь можно только на ближайшие три дня.")
        return RedirectResponse("/", status_code=303)
    week = week_start(datetime.combine(selected_date, datetime.min.time()))
    room_id = user.LaundryRoomId or 1
    machine = await machine_for_laundry_room(db, room_id)
    interval = slot_start_end(slot, week) if slot is not None else None
    expected_day = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")[selected_date.weekday()]
    if (
        slot is None
        or slot.LaundryRoomId != room_id
        or slot.Status != "Свободно"
        or slot.Day != expected_day
        or interval is None
        or not slot_is_bookable(*interval, now, booking_horizon_end)
        or (machine is not None and machine.Status in {"Неисправна", "На обслуживании"})
    ):
        flash(request, "Error", "К этому слоту нельзя присоединиться к очереди.")
        return RedirectResponse("/", status_code=303)
    occupied = await db.execute(
        select(Booking.Id).where(
            Booking.SlotId == id,
            Booking.WeekStart == week,
            Booking.Status.in_(("Забронировано", "В работе")),
        )
    )
    if occupied.scalar_one_or_none() is None:
        flash(request, "Error", "Слот уже свободен. Попробуйте забронировать его напрямую.")
        return RedirectResponse("/", status_code=303)
    existing = await db.execute(
        select(WaitlistEntry.Id).where(
            WaitlistEntry.SlotId == id,
            WaitlistEntry.WeekStart == week,
            WaitlistEntry.UserId == user_id,
            WaitlistEntry.Status.in_(("Ожидает", "Предложено")),
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(WaitlistEntry(SlotId=id, UserId=user_id, WeekStart=week, CreatedAt=now_local()))
        await db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/Home/Waitlist/Accept")
async def accept_waitlist(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse("/Account/Login", status_code=303)
    now = now_local()
    entry = await db.get(WaitlistEntry, id)
    if (
        entry is None
        or entry.UserId != user_id
        or entry.Status != "Предложено"
        or entry.OfferedUntil is None
        or (as_local(entry.OfferedUntil) or entry.OfferedUntil) <= now
    ):
        return RedirectResponse("/", status_code=303)
    slot = await db.get(WashSlot, entry.SlotId)
    user = await db.get(User, user_id)
    machine = await machine_for_laundry_room(db, slot.LaundryRoomId if slot is not None else None)
    interval = slot_start_end(slot, entry.WeekStart) if slot else None
    occupied = await db.execute(
        select(Booking.Id).where(
            Booking.SlotId == entry.SlotId,
            Booking.WeekStart == entry.WeekStart,
            Booking.Status.in_(("Забронировано", "В работе")),
        )
    )
    if (
        slot is None
        or user is None
        or slot.LaundryRoomId != (user.LaundryRoomId or 1)
        or (machine is not None and machine.Status in {"Неисправна", "На обслуживании"})
        or slot.Status != "Свободно"
        or interval is None
        or not slot_is_bookable(*interval, now, booking_window_end(now))
        or occupied.scalar_one_or_none() is not None
        or await current_user_booking_count(db, user_id, entry.WeekStart) >= max(0, user.WeeklyBookingLimit)
    ):
        return RedirectResponse("/", status_code=303)

    starts_at, ends_at = interval
    booking = Booking(
        SlotId=slot.Id,
        UserId=user_id,
        WeekStart=entry.WeekStart,
        StartsAt=starts_at,
        EndsAt=ends_at,
        CreatedAt=now,
    )
    db.add(booking)
    entry.Status = "Принято"
    slot.ReservedBy = reservation_text(user)
    slot.ReservedByUserId = user_id
    slot.ReservationWeekStart = entry.WeekStart
    slot.ReservationExpiresAt = ends_at
    db.add(
        UserNotification(
            UserId=user_id,
            Kind="Очередь",
            Message="Вы подтвердили бронирование места из очереди.",
            CreatedAt=now,
        )
    )
    await db.commit()
    return RedirectResponse("/", status_code=303)
