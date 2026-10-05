from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import (
    clear_warden_session,
    create_registration_key,
    get_session_warden_id,
    reservation_text,
    verify_password,
)
from app.announcement_services import save_announcement_image
from app.booking_services import booking_window_end, machine_for_laundry_room, notify_booking_change, offer_next_waitlist, slot_is_bookable, slot_start_end
from app.config import BASE_DIR, DEFAULT_TIMES, SCHEDULE_DAYS, SCHEDULE_STATUSES, now_local
from app.database import get_db
from app.models import (
    AdminAuditLog,
    Announcement,
    Booking,
    ChatMessage,
    LaundryRoom,
    Machine,
    RegistrationKey,
    RegistrationRequest,
    User,
    UserNotification,
    WardenAccount,
    WardenAuditLog,
    WaitlistEntry,
    WashSlot,
)
from app.registration_services import (
    approve_registration,
    reject_registration,
    wardens_for_laundry_room,
)
from app.security_limits import check_rate_limit
from app.services import cleanup_expired_reservations, clear_reservation, week_start

router = APIRouter(prefix="/Warden", tags=["warden"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


async def _current_warden(request: Request, db: AsyncSession) -> WardenAccount | None:
    warden_id = get_session_warden_id(request)
    if warden_id is None:
        return None
    warden = await db.get(WardenAccount, warden_id)
    if warden is None or warden.IsBlocked:
        clear_warden_session(request)
        return None
    return warden


async def _eligible_rooms(db: AsyncSession, warden: WardenAccount) -> list[LaundryRoom]:
    rooms = list(
        (
            await db.execute(
                select(LaundryRoom)
                .where(LaundryRoom.IsActive.is_(True))
                .order_by(LaundryRoom.Floor, LaundryRoom.RoomNumber)
            )
        ).scalars().all()
    )
    eligible = []
    for room in rooms:
        assigned = await wardens_for_laundry_room(db, room.Id)
        if any(item.Id == warden.Id for item in assigned):
            eligible.append(room)
    return eligible


async def _ensure_key_pool(
    db: AsyncSession, warden: WardenAccount, rooms: list[LaundryRoom]
) -> None:
    all_key_values = set((await db.execute(select(RegistrationKey.Key))).scalars().all())
    for room in rooms:
        unused = list(
            (
                await db.execute(
                    select(RegistrationKey)
                    .where(
                        RegistrationKey.LaundryRoomId == room.Id,
                        RegistrationKey.IsUsed.is_(False),
                        RegistrationKey.IsDeleted.is_(False),
                        RegistrationKey.UsedByRequestId.is_(None),
                        RegistrationKey.WardenId == warden.Id,
                    )
                    .order_by(RegistrationKey.CreatedAt, RegistrationKey.Id)
                )
            ).scalars().all()
        )
        missing = max(0, 100 - len(unused))
        if missing:
            unassigned = list(
                (
                    await db.execute(
                        select(RegistrationKey)
                        .where(
                            RegistrationKey.LaundryRoomId == room.Id,
                            RegistrationKey.IsUsed.is_(False),
                            RegistrationKey.IsDeleted.is_(False),
                            RegistrationKey.UsedByRequestId.is_(None),
                            RegistrationKey.WardenId.is_(None),
                        )
                        .order_by(RegistrationKey.CreatedAt, RegistrationKey.Id)
                        .limit(missing)
                    )
                ).scalars().all()
            )
            for key in unassigned:
                key.WardenId = warden.Id
            missing -= len(unassigned)
        keys = []
        while len(keys) < missing:
            value = create_registration_key()
            if value in all_key_values:
                continue
            all_key_values.add(value)
            keys.append(
                RegistrationKey(
                    Key=value,
                    LaundryRoomId=room.Id,
                    WardenId=warden.Id,
                    IsUsed=False,
                    CreatedAt=datetime.utcnow(),
                )
            )
        db.add_all(keys)


@router.get("/Login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse(
        "warden/login.html", {"request": request, "errors": []}
    )


@router.post("/Login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    publicId: str = Form(""),
    password: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = await check_rate_limit(
        db, f"warden-login:{client_ip}", 5, timedelta(minutes=15)
    )
    if not allowed:
        return templates.TemplateResponse(
            "warden/login.html",
            {"request": request, "errors": [f"Слишком много попыток входа. Повторите через {retry_after} сек."]},
        )
    warden = (
        await db.execute(
            select(WardenAccount).where(WardenAccount.PublicId == (publicId or "").strip().upper())
        )
    ).scalar_one_or_none()
    if (
        warden is None
        or warden.IsBlocked
        or not verify_password(password or "", warden.PasswordHash)
    ):
        return templates.TemplateResponse(
            "warden/login.html",
            {"request": request, "errors": ["Неверный ID или пароль старосты."]},
        )
    clear_warden_session(request)
    request.session["WardenId"] = warden.Id
    request.session["WardenPublicId"] = warden.PublicId
    request.session["WardenName"] = warden.DisplayName
    return RedirectResponse("/Warden?tab=admin-chat", status_code=303)


@router.get("/Logout")
async def logout(request: Request):
    clear_warden_session(request)
    return RedirectResponse("/Warden/Login", status_code=303)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    tab: str | None = None,
    roomId: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    warden = await _current_warden(request, db)
    if warden is None:
        return RedirectResponse("/Warden/Login", status_code=303)

    await cleanup_expired_reservations(db)
    if not tab:
        return RedirectResponse("/Warden?tab=admin-chat", status_code=303)

    rooms = await _eligible_rooms(db, warden)
    room_ids = [room.Id for room in rooms]
    selected_room = next((room for room in rooms if room.Id == roomId), None)
    if selected_room is None and rooms:
        selected_room = rooms[0]
    selected_room_id = selected_room.Id if selected_room else None

    registrations = list(
        (
            await db.execute(
                select(RegistrationRequest)
                .options(selectinload(RegistrationRequest.RegistrationKey))
                .where(
                    RegistrationRequest.WardenId == warden.Id,
                    RegistrationRequest.LaundryRoomId.in_(room_ids),
                )
                .order_by(RegistrationRequest.CreatedAt.desc())
            )
        ).scalars().all()
    )
    request_ids = [item.Id for item in registrations]
    messages: list[ChatMessage] = []
    if request_ids:
        messages = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.RegistrationRequestId.in_(request_ids),
                        ChatMessage.WardenId.is_(None),
                    )
                    .order_by(ChatMessage.CreatedAt)
                )
            ).scalars().all()
        )
        for message in messages:
            if message.SenderType == "User" and not message.IsRead:
                message.IsRead = True
        await db.commit()

    chats_by_request: dict[int, list[ChatMessage]] = {}
    for message in messages:
        if message.RegistrationRequestId is not None:
            chats_by_request.setdefault(message.RegistrationRequestId, []).append(message)

    await _ensure_key_pool(db, warden, rooms)
    keys = list(
        (
            await db.execute(
                select(RegistrationKey)
                .where(
                    RegistrationKey.WardenId == warden.Id,
                    RegistrationKey.LaundryRoomId.in_(room_ids),
                    RegistrationKey.IsDeleted.is_(False),
                )
                .order_by(RegistrationKey.CreatedAt.desc())
            )
        ).scalars().all()
    )
    room_names = {room.Id: room.Name for room in rooms}
    room_names_by_id = {room.Id: room for room in rooms}
    users = list(
        (
            await db.execute(
                select(User)
                .where(User.LaundryRoomId.in_(room_ids))
                .order_by(User.CreatedAt.desc())
            )
        ).scalars().all()
    ) if room_ids else []
    users_by_id = {user.Id: user for user in users}
    now = now_local()
    database_now = now.replace(tzinfo=None)
    current_week = week_start(now)
    booking_horizon_end = booking_window_end(now)
    database_horizon_end = booking_horizon_end.replace(tzinfo=None)
    booking_dates = [
        now.date() + timedelta(days=offset)
        for offset in range(3)
    ]
    booking_weeks = {
        week_start(datetime.combine(booking_date, datetime.min.time()))
        for booking_date in booking_dates
    }
    booking_week_by_date = {
        booking_date: week_start(datetime.combine(booking_date, datetime.min.time()))
        for booking_date in booking_dates
    }
    bookings = list(
        (
            await db.execute(
                select(Booking, WashSlot)
                .join(WashSlot, Booking.SlotId == WashSlot.Id)
                .where(
                    WashSlot.LaundryRoomId.in_(room_ids),
                    Booking.WeekStart.in_(booking_weeks),
                    Booking.Status.in_(("Забронировано", "В работе")),
                    Booking.StartsAt <= database_horizon_end,
                )
                .order_by(Booking.StartsAt)
            )
        ).all()
    ) if room_ids else []
    warden_weekly_booking_counts = {
        booking_week: (
            await db.scalar(
                select(func.count(Booking.Id)).where(
                    Booking.WardenId == warden.Id,
                    Booking.WeekStart == booking_week,
                    Booking.Status.in_(("Забронировано", "В работе")),
                    Booking.EndsAt > database_now,
                )
            )
            or 0
        )
        for booking_week in booking_weeks
    }
    warden_booking_occurrences = {
        (booking.SlotId, booking.WeekStart)
        for booking, _ in bookings
        if booking.WardenId == warden.Id
    }
    active_booking_occurrences = {
        (booking.SlotId, booking.WeekStart) for booking, _ in bookings
    }
    schedule_slots = list(
        (
            await db.execute(
                select(WashSlot)
                .where(WashSlot.LaundryRoomId == selected_room_id)
                .order_by(WashSlot.Day, WashSlot.Time, WashSlot.Id)
            )
        ).scalars().all()
    ) if selected_room_id is not None else []
    bookable_slot_occurrences = {
        slot.Id: (booking_date, booking_week_by_date[booking_date])
        for slot in schedule_slots
        for booking_date in booking_dates
        if slot.Day == SCHEDULE_DAYS[booking_date.weekday()]
        and (interval := slot_start_end(slot, booking_week_by_date[booking_date])) is not None
        and slot_is_bookable(*interval, now, booking_horizon_end)
    }
    machine = (
        await db.scalar(select(Machine).where(Machine.LaundryRoomId == selected_room_id))
        if selected_room_id is not None
        else None
    )
    announcements = list(
        (
            await db.execute(
                select(Announcement)
                .where(
                    Announcement.LaundryRoomId.in_(room_ids),
                    Announcement.CreatedByWardenId == warden.Id,
                )
                .order_by(Announcement.CreatedAt.desc())
            )
        ).scalars().all()
    ) if room_ids else []
    admin_messages = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.WardenId == warden.Id,
                    ChatMessage.UserId.is_(None),
                    ChatMessage.RegistrationRequestId.is_(None),
                )
                .order_by(ChatMessage.CreatedAt)
            )
        ).scalars().all()
    )
    for chat_message in admin_messages:
        if chat_message.SenderType == "Admin" and not chat_message.IsRead:
            chat_message.IsRead = True
    await db.commit()

    history_rows = (
        await db.execute(
            select(Booking, User, WardenAccount, WashSlot)
            .join(WashSlot, Booking.SlotId == WashSlot.Id)
            .outerjoin(User, Booking.UserId == User.Id)
            .outerjoin(WardenAccount, Booking.WardenId == WardenAccount.Id)
            .where(WashSlot.LaundryRoomId.in_(room_ids))
            .order_by(Booking.StartsAt.desc())
            .limit(1000)
        )
    ).all() if room_ids else []
    statistics_start = database_now - timedelta(days=29)
    statistics_bookings = (
        await db.execute(
            select(Booking)
            .join(WashSlot, Booking.SlotId == WashSlot.Id)
            .where(
                WashSlot.LaundryRoomId.in_(room_ids),
                Booking.StartsAt >= statistics_start,
            )
            .order_by(Booking.StartsAt)
        )
    ).scalars().all() if room_ids else []
    daily: dict[str, int] = {}
    hourly: dict[int, int] = {}
    for booking in statistics_bookings:
        day_label = booking.StartsAt.strftime("%d.%m")
        daily[day_label] = daily.get(day_label, 0) + 1
        hourly[booking.StartsAt.hour] = hourly.get(booking.StartsAt.hour, 0) + 1
    audit_logs = list(
        (
            await db.execute(
                select(WardenAuditLog)
                .where(WardenAuditLog.WardenId == warden.Id)
                .order_by(WardenAuditLog.CreatedAt.desc())
                .limit(500)
            )
        ).scalars().all()
    )
    weekly_booking_counts = (
        {
            user_id: count
            for user_id, count in (
                await db.execute(
                    select(Booking.UserId, func.count(Booking.Id))
                    .where(
                        Booking.UserId.in_([user.Id for user in users]),
                        Booking.WeekStart == current_week,
                        Booking.Status.in_(("Забронировано", "В работе")),
                        Booking.EndsAt > database_now,
                    )
                    .group_by(Booking.UserId)
                )
            ).all()
        }
        if users
        else {}
    )
    return templates.TemplateResponse(
        "warden/index.html",
        {
            "request": request,
            "warden": warden,
            "registrations": registrations,
            "chats_by_request": chats_by_request,
            "eligible_rooms": rooms,
            "keys": keys,
            "room_names": room_names,
            "room_names_by_id": room_names_by_id,
            "room_ids": room_ids,
            "selected_room": selected_room,
            "selected_room_id": selected_room_id,
            "schedule_slots": schedule_slots,
            "machine": machine,
            "week_start": current_week,
            "week_dates": [current_week + timedelta(days=index) for index in range(7)],
            "days": SCHEDULE_DAYS,
            "schedule_statuses": SCHEDULE_STATUSES,
            "default_times": DEFAULT_TIMES,
            "users": users,
            "users_by_id": users_by_id,
            "bookings": bookings,
            "booking_weeks": booking_weeks,
            "booking_week_by_date": booking_week_by_date,
            "warden_weekly_booking_counts": warden_weekly_booking_counts,
            "warden_booking_occurrences": warden_booking_occurrences,
            "active_booking_occurrences": active_booking_occurrences,
            "bookable_slot_occurrences": bookable_slot_occurrences,
            "weekly_booking_counts": weekly_booking_counts,
            "announcements": announcements,
            "admin_messages": admin_messages,
            "history_rows": history_rows,
            "statistics_bookings": statistics_bookings,
            "daily": daily,
            "hourly": hourly,
            "audit_logs": audit_logs,
            "active_warden_tab": tab if tab in {
                "admin-chat", "accounts", "schedule", "bookings", "keys",
                "requests", "announcements", "user-chat", "history", "statistics", "audit",
            } else "admin-chat",
            "message": request.session.pop("WardenMessage", None),
        },
    )


