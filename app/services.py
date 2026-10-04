from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TIMES, LOCAL_TZ, SCHEDULE_DAYS, as_local, now_local
from app.models import (
    AdminAuditLog,
    Booking,
    LaundryRoom,
    Machine,
    PersistentLogin,
    UserNotification,
    WaitlistEntry,
    WardenAuditLog,
    WashSlot,
)


def week_start(date: datetime) -> datetime:
    local_date = as_local(date) or date
    d = local_date.date()
    diff = d.weekday()
    return datetime.combine(d - timedelta(days=diff), datetime.min.time(), tzinfo=LOCAL_TZ)


def get_slot_end_server_time(slot: WashSlot, server_now: datetime) -> datetime | None:
    if not slot.Day or not slot.Time:
        return None

    try:
        day_index = SCHEDULE_DAYS.index(slot.Day)
    except ValueError:
        return None

    parts = slot.Time.replace("–", "-").replace("—", "-").split("-")
    parts = [p.strip() for p in parts]
    if len(parts) != 2:
        return None

    try:
        start_h, start_m = [int(x) for x in parts[0].split(":")]
        end_h, end_m = [int(x) for x in parts[1].split(":")]
        start = timedelta(hours=start_h, minutes=start_m)
        end = timedelta(hours=end_h, minutes=end_m)
    except ValueError:
        return None

    slot_date = week_start(server_now) + timedelta(days=day_index)
    end_date = slot_date
    if end <= start:
        end_date = end_date + timedelta(days=1)

    return datetime.combine(end_date.date(), datetime.min.time(), tzinfo=LOCAL_TZ) + end


def clear_reservation(slot: WashSlot) -> None:
    slot.ReservedBy = None
    slot.ReservedByUserId = None
    slot.ReservationWeekStart = None
    slot.ReservationExpiresAt = None


async def cleanup_expired_reservations(db: AsyncSession, server_now: datetime | None = None) -> int:
    now = as_local(server_now) or now_local()
    current_week = week_start(now)

    result = await db.execute(
        select(WashSlot).where(
            (WashSlot.ReservedByUserId.is_not(None)) | (WashSlot.ReservedBy.is_not(None))
        )
    )
    reserved_slots = result.scalars().all()
    changed = False
    expired_count = 0

    for slot in reserved_slots:
        week = slot.ReservationWeekStart
        if week is None or week.date() != current_week.date():
            clear_reservation(slot)
            changed = True
            continue

        if slot.ReservationExpiresAt is None:
            expires_at = get_slot_end_server_time(slot, now)
            if expires_at is not None:
                slot.ReservationExpiresAt = expires_at
                changed = True

        if (
            slot.ReservationExpiresAt is not None
            and (as_local(slot.ReservationExpiresAt) or slot.ReservationExpiresAt) <= now
        ):
            clear_reservation(slot)
            changed = True
            expired_count += 1

    if changed:
        await db.commit()

    return expired_count


async def cleanup_old_data(db: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    current_time = as_local(now) or now_local()
    cutoff = current_time - timedelta(days=30)

    await db.execute(
        update(UserNotification)
        .where(
            UserNotification.BookingId.in_(
                select(Booking.Id).where(Booking.EndsAt <= cutoff)
            )
        )
        .values(BookingId=None)
    )
    deleted = {
        "bookings": (
            await db.execute(delete(Booking).where(Booking.EndsAt <= cutoff))
        ).rowcount
        or 0,
        "audit_logs": (
            await db.execute(delete(AdminAuditLog).where(AdminAuditLog.CreatedAt <= cutoff))
        ).rowcount
        or 0,
        "warden_audit_logs": (
            await db.execute(delete(WardenAuditLog).where(WardenAuditLog.CreatedAt <= cutoff))
        ).rowcount
        or 0,
        "notifications": (
            await db.execute(delete(UserNotification).where(UserNotification.CreatedAt <= cutoff))
        ).rowcount
        or 0,
        "waitlist_entries": (
            await db.execute(delete(WaitlistEntry).where(WaitlistEntry.CreatedAt <= cutoff))
        ).rowcount
        or 0,
        "expired_logins": (
            await db.execute(delete(PersistentLogin).where(PersistentLogin.ExpiresAt <= current_time))
        ).rowcount
        or 0,
    }
    if any(deleted.values()):
        await db.commit()
    return deleted


async def seed_wash_slots(db: AsyncSession) -> None:
    result = await db.execute(select(WashSlot))
    existing = list(result.scalars().all())

    is_old = bool(existing) and (
        len(existing) == 42
        or any(x.Time in ("11:00–14:00", "02:00–03:00") for x in existing)
    )
    if is_old:
        for slot in existing:
            await db.delete(slot)
        await db.flush()

    rooms = (await db.execute(select(LaundryRoom))).scalars().all()
    for room in rooms:
        machine_id = await db.scalar(
            select(Machine.Id).where(Machine.LaundryRoomId == room.Id)
        )
        if machine_id is None:
            db.add(
                Machine(
                    LaundryRoomId=room.Id,
                    Name="Машина №1",
                    Status="Свободна",
                    UpdatedAt=now_local(),
                )
            )

        has_slots = await db.scalar(
            select(WashSlot.Id).where(WashSlot.LaundryRoomId == room.Id).limit(1)
        )
        if has_slots is None:
            db.add_all(
                WashSlot(Day=day, Time=time, Status="Свободно", LaundryRoomId=room.Id)
                for day in SCHEDULE_DAYS
                for time in DEFAULT_TIMES
            )
    await db.commit()
