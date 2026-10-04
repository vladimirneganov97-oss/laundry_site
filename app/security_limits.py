from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RateLimitState


async def check_rate_limit(
    db: AsyncSession,
    key: str,
    max_requests: int,
    window: timedelta,
) -> tuple[bool, int]:
    now = datetime.now()
    state = await db.get(RateLimitState, key)
    if state is None:
        state = RateLimitState(RateKey=key, WindowStartedAt=now, RequestCount=0)
        db.add(state)

    if state.BlockedUntil is not None and state.BlockedUntil > now:
        return False, max(1, ceil((state.BlockedUntil - now).total_seconds()))

    if state.BlockedUntil is not None or now - state.WindowStartedAt >= window:
        state.WindowStartedAt = now
        state.RequestCount = 0
        state.BlockedUntil = None

    if state.RequestCount >= max_requests:
        state.BlockedUntil = now + window
        await db.commit()
        return False, ceil(window.total_seconds())

    state.RequestCount += 1
    await db.commit()
    return True, 0