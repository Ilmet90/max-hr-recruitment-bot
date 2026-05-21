from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import requests

from app import db


SOURCE = "trudvsem"
DEFAULT_TIMEOUT = 25
SCHEDULE_KEYWORDS = (
    "сменная",
    "сменный",
    "полный рабочий день",
    "ненормированный",
    "гибкий",
    "вахтовый",
    "вахта",
    "неполный",
    "пятиднев",
    "шестиднев",
    "удален",
    "удалён",
    "дистанц",
)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(part for part in (_stringify(item) for item in value) if part)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text = _stringify(item)
            if text:
                parts.append(text)
        return ", ".join(parts)
    return str(value).strip()


def _pick(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None


def _pick_nested(source: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = source
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current not in (None, ""):
            return current
    return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        inner = value.get("vacancy")
        if inner is not None:
            return _as_list(inner)
        return [value]
    return []


def _unwrap_vacancy(item: Any) -> dict[str, Any]:
    if isinstance(item, dict) and isinstance(item.get("vacancy"), dict):
        return item["vacancy"]
    return item if isinstance(item, dict) else {}


def _extract_vacancy_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    candidates = [
        _pick_nested(payload, ("results", "vacancies")),
        payload.get("vacancies"),
        _pick_nested(payload, ("vacancies", "vacancy")),
        _pick_nested(payload, ("results", "vacancies", "vacancy")),
    ]
    for candidate in candidates:
        items = [_unwrap_vacancy(item) for item in _as_list(candidate)]
        items = [item for item in items if item]
        if items:
            return items
    return []


def _fetch_url(url: str) -> tuple[bool, dict[str, Any] | None, str]:
    try:
        response = requests.get(url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return False, None, "API вернул неожиданный формат ответа."
        return True, data, ""
    except requests.exceptions.RequestException as exc:
        return False, None, f"Сетевая ошибка при обращении к API: {exc}"
    except ValueError:
        return False, None, "API вернул ответ не в формате JSON."


def fetch_trudvsem_vacancies(company_code: str | None = None, inn: str | None = None) -> dict[str, Any]:
    settings = db.get_trudvsem_settings()
    api_base = (settings.get("trudvsem_api_base") or "").strip().rstrip("/")
    company_code = (company_code or settings.get("trudvsem_company_code") or "").strip()
    inn = (inn or settings.get("trudvsem_inn") or "").strip()
    endpoints: list[tuple[str, str]] = []
    if company_code:
        endpoints.append(("company_code", f"{api_base}/vacancies/company/{company_code}"))
    if inn:
        endpoints.append(("inn", f"{api_base}/vacancies/company/inn/{inn}"))
    if not endpoints:
        return {"ok": False, "vacancies": [], "error": "Укажите ИНН работодателя или код работодателя на портале “Работа России”.", "errors": []}

    errors: list[str] = []
    for endpoint_type, url in endpoints:
        ok, payload, error = _fetch_url(url)
        if not ok or payload is None:
            errors.append(f"{endpoint_type}: {error}")
            continue
        vacancies = _extract_vacancy_items(payload)
        if vacancies:
            return {
                "ok": True,
                "endpoint": endpoint_type,
                "endpoint_type": endpoint_type,
                "url": url,
                "vacancies": vacancies,
                "count": len(vacancies),
                "errors": errors,
            }
        errors.append(f"{endpoint_type}: вакансии не найдены")
    return {
        "ok": False,
        "endpoint": "",
        "endpoint_type": "",
        "url": "",
        "vacancies": [],
        "count": 0,
        "error": "Не удалось получить вакансии с портала «Работа России».",
        "errors": errors,
    }


def _format_money(value: Any) -> str:
    text = _stringify(value).replace(" ", "")
    if not text:
        return ""
    try:
        amount = float(text.replace(",", "."))
    except ValueError:
        return _stringify(value)
    if amount.is_integer():
        return str(int(amount))
    return str(amount)


def _format_salary(raw: dict[str, Any]) -> str:
    salary = _pick(raw, "salary")
    if salary and not isinstance(salary, dict):
        return _stringify(salary)
    min_value = _pick(raw, "salary_min", "salary_minimum", "salary-min")
    max_value = _pick(raw, "salary_max", "salary_maximum", "salary-max")
    if isinstance(salary, dict):
        min_value = min_value or _pick(salary, "min", "from", "salary_min", "salary-min")
        max_value = max_value or _pick(salary, "max", "to", "salary_max", "salary-max")
    min_text = _format_money(min_value)
    max_text = _format_money(max_value)
    if min_text and max_text and min_text != max_text:
        return f"от {min_text} до {max_text} рублей"
    if min_text:
        return f"от {min_text} рублей"
    if max_text:
        return f"до {max_text} рублей"
    return "уточняется при консультации"


def _join_parts(*values: Any) -> str:
    parts = []
    for value in values:
        text = _stringify(value)
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def _split_lines(text: Any) -> list[str]:
    value = _stringify(text)
    if not value:
        return []
    chunks = re.split(r"[\n\r]+|;\s*", value)
    return [" ".join(chunk.strip().split()) for chunk in chunks if chunk and chunk.strip()]


def _dedupe_lines(lines: list[str]) -> list[str]:
    result = []
    seen = set()
    for line in lines:
        normalized = " ".join(line.strip().split())
        key = normalized.lower()
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return result


def _sentence(text: str) -> str:
    text = text.strip(" \t\r\n.,;")
    if not text:
        return ""
    return text if text.endswith((".", "!", "?")) else f"{text}."


def looks_like_coordinates(text: str) -> bool:
    value = _stringify(text)
    return bool(re.search(r"\b\d{1,3}\.\d{3,}\s*[,;]\s*\d{1,3}\.\d{3,}\b", value))


def looks_like_phone(text: str) -> bool:
    value = _stringify(text)
    if "http://" in value.lower() or "https://" in value.lower():
        return False
    digits = re.sub(r"\D", "", value)
    return len(digits) >= 10


def looks_like_contact_person(text: str) -> bool:
    value = _stringify(text).strip()
    lowered = value.lower()
    if any(word in lowered for word in ("контакт", "телефон", "ответственный")):
        return True
    words = re.findall(r"[А-ЯЁ][а-яё-]+", value)
    return len(words) >= 3 and len(" ".join(words)) >= len(value.replace("-", " ")) - 2


def looks_like_address(text: str) -> bool:
    lowered = _stringify(text).lower()
    markers = (
        "область",
        "край",
        "республика",
        "район",
        "город",
        " г.",
        "г. ",
        "улица",
        " ул.",
        "ул. ",
        "проспект",
        "пр-т",
        "переулок",
        "проезд",
        "шоссе",
        "дом:",
        " дом ",
        "д.",
        "корпус",
        "строение",
    )
    return any(marker in lowered for marker in markers)


def clean_requirement_text(text: Any) -> str:
    cleaned = []
    for line in _split_lines(text):
        if looks_like_address(line) or looks_like_coordinates(line) or looks_like_phone(line) or looks_like_contact_person(line):
            continue
        line = re.sub(r"(?:[,;]\s*|\s+)[01]\s*$", "", line).strip(" ,;")
        if line in {"0", "1"}:
            continue
        if line:
            cleaned.append(line.strip(" \t\r\n,;"))
    result = _dedupe_lines(cleaned)
    return "\n".join(result) if result else "Требования уточняются при консультации."


def clean_schedule_text(text: Any) -> str:
    schedules = []
    for line in _split_lines(text):
        if looks_like_address(line) or looks_like_coordinates(line) or looks_like_phone(line) or looks_like_contact_person(line):
            continue
        lowered = line.lower()
        if any(keyword in lowered for keyword in SCHEDULE_KEYWORDS):
            schedules.append(line.strip(" .;"))
    return "; ".join(_dedupe_lines(schedules))


def _raw_values(raw: Any, *keys: str) -> list[Any]:
    if not isinstance(raw, dict):
        return [raw]
    values = []
    for key in keys:
        value = _pick(raw, key)
        if value not in (None, ""):
            values.append(value)
    return values


def clean_conditions_text(raw: Any) -> str:
    values: list[Any] = []
    if isinstance(raw, dict):
        values.extend(
            _raw_values(
                raw,
                "conditions",
                "working_conditions",
                "work-conditions",
                "labor_conditions",
                "schedule",
                "employment",
                "employment_type",
                "employment-type",
            )
        )
    else:
        values.append(raw)
    conditions = []
    schedules = []
    for value in values:
        for line in _split_lines(value):
            lowered = line.lower().strip(" .;")
            if lowered in {"оптимальные", "допустимые", "вредные", "опасные"}:
                conditions.append(f"Условия труда: {lowered}.")
                continue
            schedule = clean_schedule_text(line)
            if schedule:
                schedules.append(f"График работы: {schedule}.")
                continue
            if looks_like_address(line) or looks_like_coordinates(line) or looks_like_phone(line) or looks_like_contact_person(line):
                continue
            if "http://" in lowered or "https://" in lowered:
                continue
            conditions.append(_sentence(line))
    result = _dedupe_lines(conditions + schedules)
    return "\n".join(result) if result else "Условия уточняются при консультации."


def _find_trudvsem_url(raw: Any) -> str:
    text = _stringify(raw)
    match = re.search(r"https?://trudvsem\.ru/\S+", text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;)")


def clean_note_text(raw: Any, external_url: str | None = None) -> str:
    url = (external_url or "").strip() or _find_trudvsem_url(raw)
    lines = ["Источник: портал «Работа России»."]
    if url:
        lines.append(f"Ссылка на вакансию: {url}")
    return "\n".join(lines)


def _clean_public_text(text: Any, fallback: str) -> str:
    cleaned = []
    for line in _split_lines(text):
        if looks_like_address(line) or looks_like_coordinates(line) or looks_like_phone(line) or looks_like_contact_person(line):
            continue
        if "http://" in line.lower() or "https://" in line.lower():
            continue
        cleaned.append(_sentence(line))
    result = _dedupe_lines(cleaned)
    return "\n".join(result) if result else fallback


def _hash_external_id(raw: dict[str, Any], title: str, salary: str) -> str:
    company = _stringify(_pick(raw, "company", "company-name", "organization"))
    address = _stringify(_pick(raw, "address", "addresses", "region"))
    base = "|".join([title, salary, company, address])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def normalize_trudvsem_vacancy(raw: dict[str, Any]) -> dict[str, Any]:
    raw = _unwrap_vacancy(raw)
    title = _stringify(_pick(raw, "job-name", "name", "title")) or "Вакансия без названия"
    salary = _format_salary(raw)
    external_id = _stringify(_pick(raw, "id", "vacancy_id", "vacancy-id"))
    if not external_id:
        external_id = _hash_external_id(raw, title, salary)
    settings = db.get_trudvsem_settings()
    company_code = settings.get("trudvsem_company_code", "").strip()
    external_url = _stringify(_pick(raw, "url", "vacancy_url", "vac_url"))
    if not external_url and company_code and external_id:
        external_url = f"https://trudvsem.ru/vacancy/card/{company_code}/{external_id}"
    duties_raw = _join_parts(
        _pick(raw, "duty", "duties", "job-description", "responsibilities"),
        _pick_nested(raw, ("requirement", "duties")),
    )
    requirements_raw = _join_parts(
        _pick(raw, "requirement", "requirements", "qualification"),
        _pick_nested(raw, ("requirements", "qualification")),
    )
    duties = _clean_public_text(duties_raw, "Обязанности уточняются при консультации.")
    requirements = clean_requirement_text(requirements_raw)
    conditions = clean_conditions_text(raw)
    note = clean_note_text(raw, external_url)
    external_updated_at = _stringify(_pick(raw, "creation-date", "date_create", "date_modify", "modified", "published_at"))
    return {
        "external_source": SOURCE,
        "external_id": external_id,
        "external_url": external_url,
        "external_updated_at": external_updated_at,
        "external_raw_json": json.dumps(raw, ensure_ascii=False),
        "title": title,
        "salary": salary,
        "duties": duties,
        "requirements": requirements,
        "conditions": conditions,
        "note": note,
        "raw": raw,
    }


def preview_trudvsem_vacancies() -> dict[str, Any]:
    settings = db.get_trudvsem_settings()
    company_code = (settings.get("trudvsem_company_code") or "").strip()
    inn = (settings.get("trudvsem_inn") or "").strip()
    if not company_code and not inn:
        return {"ok": False, "count": 0, "items": [], "error": "Укажите ИНН работодателя или код работодателя на портале “Работа России”.", "settings": settings}
    if str(settings.get("trudvsem_enabled") or "0") != "1":
        return {"ok": False, "count": 0, "items": [], "error": "Импорт с портала «Работа России» отключён в настройках.", "settings": settings}
    fetched = fetch_trudvsem_vacancies(company_code, inn)
    if not fetched.get("ok"):
        return {**fetched, "items": [], "settings": settings}
    items = []
    for raw in fetched.get("vacancies", []):
        item = normalize_trudvsem_vacancy(raw)
        existing = db.fetch_one(
            "SELECT id FROM vacancies WHERE external_source = ? AND external_id = ?",
            (SOURCE, item["external_id"]),
        )
        item["status"] = "будет обновлена" if existing else "новая"
        item["existing_id"] = existing["id"] if existing else None
        items.append(item)
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "endpoint_type": fetched.get("endpoint_type", ""),
        "endpoint": fetched.get("endpoint_type", ""),
        "url": fetched.get("url", ""),
        "errors": fetched.get("errors", []),
        "settings": settings,
    }


def import_trudvsem_vacancies(mode: str = "upsert", actor_id: int | None = None, actor_name: str = "Система") -> dict[str, Any]:
    if mode != "upsert":
        return {"ok": False, "added": 0, "updated": 0, "skipped": 0, "errors": ["Поддерживается только режим upsert."]}
    preview = preview_trudvsem_vacancies()
    if not preview.get("ok"):
        return {
            "ok": False,
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": preview.get("errors") or [preview.get("error") or "Не удалось получить вакансии."],
            "endpoint_type": preview.get("endpoint_type", ""),
            "endpoint": preview.get("endpoint_type", ""),
            "items": [],
        }
    added = 0
    updated = 0
    skipped = 0
    errors: list[str] = []
    imported_items: list[dict[str, Any]] = []
    next_sort_row = db.fetch_one("SELECT COALESCE(MAX(sort_order), 90) + 10 AS next_sort FROM vacancies")
    next_sort = int(next_sort_row["next_sort"] if next_sort_row else 100)
    for item in preview.get("items", []):
        if not item.get("title") or not item.get("external_id"):
            skipped += 1
            errors.append("Пропущена вакансия без названия или внешнего ID.")
            continue
        existing = db.fetch_one(
            "SELECT * FROM vacancies WHERE external_source = ? AND external_id = ?",
            (SOURCE, item["external_id"]),
        )
        params = (
            item["title"],
            item["salary"],
            item["duties"],
            item["requirements"],
            item["conditions"],
            item["note"],
            item["external_url"],
            item["external_updated_at"],
            item["external_raw_json"],
            db.now_iso(),
        )
        if existing:
            db.execute(
                """
                UPDATE vacancies
                SET title = ?, salary = ?, duties = ?, requirements = ?, conditions = ?, note = ?,
                    external_url = ?, external_updated_at = ?, external_raw_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (*params, existing["id"]),
            )
            item["import_action"] = "updated"
            item["vacancy_id"] = existing["id"]
            updated += 1
        else:
            vacancy_id = db.execute(
                """
                INSERT INTO vacancies
                (title, salary, duties, requirements, conditions, note, is_active, sort_order,
                 created_at, updated_at, external_source, external_id, external_url, external_updated_at, external_raw_json)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["title"],
                    item["salary"],
                    item["duties"],
                    item["requirements"],
                    item["conditions"],
                    item["note"],
                    next_sort,
                    db.now_iso(),
                    db.now_iso(),
                    SOURCE,
                    item["external_id"],
                    item["external_url"],
                    item["external_updated_at"],
                    item["external_raw_json"],
                ),
            )
            next_sort += 10
            item["import_action"] = "added"
            item["vacancy_id"] = vacancy_id
            added += 1
        imported_items.append(item)
    if added or updated:
        db.set_trudvsem_last_sync_at()
    details = f"Добавлено: {added}; обновлено: {updated}; пропущено: {skipped}"
    db.audit_log(actor_id, actor_name, "trudvsem_vacancies_imported", "vacancy", None, details)
    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "endpoint_type": preview.get("endpoint_type", ""),
        "endpoint": preview.get("endpoint_type", ""),
        "count": len(imported_items),
        "items": imported_items,
    }


