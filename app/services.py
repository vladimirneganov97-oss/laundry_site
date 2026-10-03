from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TIMES, SCHEDULE_DAYS
from app.models import WashSlot


def week_start(date: datetime) -> datetime:
    d = date.date()
    # Monday = 0
    diff = d.weekday()
    return datetime.combine(d - timedelta(days=diff), datetime.min.time())


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

    return datetime.combine(end_date.date(), datetime.min.time()) + end


def clear_reservation(slot: WashSlot) -> None:
    slot.ReservedBy = None
    slot.ReservedByUserId = None
    slot.ReservationWeekStart = None
    slot.ReservationExpiresAt = None


async def cleanup_expired_reservations(db: AsyncSession, server_now: datetime | None = None) -> int:
    now = server_now or datetime.now()
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

        if slot.ReservationExpiresAt is not None and slot.ReservationExpiresAt <= now:
            clear_reservation(slot)
            changed = True
            expired_count += 1

    if changed:
        await db.commit()

    return expired_count


async def seed_wash_slots(db: AsyncSession) -> None:
    result = await db.execute(select(WashSlot))
    existing = list(result.scalars().all())

    if existing:
        is_old = len(existing) == 42 or any(
            x.Time in ("11:00–14:00", "02:00–03:00") for x in existing
        )
        if not is_old:
            return
        for slot in existing:
            await db.delete(slot)
        await db.commit()

    slots = [
        WashSlot(Day=day, Time=time, Status="Свободно")
        for day in SCHEDULE_DAYS
        for time in DEFAULT_TIMES
    ]
    db.add_all(slots)
    await db.commit()
