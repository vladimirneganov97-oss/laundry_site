from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.auth import restore_remember_me, validate_session_user
from app.config import BASE_DIR, SESSION_SECRET, UPLOAD_DIR
from app.database import SessionLocal, init_db
from app.models import RegistrationKey
from app.auth import create_registration_key
from app.routers import account, admin, chat, home
from app.services import cleanup_expired_reservations, seed_wash_slots

logger = logging.getLogger("laundry")


def fmt_local(value):
    if value is None:
        return "—"
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def sender_label(sender_type: str, admin_view: bool = False) -> str:
    if sender_type == "Admin":
        return "Вы" if admin_view else "Администратор"
    if sender_type == "System":
        return "Система"
    return "Пользователь" if admin_view else "Вы"


async def seed_registration_keys() -> None:
    async with SessionLocal() as db:
        existing = (await db.execute(select(RegistrationKey))).scalars().first()
        if existing is not None:
            return
        keys = [RegistrationKey(Key=create_registration_key(), CreatedAt=datetime.utcnow()) for _ in range(100)]
        db.add_all(keys)
        await db.commit()


async def cleanup_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with SessionLocal() as db:
                await cleanup_expired_reservations(db)
        except Exception:
            logger.exception("Ошибка при автоматической очистке просроченных записей.")
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except asyncio.TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with SessionLocal() as db:
        await seed_wash_slots(db)
    await seed_registration_keys()

    stop = asyncio.Event()
    task = asyncio.create_task(cleanup_loop(stop))
    yield
    stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="График стирки", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

app.include_router(home.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(chat.router)


@app.middleware("http")
async def security_and_session(request: Request, call_next):
    async with SessionLocal() as db:
        await restore_remember_me(request, db)
        await validate_session_user(request, db)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")


@app.get("/Account")
async def account_root():
    return RedirectResponse("/Account/Login", status_code=303)


for templates in (home.templates, account.templates, admin.templates, chat.templates):
    templates.env.filters["localtime"] = fmt_local
    templates.env.globals["sender_label"] = sender_label
