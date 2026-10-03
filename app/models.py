from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "Users"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    PublicId: Mapped[str] = mapped_column(String, nullable=False, default="", unique=True, index=True)
    FirstName: Mapped[str] = mapped_column(String, nullable=False, default="")
    LastName: Mapped[str] = mapped_column(String, nullable=False, default="")
    RoomNumber: Mapped[str] = mapped_column(String, nullable=False, default="")
    PasswordHash: Mapped[str] = mapped_column(String, nullable=False, default="")
    WeeklyBookingLimit: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class WashSlot(Base):
    __tablename__ = "WashSlots"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    Day: Mapped[str] = mapped_column(String, nullable=False, default="")
    Time: Mapped[str] = mapped_column(String, nullable=False, default="")
    Status: Mapped[str] = mapped_column(String, nullable=False, default="Свободно")
    ReservedBy: Mapped[str | None] = mapped_column(String, nullable=True)
    ReservedByUserId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ReservationWeekStart: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ReservationExpiresAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RegistrationKey(Base):
    __tablename__ = "RegistrationKeys"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    Key: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    IsUsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    UsedByRequestId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    UsedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RegistrationRequest(Base):
    __tablename__ = "RegistrationRequests"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    FirstName: Mapped[str] = mapped_column(String, nullable=False)
    LastName: Mapped[str] = mapped_column(String, nullable=False)
    RoomNumber: Mapped[str] = mapped_column(String, nullable=False)
    PasswordHash: Mapped[str] = mapped_column(String, nullable=False)
    RegistrationKeyId: Mapped[int] = mapped_column(
        Integer, ForeignKey("RegistrationKeys.Id", ondelete="RESTRICT"), nullable=False
    )
    Status: Mapped[str] = mapped_column(String, nullable=False, default="Pending")
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    ReviewedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ApprovedUserId: Mapped[int | None] = mapped_column(Integer, nullable=True)

    RegistrationKey: Mapped[RegistrationKey] = relationship("RegistrationKey")


class ChatMessage(Base):
    __tablename__ = "ChatMessages"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    UserId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    RegistrationRequestId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    SenderType: Mapped[str] = mapped_column(String, nullable=False, default="User")
    Message: Mapped[str] = mapped_column(Text, nullable=False)
    IsRead: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    User: Mapped[User | None] = relationship(
        "User",
        primaryjoin="foreign(ChatMessage.UserId)==User.Id",
        viewonly=True,
    )
    RegistrationRequest: Mapped[RegistrationRequest | None] = relationship(
        "RegistrationRequest",
        primaryjoin="foreign(ChatMessage.RegistrationRequestId)==RegistrationRequest.Id",
        viewonly=True,
    )


class PersistentLogin(Base):
    __tablename__ = "PersistentLogins"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    UserId: Mapped[int] = mapped_column(Integer, nullable=False)
    Token: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    ExpiresAt: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Announcement(Base):
    __tablename__ = "Announcements"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    Text: Mapped[str] = mapped_column(Text, nullable=False)
    ImagePath: Mapped[str | None] = mapped_column(String, nullable=True)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
