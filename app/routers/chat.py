from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_session_user_id
from app.config import BASE_DIR
from app.database import get_db
from app.models import ChatMessage, RegistrationKey, RegistrationRequest

router = APIRouter(prefix="/Chat", tags=["chat"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def normalize_key(key: str | None) -> str:
    return (key or "").strip().upper()


async def mark_admin_messages_read(db: AsyncSession, messages: list[ChatMessage]) -> None:
    unread = [m for m in messages if m.SenderType != "User" and not m.IsRead]
    if not unread:
        return
    for message in unread:
        message.IsRead = True
    await db.commit()


async def find_request_by_key(db: AsyncSession, key: str) -> RegistrationRequest | None:
    return (
        await db.execute(
            select(RegistrationRequest)
            .options(selectinload(RegistrationRequest.RegistrationKey))
            .join(RegistrationKey)
            .where(RegistrationKey.Key == key)
        )
    ).scalar_one_or_none()


@router.get("/Index", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def chat_index(request: Request, key: str | None = None, db: AsyncSession = Depends(get_db)):
    user_id = get_session_user_id(request)

    if user_id is not None:
        approved_ids = (
            await db.execute(
                select(RegistrationRequest.Id).where(RegistrationRequest.ApprovedUserId == user_id)
            )
        ).scalars().all()
        conditions = [ChatMessage.UserId == user_id]
        if approved_ids:
            conditions.append(
                (ChatMessage.UserId.is_(None)) & (ChatMessage.RegistrationRequestId.in_(approved_ids))
            )
        messages = (
            await db.execute(select(ChatMessage).where(or_(*conditions)).order_by(ChatMessage.CreatedAt))
        ).scalars().all()
        await mark_admin_messages_read(db, list(messages))
        return templates.TemplateResponse(
            "chat/index.html",
            {
                "request": request,
                "messages": messages,
                "chat_key": "",
                "need_key": False,
                "error": None,
                "reg_request": None,
            },
        )

    key = normalize_key(key or request.session.get("PendingRegistrationKey"))
    if not key:
        return templates.TemplateResponse(
            "chat/index.html",
            {
                "request": request,
                "messages": [],
                "chat_key": "",
                "need_key": True,
                "error": None,
                "reg_request": None,
            },
        )

    reg_request = await find_request_by_key(db, key)
    if reg_request is None:
        return templates.TemplateResponse(
            "chat/index.html",
            {
                "request": request,
                "messages": [],
                "chat_key": key,
                "need_key": False,
                "error": "Заявка с таким ключом не найдена.",
                "reg_request": None,
            },
        )

    messages = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.RegistrationRequestId == reg_request.Id)
            .order_by(ChatMessage.CreatedAt)
        )
    ).scalars().all()
    await mark_admin_messages_read(db, list(messages))
    return templates.TemplateResponse(
        "chat/index.html",
        {
            "request": request,
            "messages": messages,
            "chat_key": key,
            "need_key": False,
            "error": None,
            "reg_request": reg_request,
        },
    )


@router.post("/Send")
async def chat_send(
    request: Request,
    message: str = Form(""),
    key: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    message = (message or "").strip()
    if not message or len(message) > 2000:
        return RedirectResponse(f"/Chat/Index{('?key=' + key) if key else ''}", status_code=303)

    user_id = get_session_user_id(request)
    request_id = None

    if user_id is not None:
        request_id = (
            await db.execute(
                select(RegistrationRequest.Id)
                .where(RegistrationRequest.ApprovedUserId == user_id)
                .order_by(RegistrationRequest.ReviewedAt.desc())
            )
        ).scalars().first()
    else:
        key = normalize_key(key or request.session.get("PendingRegistrationKey"))
        if not key:
            return RedirectResponse("/Chat/Index", status_code=303)
        reg_request = await find_request_by_key(db, key)
        if reg_request is None:
            return RedirectResponse(f"/Chat/Index?key={key}", status_code=303)
        request_id = reg_request.Id
        user_id = reg_request.ApprovedUserId

    db.add(
        ChatMessage(
            UserId=user_id,
            RegistrationRequestId=request_id,
            SenderType="User",
            Message=message,
            IsRead=False,
            CreatedAt=datetime.utcnow(),
        )
    )
    await db.commit()
    redirect_key = None if get_session_user_id(request) else key
    url = "/Chat/Index" if not redirect_key else f"/Chat/Index?key={redirect_key}"
    return RedirectResponse(url, status_code=303)


@router.get("/UnreadCount")
async def unread_count(request: Request, key: str | None = None, db: AsyncSession = Depends(get_db)):
    user_id = get_session_user_id(request)
    if user_id is not None:
        count = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.UserId == user_id,
                    ChatMessage.SenderType != "User",
                    ChatMessage.IsRead.is_(False),
                )
            )
        ).scalars().all()
        return JSONResponse({"count": len(count)})

    key = normalize_key(key or request.session.get("PendingRegistrationKey"))
    if not key:
        return JSONResponse({"count": 0})

    reg_request = await find_request_by_key(db, key)
    if reg_request is None:
        return JSONResponse({"count": 0})

    count = (
        await db.execute(
            select(ChatMessage).where(
                ChatMessage.RegistrationRequestId == reg_request.Id,
                ChatMessage.SenderType != "User",
                ChatMessage.IsRead.is_(False),
            )
        )
    ).scalars().all()
    return JSONResponse({"count": len(count)})