@router.post("/GenerateRegistrationKey")
async def generate_registration_key(
    request: Request,
    laundryRoomId: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    warden = await _current_warden(request, db)
    if warden is None:
        return RedirectResponse("/Warden/Login", status_code=303)
    room = await db.get(LaundryRoom, laundryRoomId)
    allowed_wardens = await wardens_for_laundry_room(db, laundryRoomId)
    if room is None or not room.IsActive or not any(item.Id == warden.Id for item in allowed_wardens):
        request.session["WardenMessage"] = "Эта постирочная не относится к вам либо для неё назначен другой староста."
        return _warden_redirect("keys")

    existing_keys = set((await db.execute(select(RegistrationKey.Key))).scalars().all())
    value = create_registration_key()
    while value in existing_keys:
        value = create_registration_key()
    db.add(
        RegistrationKey(
            Key=value,
            LaundryRoomId=room.Id,
            WardenId=warden.Id,
            IsUsed=False,
            CreatedAt=datetime.utcnow(),
        )
    )
    await db.commit()
    request.session["WardenMessage"] = f"Создан ключ для {room.Name}: {value}"
    return _warden_redirect("keys")


@router.post("/Reply")
async def reply(
    request: Request,
    registrationId: int = Form(...),
    message: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    warden = await _current_warden(request, db)
    if warden is None:
        return RedirectResponse("/Warden/Login", status_code=303)
    text = (message or "").strip()
    registration = await db.get(RegistrationRequest, registrationId)
    allowed_room_ids = {room.Id for room in await _eligible_rooms(db, warden)}
    if (
        registration is None
        or registration.WardenId != warden.Id
        or registration.LaundryRoomId not in allowed_room_ids
        or not text
        or len(text) > 2000
    ):
        return _warden_redirect("user-chat")
    db.add(
        ChatMessage(
            UserId=registration.ApprovedUserId,
            RegistrationRequestId=registration.Id,
            SenderType="Warden",
            Message=text,
            IsRead=False,
            CreatedAt=datetime.utcnow(),
        )
    )
    await db.commit()
    return _warden_redirect("user-chat")


def _warden_redirect(tab: str, room_id: int | None = None) -> RedirectResponse:
        suffix = f"&roomId={room_id}" if room_id is not None else ""
        return RedirectResponse(f"/Warden?tab={tab}{suffix}", status_code=303)


async def _authorized_room(
        db: AsyncSession, warden: WardenAccount, room_id: int
) -> LaundryRoom | None:
        room = await db.get(LaundryRoom, room_id)
        if room is None or not room.IsActive:
            return None
        eligible_ids = {item.Id for item in await _eligible_rooms(db, warden)}
        return room if room.Id in eligible_ids else None


@router.post("/ReplyAdmin")
async def reply_admin(
        request: Request,
        message: str = Form(""),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)
        text = (message or "").strip()
        if text and len(text) <= 2000:
            db.add(
                ChatMessage(
                    WardenId=warden.Id,
                    SenderType="Warden",
                    Message=text,
                    IsRead=False,
                    CreatedAt=datetime.utcnow(),
                )
            )
            await db.commit()
        return _warden_redirect("admin-chat")


@router.post("/ApplyScheduleChanges")
async def apply_schedule_changes(
        request: Request,
        laundryRoomId: int = Form(...),
        machine_status: str = Form(""),
        slot_ids: list[int] = Form(default=[]),
        slot_days: list[str] = Form(default=[]),
        slot_times: list[str] = Form(default=[]),
        slot_statuses: list[str] = Form(default=[]),
        new_days: list[str] = Form(default=[]),
        new_times: list[str] = Form(default=[]),
        new_statuses: list[str] = Form(default=[]),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)
        room = await _authorized_room(db, warden, laundryRoomId)
        if room is None:
            request.session["WardenMessage"] = "У вас нет доступа к этой постирочной."
            return _warden_redirect("schedule")

        redirect = _warden_redirect("schedule", room.Id)
        machine_statuses = {"Свободна", "Забронирована", "Работает", "Неисправна", "На обслуживании"}
        count = len(slot_ids)
        if (
            count > 1000
            or len(slot_days) != count
            or len(slot_times) != count
            or len(slot_statuses) != count
            or len(new_days) != len(new_times)
            or len(new_days) != len(new_statuses)
            or len(new_days) > len(SCHEDULE_DAYS)
            or (machine_status and machine_status not in machine_statuses)
            or len(set(slot_ids)) != count
        ):
            request.session["WardenMessage"] = "Проверьте переданные значения расписания."
            return redirect

        changes = []
        for slot_id, day, time, status in zip(slot_ids, slot_days, slot_times, slot_statuses):
            day, time, status = day.strip(), time.strip(), status.strip()
            if slot_id <= 0 or day not in SCHEDULE_DAYS or not 0 < len(time) <= 30 or status not in SCHEDULE_STATUSES:
                request.session["WardenMessage"] = "Проверьте день, время и статус стирок."
                return redirect
            slot = await db.get(WashSlot, slot_id)
            if slot is None or slot.LaundryRoomId != room.Id:
                request.session["WardenMessage"] = "Одна из стирок не относится к выбранной постирочной."
                return redirect
            changes.append((slot, day, time, status))

        additions = []
        for day, time, status in zip(new_days, new_times, new_statuses):
            day, time, status = day.strip(), time.strip(), status.strip()
            if day not in SCHEDULE_DAYS or (time and (len(time) > 30 or status not in SCHEDULE_STATUSES)):
                request.session["WardenMessage"] = "Проверьте данные добавляемой стирки."
                return redirect
            if time:
                additions.append((day, time, status))

        now = datetime.now()
        for slot, day, time, status in changes:
            if (slot.Day, slot.Time, slot.Status) == (day, time, status):
                continue
            if day != slot.Day or time != slot.Time or status != "Свободно":
                active_bookings = (
                    await db.execute(
                        select(Booking).where(
                            Booking.SlotId == slot.Id,
                            Booking.Status.in_(("Забронировано", "В работе")),
                        )
                    )
                ).scalars().all()
                for booking in active_bookings:
                    booking.Status = "Отменено"
                    booking.CancelledAt = now
                    await notify_booking_change(
                        db,
                        booking,
                        "Расписание изменено старостой. Бронирование отменено.",
                        "Изменение",
                    )
            slot.Day, slot.Time, slot.Status = day, time, status
            if status != "Свободно":
                clear_reservation(slot)

        for day, time, status in additions:
            db.add(WashSlot(LaundryRoomId=room.Id, Day=day, Time=time, Status=status))
        machine = await db.scalar(select(Machine).where(Machine.LaundryRoomId == room.Id))
        if machine is not None and machine_status and machine.Status != machine_status:
            if machine_status in {"Неисправна", "На обслуживании"}:
                room_slots = (
                    await db.execute(select(WashSlot).where(WashSlot.LaundryRoomId == room.Id))
                ).scalars().all()
                for slot in room_slots:
                    active_bookings = (
                        await db.execute(
                            select(Booking).where(
                                Booking.SlotId == slot.Id,
                                Booking.Status.in_(("Забронировано", "В работе")),
                            )
                        )
                    ).scalars().all()
                    for booking in active_bookings:
                        booking.Status = "Отменено"
                        booking.CancelledAt = now
                        await notify_booking_change(
                            db,
                            booking,
                            f"Машина переведена в статус «{machine_status}». Бронирование отменено.",
                            "Изменение",
                        )
                    clear_reservation(slot)
            machine.Status = machine_status
            machine.UpdatedAt = now
        await db.commit()
        request.session["WardenMessage"] = f"Расписание постирочной «{room.Name}» обновлено."
        return redirect


@router.post("/ResetWeeklySchedule")
async def reset_weekly_schedule(
        request: Request,
        laundryRoomId: int = Form(...),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)

        room = await _authorized_room(db, warden, laundryRoomId)
        if room is None:
            request.session["WardenMessage"] = "У вас нет доступа к этой постирочной."
            return _warden_redirect("schedule")

        slots = (
            await db.execute(select(WashSlot).where(WashSlot.LaundryRoomId == room.Id))
        ).scalars().all()
        if not slots:
            request.session["WardenMessage"] = f"В постирочной «{room.Name}» пока нет стирок для сброса."
            return _warden_redirect("schedule", room.Id)

        current_week = week_start(now_local())
        booked_slot_ids = set(
            (
                await db.execute(
                    select(Booking.SlotId).where(
                        Booking.WeekStart == current_week,
                        Booking.Status.in_(("Забронировано", "В работе")),
                        Booking.SlotId.in_([slot.Id for slot in slots]),
                    )
                )
            ).scalars().all()
        )
        resettable_statuses = {"Технические работы", "Выходной", "Уборка", "Закрыто"}
        for slot in slots:
            if slot.Status not in resettable_statuses:
                continue
            has_current_reservation = (
                slot.ReservedByUserId is not None
                and slot.ReservationWeekStart is not None
                and week_start(slot.ReservationWeekStart) == current_week
            )
            if slot.Id in booked_slot_ids or has_current_reservation:
                continue
            slot.Status = "Свободно"

        await db.commit()
        request.session["WardenMessage"] = (
            f"Расписание по умолчанию установлено для постирочной «{room.Name}». "
            "Занятые и забронированные стирки сохранены."
        )
        return _warden_redirect("schedule", room.Id)


@router.post("/ReserveSlot")
async def reserve_slot(
    request: Request,
    slotId: int = Form(...),
    laundryRoomId: int = Form(...),
    targetDate: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    warden = await _current_warden(request, db)
    if warden is None:
        return RedirectResponse("/Warden/Login", status_code=303)

    room = await _authorized_room(db, warden, laundryRoomId)
    redirect = _warden_redirect("schedule", laundryRoomId)
    if room is None:
        request.session["WardenMessage"] = "У вас нет доступа к этой постирочной."
        return redirect

    now = now_local()
    booking_horizon_end = booking_window_end(now)
    try:
        selected_date = date.fromisoformat(targetDate)
    except ValueError:
        request.session["WardenMessage"] = "Выберите корректную дату стирки."
        return redirect
    if not now.date() <= selected_date < booking_horizon_end.date():
        request.session["WardenMessage"] = "Записаться можно только на ближайшие три дня."
        return redirect
    week = week_start(datetime.combine(selected_date, datetime.min.time()))
    slot = await db.get(WashSlot, slotId)
    machine = await machine_for_laundry_room(db, room.Id)
    if (
        slot is None
        or slot.LaundryRoomId != room.Id
        or slot.Day != SCHEDULE_DAYS[selected_date.weekday()]
        or slot.Status != "Свободно"
        or (machine is not None and machine.Status in {"Неисправна", "На обслуживании"})
    ):
        request.session["WardenMessage"] = "Этот слот недоступен для бронирования."
        return redirect

    interval = slot_start_end(slot, week)
    if interval is None or not slot_is_bookable(*interval, now, booking_horizon_end):
        request.session["WardenMessage"] = "Стирку можно занять до окончания её времени и только на ближайшие три дня."
        return redirect

    if (
        slot.ReservationWeekStart is not None
        and slot.ReservationWeekStart.date() == week.date()
        and (slot.ReservedBy or slot.ReservedByUserId is not None)
    ):
        request.session["WardenMessage"] = "Этот слот уже занят."
        return redirect

    existing_booking = await db.scalar(
        select(Booking.Id).where(
            Booking.SlotId == slot.Id,
            Booking.WeekStart == week,
            Booking.Status.in_(("Забронировано", "В работе")),
        )
    )
    if existing_booking is not None:
        request.session["WardenMessage"] = "Этот слот уже занят."
        return redirect

    active_offer = await db.scalar(
        select(WaitlistEntry.Id).where(
            WaitlistEntry.SlotId == slot.Id,
            WaitlistEntry.WeekStart == week,
            WaitlistEntry.Status == "Предложено",
            WaitlistEntry.OfferedUntil > now,
        )
    )
    if active_offer is not None:
        request.session["WardenMessage"] = "Слот сейчас предложен участнику очереди; попробуйте позже."
        return redirect

    booking_count = await db.scalar(
        select(func.count(Booking.Id)).where(
            Booking.WardenId == warden.Id,
            Booking.WeekStart == week,
            Booking.Status.in_(("Забронировано", "В работе")),
            Booking.EndsAt > now,
        )
    ) or 0
    if booking_count >= 2:
        request.session["WardenMessage"] = "Староста может занять не более двух слотов за неделю."
        return redirect

    starts_at, ends_at = interval
    db.add(
        Booking(
            SlotId=slot.Id,
            UserId=None,
            WardenId=warden.Id,
            WeekStart=week,
            StartsAt=starts_at,
            EndsAt=ends_at,
            Status="Забронировано",
            CreatedAt=now,
        )
    )
    slot.ReservedBy = f"Староста: {warden.DisplayName}"
    slot.ReservedByUserId = None
    slot.ReservationWeekStart = week
    slot.ReservationExpiresAt = ends_at
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        request.session["WardenMessage"] = "Этот слот только что заняли. Обновите расписание."
        return redirect

    request.session["WardenMessage"] = f"Вы заняли слот: {slot.Day}, {slot.Time}."
    return redirect


@router.post("/DeleteSchedule")
async def delete_schedule(
        request: Request,
        id: int = Form(...),
        laundryRoomId: int = Form(...),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)
        room = await _authorized_room(db, warden, laundryRoomId)
        slot = await db.get(WashSlot, id)
        if room is None or slot is None or slot.LaundryRoomId != room.Id:
            request.session["WardenMessage"] = "Стирка не найдена или недоступна."
            return _warden_redirect("schedule", laundryRoomId)
        history_exists = await db.scalar(select(func.count(Booking.Id)).where(Booking.SlotId == slot.Id)) or 0
        if history_exists:
            slot.Status = "Закрыто"
            bookings = (
                await db.execute(
                    select(Booking).where(
                        Booking.SlotId == slot.Id,
                        Booking.Status.in_(("Забронировано", "В работе")),
                    )
                )
            ).scalars().all()
            for booking in bookings:
                booking.Status = "Отменено"
                booking.CancelledAt = datetime.now()
                await notify_booking_change(db, booking, "Слот закрыт старостой.", "Изменение")
        else:
            await db.delete(slot)
        await db.commit()
        return _warden_redirect("schedule", room.Id)


@router.post("/ClearReservation")
async def clear_reservation_action(
        request: Request,
        id: int = Form(...),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)
        booking = await db.get(Booking, id)
        slot = await db.get(WashSlot, booking.SlotId) if booking is not None else None
        room_ids = {room.Id for room in await _eligible_rooms(db, warden)}
        if (
            booking is not None
            and slot is not None
            and slot.LaundryRoomId in room_ids
            and booking.Status in {"Забронировано", "В работе"}
        ):
            now = datetime.now()
            booking.Status = "Отменено"
            booking.CancelledAt = now
            await notify_booking_change(db, booking, "Бронирование отменено старостой.", "Изменение")
            if slot.ReservationWeekStart == booking.WeekStart:
                clear_reservation(slot)
            await offer_next_waitlist(db, slot.Id, booking.WeekStart, now)
            await db.commit()
        return _warden_redirect("bookings")


@router.post("/UpdateUserLimit")
async def update_user_limit(
        request: Request,
        userId: int = Form(...),
        weeklyBookingLimit: int = Form(...),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)
        user = await db.get(User, userId)
        allowed_ids = {room.Id for room in await _eligible_rooms(db, warden)}
        if user is not None and user.LaundryRoomId in allowed_ids:
            user.WeeklyBookingLimit = max(0, min(50, weeklyBookingLimit))
            await db.commit()
        return _warden_redirect("accounts")


@router.post("/ToggleUserBlock")
async def toggle_user_block(
        request: Request,
        userId: int = Form(...),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)
        user = await db.get(User, userId)
        allowed_ids = {room.Id for room in await _eligible_rooms(db, warden)}
        if user is not None and user.LaundryRoomId in allowed_ids:
            user.IsBlocked = not user.IsBlocked
            if user.IsBlocked:
                bookings = (
                    await db.execute(
                        select(Booking).where(
                            Booking.UserId == user.Id,
                            Booking.Status.in_(("Забронировано", "В работе")),
                        )
                    )
                ).scalars().all()
                for booking in bookings:
                    booking.Status = "Отменено"
                    booking.CancelledAt = datetime.now()
                    await notify_booking_change(db, booking, "Аккаунт заблокирован старостой; бронирование отменено.", "Блокировка")
                slots = (
                    await db.execute(
                        select(WashSlot).where(
                            WashSlot.ReservedByUserId == user.Id,
                            WashSlot.LaundryRoomId.in_(allowed_ids),
                        )
                    )
                ).scalars().all()
                for slot in slots:
                    clear_reservation(slot)
            await db.commit()
        return _warden_redirect("accounts")


@router.post("/AddAnnouncement")
async def add_announcement(
        request: Request,
        laundryRoomId: int = Form(...),
        title: str = Form(""),
        text: str = Form(""),
        category: str = Form("Другое"),
        image: UploadFile | None = File(None),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)
        room = await _authorized_room(db, warden, laundryRoomId)
        own_count = await db.scalar(
            select(func.count(Announcement.Id)).where(Announcement.CreatedByWardenId == warden.Id)
        ) or 0
        title = (title or "").strip()[:120]
        text = (text or "").strip()
        valid_categories = {"Важно", "Расписание", "Ремонт", "Новости", "Другое"}
        if room is None:
            request.session["WardenMessage"] = "Вы не можете публиковать объявления для этой постирочной."
        elif own_count >= 6:
            request.session["WardenMessage"] = "Лимит достигнут: можно разместить не более 6 объявлений."
        elif not title or not text or len(text) > 5000:
            request.session["WardenMessage"] = "Укажите заголовок и текст объявления (до 5000 символов)."
        else:
            path, image_error = await save_announcement_image(image)
            if image_error:
                request.session["WardenMessage"] = image_error
            else:
                db.add(
                    Announcement(
                        LaundryRoomId=room.Id,
                        CreatedByWardenId=warden.Id,
                        Title=title,
                        Text=text,
                        Category=category if category in valid_categories else "Другое",
                        ImagePath=path,
                        CreatedAt=datetime.utcnow(),
                    )
                )
                await db.commit()
                request.session["WardenMessage"] = "Объявление опубликовано для выбранной постирочной."
        return _warden_redirect("announcements")


@router.post("/DeleteAnnouncement")
async def delete_announcement(
        request: Request,
        id: int = Form(...),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)
        announcement = await db.get(Announcement, id)
        allowed_ids = {room.Id for room in await _eligible_rooms(db, warden)}
        if (
            announcement is not None
            and announcement.CreatedByWardenId == warden.Id
            and announcement.LaundryRoomId in allowed_ids
        ):
            await db.delete(announcement)
            await db.commit()
        return _warden_redirect("announcements")


@router.post("/MarkAdminChatRead")
async def mark_admin_chat_read(
        request: Request,
        id: int = Form(...),
        db: AsyncSession = Depends(get_db),
):
        warden = await _current_warden(request, db)
        if warden is None:
            return RedirectResponse("/Warden/Login", status_code=303)
        message = await db.get(ChatMessage, id)
        if (
            message is not None
            and message.WardenId == warden.Id
            and message.SenderType == "Admin"
            and message.RegistrationRequestId is None
        ):
            message.IsRead = True
            await db.commit()
        return _warden_redirect("admin-chat")


async def _get_pending_registration(
    db: AsyncSession, warden_id: int, registration_id: int
) -> RegistrationRequest | None:
    return (
        await db.execute(
            select(RegistrationRequest)
            .options(selectinload(RegistrationRequest.RegistrationKey))
            .where(
                RegistrationRequest.Id == registration_id,
                RegistrationRequest.WardenId == warden_id,
                RegistrationRequest.Status == "Pending",
            )
        )
    ).scalar_one_or_none()


@router.post("/ApproveRegistration")
async def approve(
    request: Request,
    id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    warden = await _current_warden(request, db)
    if warden is None:
        return RedirectResponse("/Warden/Login", status_code=303)
    registration = await _get_pending_registration(db, warden.Id, id)
    if registration is None:
        request.session["WardenMessage"] = "Заявка уже обработана или не найдена."
        return _warden_redirect("requests")
    user, error = await approve_registration(db, registration, "Староста")
    if error or user is None:
        if error in {
            "Аккаунт с такими данными уже существует.",
            "Логин из заявки уже занят. Отклоните заявку и попросите пользователя зарегистрироваться с другим логином.",
        }:
            reject_registration(
                db,
                registration,
                "Староста",
                f"Заявка отклонена: {error}",
            )
        request.session["WardenMessage"] = f"Не удалось подтвердить заявку: {error}"
    else:
        request.session["WardenMessage"] = "Регистрация подтверждена."
    await db.commit()
    return _warden_redirect("requests")


@router.post("/RejectRegistration")
async def reject(
    request: Request,
    id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    warden = await _current_warden(request, db)
    if warden is None:
        return RedirectResponse("/Warden/Login", status_code=303)
    registration = await _get_pending_registration(db, warden.Id, id)
    if registration is not None:
        reject_registration(db, registration, "Староста")
        await db.commit()
        request.session["WardenMessage"] = "Заявка отклонена, ключ снова доступен для регистрации."
    return _warden_redirect("requests")