def renormalize_existing_trudvsem_vacancies() -> dict[str, Any]:
    rows = db.fetch_all(
        """
        SELECT id, external_raw_json
        FROM vacancies
        WHERE external_source = ? AND COALESCE(external_raw_json, '') != ''
        ORDER BY id
        """,
        (SOURCE,),
    )
    updated = 0
    skipped = 0
    errors: list[str] = []
    for row in rows:
        try:
            raw = json.loads(row["external_raw_json"])
            if not isinstance(raw, dict):
                skipped += 1
                errors.append(f"Вакансия #{row['id']}: исходный JSON имеет неожиданный формат.")
                continue
            item = normalize_trudvsem_vacancy(raw)
            db.execute(
                """
                UPDATE vacancies
                SET title = ?, salary = ?, duties = ?, requirements = ?, conditions = ?, note = ?,
                    external_url = ?, external_updated_at = ?, external_raw_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    item["title"],
                    item["salary"],
                    item["duties"],
                    item["requirements"],
                    item["conditions"],
                    item["note"],
                    item["external_url"],
                    item["external_updated_at"],
                    item["external_raw_json"],
                    db.now_iso(),
                    row["id"],
                ),
            )
            updated += 1
        except (json.JSONDecodeError, TypeError) as exc:
            skipped += 1
            errors.append(f"Вакансия #{row['id']}: не удалось прочитать исходный JSON ({exc}).")
        except Exception as exc:
            skipped += 1
            errors.append(f"Вакансия #{row['id']}: ошибка переочистки ({exc}).")
    return {"ok": True, "updated": updated, "skipped": skipped, "errors": errors}
