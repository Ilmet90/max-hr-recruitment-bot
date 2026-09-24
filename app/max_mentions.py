"""Safe candidate references in MAX Markdown notifications."""

from __future__ import annotations

import re
from typing import Any


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]()~+^#><|{}!&])")
_MAX_USER_ID = re.compile(r"[0-9]+\Z")
MAX_PROFILE_NAME_LENGTH = 120


def escape_markdown(value: Any) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", str(value or ""))


def _name(value: Any) -> str:
    return " ".join(str(value or "").split())


def candidate_mention(user: dict[str, Any]) -> str:
    """Link only a profile name to a validated MAX user ID."""
    first = _name(user.get("first_name"))
    last = _name(user.get("last_name"))
    user_id = str(user.get("external_user_id") or "").strip()
    profile_name = " ".join(part for part in (first, last) if part)
    if (first and len(profile_name) <= MAX_PROFILE_NAME_LENGTH and len(user_id) <= 19
            and _MAX_USER_ID.fullmatch(user_id) and 0 < int(user_id) <= 9223372036854775807):
        return f"[{escape_markdown(profile_name)}](max://user/{int(user_id)})"
    fallback = _name(user.get("display_name")) or profile_name or _name(user.get("username")) or "Пользователь MAX"
    return escape_markdown(fallback[:MAX_PROFILE_NAME_LENGTH])


def candidate_identity(user: dict[str, Any]) -> str:
    reference = candidate_mention(user)
    username = _name(user.get("username")).lstrip("@")[:MAX_PROFILE_NAME_LENGTH]
    name = _name(user.get("display_name"))
    if username and (not name or username != name.lstrip("@")):
        return f"{reference} · @{escape_markdown(username)}"
    return reference
