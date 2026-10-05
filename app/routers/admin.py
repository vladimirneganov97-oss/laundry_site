from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta
from math import ceil

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import create_public_id, create_registration_key, flash, hash_password, is_admin, pop_flash
from app.announcement_services import save_announcement_image
from app.config import BASE_DIR, DEFAULT_TIMES, SCHEDULE_DAYS, SCHEDULE_STATUSES
from app.database import get_db
from app.models import (
    Announcement,
    AdminAuditLog,
    Booking,
    LaundryRoom,
    LoginAttempt,
    Machine,
    ChatMessage,
    RegistrationKey,
    RegistrationRequest,
    RoomTransferRequest,
    User,
    WardenAccount,
    WardenAuditLog,
    WardenKeyLaundryRoom,
    WardenLaundryRoom,
    WardenRegistrationKey,
    WashSlot,
)
from app.booking_services import machine_for_laundry_room, notify_booking_change, offer_next_waitlist
from app.services import clear_reservation, week_start
from app.registration_services import (
    approve_registration as approve_registration_request,
    reject_registration as reject_registration_request,
    wardens_for_laundry_room,
)

router = APIRouter(prefix="/Admin", tags=["admin"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def require_admin(request: Request):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    return None


def validate_laundry_room_number(floor: int, room_number: int) -> bool:
    if not 1 <= floor <= 9:
        return False
    return room_number // 100 == floor and room_number % 100 in {2, 17}


async def ensure_default_laundry_room(db: AsyncSession) -> LaundryRoom:
    room = await db.get(LaundryRoom, 1)
    if room is None:
        room = LaundryRoom(Id=1, Floor=1, RoomNumber=100, Name="Постирочная 100", IsActive=True)
        db.add(room)
        await db.commit()
    return room


async def cancel_slot_bookings(
    db: AsyncSession,
    slot_id: int,
    reason: str,
    now: datetime,
    week: datetime | None = None,
    offer_waitlist: bool = True,
) -> None:
    query = select(Booking).where(
        Booking.SlotId == slot_id,
        Booking.Status.in_(("Забронировано", "В работе")),
    )
    if week is not None:
        query = query.where(Booking.WeekStart == week)
    bookings = (await db.execute(query)).scalars().all()
    for booking in bookings:
        booking.Status = "Отменено"
        booking.CancelledAt = now
        await notify_booking_change(db, booking, reason, "Изменение")
        if offer_waitlist:
            await offer_next_waitlist(db, slot_id, booking.WeekStart, now)


@router.get("", response_class=HTMLResponse)
@router.get("/Index", response_class=HTMLResponse)
async def admin_index(
    request: Request,
    tab: str | None = None,
    roomId: int | None = None,
    q: str | None = None,
    history_q: str | None = None,
    history_date_from: str | None = None,
    history_date_to: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)

    week = week_start(datetime.now())
    user_query = select(User).order_by(User.CreatedAt.desc())
    search = (q or "").strip()
    if search:
        normalized = search.casefold()
        user_query = user_query.where(
            or_(
                User.PublicId.ilike(f"%{search}%"),
                func.lower(User.FirstName).contains(normalized),
                func.lower(User.LastName).contains(normalized),
                User.RoomNumber.contains(search),
            )
        )
    users = list((await db.execute(user_query)).scalars().all())
    slots = list((await db.execute(select(WashSlot).order_by(WashSlot.Id))).scalars().all())
    history_query = (
        select(Booking, User, WardenAccount, WashSlot)
        .outerjoin(User, Booking.UserId == User.Id)
        .outerjoin(WardenAccount, Booking.WardenId == WardenAccount.Id)
        .join(WashSlot, Booking.SlotId == WashSlot.Id)
    )
    filtered_history_q = (history_q or "").strip()
    if filtered_history_q:
        normalized_history_q = filtered_history_q.casefold()
        history_query = history_query.where(
            or_(
                User.PublicId.ilike(f"%{filtered_history_q}%"),
                func.lower(User.FirstName).contains(normalized_history_q),
                func.lower(User.LastName).contains(normalized_history_q),
                User.RoomNumber.contains(filtered_history_q),
                WardenAccount.DisplayName.ilike(f"%{filtered_history_q}%"),
            )
        )
    if history_date_from:
        try:
            history_query = history_query.where(
                Booking.StartsAt >= datetime.fromisoformat(history_date_from)
            )
        except ValueError:
            history_date_from = None
    if history_date_to:
        try:
            history_query = history_query.where(
                Booking.StartsAt < datetime.fromisoformat(history_date_to) + timedelta(days=1)
            )
        except ValueError:
            history_date_to = None
    history_rows = (
        await db.execute(history_query.order_by(Booking.StartsAt.desc()).limit(1000))
    ).all()
    audit_logs = list(
        (
            await db.execute(
                select(AdminAuditLog).order_by(AdminAuditLog.CreatedAt.desc()).limit(500)
            )
        ).scalars().all()
    )
    statistics_now = datetime.now()
    statistics_bookings = (
        await db.execute(
            select(Booking)
            .where(Booking.StartsAt >= statistics_now - timedelta(days=29))
            .order_by(Booking.StartsAt)
        )
    ).scalars().all()
    statistics_daily: dict[str, int] = {}
    statistics_hourly: dict[int, int] = {}
    for booking in statistics_bookings:
        day = booking.StartsAt.strftime("%d.%m")
        statistics_daily[day] = statistics_daily.get(day, 0) + 1
        statistics_hourly[booking.StartsAt.hour] = statistics_hourly.get(booking.StartsAt.hour, 0) + 1

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
        (
            await db.execute(
                select(RegistrationKey)
                .where(RegistrationKey.IsDeleted.is_(False))
                .order_by(RegistrationKey.CreatedAt.desc())
            )
        ).scalars().all()
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
                    selectinload(ChatMessage.Warden),
                )
                .where(
                    or_(
                        ChatMessage.WardenId.is_(None),
                        ChatMessage.UserId.is_not(None),
                        ChatMessage.RegistrationRequestId.is_not(None),
                    )
                )
                .order_by(ChatMessage.CreatedAt)
            )
        ).scalars().all()
    )
    unread_chat_count = sum(1 for m in chat_messages if m.SenderType in {"User", "Warden"} and not m.IsRead)
    announcements = list(
        (await db.execute(select(Announcement).order_by(Announcement.CreatedAt.desc()))).scalars().all()
    )
    rooms = list((await db.execute(select(LaundryRoom).order_by(LaundryRoom.RoomNumber))).scalars().all())
    active_rooms = [room for room in rooms if room.IsActive]
    schedule_rooms = active_rooms or rooms
    selected_room = next((room for room in schedule_rooms if room.Id == roomId), None)
    if selected_room is None and schedule_rooms:
        selected_room = schedule_rooms[0]
    selected_room_id = selected_room.Id if selected_room is not None else None
    schedule_slots = [
        slot for slot in slots
        if selected_room_id is not None and slot.LaundryRoomId == selected_room_id
    ]
    schedule_slots.sort(key=lambda slot: (slot.Day, slot.Time, slot.Id))
    machine = await machine_for_laundry_room(db, selected_room_id)
    wardens = list((await db.execute(select(WardenAccount).order_by(WardenAccount.CreatedAt.desc()))).scalars().all())
    warden_room_ids: dict[int, list[int]] = {}
    for warden_id, laundry_room_id in (
        await db.execute(select(WardenLaundryRoom.WardenId, WardenLaundryRoom.LaundryRoomId))
    ).all():
        warden_room_ids.setdefault(warden_id, []).append(laundry_room_id)
    rooms_by_id = {room.Id: room for room in rooms}
    warden_rooms = {
        warden.Id: ", ".join(
            rooms_by_id[room_id].Name
            for room_id in warden_room_ids.get(warden.Id, [])
            if room_id in rooms_by_id
        )
        for warden in wardens
    }
    warden_keys = list(
        (
            await db.execute(
                select(WardenRegistrationKey)
                .where(WardenRegistrationKey.IsDeleted.is_(False))
                .order_by(WardenRegistrationKey.CreatedAt.desc())
            )
        ).scalars().all()
    )
    key_room_ids: dict[int, list[int]] = {}
    for key_id, laundry_room_id in (
        await db.execute(select(WardenKeyLaundryRoom.KeyId, WardenKeyLaundryRoom.LaundryRoomId))
    ).all():
        key_room_ids.setdefault(key_id, []).append(laundry_room_id)
    warden_key_rooms = {
        key.Id: ", ".join(
            rooms_by_id[room_id].Name
            for room_id in key_room_ids.get(key.Id, [])
            if room_id in rooms_by_id
        )
        for key in warden_keys
    }
    key_type = (request.query_params.get("keyType") or "registration").strip().lower()
    if key_type not in {"registration", "warden"}:
        key_type = "registration"
    key_floor = request.query_params.get("keyFloor")
    key_room_id = request.query_params.get("keyRoomId")
    try:
        key_floor_value = int(key_floor) if key_floor is not None and key_floor != "" else None
    except ValueError:
        key_floor_value = None
    try:
        key_room_value = int(key_room_id) if key_room_id is not None and key_room_id != "" else None
    except ValueError:
        key_room_value = None
    rooms_by_floor = {floor: [room for room in rooms if room.IsActive and room.Floor == floor] for floor in sorted({room.Floor for room in rooms if room.IsActive})}
    if key_floor_value is not None and key_floor_value not in rooms_by_floor:
        key_floor_value = None
    if key_room_value is not None and key_room_value not in {room.Id for room in rooms if room.IsActive}:
        key_room_value = None
    if key_floor_value is not None and key_room_value is not None:
        key_room = next((room for room in rooms if room.Id == key_room_value), None)
        if key_room is None or key_room.Floor != key_floor_value:
            key_room_value = None
    filtered_warden_keys = []
    for key in warden_keys:
        if key_type != "warden":
            continue
        key_rooms = [room_id for room_id in key_room_ids.get(key.Id, []) if room_id in rooms_by_id]
        if key_room_value is not None and key_room_value not in key_rooms:
            continue
        if key_floor_value is not None and not any(rooms_by_id[room_id].Floor == key_floor_value for room_id in key_rooms):
            continue
        filtered_warden_keys.append(key)
    filtered_registration_keys = []
    for key in registration_keys:
        if key_type != "registration":
            continue
        if key_room_value is not None and key.LaundryRoomId != key_room_value:
            continue
        if key_floor_value is not None and (key.LaundryRoomId is None or rooms_by_id.get(key.LaundryRoomId).Floor != key_floor_value):
            continue
        filtered_registration_keys.append(key)
    active_admin_tab = (tab or "accounts").strip().lower()
    warden_admin_messages = list(
        (
            await db.execute(
                select(ChatMessage)
                .options(selectinload(ChatMessage.Warden))
                .where(
                    ChatMessage.WardenId.is_not(None),
                    ChatMessage.UserId.is_(None),
                    ChatMessage.RegistrationRequestId.is_(None),
                )
                .order_by(ChatMessage.CreatedAt)
            )
        ).scalars().all()
    )
    if active_admin_tab == "warden-chat":
        for message in warden_admin_messages:
            if message.SenderType == "Warden" and not message.IsRead:
                message.IsRead = True
        await db.commit()
    warden_admin_threads: dict[int, list[ChatMessage]] = {}
    for message in warden_admin_messages:
        if message.WardenId is not None:
            warden_admin_threads.setdefault(message.WardenId, []).append(message)
    unread_warden_chat_count = sum(
        message.SenderType == "Warden" and not message.IsRead
        for message in warden_admin_messages
    )

    threads: list[dict] = []
    grouped: dict[tuple, list[ChatMessage]] = {}
    for message in chat_messages:
        grouped.setdefault((message.UserId, message.RegistrationRequestId, message.WardenId), []).append(message)
    for (uid, rid, wid), group in grouped.items():
        first = group[0]
        user = first.User or next((u for u in users if u.Id == uid), None)
        req = first.RegistrationRequest or next((r for r in registration_requests if r.Id == rid), None)
        warden = first.Warden
        if user is not None:
            title = f"{user.FirstName} {user.LastName} · комната {user.RoomNumber}"
        elif warden is not None:
            title = f"Староста {warden.DisplayName} · {warden.PublicId}"
        elif req is not None:
            title = f"Заявка #{req.Id} · {req.FirstName} {req.LastName} · комната {req.RoomNumber}"
        else:
            title = "Пользователь"
        threads.append({"title": title, "messages": group, "user": user, "request": req, "warden": warden})

    return templates.TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "users": users,
            "machine": machine,
            "user_search": search,
            "slots": slots,
            "schedule_slots": schedule_slots,
            "schedule_rooms": schedule_rooms,
            "selected_room": selected_room,
            "selected_laundry_room_id": selected_room_id,
            "weekly_counts": counts,
            "week_start": week,
            "week_dates": [week + timedelta(days=index) for index in range(len(SCHEDULE_DAYS))],
            "history_rows": history_rows,
            "history_q": filtered_history_q,
            "history_date_from": history_date_from,
            "history_date_to": history_date_to,
            "audit_logs": audit_logs,
            "users_count": await db.scalar(select(func.count(User.Id))) or 0,
            "bookings_count": await db.scalar(select(func.count(Booking.Id))) or 0,
            "today_count": await db.scalar(
                select(func.count(Booking.Id)).where(
                    func.date(Booking.StartsAt) == statistics_now.date().isoformat()
                )
            ) or 0,
            "cancellations_count": await db.scalar(
                select(func.count(Booking.Id)).where(Booking.Status == "Отменено")
            ) or 0,
            "machines_count": await db.scalar(
                select(func.count(func.distinct(Machine.LaundryRoomId))).where(Machine.LaundryRoomId.is_not(None))
            ) or 0,
            "daily": statistics_daily,
            "hourly": statistics_hourly,
            "registration_keys": registration_keys,
            "filtered_registration_keys": filtered_registration_keys,
            "filtered_warden_keys": filtered_warden_keys,
            "key_type": key_type,
            "key_floor": key_floor_value,
            "key_room_id": key_room_value,
            "rooms_by_floor": rooms_by_floor,
            "registration_key_wardens": {warden.Id: warden.DisplayName for warden in wardens},
            "registration_room_names": {room.Id: room.Name for room in rooms},
            "registration_requests": registration_requests,
            "chat_messages": chat_messages,
            "chat_threads": threads,
            "unread_chat_count": unread_chat_count,
            "announcements": announcements,
            "rooms": rooms,
            "wardens": wardens,
            "warden_rooms": warden_rooms,
            "warden_room_ids": warden_room_ids,
            "warden_keys": warden_keys,
            "warden_key_rooms": warden_key_rooms,
            "warden_admin_threads": warden_admin_threads,
            "unread_warden_chat_count": unread_warden_chat_count,
            "active_admin_tab": active_admin_tab,
            "admin_message": pop_flash(request, "AdminMessage"),
            "days": SCHEDULE_DAYS,
            "schedule_statuses": SCHEDULE_STATUSES,
        },
    )


