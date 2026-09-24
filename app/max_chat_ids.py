"""Validated MAX dialog IDs used by the admin browser link."""

from __future__ import annotations

import re
from typing import Any


MAX_INT64 = 9223372036854775807
MAX_WEB_BASE = "https://web.max.ru/"


def canonical_chat_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 1 <= value <= MAX_INT64 else None
    if not isinstance(value, str):
        return None
    raw = str(value)
    if not re.fullmatch(r"[0-9]+", raw, flags=re.ASCII):
        return None
    digits = raw.lstrip("0")
    if not digits or len(digits) > 19:
        return None
    number = int(digits)
    return str(number) if 1 <= number <= MAX_INT64 else None


def max_web_url(messenger: str | None, external_chat_id: Any) -> str | None:
    chat_id = canonical_chat_id(external_chat_id)
    return f"{MAX_WEB_BASE}{chat_id}" if messenger == "max" and chat_id else None
