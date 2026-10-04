from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class LaundryRoom(Base):
    __tablename__ = "LaundryRooms"
    __table_args__ = (UniqueConstraint("RoomNumber", name="uq_laundry_room_number"),)

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    Floor: Mapped[int] = mapped_column(Integer, nullable=False)
    RoomNumber: Mapped[int] = mapped_column(Integer, nullable=False)
    Name: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    IsActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class WardenAccount(Base):
    __tablename__ = "WardenAccounts"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    PublicId: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    DisplayName: Mapped[str] = mapped_column(String(80), nullable=False)
    PasswordHash: Mapped[str] = mapped_column(String, nullable=False)
    IsBlocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    IsUniversal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class WardenRegistrationKey(Base):
    __tablename__ = "WardenRegistrationKeys"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    Key: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    WardenId: Mapped[int | None] = mapped_column(ForeignKey("WardenAccounts.Id"), nullable=True, index=True)
    IsUsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    IsDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class WardenKeyLaundryRoom(Base):
    __tablename__ = "WardenKeyLaundryRooms"
    __table_args__ = (UniqueConstraint("KeyId", "LaundryRoomId", name="uq_warden_key_room"),)

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    KeyId: Mapped[int] = mapped_column(ForeignKey("WardenRegistrationKeys.Id"), nullable=False, index=True)
    LaundryRoomId: Mapped[int] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=False, index=True)


class WardenLaundryRoom(Base):
    __tablename__ = "WardenLaundryRooms"
    __table_args__ = (UniqueConstraint("WardenId", "LaundryRoomId", name="uq_warden_room"),)

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    WardenId: Mapped[int] = mapped_column(ForeignKey("WardenAccounts.Id"), nullable=False, index=True)
    LaundryRoomId: Mapped[int] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=False, index=True)


