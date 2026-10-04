import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

BASE_DIR = Path(__file__).resolve().parent.parent

if ZoneInfo is not None:
    try:
        LOCAL_TZ = ZoneInfo("Asia/Omsk")
    except Exception:
        LOCAL_TZ = timezone(timedelta(hours=6), "Asia/Omsk")
else:
    LOCAL_TZ = timezone(timedelta(hours=6), "Asia/Omsk")


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def as_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(LOCAL_TZ)

_configured_db = os.getenv("LAUNDRY_DB_PATH", "").strip()
if _configured_db:
    DB_PATH = Path(_configured_db) if Path(_configured_db).is_absolute() else (BASE_DIR / _configured_db)
else:
    DB_PATH = BASE_DIR / "data" / "laundry.db"

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

ADMIN_PASSWORD = os.getenv("LAUNDRY_ADMIN_PASSWORD", "").strip()
SESSION_SECRET = os.getenv("LAUNDRY_SESSION_SECRET", "").strip()
if not ADMIN_PASSWORD:
    raise RuntimeError("Set LAUNDRY_ADMIN_PASSWORD in the environment.")
if not SESSION_SECRET:
    raise RuntimeError("Set LAUNDRY_SESSION_SECRET in the environment.")
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"

SCHEDULE_DAYS = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)

SCHEDULE_STATUSES = (
    "Свободно",
    "Технические работы",
    "Занято",
    "Выходной",
    "Уборка",
    "Закрыто",
)

DEFAULT_TIMES = (
    "08:00–11:00",
    "12:00–15:00",
    "16:00–19:00",
    "20:00–23:00",
    "00:00–03:00",
)
