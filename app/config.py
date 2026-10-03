import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

_configured_db = os.getenv("LAUNDRY_DB_PATH", "").strip()
if _configured_db:
    DB_PATH = Path(_configured_db) if Path(_configured_db).is_absolute() else (BASE_DIR / _configured_db)
else:
    DB_PATH = BASE_DIR / "data" / "laundry.db"

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

ADMIN_PASSWORD = os.getenv("LAUNDRY_ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("LAUNDRY_SESSION_SECRET", "laundry-dev-secret-change-me")
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