class RoomTransferRequest(Base):
    __tablename__ = "RoomTransferRequests"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    UserId: Mapped[int] = mapped_column(ForeignKey("Users.Id"), nullable=False, index=True)
    SourceRoomId: Mapped[int] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=False)
    TargetRoomId: Mapped[int] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=False)
    SourceApproved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    TargetApproved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    Status: Mapped[str] = mapped_column(String, nullable=False, default="Ожидает")
    RequestedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    CompletedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class User(Base):
    __tablename__ = "Users"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    PublicId: Mapped[str] = mapped_column(String, nullable=False, default="", unique=True, index=True)
    FirstName: Mapped[str] = mapped_column(String, nullable=False, default="")
    LastName: Mapped[str] = mapped_column(String, nullable=False, default="")
    RoomNumber: Mapped[str] = mapped_column(String, nullable=False, default="")
    PasswordHash: Mapped[str] = mapped_column(String, nullable=False, default="")
    WeeklyBookingLimit: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    LaundryRoomId: Mapped[int | None] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=True, index=True)
    IsBlocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class WardenAuditLog(Base):
    __tablename__ = "WardenAuditLogs"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    WardenId: Mapped[int] = mapped_column(ForeignKey("WardenAccounts.Id"), nullable=False, index=True)
    LaundryRoomId: Mapped[int | None] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=True, index=True)
    Action: Mapped[str] = mapped_column(String, nullable=False)
    Details: Mapped[str] = mapped_column(Text, nullable=False)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class WashSlot(Base):
    __tablename__ = "WashSlots"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    LaundryRoomId: Mapped[int | None] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=True, index=True)
    Day: Mapped[str] = mapped_column(String, nullable=False, default="")
    Time: Mapped[str] = mapped_column(String, nullable=False, default="")
    Status: Mapped[str] = mapped_column(String, nullable=False, default="Свободно")
    ReservedBy: Mapped[str | None] = mapped_column(String, nullable=True)
    ReservedByUserId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ReservationWeekStart: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ReservationExpiresAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Machine(Base):
    __tablename__ = "Machines"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True)
    LaundryRoomId: Mapped[int | None] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=True, unique=True)
    Name: Mapped[str] = mapped_column(String, nullable=False, default="Машина №1")
    Status: Mapped[str] = mapped_column(String, nullable=False, default="Свободна")
    UpdatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class Booking(Base):
    __tablename__ = "Bookings"
    __table_args__ = (
        Index(
            "uq_booking_slot_week_active",
            "SlotId",
            "WeekStart",
            unique=True,
            sqlite_where=text('"Status" IN (\'Забронировано\', \'В работе\')'),
        ),
    )

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    SlotId: Mapped[int] = mapped_column(ForeignKey("WashSlots.Id"), nullable=False, index=True)
    UserId: Mapped[int | None] = mapped_column(ForeignKey("Users.Id"), nullable=True, index=True)
    WardenId: Mapped[int | None] = mapped_column(ForeignKey("WardenAccounts.Id"), nullable=True, index=True)
    WeekStart: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    StartsAt: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    EndsAt: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    Status: Mapped[str] = mapped_column(String, nullable=False, default="Забронировано")
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    CancelledAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WaitlistEntry(Base):
    __tablename__ = "WaitlistEntries"
    __table_args__ = (UniqueConstraint("SlotId", "WeekStart", "UserId", name="uq_waitlist_slot_user_week"),)

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    SlotId: Mapped[int] = mapped_column(ForeignKey("WashSlots.Id"), nullable=False, index=True)
    UserId: Mapped[int] = mapped_column(ForeignKey("Users.Id"), nullable=False, index=True)
    WeekStart: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    Status: Mapped[str] = mapped_column(String, nullable=False, default="Ожидает")
    OfferedUntil: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class UserNotification(Base):
    __tablename__ = "UserNotifications"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    UserId: Mapped[int] = mapped_column(ForeignKey("Users.Id"), nullable=False, index=True)
    BookingId: Mapped[int | None] = mapped_column(ForeignKey("Bookings.Id"), nullable=True)
    Kind: Mapped[str] = mapped_column(String, nullable=False)
    Message: Mapped[str] = mapped_column(Text, nullable=False)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    ReadAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AdminAuditLog(Base):
    __tablename__ = "AdminAuditLogs"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    Action: Mapped[str] = mapped_column(String, nullable=False)
    Details: Mapped[str] = mapped_column(Text, nullable=False)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class RegistrationKey(Base):
    __tablename__ = "RegistrationKeys"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    LaundryRoomId: Mapped[int | None] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=True, index=True)
    WardenId: Mapped[int | None] = mapped_column(ForeignKey("WardenAccounts.Id"), nullable=True, index=True)
    Key: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    IsUsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    IsDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    UsedByRequestId: Mapped[int | None] = mapped_column(Integer, nullable=True)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    UsedAt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RegistrationRequest(Base):
    __tablename__ = "RegistrationRequests"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    FirstName: Mapped[str] = mapped_column(String, nullable=False)
    LastName: Mapped[str] = mapped_column(String, nullable=False)
    RoomNumber: Mapped[str] = mapped_column(String, nullable=False)
    LaundryRoomId: Mapped[int | None] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=True, index=True)
    WardenId: Mapped[int | None] = mapped_column(ForeignKey("WardenAccounts.Id"), nullable=True, index=True)
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
    WardenId: Mapped[int | None] = mapped_column(ForeignKey("WardenAccounts.Id"), nullable=True, index=True)
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
    Warden: Mapped[WardenAccount | None] = relationship(
        "WardenAccount",
        primaryjoin="foreign(ChatMessage.WardenId)==WardenAccount.Id",
        viewonly=True,
    )


class PersistentLogin(Base):
    __tablename__ = "PersistentLogins"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    UserId: Mapped[int] = mapped_column(Integer, nullable=False)
    Token: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    ExpiresAt: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RegistrationKeyAttempt(Base):
    __tablename__ = "RegistrationKeyAttempts"

    ClientIp: Mapped[str] = mapped_column(String(64), primary_key=True)
    FailedAttempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    BlockedUntil: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginAttempt(Base):
    __tablename__ = "LoginAttempts"

    ClientIp: Mapped[str] = mapped_column(String(64), primary_key=True)
    FailedAttempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    BlockedUntil: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RateLimitState(Base):
    __tablename__ = "RateLimitStates"

    RateKey: Mapped[str] = mapped_column(String(160), primary_key=True)
    WindowStartedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    RequestCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    BlockedUntil: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Announcement(Base):
    __tablename__ = "Announcements"

    Id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    LaundryRoomId: Mapped[int | None] = mapped_column(ForeignKey("LaundryRooms.Id"), nullable=True, index=True)
    CreatedByWardenId: Mapped[int | None] = mapped_column(ForeignKey("WardenAccounts.Id"), nullable=True, index=True)
    Title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    Text: Mapped[str] = mapped_column(Text, nullable=False)
    Category: Mapped[str] = mapped_column(String(32), nullable=False, default="Другое")
    ImagePath: Mapped[str | None] = mapped_column(String, nullable=True)
    CreatedAt: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
