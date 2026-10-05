from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_public_id
from app.models import (
    ChatMessage,
    RegistrationRequest,
    User,
    WardenAccount,
    WardenLaundryRoom,
)


async def wardens_for_laundry_room(db: AsyncSession, laundry_room_id: int) -> list[WardenAccount]:
    assigned = list(
        (
            await db.execute(
                select(WardenAccount)
                .join(WardenLaundryRoom, WardenLaundryRoom.WardenId == WardenAccount.Id)
                .where(
                    WardenLaundryRoom.LaundryRoomId == laundry_room_id,
                    WardenAccount.IsBlocked.is_(False),
                    WardenAccount.IsUniversal.is_(False),
                )
                .order_by(WardenAccount.DisplayName)
            )
        ).scalars().all()
    )
    if assigned:
        return assigned
    return list(
        (
            await db.execute(
                select(WardenAccount)
                .where(
                    WardenAccount.IsUniversal.is_(True),
                    WardenAccount.IsBlocked.is_(False),
                )
                .order_by(WardenAccount.DisplayName)
            )
        ).scalars().all()
    )


async def approve_registration(
    db: AsyncSession, registration: RegistrationRequest, reviewer: str
) -> tuple[User | None, str | None]:
    if registration.RegistrationKey is None:
        return None, "Ключ регистрации не найден."

    login_exists = await db.scalar(
        select(User.Id).where(func.lower(User.Login) == registration.Login.lower()).limit(1)
    )
    if login_exists is not None:
        return None, "Логин из заявки уже занят. Отклоните заявку и попросите пользователя зарегистрироваться с другим логином."

    existing = (
        await db.execute(
            select(User).where(
                func.lower(User.FirstName) == registration.FirstName.lower(),
                func.lower(User.LastName) == registration.LastName.lower(),
                User.RoomNumber == registration.RoomNumber,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None, "Аккаунт с такими данными уже существует."

    if registration.LaundryRoomId is None:
        return None, "В заявке не указана постирочная."

    while True:
        public_id = create_public_id()
        if await db.scalar(select(User.Id).where(User.PublicId == public_id)) is None:
            break

    user = User(
        PublicId=public_id,
        Login=registration.Login,
        FirstName=registration.FirstName,
        LastName=registration.LastName,
        RoomNumber=registration.RoomNumber,
        LaundryRoomId=registration.LaundryRoomId,
        PasswordHash=registration.PasswordHash,
        WeeklyBookingLimit=2,
        CreatedAt=datetime.utcnow(),
    )
    db.add(user)
    await db.flush()
    registration.Status = "Approved"
    registration.ReviewedAt = datetime.utcnow()
    registration.ApprovedUserId = user.Id
    registration.RegistrationKey.IsUsed = True
    registration.RegistrationKey.UsedAt = datetime.utcnow()
    await db.flush()

    pending_chat = (
        await db.execute(
            select(ChatMessage).where(
                ChatMessage.RegistrationRequestId == registration.Id,
                ChatMessage.UserId.is_(None),
            )
        )
    ).scalars().all()
    for message in pending_chat:
        message.UserId = user.Id
    db.add(
        ChatMessage(
            UserId=user.Id,
            RegistrationRequestId=registration.Id,
            SenderType="System",
            Message=(
                f"{reviewer} подтвердил регистрацию. Ваш логин: {user.Login}; ID аккаунта: {user.PublicId}. "
                "Теперь вы можете войти по логину."
            ),
            IsRead=False,
            CreatedAt=datetime.utcnow(),
        )
    )
    return user, None


def reject_registration(
    db: AsyncSession,
    registration: RegistrationRequest,
    reviewer: str,
    reason: str | None = None,
) -> None:
    registration.Status = "Rejected"
    registration.ReviewedAt = datetime.utcnow()
    if registration.RegistrationKey is not None:
        registration.RegistrationKey.IsUsed = False
        registration.RegistrationKey.UsedByRequestId = None
        registration.RegistrationKey.UsedAt = None
    rejection_text = reason or f"{reviewer} отклонил заявку. Ключ снова доступен для регистрации."
    db.add(
        ChatMessage(
            UserId=registration.ApprovedUserId,
            RegistrationRequestId=registration.Id,
            SenderType="System",
            Message=rejection_text,
            IsRead=False,
            CreatedAt=datetime.utcnow(),
        )
    )
