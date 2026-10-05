from collections.abc import AsyncGenerator
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import DATABASE_URL
from app.models import (
    Base,
    Booking,
    LaundryRoom,
    Machine,
    WashSlot,
    WardenKeyLaundryRoom,
    WardenRegistrationKey,
)

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        columns = await conn.exec_driver_sql("PRAGMA table_info('Announcements')")
        existing_columns = {row[1] for row in columns.fetchall()}
        if "Title" not in existing_columns:
            await conn.exec_driver_sql("ALTER TABLE Announcements ADD COLUMN Title VARCHAR(120) NOT NULL DEFAULT ''")
        if "Category" not in existing_columns:
            await conn.exec_driver_sql("ALTER TABLE Announcements ADD COLUMN Category VARCHAR(32) NOT NULL DEFAULT 'Другое'")
        user_columns = await conn.exec_driver_sql("PRAGMA table_info('Users')")
        existing_user_columns = {row[1] for row in user_columns.fetchall()}
        if "Login" not in existing_user_columns:
            await conn.exec_driver_sql(
                "ALTER TABLE Users ADD COLUMN Login VARCHAR(32) COLLATE NOCASE NOT NULL DEFAULT ''"
            )
        await conn.exec_driver_sql(
            "UPDATE Users SET Login = PublicId WHERE Login = ''"
        )
        await conn.exec_driver_sql(
            'CREATE UNIQUE INDEX IF NOT EXISTS "ix_Users_Login" ON "Users" ("Login" COLLATE NOCASE)'
        )
        if "IsBlocked" not in existing_user_columns:
            await conn.exec_driver_sql("ALTER TABLE Users ADD COLUMN IsBlocked BOOLEAN NOT NULL DEFAULT 0")
        registration_columns = await conn.exec_driver_sql("PRAGMA table_info('RegistrationRequests')")
        existing_registration_columns = {row[1] for row in registration_columns.fetchall()}
        if "Login" not in existing_registration_columns:
            await conn.exec_driver_sql(
                "ALTER TABLE RegistrationRequests ADD COLUMN Login VARCHAR(32) COLLATE NOCASE NOT NULL DEFAULT ''"
            )
        await conn.exec_driver_sql(
            "UPDATE RegistrationRequests SET Login = 'legacy-' || Id WHERE Login = ''"
        )
        await conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS "uq_registration_requests_pending_login"
            ON "RegistrationRequests" ("Login" COLLATE NOCASE)
            WHERE "Status" = 'Pending'
            """
        )
        warden_columns = await conn.exec_driver_sql("PRAGMA table_info('WardenAccounts')")
        existing_warden_columns = {row[1] for row in warden_columns.fetchall()}
        if "IsUniversal" not in existing_warden_columns:
            await conn.exec_driver_sql("ALTER TABLE WardenAccounts ADD COLUMN IsUniversal BOOLEAN NOT NULL DEFAULT 0")
        warden_key_columns = await conn.exec_driver_sql("PRAGMA table_info('WardenRegistrationKeys')")
        existing_warden_key_columns = {row[1] for row in warden_key_columns.fetchall()}
        if "WardenId" not in existing_warden_key_columns:
            await conn.exec_driver_sql(
                "ALTER TABLE WardenRegistrationKeys ADD COLUMN WardenId INTEGER NULL"
            )
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_WardenRegistrationKeys_WardenId "
            "ON WardenRegistrationKeys (WardenId)"
        )
        migrations = {
            "Users": ("LaundryRoomId", "INTEGER NULL"),
            "WashSlots": ("LaundryRoomId", "INTEGER NULL"),
            "Machines": ("LaundryRoomId", "INTEGER NULL"),
            "RegistrationKeys": ("LaundryRoomId", "INTEGER NULL"),
            "RegistrationRequests": ("LaundryRoomId", "INTEGER NULL"),
            "Announcements": ("LaundryRoomId", "INTEGER NULL"),
        }
        for table, (column, definition) in migrations.items():
            result = await conn.exec_driver_sql(f"PRAGMA table_info('{table}')")
            columns = {row[1] for row in result.fetchall()}
            if column not in columns:
                await conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
        await conn.exec_driver_sql(
            """
            DELETE FROM Machines
            WHERE LaundryRoomId IS NOT NULL
              AND Id NOT IN (
                  SELECT MIN(Id)
                  FROM Machines
                  WHERE LaundryRoomId IS NOT NULL
                  GROUP BY LaundryRoomId
              )
            """
        )
        await conn.exec_driver_sql(
            """
            DELETE FROM Machines
            WHERE LaundryRoomId IS NULL
              AND (Id <> 1 OR EXISTS (
                  SELECT 1 FROM Machines AS assigned
                  WHERE assigned.LaundryRoomId = 1
              ))
            """
        )
        await conn.exec_driver_sql(
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_machines_laundry_room" '
            'ON "Machines" ("LaundryRoomId")'
        )
        registration_columns = await conn.exec_driver_sql("PRAGMA table_info('RegistrationRequests')")
        existing_registration_columns = {row[1] for row in registration_columns.fetchall()}
        if "WardenId" not in existing_registration_columns:
            await conn.exec_driver_sql("ALTER TABLE RegistrationRequests ADD COLUMN WardenId INTEGER NULL")
        key_columns = await conn.exec_driver_sql("PRAGMA table_info('RegistrationKeys')")
        existing_key_columns = {row[1] for row in key_columns.fetchall()}
        if "WardenId" not in existing_key_columns:
            await conn.exec_driver_sql("ALTER TABLE RegistrationKeys ADD COLUMN WardenId INTEGER NULL")
        if "IsDeleted" not in existing_key_columns:
            await conn.exec_driver_sql("ALTER TABLE RegistrationKeys ADD COLUMN IsDeleted BOOLEAN NOT NULL DEFAULT 0")
        warden_key_columns = await conn.exec_driver_sql("PRAGMA table_info('WardenRegistrationKeys')")
        existing_warden_key_columns = {row[1] for row in warden_key_columns.fetchall()}
        if "IsDeleted" not in existing_warden_key_columns:
            await conn.exec_driver_sql(
                "ALTER TABLE WardenRegistrationKeys ADD COLUMN IsDeleted BOOLEAN NOT NULL DEFAULT 0"
            )
        chat_columns = await conn.exec_driver_sql("PRAGMA table_info('ChatMessages')")
        existing_chat_columns = {row[1] for row in chat_columns.fetchall()}
        if "WardenId" not in existing_chat_columns:
            await conn.exec_driver_sql("ALTER TABLE ChatMessages ADD COLUMN WardenId INTEGER NULL")
        announcement_columns = await conn.exec_driver_sql("PRAGMA table_info('Announcements')")
        existing_announcement_columns = {row[1] for row in announcement_columns.fetchall()}
        if "CreatedByWardenId" not in existing_announcement_columns:
            await conn.exec_driver_sql("ALTER TABLE Announcements ADD COLUMN CreatedByWardenId INTEGER NULL")

    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.commit()
        async with conn.begin():
            bookings_ddl = await conn.scalar(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='Bookings'")
            )
            booking_columns = await conn.exec_driver_sql("PRAGMA table_info('Bookings')")
            booking_column_info = {row[1]: row for row in booking_columns.fetchall()}
            needs_booking_migration = bool(
                bookings_ddl
                and (
                    "uq_booking_slot_week" in bookings_ddl
                    or "WardenId" not in booking_column_info
                    or booking_column_info.get("UserId", (None, None, None, 1))[3] == 1
                )
            )
            if needs_booking_migration:
                warden_column = (
                    '"WardenId"'
                    if "WardenId" in booking_column_info
                    else "NULL"
                )
                await conn.exec_driver_sql(
                    f"""
                    CREATE TABLE "Bookings_migrating" (
                        "Id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        "SlotId" INTEGER NOT NULL,
                        "UserId" INTEGER,
                        "WardenId" INTEGER,
                        "WeekStart" DATETIME NOT NULL,
                        "StartsAt" DATETIME NOT NULL,
                        "EndsAt" DATETIME NOT NULL,
                        "Status" VARCHAR NOT NULL,
                        "CreatedAt" DATETIME NOT NULL,
                        "CancelledAt" DATETIME,
                        FOREIGN KEY("SlotId") REFERENCES "WashSlots" ("Id"),
                        FOREIGN KEY("UserId") REFERENCES "Users" ("Id"),
                        FOREIGN KEY("WardenId") REFERENCES "WardenAccounts" ("Id")
                    )
                    """
                )
                await conn.exec_driver_sql(
                    f"""
                    INSERT INTO "Bookings_migrating"
                    ("Id", "SlotId", "UserId", "WardenId", "WeekStart", "StartsAt", "EndsAt", "Status", "CreatedAt", "CancelledAt")
                    SELECT "Id", "SlotId", "UserId", {warden_column}, "WeekStart", "StartsAt", "EndsAt", "Status", "CreatedAt", "CancelledAt"
                    FROM "Bookings"
                    """
                )
                await conn.exec_driver_sql('DROP TABLE "Bookings"')
                await conn.exec_driver_sql(
                    'ALTER TABLE "Bookings_migrating" RENAME TO "Bookings"'
                )
            await conn.exec_driver_sql(
                'CREATE INDEX IF NOT EXISTS "ix_Bookings_SlotId" ON "Bookings" ("SlotId")'
            )
            await conn.exec_driver_sql(
                'CREATE INDEX IF NOT EXISTS "ix_Bookings_UserId" ON "Bookings" ("UserId")'
            )
            await conn.exec_driver_sql(
                'CREATE INDEX IF NOT EXISTS "ix_Bookings_WardenId" ON "Bookings" ("WardenId")'
            )
            await conn.exec_driver_sql(
                'CREATE INDEX IF NOT EXISTS "ix_Announcements_CreatedByWardenId" '
                'ON "Announcements" ("CreatedByWardenId")'
            )
            await conn.exec_driver_sql(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS "uq_booking_slot_week_active"
                ON "Bookings" ("SlotId", "WeekStart")
                WHERE "Status" IN ('Забронировано', 'В работе')
                """
            )
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.commit()

    async with SessionLocal() as session:
        had_default_room = await session.get(LaundryRoom, 1) is not None
        active_room_id = await session.scalar(
            select(LaundryRoom.Id)
            .where(LaundryRoom.IsActive.is_(True))
            .order_by(LaundryRoom.Floor, LaundryRoom.RoomNumber)
            .limit(1)
        )
        default_room = await session.get(LaundryRoom, 1)
        if default_room is None:
            default_room = LaundryRoom(
                Id=1,
                Floor=1,
                RoomNumber=100,
                Name="Постирочная 100",
                IsActive=True,
            )
            session.add(default_room)
            await session.flush()
        elif active_room_id is None:
            default_room.IsActive = True

        active_room_ids = list(
            (
                await session.execute(
                    select(LaundryRoom.Id).where(LaundryRoom.IsActive.is_(True))
                )
            ).scalars().all()
        )
        if len(active_room_ids) == 1:
            key_room_links = (
                await session.execute(select(WardenKeyLaundryRoom.KeyId))
            ).scalars().all()
            linked_key_ids = set(key_room_links)
            orphaned_keys = (
                await session.execute(
                    select(WardenRegistrationKey).where(
                        WardenRegistrationKey.IsUsed.is_(False),
                        WardenRegistrationKey.IsDeleted.is_(False),
                    )
                )
            ).scalars().all()
            session.add_all(
                WardenKeyLaundryRoom(
                    KeyId=key.Id,
                    LaundryRoomId=active_room_ids[0],
                )
                for key in orphaned_keys
                if key.Id not in linked_key_ids
            )

        has_room_slots = await session.scalar(
            select(WashSlot.Id).where(WashSlot.LaundryRoomId.is_not(None)).limit(1)
        )
        if had_default_room and has_room_slots is None:
            for table in ("Users", "WashSlots", "RegistrationKeys", "RegistrationRequests", "Announcements"):
                await session.execute(text(f'UPDATE "{table}" SET "LaundryRoomId" = 1 WHERE "LaundryRoomId" IS NULL'))
        machine = await session.scalar(
            select(Machine).where(Machine.LaundryRoomId == 1).limit(1)
        )
        if machine is None:
            legacy_machine = await session.get(Machine, 1)
            if legacy_machine is not None and legacy_machine.LaundryRoomId is None:
                legacy_machine.LaundryRoomId = 1
            else:
                session.add(
                    Machine(
                        LaundryRoomId=1,
                        Name="Машина №1",
                        Status="Свободна",
                        UpdatedAt=datetime.now(),
                    )
                )
        slots = (
            await session.execute(
                select(WashSlot).where(
                    WashSlot.ReservedByUserId.is_not(None),
                    WashSlot.ReservationWeekStart.is_not(None),
                    WashSlot.ReservationExpiresAt.is_not(None),
                )
            )
        ).scalars().all()
        for slot in slots:
            week_start = slot.ReservationWeekStart.replace(hour=0, minute=0, second=0, microsecond=0)
            existing = await session.execute(
                select(Booking.Id)
                .where(Booking.SlotId == slot.Id, Booking.WeekStart == week_start)
                .limit(1)
            )
            if existing.scalar_one_or_none() is None:
                end = slot.ReservationExpiresAt
                session.add(
                    Booking(
                        SlotId=slot.Id,
                        UserId=slot.ReservedByUserId,
                        WeekStart=week_start,
                        StartsAt=end - timedelta(hours=3),
                        EndsAt=end,
                        CreatedAt=end - timedelta(hours=3),
                    )
                )
        await session.commit()