@router.post("/AddLaundryRoom")
async def add_laundry_room(request: Request, floor: int = Form(...), room_number: int = Form(...), name: str = Form(""), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    floor = int(floor or 0)
    room_number = int(room_number or 0)
    room_name = (name or "").strip() or f"Постирочная {room_number}"
    if not validate_laundry_room_number(floor, room_number):
        flash(request, "AdminMessage", "Разрешены номера 102 и 117 на 1-м этаже, 202 и 217 на 2-м и так далее до 902 и 917.")
        return RedirectResponse("/Admin?tab=rooms", status_code=303)
    if await db.scalar(select(LaundryRoom.Id).where(LaundryRoom.RoomNumber == room_number)) is not None:
        flash(request, "AdminMessage", "Комната с таким номером уже существует.")
        return RedirectResponse("/Admin?tab=rooms", status_code=303)
    room = LaundryRoom(Floor=floor, RoomNumber=room_number, Name=room_name, IsActive=True, CreatedAt=datetime.now())
    db.add(room)
    await db.flush()
    db.add(Machine(LaundryRoomId=room.Id, Name="Машина №1", Status="Свободна", UpdatedAt=datetime.now()))
    db.add_all(
        WashSlot(Day=day, Time=time, Status="Свободно", LaundryRoomId=room.Id)
        for day in SCHEDULE_DAYS
        for time in DEFAULT_TIMES
    )
    db.add_all(
        RegistrationKey(
            Key=create_registration_key(),
            LaundryRoomId=room.Id,
            WardenId=None,
            IsUsed=False,
            CreatedAt=datetime.now(),
        )
        for _ in range(100)
    )
    await db.commit()
    flash(request, "AdminMessage", f"Создана постирочная {room_name}.")
    return RedirectResponse("/Admin?tab=rooms", status_code=303)


@router.post("/RenameLaundryRoom")
async def rename_laundry_room(
    request: Request,
    roomId: int = Form(...),
    name: str = Form("", max_length=80),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    room = await db.get(LaundryRoom, roomId)
    room_name = name.strip()
    if room is None:
        flash(request, "AdminMessage", "Постирочная не найдена.")
    elif not room_name:
        flash(request, "AdminMessage", "Название постирочной не может быть пустым.")
    else:
        room.Name = room_name
        await db.commit()
        flash(request, "AdminMessage", f"Название постирочной изменено на «{room_name}».")
    return RedirectResponse("/Admin?tab=rooms", status_code=303)


@router.post("/DeleteLaundryRoom")
async def delete_laundry_room(
    request: Request,
    roomId: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    room = await db.get(LaundryRoom, roomId)
    if room is None:
        flash(request, "AdminMessage", "Постирочная не найдена.")
        return RedirectResponse("/Admin?tab=rooms", status_code=303)
    if room.IsActive:
        active_room_count = await db.scalar(
            select(func.count(LaundryRoom.Id)).where(LaundryRoom.IsActive.is_(True))
        ) or 0
        if active_room_count <= 1:
            flash(
                request,
                "AdminMessage",
                "Нельзя удалить последнюю активную постирочную. Сначала добавьте другую.",
            )
            return RedirectResponse("/Admin?tab=rooms", status_code=303)

    room_name = room.Name
    await db.execute(delete(WardenKeyLaundryRoom).where(WardenKeyLaundryRoom.LaundryRoomId == room.Id))
    await db.execute(delete(WardenLaundryRoom).where(WardenLaundryRoom.LaundryRoomId == room.Id))
    await db.execute(update(User).where(User.LaundryRoomId == room.Id).values(LaundryRoomId=None))
    await db.execute(update(RegistrationKey).where(RegistrationKey.LaundryRoomId == room.Id).values(LaundryRoomId=None))
    await db.execute(update(WashSlot).where(WashSlot.LaundryRoomId == room.Id).values(LaundryRoomId=None))
    await db.execute(update(Machine).where(Machine.LaundryRoomId == room.Id).values(LaundryRoomId=None))
    await db.execute(update(Announcement).where(Announcement.LaundryRoomId == room.Id).values(LaundryRoomId=None))
    await db.execute(update(WardenAuditLog).where(WardenAuditLog.LaundryRoomId == room.Id).values(LaundryRoomId=None))
    await db.execute(update(RegistrationRequest).where(RegistrationRequest.LaundryRoomId == room.Id).values(LaundryRoomId=None))
    await db.execute(delete(RoomTransferRequest).where(or_(RoomTransferRequest.SourceRoomId == room.Id, RoomTransferRequest.TargetRoomId == room.Id)))
    await db.delete(room)
    await db.commit()
    flash(request, "AdminMessage", f"Постирочная «{room_name}» удалена.")
    return RedirectResponse("/Admin?tab=rooms", status_code=303)


@router.post("/AddWardenKey")
async def add_warden_key(request: Request, roomIds: list[int] = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    values = roomIds or []
    rooms = list(
        (
            await db.execute(
                select(LaundryRoom).where(
                    LaundryRoom.Id.in_(values),
                    LaundryRoom.IsActive.is_(True),
                )
            )
        ).scalars().all()
    )
    if not rooms or {room.Id for room in rooms} != set(values):
        flash(request, "AdminMessage", "Нужно выбрать хотя бы одну постирочную для ключа старосты.")
        return RedirectResponse("/Admin?tab=wardens", status_code=303)
    key = WardenRegistrationKey(Key=create_registration_key(), CreatedAt=datetime.now())
    db.add(key)
    await db.flush()
    for room in rooms:
        db.add(WardenKeyLaundryRoom(KeyId=key.Id, LaundryRoomId=room.Id))
    await db.commit()
    flash(request, "AdminMessage", f"Сгенерирован ключ старосты: {key.Key}")
    return RedirectResponse("/Admin?tab=wardens", status_code=303)


@router.post("/CreateWardenAccount")
async def create_warden_account(
    request: Request,
    displayName: str = Form(""),
    key: str = Form(""),
    password: str = Form(""),
    roomIds: list[int] = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    display = (displayName or "").strip()
    if not display or len(password or "") < 8:
        flash(request, "AdminMessage", "Нужно указать имя старосты и минимум 8 символов пароля.")
        return RedirectResponse("/Admin?tab=wardens", status_code=303)
    key_value = (key or "").strip().upper()
    ward_key = await db.scalar(
        select(WardenRegistrationKey).where(
            WardenRegistrationKey.Key == key_value,
            WardenRegistrationKey.IsDeleted.is_(False),
        )
    )
    if ward_key is None or ward_key.IsUsed:
        flash(request, "AdminMessage", "Ключ старосты не найден или уже использован.")
        return RedirectResponse("/Admin?tab=wardens", status_code=303)
    selected_room_ids = set(roomIds or [])
    selected_rooms = list(
        (
            await db.execute(
                select(LaundryRoom).where(
                    LaundryRoom.Id.in_(selected_room_ids),
                    LaundryRoom.IsActive.is_(True),
                )
            )
        ).scalars().all()
    )
    if not selected_rooms or {room.Id for room in selected_rooms} != selected_room_ids:
        flash(request, "AdminMessage", "Нужно назначить старосте хотя бы одну постирочную.")
        return RedirectResponse("/Admin?tab=wardens", status_code=303)
    allowed_room_ids = set(
        (
            await db.execute(
                select(WardenKeyLaundryRoom.LaundryRoomId).where(WardenKeyLaundryRoom.KeyId == ward_key.Id)
            )
        ).scalars().all()
    )
    if not selected_room_ids.issubset(allowed_room_ids):
        flash(request, "AdminMessage", "Выбранные постирочные не разрешены ключом старосты.")
        return RedirectResponse("/Admin?tab=wardens", status_code=303)
    warden = WardenAccount(
        PublicId=create_public_id(),
        DisplayName=display,
        PasswordHash=hash_password(password),
        IsBlocked=False,
        IsUniversal=False,
        CreatedAt=datetime.now(),
    )
    db.add(warden)
    await db.flush()
    for room in selected_rooms:
        db.add(WardenLaundryRoom(WardenId=warden.Id, LaundryRoomId=room.Id))
        available_keys = list(
            (
                await db.execute(
                    select(RegistrationKey)
                    .where(
                        RegistrationKey.LaundryRoomId == room.Id,
                        RegistrationKey.WardenId.is_(None),
                        RegistrationKey.IsUsed.is_(False),
                        RegistrationKey.IsDeleted.is_(False),
                        RegistrationKey.UsedByRequestId.is_(None),
                    )
                    .order_by(RegistrationKey.CreatedAt, RegistrationKey.Id)
                    .limit(100)
                )
            ).scalars().all()
        )
        for registration_key in available_keys:
            registration_key.WardenId = warden.Id
    ward_key.WardenId = warden.Id
    ward_key.IsUsed = True
    await db.commit()
    flash(request, "AdminMessage", f"Создан староста {display} с ID {warden.PublicId}.")
    return RedirectResponse("/Admin?tab=wardens", status_code=303)


@router.post("/UpdateWardenRooms")
async def update_warden_rooms(
    request: Request,
    wardenId: int = Form(...),
    roomIds: list[int] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    warden = await db.get(WardenAccount, wardenId)
    selected_room_ids = set(roomIds)
    selected_rooms = list(
        (
            await db.execute(
                select(LaundryRoom).where(
                    LaundryRoom.Id.in_(selected_room_ids),
                    LaundryRoom.IsActive.is_(True),
                )
            )
        ).scalars().all()
    ) if selected_room_ids else []
    if warden is None:
        flash(request, "AdminMessage", "Староста не найден.")
        return RedirectResponse("/Admin?tab=wardens", status_code=303)
    if not selected_rooms or {room.Id for room in selected_rooms} != selected_room_ids:
        flash(request, "AdminMessage", "Назначьте старосте хотя бы одну доступную постирочную.")
        return RedirectResponse("/Admin?tab=wardens", status_code=303)

    existing_assignments = list(
        (
            await db.execute(
                select(WardenLaundryRoom).where(WardenLaundryRoom.WardenId == warden.Id)
            )
        ).scalars().all()
    )
    previous_room_ids = {assignment.LaundryRoomId for assignment in existing_assignments}
    removed_room_ids = previous_room_ids - selected_room_ids
    for registration in (
        await db.execute(
            select(RegistrationRequest).where(
                RegistrationRequest.WardenId == warden.Id,
                RegistrationRequest.Status == "Pending",
                RegistrationRequest.LaundryRoomId.in_(removed_room_ids),
            )
        )
    ).scalars().all() if removed_room_ids else []:
        replacement_warden_id = await db.scalar(
            select(WardenLaundryRoom.WardenId)
            .join(WardenAccount, WardenAccount.Id == WardenLaundryRoom.WardenId)
            .where(
                WardenLaundryRoom.LaundryRoomId == registration.LaundryRoomId,
                WardenLaundryRoom.WardenId != warden.Id,
                WardenAccount.IsBlocked.is_(False),
            )
            .order_by(WardenLaundryRoom.WardenId)
            .limit(1)
        )
        registration.WardenId = replacement_warden_id

    for assignment in existing_assignments:
        await db.delete(assignment)
    warden.IsUniversal = False
    db.add_all(
        WardenLaundryRoom(WardenId=warden.Id, LaundryRoomId=room.Id)
        for room in selected_rooms
    )
    for room in selected_rooms:
        available_keys = list(
            (
                await db.execute(
                    select(RegistrationKey)
                    .where(
                        RegistrationKey.LaundryRoomId == room.Id,
                        RegistrationKey.WardenId.is_(None),
                        RegistrationKey.IsUsed.is_(False),
                        RegistrationKey.IsDeleted.is_(False),
                        RegistrationKey.UsedByRequestId.is_(None),
                    )
                    .order_by(RegistrationKey.CreatedAt, RegistrationKey.Id)
                    .limit(100)
                )
            ).scalars().all()
        )
        for registration_key in available_keys:
            registration_key.WardenId = warden.Id
    await db.commit()
    flash(request, "AdminMessage", f"Назначение постирочных для старосты {warden.DisplayName} обновлено.")
    return RedirectResponse("/Admin?tab=wardens", status_code=303)


@router.post("/DeleteWarden")
async def delete_warden(
    request: Request,
    wardenId: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    warden = await db.get(WardenAccount, wardenId)
    if warden is None:
        flash(request, "AdminMessage", "Староста не найден.")
        return RedirectResponse("/Admin?tab=wardens", status_code=303)
    assigned_room_ids = set(
        (
            await db.execute(
                select(WardenLaundryRoom.LaundryRoomId).where(
                    WardenLaundryRoom.WardenId == warden.Id
                )
            )
        ).scalars().all()
    )
    if assigned_room_ids:
        legacy_keys = list(
            (
                await db.execute(
                    select(WardenRegistrationKey).where(
                        WardenRegistrationKey.IsUsed.is_(True),
                        WardenRegistrationKey.WardenId.is_(None),
                    )
                )
            ).scalars().all()
        )
        wardens = list((await db.execute(select(WardenAccount))).scalars().all())
        room_ids_by_warden: dict[int, set[int]] = {}
        for assigned_warden_id, assigned_room_id in (
            await db.execute(select(WardenLaundryRoom.WardenId, WardenLaundryRoom.LaundryRoomId))
        ).all():
            room_ids_by_warden.setdefault(assigned_warden_id, set()).add(assigned_room_id)
        matching_legacy_keys = []
        for legacy_key in legacy_keys:
            key_room_ids = set(
                (
                    await db.execute(
                        select(WardenKeyLaundryRoom.LaundryRoomId).where(
                            WardenKeyLaundryRoom.KeyId == legacy_key.Id
                        )
                    )
                ).scalars().all()
            )
            if not assigned_room_ids.issubset(key_room_ids) or legacy_key.CreatedAt > warden.CreatedAt:
                continue
            possible_wardens = {
                candidate.Id
                for candidate in wardens
                if room_ids_by_warden.get(candidate.Id)
                and room_ids_by_warden[candidate.Id].issubset(key_room_ids)
                and candidate.CreatedAt >= legacy_key.CreatedAt
            }
            if possible_wardens == {warden.Id}:
                matching_legacy_keys.append(legacy_key)
        if len(matching_legacy_keys) == 1:
            matching_legacy_keys[0].WardenId = warden.Id

    replaced_key_count = 0
    used_warden_keys = list(
        (
            await db.execute(
                select(WardenRegistrationKey).where(
                    WardenRegistrationKey.WardenId == warden.Id,
                    WardenRegistrationKey.IsUsed.is_(True),
                )
            )
        ).scalars().all()
    )
    for used_key in used_warden_keys:
        allowed_room_ids = list(
            (
                await db.execute(
                    select(WardenKeyLaundryRoom.LaundryRoomId).where(
                        WardenKeyLaundryRoom.KeyId == used_key.Id
                    )
                )
            ).scalars().all()
        )
        used_key.WardenId = None
        if not allowed_room_ids:
            continue
        replacement_key = WardenRegistrationKey(
            Key=create_registration_key(),
            IsUsed=False,
            CreatedAt=datetime.now(),
        )
        db.add(replacement_key)
        await db.flush()
        db.add_all(
            WardenKeyLaundryRoom(KeyId=replacement_key.Id, LaundryRoomId=room_id)
            for room_id in allowed_room_ids
        )
        replaced_key_count += 1

    pending_registrations = list(
        (
            await db.execute(
                select(RegistrationRequest).where(
                    RegistrationRequest.WardenId == warden.Id,
                    RegistrationRequest.Status == "Pending",
                )
            )
        ).scalars().all()
    )
    for registration in pending_registrations:
        registration.WardenId = await db.scalar(
            select(WardenLaundryRoom.WardenId)
            .join(WardenAccount, WardenAccount.Id == WardenLaundryRoom.WardenId)
            .where(
                WardenLaundryRoom.LaundryRoomId == registration.LaundryRoomId,
                WardenLaundryRoom.WardenId != warden.Id,
                WardenAccount.IsBlocked.is_(False),
            )
            .order_by(WardenLaundryRoom.WardenId)
            .limit(1)
        )

    await db.execute(
        update(RegistrationKey)
        .where(RegistrationKey.WardenId == warden.Id)
        .values(WardenId=None)
    )
    await db.execute(
        update(Booking)
        .where(Booking.WardenId == warden.Id)
        .values(WardenId=None)
    )
    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.WardenId == warden.Id)
        .values(WardenId=None)
    )
    await db.execute(
        update(Announcement)
        .where(Announcement.CreatedByWardenId == warden.Id)
        .values(CreatedByWardenId=None)
    )
    await db.execute(delete(WardenAuditLog).where(WardenAuditLog.WardenId == warden.Id))
    await db.execute(delete(WardenLaundryRoom).where(WardenLaundryRoom.WardenId == warden.Id))
    warden_name = warden.DisplayName
    await db.delete(warden)
    await db.commit()
    replacement_message = (
        f" Создано новых ключей старосты: {replaced_key_count}."
        if replaced_key_count
        else ""
    )
    flash(request, "AdminMessage", f"Староста {warden_name} удалён.{replacement_message}")
    return RedirectResponse("/Admin?tab=wardens", status_code=303)


@router.post("/UpdateUserLaundryRoom")
async def update_user_laundry_room(request: Request, userId: int = Form(...), laundryRoomId: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    user = await db.get(User, userId)
    room = await db.get(LaundryRoom, laundryRoomId)
    if user is None or room is None:
        flash(request, "AdminMessage", "Пользователь или постирочная не найдены.")
        return RedirectResponse("/Admin?tab=accounts", status_code=303)
    user.LaundryRoomId = room.Id
    await db.commit()
    flash(request, "AdminMessage", f"Пользователь {user.PublicId} привязан к постирочной {room.Name}.")
    return RedirectResponse("/Admin?tab=accounts", status_code=303)


@router.post("/UpdateMachineStatus")
async def update_machine_status(
    request: Request,
    status: str = Form(""),
    laundryRoomId: int = Form(1),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    allowed = {"Свободна", "Забронирована", "Работает", "Неисправна", "На обслуживании"}
    machine = await machine_for_laundry_room(db, laundryRoomId)
    if machine is not None and status in allowed:
        if status in {"Неисправна", "На обслуживании"}:
            slots = (
                await db.execute(select(WashSlot).where(WashSlot.LaundryRoomId == laundryRoomId))
            ).scalars().all()
            for slot in slots:
                await cancel_slot_bookings(
                    db,
                    slot.Id,
                    f"Машина №1 переведена в статус «{status}». Бронирование отменено.",
                    datetime.now(),
                    offer_waitlist=False,
                )
                clear_reservation(slot)
        machine.Status = status
        machine.UpdatedAt = datetime.now()
        await db.commit()
        flash(request, "AdminMessage", f"Статус Машины №1 изменён: {status}.")
    return RedirectResponse(f"/Admin?tab=schedule&roomId={laundryRoomId}", status_code=303)


@router.post("/ApplyScheduleChanges")
async def apply_schedule_changes(
    request: Request,
    laundryRoomId: int = Form(1),
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
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)

    room = await db.get(LaundryRoom, laundryRoomId)
    redirect = f"/Admin?tab=schedule&roomId={laundryRoomId}"
    if room is None or not room.IsActive:
        flash(request, "AdminMessage", "Не удалось внести изменения: постирочная не найдена или неактивна.")
        return RedirectResponse("/Admin?tab=schedule", status_code=303)

    allowed_machine_statuses = {"Свободна", "Забронирована", "Работает", "Неисправна", "На обслуживании"}
    slot_count = len(slot_ids)
    if (
        slot_count > 1000
        or len(slot_days) != slot_count
        or len(slot_times) != slot_count
        or len(slot_statuses) != slot_count
        or len(new_days) != len(new_times)
        or len(new_days) != len(new_statuses)
        or len(new_days) > len(SCHEDULE_DAYS)
        or (machine_status and machine_status not in allowed_machine_statuses)
    ):
        flash(request, "AdminMessage", "Не удалось внести изменения: проверьте данные расписания.")
        return RedirectResponse(redirect, status_code=303)

    if len(set(slot_ids)) != slot_count:
        flash(request, "AdminMessage", "Не удалось внести изменения: слот расписания указан несколько раз.")
        return RedirectResponse(redirect, status_code=303)

    slot_changes = []
    for slot_id, day, time, status in zip(slot_ids, slot_days, slot_times, slot_statuses):
        day = day.strip()
        time = time.strip()
        status = status.strip()
        if slot_id <= 0 or day not in SCHEDULE_DAYS or not 0 < len(time) <= 30 or status not in SCHEDULE_STATUSES:
            flash(request, "AdminMessage", "Не удалось внести изменения: проверьте день, время и статус каждой стирки.")
            return RedirectResponse(redirect, status_code=303)
        slot_changes.append((slot_id, day, time, status))

    new_slots = []
    for day, time, status in zip(new_days, new_times, new_statuses):
        day = day.strip()
        time = time.strip()
        status = status.strip()
        if day not in SCHEDULE_DAYS:
            flash(request, "AdminMessage", "Не удалось внести изменения: указан неизвестный день недели.")
            return RedirectResponse(redirect, status_code=303)
        if not time:
            continue
        if len(time) > 30 or status not in SCHEDULE_STATUSES:
            flash(request, "AdminMessage", "Не удалось внести изменения: проверьте время и статус новой стирки.")
            return RedirectResponse(redirect, status_code=303)
        new_slots.append((day, time, status))

    slots_by_id = {}
    for slot_id, _, _, _ in slot_changes:
        slot = await db.get(WashSlot, slot_id)
        if slot is None or slot.LaundryRoomId != room.Id:
            flash(request, "AdminMessage", "Не удалось внести изменения: один из слотов удалён или относится к другой постирочной.")
            return RedirectResponse(redirect, status_code=303)
        slots_by_id[slot_id] = slot

    machine = await machine_for_laundry_room(db, room.Id) if machine_status else None
    if machine_status and machine is None:
        flash(request, "AdminMessage", "Не удалось внести изменения: машина не найдена.")
        return RedirectResponse(redirect, status_code=303)

    now = datetime.now()
    for slot_id, day, time, status in slot_changes:
        slot = slots_by_id[slot_id]
        if day != slot.Day or time != slot.Time or status != slot.Status:
            if day != slot.Day or time != slot.Time or status != "Свободно":
                await cancel_slot_bookings(
                    db,
                    slot_id,
                    "Расписание изменено администратором. Бронирование отменено.",
                    now,
                    offer_waitlist=False,
                )
            slot.Day = day
            slot.Time = time
            slot.Status = status
            if status != "Свободно":
                clear_reservation(slot)

    for day, time, status in new_slots:
        db.add(WashSlot(Day=day, Time=time, Status=status, LaundryRoomId=room.Id))

    if machine is not None and machine.Status != machine_status:
        if machine_status in {"Неисправна", "На обслуживании"}:
            all_slots = (
                await db.execute(select(WashSlot).where(WashSlot.LaundryRoomId == room.Id))
            ).scalars().all()
            for slot in all_slots:
                await cancel_slot_bookings(
                    db,
                    slot.Id,
                    f"Машина №1 переведена в статус «{machine_status}». Бронирование отменено.",
                    now,
                    offer_waitlist=False,
                )
                clear_reservation(slot)
        machine.Status = machine_status
        machine.UpdatedAt = now

    await db.commit()
    flash(
        request,
        "AdminMessage",
        f"Изменения внесены для постирочной «{room.Name}»: обновлено стирок — {len(slot_changes)}, добавлено — {len(new_slots)}.",
    )
    return RedirectResponse(redirect, status_code=303)


@router.post("/ResetWeeklySchedule")
async def reset_weekly_schedule(
    request: Request,
    laundryRoomId: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)

    room = await db.get(LaundryRoom, laundryRoomId)
    if room is None or not room.IsActive:
        flash(request, "AdminMessage", "Не удалось сбросить расписание: постирочная не найдена или неактивна.")
        return RedirectResponse("/Admin?tab=schedule", status_code=303)

    slots = (
        await db.execute(select(WashSlot).where(WashSlot.LaundryRoomId == room.Id))
    ).scalars().all()
    if not slots:
        flash(request, "AdminMessage", f"В постирочной «{room.Name}» пока нет стирок для сброса.")
        return RedirectResponse(f"/Admin?tab=schedule&roomId={room.Id}", status_code=303)

    now = datetime.now()
    current_week = week_start(now)
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
    flash(
        request,
        "AdminMessage",
        f"Расписание по умолчанию установлено для постирочной «{room.Name}». Занятые и забронированные стирки сохранены.",
    )
    return RedirectResponse(f"/Admin?tab=schedule&roomId={room.Id}", status_code=303)


@router.get("/History", response_class=HTMLResponse)
async def admin_history(
    request: Request,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    query = (
        select(Booking, User, WardenAccount, WashSlot)
        .outerjoin(User, Booking.UserId == User.Id)
        .outerjoin(WardenAccount, Booking.WardenId == WardenAccount.Id)
        .join(WashSlot, Booking.SlotId == WashSlot.Id)
    )
    search = (q or "").strip()
    if search:
        normalized = search.casefold()
        query = query.where(
            or_(
                User.PublicId.ilike(f"%{search}%"),
                func.lower(User.FirstName).contains(normalized),
                func.lower(User.LastName).contains(normalized),
                User.RoomNumber.contains(search),
                WardenAccount.DisplayName.ilike(f"%{search}%"),
            )
        )
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
    rows = (await db.execute(query.order_by(Booking.StartsAt.desc()).limit(1000))).all()
    return templates.TemplateResponse(
        "admin/history.html",
        {"request": request, "rows": rows, "q": search, "date_from": date_from, "date_to": date_to},
    )


@router.get("/Statistics", response_class=HTMLResponse)
async def admin_statistics(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    now = datetime.now()
    history_start = now - timedelta(days=29)
    bookings = (
        await db.execute(select(Booking).where(Booking.StartsAt >= history_start).order_by(Booking.StartsAt))
    ).scalars().all()
    daily: dict[str, int] = {}
    hourly: dict[int, int] = {}
    for booking in bookings:
        daily[booking.StartsAt.strftime("%d.%m")] = daily.get(booking.StartsAt.strftime("%d.%m"), 0) + 1
        hourly[booking.StartsAt.hour] = hourly.get(booking.StartsAt.hour, 0) + 1
    context = {
        "request": request,
        "users_count": await db.scalar(select(func.count(User.Id))) or 0,
        "bookings_count": await db.scalar(select(func.count(Booking.Id))) or 0,
        "today_count": await db.scalar(select(func.count(Booking.Id)).where(func.date(Booking.StartsAt) == now.date().isoformat())) or 0,
        "cancellations_count": await db.scalar(select(func.count(Booking.Id)).where(Booking.Status == "Отменено")) or 0,
        "machines_count": await db.scalar(
            select(func.count(func.distinct(Machine.LaundryRoomId))).where(Machine.LaundryRoomId.is_not(None))
        ) or 0,
        "daily": daily,
        "hourly": hourly,
    }
    return templates.TemplateResponse("admin/statistics.html", context)


@router.get("/Audit", response_class=HTMLResponse)
async def admin_audit(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    logs = (
        await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.CreatedAt.desc()).limit(500))
    ).scalars().all()
    return templates.TemplateResponse("admin/audit.html", {"request": request, "logs": logs})


@router.get("/Login", response_class=HTMLResponse)
async def admin_login_get(request: Request):
    return templates.TemplateResponse("admin/login.html", {"request": request, "error": None})


@router.post("/Login", response_class=HTMLResponse)
async def admin_login_post(request: Request, password: str = Form(""), db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    attempt_ip = f"admin:{client_ip}"
    login_attempt = await db.get(LoginAttempt, attempt_ip)
    now = datetime.now()
    if login_attempt is not None and login_attempt.BlockedUntil is not None and login_attempt.BlockedUntil > now:
        minutes = max(1, ceil((login_attempt.BlockedUntil - now).total_seconds() / 60))
        return templates.TemplateResponse("admin/login.html", {"request": request, "error": f"Слишком много попыток. Повторите через {minutes} мин."})

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
        if login_attempt is not None:
            await db.delete(login_attempt)
            await db.commit()
        request.session["Admin"] = "true"
        return RedirectResponse("/Admin", status_code=303)

    if login_attempt is None:
        login_attempt = LoginAttempt(ClientIp=attempt_ip, FailedAttempts=0)
        db.add(login_attempt)
    login_attempt.FailedAttempts += 1
    if login_attempt.FailedAttempts >= 5:
        login_attempt.BlockedUntil = now + timedelta(minutes=15)
        error = "Слишком много неверных попыток. Вход заблокирован на 15 минут."
    else:
        error = f"Неверный пароль. Осталось попыток: {5 - login_attempt.FailedAttempts}."
    await db.commit()
    return templates.TemplateResponse("admin/login.html", {"request": request, "error": error})


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
        has_history = await db.scalar(select(func.count(Booking.Id)).where(Booking.UserId == user.Id)) or 0
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
            await notify_booking_change(db, booking, "Администратор отменил бронирование.", "Изменение")
            await offer_next_waitlist(db, booking.SlotId, booking.WeekStart)
        slots = (await db.execute(select(WashSlot))).scalars().all()
        identity = f"{user.FirstName} {user.LastName} (комната {user.RoomNumber})"
        for slot in slots:
            if slot.ReservedByUserId == user.Id or slot.ReservedBy == identity:
                clear_reservation(slot)
        if has_history:
            user.IsBlocked = True
            flash(request, "AdminMessage", "У пользователя есть история стирок: аккаунт заблокирован, история сохранена.")
        else:
            await db.delete(user)
        await db.commit()
    return RedirectResponse("/Admin", status_code=303)


@router.post("/ToggleUserBlock")
async def toggle_user_block(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    user = await db.get(User, id)
    if user is not None:
        user.IsBlocked = not user.IsBlocked
        if user.IsBlocked:
            now = datetime.now()
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
                booking.CancelledAt = now
                await notify_booking_change(db, booking, "Ваш аккаунт заблокирован; бронирование отменено.", "Блокировка")
                await offer_next_waitlist(db, booking.SlotId, booking.WeekStart, now)
            slots = (await db.execute(select(WashSlot).where(WashSlot.ReservedByUserId == user.Id))).scalars().all()
            for slot in slots:
                clear_reservation(slot)
        await db.commit()
        flash(request, "AdminMessage", f"Аккаунт {user.PublicId}: {'заблокирован' if user.IsBlocked else 'разблокирован'}.")
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
        await cancel_slot_bookings(db, id, "Администратор отменил ваше бронирование.", datetime.now(), week_start(datetime.now()))
        clear_reservation(slot)
        await db.commit()
    return RedirectResponse("/Admin", status_code=303)


@router.post("/UpdateSchedule")
async def update_schedule(
    request: Request,
    id: int = Form(...),
    laundryRoomId: int = Form(1),
    day: str = Form(""),
    time: str = Form(""),
    status: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    slot = await db.get(WashSlot, id)
    redirect = f"/Admin?tab=schedule&roomId={laundryRoomId}"
    if slot is None or slot.LaundryRoomId != laundryRoomId:
        return RedirectResponse(redirect, status_code=303)
    day = (day or "").strip()
    time = (time or "").strip()
    status = (status or "").strip()
    if day in SCHEDULE_DAYS and 0 < len(time) <= 30 and status in SCHEDULE_STATUSES:
        if day != slot.Day or time != slot.Time or status != "Свободно":
            await cancel_slot_bookings(db, id, "Расписание изменено администратором. Бронирование отменено.", datetime.now(), offer_waitlist=False)
        slot.Day = day
        slot.Time = time
        slot.Status = status
        if status != "Свободно":
            clear_reservation(slot)
        await db.commit()
        flash(request, "AdminMessage", f"Расписание изменено: {day}, {time}, статус «{status}».")
    return RedirectResponse(redirect, status_code=303)


@router.post("/AddSchedule")
async def add_schedule(
    request: Request,
    laundryRoomId: int = Form(1),
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
    room = await db.get(LaundryRoom, laundryRoomId)
    if room is not None and room.IsActive and day in SCHEDULE_DAYS and 0 < len(time) <= 30 and status in SCHEDULE_STATUSES:
        db.add(WashSlot(Day=day, Time=time, Status=status, LaundryRoomId=room.Id))
        await db.commit()
        flash(request, "AdminMessage", f"Добавлена стирка в постирочной «{room.Name}»: {day}, {time}.")
    return RedirectResponse(f"/Admin?tab=schedule&roomId={laundryRoomId}", status_code=303)


@router.post("/DeleteSchedule")
async def delete_schedule(
    request: Request,
    id: int = Form(...),
    laundryRoomId: int = Form(1),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    slot = await db.get(WashSlot, id)
    redirect = f"/Admin?tab=schedule&roomId={laundryRoomId}"
    if slot is not None and slot.LaundryRoomId == laundryRoomId:
        history_exists = await db.scalar(select(func.count(Booking.Id)).where(Booking.SlotId == id)) or 0
        if history_exists:
            await cancel_slot_bookings(db, id, "Слот закрыт администратором. Бронирование отменено.", datetime.now(), offer_waitlist=False)
            slot.Status = "Закрыто"
            await db.commit()
            flash(request, "AdminMessage", "Слот закрыт; история бронирований сохранена.")
            return RedirectResponse(redirect, status_code=303)
        await db.delete(slot)
        await db.commit()
        flash(request, "AdminMessage", "Стирка удалена из расписания.")
    return RedirectResponse(redirect, status_code=303)


@router.post("/AddRegistrationKeys")
async def add_registration_keys(
    request: Request,
    count: int = Form(10),
    wardenId: int = Form(...),
    laundryRoomId: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    eligible_wardens = await wardens_for_laundry_room(db, laundryRoomId)
    selected_warden = await db.get(WardenAccount, wardenId)
    room = await db.get(LaundryRoom, laundryRoomId)
    if (
        room is None
        or not room.IsActive
        or selected_warden is None
        or not any(warden.Id == selected_warden.Id for warden in eligible_wardens)
    ):
        flash(request, "AdminMessage", "Выбранный староста не обслуживает эту постирочную.")
        return RedirectResponse("/Admin?tab=keys", status_code=303)
    count = max(1, min(500, count))
    existing = {k.Key for k in (await db.execute(select(RegistrationKey))).scalars().all()}
    keys = []
    while len(keys) < count:
        value = create_registration_key()
        if value in existing:
            continue
        existing.add(value)
        keys.append(
            RegistrationKey(
                Key=value,
                LaundryRoomId=room.Id,
                WardenId=selected_warden.Id,
                CreatedAt=datetime.utcnow(),
            )
        )
    db.add_all(keys)
    await db.commit()
    flash(request, "AdminMessage", f"Добавлено ключей регистрации: {count}.")
    return RedirectResponse("/Admin?tab=keys", status_code=303)


@router.post("/DeleteRegistrationKey")
async def delete_registration_key(
    request: Request,
    id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    key = await db.get(RegistrationKey, id)
    if key is None or key.IsDeleted:
        flash(request, "AdminMessage", "Ключ регистрации не найден.")
        return RedirectResponse("/Admin?tab=keys&keyType=registration", status_code=303)
    active_request_id = await db.scalar(
        select(RegistrationRequest.Id)
        .where(
            RegistrationRequest.RegistrationKeyId == key.Id,
            RegistrationRequest.Status == "Pending",
        )
        .limit(1)
    )
    if active_request_id is not None:
        flash(request, "AdminMessage", "Нельзя удалить ключ, пока по нему ожидает рассмотрения заявка.")
        return RedirectResponse("/Admin?tab=keys&keyType=registration", status_code=303)
    key.IsDeleted = True
    await db.commit()
    flash(request, "AdminMessage", "Ключ регистрации удалён.")
    return RedirectResponse("/Admin?tab=keys&keyType=registration", status_code=303)


@router.post("/ReplaceRegistrationKey")
async def replace_registration_key(
    request: Request,
    id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    key = await db.get(RegistrationKey, id)
    if key is None or key.IsDeleted:
        flash(request, "AdminMessage", "Ключ регистрации не найден.")
        return RedirectResponse("/Admin?tab=keys&keyType=registration", status_code=303)
    active_request_id = await db.scalar(
        select(RegistrationRequest.Id)
        .where(
            RegistrationRequest.RegistrationKeyId == key.Id,
            RegistrationRequest.Status == "Pending",
        )
        .limit(1)
    )
    if active_request_id is not None:
        flash(request, "AdminMessage", "Нельзя заменить ключ, пока по нему ожидает рассмотрения заявка.")
        return RedirectResponse("/Admin?tab=keys&keyType=registration", status_code=303)
    if key.LaundryRoomId is None:
        flash(request, "AdminMessage", "Нельзя заменить ключ без привязанной постирочной.")
        return RedirectResponse("/Admin?tab=keys&keyType=registration", status_code=303)

    existing_keys = set((await db.execute(select(RegistrationKey.Key))).scalars().all())
    existing_keys.update((await db.execute(select(WardenRegistrationKey.Key))).scalars().all())
    replacement_value = create_registration_key()
    while replacement_value in existing_keys:
        replacement_value = create_registration_key()
    replacement = RegistrationKey(
        Key=replacement_value,
        LaundryRoomId=key.LaundryRoomId,
        WardenId=key.WardenId,
        IsUsed=False,
        CreatedAt=datetime.utcnow(),
    )
    key.IsDeleted = True
    db.add(replacement)
    await db.commit()
    flash(request, "AdminMessage", f"Ключ регистрации заменён. Новый ключ: {replacement_value}")
    return RedirectResponse("/Admin?tab=keys&keyType=registration", status_code=303)


@router.post("/DeleteWardenRegistrationKey")
async def delete_warden_registration_key(
    request: Request,
    id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    key = await db.get(WardenRegistrationKey, id)
    if key is None or key.IsDeleted:
        flash(request, "AdminMessage", "Ключ старосты не найден.")
        return RedirectResponse("/Admin?tab=keys&keyType=warden", status_code=303)
    key.IsDeleted = True
    await db.commit()
    flash(request, "AdminMessage", "Ключ старосты удалён.")
    return RedirectResponse("/Admin?tab=keys&keyType=warden", status_code=303)


@router.post("/ReplaceWardenRegistrationKey")
async def replace_warden_registration_key(
    request: Request,
    id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    key = await db.get(WardenRegistrationKey, id)
    if key is None or key.IsDeleted:
        flash(request, "AdminMessage", "Ключ старосты не найден.")
        return RedirectResponse("/Admin?tab=keys&keyType=warden", status_code=303)
    allowed_room_ids = list(
        (
            await db.execute(
                select(WardenKeyLaundryRoom.LaundryRoomId).where(
                    WardenKeyLaundryRoom.KeyId == key.Id
                )
            )
        ).scalars().all()
    )
    if not allowed_room_ids:
        flash(request, "AdminMessage", "Нельзя заменить ключ старосты без списка доступных постирочных.")
        return RedirectResponse("/Admin?tab=keys&keyType=warden", status_code=303)

    existing_keys = set((await db.execute(select(RegistrationKey.Key))).scalars().all())
    existing_keys.update((await db.execute(select(WardenRegistrationKey.Key))).scalars().all())
    replacement_value = create_registration_key()
    while replacement_value in existing_keys:
        replacement_value = create_registration_key()
    replacement = WardenRegistrationKey(
        Key=replacement_value,
        IsUsed=False,
        CreatedAt=datetime.utcnow(),
    )
    key.IsDeleted = True
    db.add(replacement)
    await db.flush()
    db.add_all(
        WardenKeyLaundryRoom(KeyId=replacement.Id, LaundryRoomId=room_id)
        for room_id in allowed_room_ids
    )
    await db.commit()
    flash(request, "AdminMessage", f"Ключ старосты заменён. Новый ключ: {replacement_value}")
    return RedirectResponse("/Admin?tab=keys&keyType=warden", status_code=303)


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
    if req is None:
        flash(request, "AdminMessage", "Заявка уже обработана или не найдена.")
        return RedirectResponse("/Admin?tab=requests", status_code=303)

    user, error = await approve_registration_request(db, req, "Администратор")
    if error or user is None:
        if error in {
            "Аккаунт с такими данными уже существует.",
            "Логин из заявки уже занят. Отклоните заявку и попросите пользователя зарегистрироваться с другим логином.",
        }:
            reject_registration_request(db, req, "Администратор", f"Заявка отклонена: {error}")
        await db.commit()
        flash(request, "AdminMessage", error or "Не удалось создать аккаунт по заявке.")
        return RedirectResponse("/Admin?tab=requests", status_code=303)

    await db.commit()
    flash(request, "AdminMessage", f"Регистрация подтверждена. Логин: {user.Login}; ID аккаунта: {user.PublicId}.")
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
        reject_registration_request(db, req, "Администратор")
        await db.commit()
        flash(request, "AdminMessage", "Заявка отклонена. Ключ снова доступен для регистрации.")
    return RedirectResponse("/Admin?tab=requests", status_code=303)


@router.post("/ReplyToUser")
async def reply_to_user(
    request: Request,
    message: str = Form(""),
    userId: str = Form(""),
    requestId: str = Form(""),
    wardenId: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    message = (message or "").strip()
    if not message or len(message) > 2000:
        return RedirectResponse("/Admin?tab=chat", status_code=303)

    uid = int(userId) if str(userId).isdigit() else None
    rid = int(requestId) if str(requestId).isdigit() else None
    wid = int(wardenId) if str(wardenId).isdigit() else None
    user = await db.get(User, uid) if uid else None
    req = await db.get(RegistrationRequest, rid) if rid else None
    warden = await db.get(WardenAccount, wid) if wid else None
    if user is None and req is not None and req.ApprovedUserId:
        user = await db.get(User, req.ApprovedUserId)
    if req is None and user is not None:
        req = (
            await db.execute(
                select(RegistrationRequest).where(RegistrationRequest.ApprovedUserId == user.Id)
            )
        ).scalars().first()
    if user is None and req is None and warden is None:
        return RedirectResponse("/Admin?tab=chat", status_code=303)

    db.add(
        ChatMessage(
            UserId=user.Id if user else None,
            RegistrationRequestId=req.Id if req else None,
            WardenId=warden.Id if warden else None,
            SenderType="Admin",
            Message=message,
            IsRead=False,
            CreatedAt=datetime.utcnow(),
        )
    )
    await db.commit()
    return RedirectResponse("/Admin?tab=chat", status_code=303)


@router.post("/ReplyToWarden")
async def reply_to_warden(
    request: Request,
    wardenId: int = Form(...),
    message: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    text = (message or "").strip()
    warden = await db.get(WardenAccount, wardenId)
    if warden is None:
        flash(request, "AdminMessage", "Выбранный староста не найден.")
        return RedirectResponse("/Admin?tab=warden-chat", status_code=303)
    if not text or len(text) > 2000:
        flash(request, "AdminMessage", "Введите сообщение длиной не более 2000 символов.")
        return RedirectResponse("/Admin?tab=warden-chat", status_code=303)
    db.add(
        ChatMessage(
            WardenId=warden.Id,
            SenderType="Admin",
            Message=text,
            IsRead=False,
            CreatedAt=datetime.utcnow(),
        )
    )
    await db.commit()
    return RedirectResponse("/Admin?tab=warden-chat", status_code=303)


@router.post("/MarkWardenChatRead")
async def mark_warden_chat_read(
    request: Request,
    id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    message = await db.get(ChatMessage, id)
    if (
        message is not None
        and message.SenderType == "Warden"
        and message.WardenId is not None
        and message.UserId is None
        and message.RegistrationRequestId is None
    ):
        message.IsRead = True
        await db.commit()
    return RedirectResponse("/Admin?tab=warden-chat", status_code=303)


@router.post("/MarkChatRead")
async def mark_chat_read(request: Request, id: int = Form(...), db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)
    message = await db.get(ChatMessage, id)
    if message is not None and message.SenderType in {"User", "Warden"}:
        message.IsRead = True
        await db.commit()
    return RedirectResponse("/Admin?tab=chat", status_code=303)


@router.get("/UnreadChatCount")
async def unread_chat_count(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_admin(request):
        return JSONResponse({"count": 0})
    messages = (
        await db.execute(
            select(ChatMessage).where(
                ChatMessage.SenderType.in_(("User", "Warden")),
                ChatMessage.IsRead.is_(False),
                or_(
                    ChatMessage.WardenId.is_(None),
                    ChatMessage.UserId.is_not(None),
                    ChatMessage.RegistrationRequestId.is_not(None),
                ),
            )
        )
    ).scalars().all()
    return JSONResponse({"count": len(messages)})


@router.post("/AddAnnouncement")
async def add_announcement(
    request: Request,
    title: str = Form(""),
    text: str = Form(""),
    category: str = Form("Другое"),
    laundryRoomId: str = Form(""),
    image: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    if not is_admin(request):
        return RedirectResponse("/Admin/Login", status_code=303)

    title = (title or "").strip()[:120]
    text = (text or "").strip()
    categories = {"Важно", "Расписание", "Ремонт", "Новости", "Другое"}
    category = category if category in categories else "Другое"
    admin_count = await db.scalar(
        select(func.count(Announcement.Id)).where(Announcement.CreatedByWardenId.is_(None))
    ) or 0
    if admin_count >= 6:
        flash(request, "AdminMessage", "Лимит достигнут: можно разместить не более 6 объявлений администратора.")
        return RedirectResponse("/Admin?tab=announcements", status_code=303)
    if not title or not text or len(text) > 5000:
        flash(request, "AdminMessage", "Укажите заголовок и текст объявления (до 5000 символов).")
        return RedirectResponse("/Admin?tab=announcements", status_code=303)

    room_id = None
    if laundryRoomId:
        try:
            room_id = int(laundryRoomId)
        except ValueError:
            flash(request, "AdminMessage", "Выберите корректную постирочную.")
            return RedirectResponse("/Admin?tab=announcements", status_code=303)
        room = await db.get(LaundryRoom, room_id)
        if room is None or not room.IsActive:
            flash(request, "AdminMessage", "Выбранная постирочная недоступна.")
            return RedirectResponse("/Admin?tab=announcements", status_code=303)

    path, image_error = await save_announcement_image(image)
    if image_error:
        flash(request, "AdminMessage", image_error)
        return RedirectResponse("/Admin?tab=announcements", status_code=303)

    db.add(
        Announcement(
            LaundryRoomId=room_id,
            Title=title,
            Text=text,
            Category=category,
            ImagePath=path,
            CreatedAt=datetime.utcnow(),
        )
    )
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
