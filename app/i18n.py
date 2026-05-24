from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping


DEFAULT_LOCALE = "ru"
SUPPORTED_LOCALES = {"ru", "en"}
LOCALE_LABEL_FALLBACKS = {
    "ru": "Русский",
    "en": "English",
}

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
_TRANSLATIONS: dict[str, dict[str, str]] = {}


def _normalize_locale(locale: str | None) -> str:
    value = (locale or "").strip().lower()
    return value if value in SUPPORTED_LOCALES else DEFAULT_LOCALE


def _load_locale(locale: str) -> dict[str, str]:
    normalized = _normalize_locale(locale)
    if normalized in _TRANSLATIONS:
        return _TRANSLATIONS[normalized]

    path = LOCALES_DIR / f"{normalized}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"i18n: locale file is missing: {path.name}", file=sys.stderr)
        data = {}
    except json.JSONDecodeError as exc:
        print(f"i18n: locale file is invalid: {path.name}: {exc}", file=sys.stderr)
        data = {}
    except OSError as exc:
        print(f"i18n: locale file cannot be read: {path.name}: {exc}", file=sys.stderr)
        data = {}

    if not isinstance(data, dict):
        print(f"i18n: locale file has unsupported format: {path.name}", file=sys.stderr)
        data = {}

    _TRANSLATIONS[normalized] = {str(key): str(value) for key, value in data.items()}
    return _TRANSLATIONS[normalized]


def get_locale(settings: Mapping[str, Any] | str | None = None) -> str:
    if isinstance(settings, str):
        return _normalize_locale(settings)
    if settings:
        return _normalize_locale(str(settings.get("web_interface_language") or ""))
    return DEFAULT_LOCALE


def get_supported_locales() -> tuple[str, ...]:
    return (DEFAULT_LOCALE, *tuple(sorted(SUPPORTED_LOCALES - {DEFAULT_LOCALE})))


def get_locale_label(locale: str, current_locale: str | None = None) -> str:
    normalized = _normalize_locale(locale)
    fallback = LOCALE_LABEL_FALLBACKS.get(normalized, normalized)
    return translate(f"language.names.{normalized}", default=fallback, locale=current_locale or normalized)


def translate(key: str, default: str | None = None, locale: str | None = None) -> str:
    normalized = get_locale(locale)
    value = _load_locale(normalized).get(key)
    if value is None and normalized != DEFAULT_LOCALE:
        value = _load_locale(DEFAULT_LOCALE).get(key)
    if value is None:
        return default if default is not None else key
    return value


def t(key: str, default: str | None = None, locale: str | None = None) -> str:
    return translate(key, default=default, locale=locale)
