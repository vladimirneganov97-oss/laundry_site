from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import LOCAL_TZ, SCHEDULE_DAYS, as_local, now_local
from app.models import Booking, Machine, UserNotification, WaitlistEntry, WashSlot
from app.services import week_start


def booking_window_end(now: datetime | None = None) -> datetime:
    local_now = as_local(now) or now_local()
    last_bookable_date = local_now.date() + timedelta(days=3)
    return datetime.combine(last_bookable_date, time.min, tzinfo=LOCAL_TZ)


def slot_is_bookable(
    start: datetime,
    end: datetime,
    now: datetime,
    booking_window_end_at: datetime,
) -> bool:
    local_now = as_local(now) or now_local()
    return start < booking_window_end_at and end > local_now


async def machine_for_laundry_room(db: AsyncSession, laundry_room_id: int | None) -> Machine | None:
    room_id = laundry_room_id or 1
    return (
        await db.execute(select(Machine).where(Machine.LaundryRoomId == room_id))
    ).scalars().first()


def slot_start_end(slot: WashSlot, week: datetime) -> tuple[datetime, datetime] | None:
    try:
        day_index = SCHEDULE_DAYS.index(slot.Day)
        start_text, end_text = slot.Time.replace("–", "-").replace("—", "-").split("-", 1)
        start_delta = timedelta(
            hours=int(start_text.strip().split(":")[0]),
            minutes=int(start_text.strip().split(":")[1]),
        )
        end_delta = timedelta(
            hours=int(end_text.strip().split(":")[0]),
            minutes=int(end_text.strip().split(":")[1]),
        )
    except (ValueError, IndexError):
        return None

    local_week = as_local(week) or week
    start = week_start(local_week) + timedelta(days=day_index) + start_delta
    end = week_start(local_week) + timedelta(days=day_index) + end_delta
    if end_delta <= start_delta:
        end += timedelta(days=1)
    return start, end


async def add_notification(
    db: AsyncSession,
    user_id: int,
    kind: str,
    message: str,
    booking_id: int | None = None,
) -> None:
    if booking_id is not None:
        exists = await db.execute(
            select(UserNotification.Id).where(
                UserNotification.BookingId == booking_id,
                UserNotification.Kind == kind,
            )
        )
        if exists.scalar_one_or_none() is not None:
            return
    db.add(
        UserNotification(
            UserId=user_id,
            BookingId=booking_id,
            Kind=kind,
            Message=message,
            CreatedAt=now_local(),
        )
    )


async def offer_next_waitlist(
    db: AsyncSession,
    slot_id: int,
    week: datetime,
    now: datetime | None = None,
) -> WaitlistEntry | None:
    now = as_local(now) or now_local()
    slot = await db.get(WashSlot, slot_id)
    if slot is None:
        return None
    machine = await machine_for_laundry_room(db, slot.LaundryRoomId)
    if machine is not None and machine.Status in {"Неисправна", "На обслуживании"}:
        return None
    entry = (
        await db.execute(
            select(WaitlistEntry)
            .where(
                WaitlistEntry.SlotId == slot_id,
                WaitlistEntry.WeekStart == week_start(week),
                WaitlistEntry.Status == "Ожидает",
            )
            .order_by(WaitlistEntry.CreatedAt, WaitlistEntry.Id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if entry is None:
        return None

    entry.Status = "Предложено"
    entry.OfferedUntil = now + timedelta(minutes=10)
    time_label = ""
    time_label = f"{slot.Day}, {slot.Time}"
    await add_notification(
        db,
        entry.UserId,
        "Очередь",
        f"Место освободилось ({time_label}). Вы первые в очереди; подтвердите бронирование в течение 10 минут.",
    )
    return entry


async def process_booking_notifications(db: AsyncSession, now: datetime | None = None) -> None:
    now = as_local(now) or now_local()
    bookings = (
        await db.execute(
            select(Booking).where(Booking.Status.in_(("Забронировано", "В работе")))
        )
    ).scalars().all()
    active_rooms: set[int] = set()
    for booking in bookings:
        slot = await db.get(WashSlot, booking.SlotId)
        if slot is None:
            continue
        time_label = f"Машина №1, {slot.Day}, {slot.Time}"
        starts_at = as_local(booking.StartsAt) or booking.StartsAt
        ends_at = as_local(booking.EndsAt) or booking.EndsAt
        if booking.UserId is not None and starts_at - timedelta(minutes=15) <= now < starts_at:
            await add_notification(
                db, booking.UserId, "Напоминание", f"Через 15 минут начнётся стирка: {time_label}.", booking.Id
            )
        if starts_at <= now < ends_at:
            booking.Status = "В работе"
            active_rooms.add(slot.LaundryRoomId or 1)
            if booking.UserId is not None:
                await add_notification(db, booking.UserId, "Начало", f"Стирка началась: {time_label}.", booking.Id)
        if ends_at <= now:
            booking.Status = "Завершено"
            if booking.UserId is not None:
                await add_notification(db, booking.UserId, "Окончание", f"Стирка завершена: {time_label}.", booking.Id)

    machines = (await db.execute(select(Machine))).scalars().all()
    for machine in machines:
        if machine.Status not in {"Неисправна", "На обслуживании"}:
            next_status = "Работает" if (machine.LaundryRoomId or 1) in active_rooms else "Свободна"
            if machine.Status != next_status:
                machine.Status = next_status
                machine.UpdatedAt = now

    expired_offers = (
        await db.execute(
            select(WaitlistEntry).where(
                WaitlistEntry.Status == "Предложено",
                WaitlistEntry.OfferedUntil <= now,
            )
        )
    ).scalars().all()
    for entry in expired_offers:
        entry.Status = "Истекло"
        await offer_next_waitlist(db, entry.SlotId, entry.WeekStart, now)

    await db.commit()


async def notify_booking_change(
    db: AsyncSession,
    booking: Booking,
    message: str,
    kind: str,
) -> None:
    if booking.UserId is not None:
        await add_notification(db, booking.UserId, kind, message, booking.Id)