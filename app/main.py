from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import restore_remember_me, validate_session_user, validate_session_warden
from app.config import BASE_DIR, LOCAL_TZ, SESSION_SECRET, UPLOAD_DIR, now_local
from app.database import SessionLocal, init_db
from app.models import AdminAuditLog, WardenAuditLog
from app.routers import account, admin, chat, home, warden
from app.booking_services import process_booking_notifications
from app.security_limits import check_rate_limit
from app.services import cleanup_expired_reservations, cleanup_old_data, seed_wash_slots

logger = logging.getLogger("laundry")


def fmt_local(value):
    if value is None:
        return "—"
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def fmt_utc_local(value):
    if value is None:
        return "—"
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def sender_label(sender_type: str, admin_view: bool = False) -> str:
    if sender_type == "Admin":
        return "Вы" if admin_view else "Администратор"
    if sender_type == "System":
        return "Система"
    if sender_type == "Warden":
        return "Староста"
    return "Пользователь" if admin_view else "Вы"


async def cleanup_loop(stop: asyncio.Event) -> None:
    next_data_cleanup = datetime.min.replace(tzinfo=LOCAL_TZ)
    while not stop.is_set():
        try:
            async with SessionLocal() as db:
                await cleanup_expired_reservations(db)
                await process_booking_notifications(db)
                now = now_local()
                if now >= next_data_cleanup:
                    next_data_cleanup = now + timedelta(days=1)
                    try:
                        deleted = await cleanup_old_data(db, now)
                    except Exception:
                        next_data_cleanup = now + timedelta(hours=1)
                        logger.exception("Ошибка при очистке старых данных.")
                    else:
                        removed_count = sum(deleted.values())
                        if removed_count:
                            logger.info("Автоматически удалены старые записи: %s", deleted)
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
app.include_router(warden.router)


@app.middleware("http")
async def security_and_session(request: Request, call_next):
    async with SessionLocal() as db:
        await restore_remember_me(request, db)
        await validate_session_user(request, db)
        await validate_session_warden(request, db)

    was_admin = request.session.get("Admin") == "true"
    warden_id = request.session.get("WardenId")
    if was_admin and request.method == "POST" and request.url.path.startswith("/Admin/") and request.url.path != "/Admin/Login":
        ip = request.client.host if request.client else "unknown"
        async with SessionLocal() as db:
            allowed, retry_after = await check_rate_limit(db, f"admin:{ip}", 30, timedelta(seconds=10))
        if not allowed:
            return PlainTextResponse(
                f"Слишком много административных запросов. Повторите через {retry_after} сек.",
                status_code=429,
            )
    response = await call_next(request)

    path = request.url.path
    is_login = path == "/Admin/Login" and request.method == "POST" and response.status_code == 303
    is_logout = path == "/Admin/Logout" and was_admin and response.status_code < 400
    is_admin_action = was_admin and path.startswith("/Admin/") and request.method == "POST" and response.status_code < 400
    if is_login or is_logout or is_admin_action:
        action = "Вход администратора" if is_login else "Выход администратора" if is_logout else path.removeprefix("/Admin/")
        async with SessionLocal() as db:
            db.add(
                AdminAuditLog(
                    Action=action,
                    Details=f"{request.method} {path}, HTTP {response.status_code}",
                    CreatedAt=now_local(),
                )
            )
            await db.commit()

    is_warden_login = path == "/Warden/Login" and request.method == "POST" and response.status_code == 303
    is_warden_logout = path == "/Warden/Logout" and warden_id is not None and response.status_code < 400
    is_warden_action = (
        warden_id is not None
        and path.startswith("/Warden/")
        and path not in {"/Warden/Login", "/Warden/Logout"}
        and request.method == "POST"
        and response.status_code < 400
    )
    if is_warden_login or is_warden_logout or is_warden_action:
        action = (
            "Вход старосты"
            if is_warden_login
            else "Выход старосты"
            if is_warden_logout
            else path.removeprefix("/Warden/")
        )
        async with SessionLocal() as db:
            db.add(
                WardenAuditLog(
                    WardenId=int(warden_id or request.session.get("WardenId") or 0),
                    Action=action,
                    Details=f"{request.method} {path}, HTTP {response.status_code}",
                    CreatedAt=now_local(),
                )
            )
            await db.commit()

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")


@app.get("/Account")
async def account_root():
    return RedirectResponse("/Account/Login", status_code=303)


for templates in (home.templates, account.templates, admin.templates, chat.templates, warden.templates):
    templates.env.filters["localtime"] = fmt_local
    templates.env.filters["utc_localtime"] = fmt_utc_local
    templates.env.globals["sender_label"] = sender_label
