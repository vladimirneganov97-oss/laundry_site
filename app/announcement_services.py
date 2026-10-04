from __future__ import annotations

import io
import uuid
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError

from app.config import UPLOAD_DIR


async def save_announcement_image(image: UploadFile | None) -> tuple[str | None, str | None]:
    if image is None or not image.filename:
        return None, None

    suffix = Path(image.filename).suffix.lower()
    expected_formats = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
    if suffix not in expected_formats:
        return None, "Разрешены только изображения JPG, PNG и WEBP."

    content = await image.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        return None, "Размер изображения не должен превышать 5 МБ."

    try:
        with Image.open(io.BytesIO(content)) as decoded:
            valid_content = (
                decoded.format == expected_formats[suffix]
                and decoded.width * decoded.height <= 40_000_000
            )
            if valid_content:
                decoded.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        valid_content = False

    if not valid_content:
        return None, "Содержимое файла не соответствует формату изображения."

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex + suffix
    (UPLOAD_DIR / name).write_bytes(content)
    return "/uploads/" + name, None
