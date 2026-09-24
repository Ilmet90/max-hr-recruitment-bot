"""Recruitment analytics derived from recorded candidate activity."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app import db


MSK = ZoneInfo("Europe/Moscow")
PERIODS = {"today": "Сегодня", "7d": "7 дней", "30d": "30 дней", "all": "Всё время"}
TRACKED_EVENTS = tuple(sorted(db.ACTIVITY_EVENTS))
EVENT_MARKS = ", ".join("?" for _ in TRACKED_EVENTS)


def period_bounds(period: str, now: datetime | None = None) -> tuple[str | None, str | None]:
    if period not in PERIODS:
        raise ValueError("Unsupported analytics period")
    if period == "all":
        return None, None
    current = (now or datetime.now(timezone.utc)).astimezone(MSK)
    today = current.date()
    first = today - timedelta(days={"today": 0, "7d": 6, "30d": 29}[period])
    start = datetime.combine(first, time.min, MSK)
    end = datetime.combine(today + timedelta(days=1), time.min, MSK)
    def stamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return stamp(start), stamp(end)


def _event_scope(bounds: tuple[str | None, str | None]) -> tuple[str, tuple[Any, ...]]:
    start, end = bounds
    clauses = [f"event_type IN ({EVENT_MARKS})"]
    params: list[Any] = list(TRACKED_EVENTS)
    if start is not None:
        clauses.append("created_at >= ?")
        params.append(start)
    if end is not None:
        clauses.append("created_at < ?")
        params.append(end)
    return " AND ".join(clauses), tuple(params)


def percentage(numerator: int, denominator: int) -> str:
    return "—" if denominator == 0 else f"{100 * numerator / denominator:.1f}%"


def _sequence_counts(where: str, params: tuple[Any, ...]) -> dict[int, dict[str, int]]:
    rows = db.fetch_all(f"""
        WITH events AS MATERIALIZED (
            SELECT id, messenger_user_id, vacancy_id, event_type, created_at
            FROM user_activity_events WHERE {where} AND vacancy_id IS NOT NULL
        ),
        first_starts AS (
            SELECT * FROM (
                SELECT e.*, ROW_NUMBER() OVER (
                    PARTITION BY messenger_user_id, vacancy_id ORDER BY created_at, id) AS rn
                FROM events e WHERE event_type = 'vacancy_apply_started'
            ) WHERE rn = 1
        ),
        converted AS (
            SELECT DISTINCT s.messenger_user_id, s.vacancy_id
            FROM first_starts s JOIN events e
              ON e.messenger_user_id = s.messenger_user_id AND e.vacancy_id = s.vacancy_id
             AND e.event_type = 'vacancy_application_submitted'
             AND (e.created_at, e.id) > (s.created_at, s.id)
        ),
        first_views AS (
            SELECT * FROM (
                SELECT e.*, ROW_NUMBER() OVER (
                    PARTITION BY messenger_user_id, vacancy_id ORDER BY created_at, id) AS rn
                FROM events e WHERE event_type = 'vacancy_viewed'
            ) WHERE rn = 1
        ),
        funnel_starts AS (
            SELECT * FROM (
                SELECT s.*, ROW_NUMBER() OVER (
                    PARTITION BY s.messenger_user_id, s.vacancy_id ORDER BY s.created_at, s.id) AS rn
                FROM events s JOIN first_views v
                  ON s.messenger_user_id = v.messenger_user_id AND s.vacancy_id = v.vacancy_id
                 AND s.event_type = 'vacancy_apply_started'
                 AND (s.created_at, s.id) > (v.created_at, v.id)
            ) WHERE rn = 1
        ),
        funnel_submitted AS (
            SELECT DISTINCT s.messenger_user_id, s.vacancy_id
            FROM funnel_starts s JOIN events e
              ON e.messenger_user_id = s.messenger_user_id AND e.vacancy_id = s.vacancy_id
             AND e.event_type = 'vacancy_application_submitted'
             AND (e.created_at, e.id) > (s.created_at, s.id)
        ),
        conversion_counts AS (
            SELECT s.vacancy_id, COUNT(*) AS started, COUNT(c.messenger_user_id) AS converted
            FROM first_starts s LEFT JOIN converted c
              ON c.messenger_user_id = s.messenger_user_id AND c.vacancy_id = s.vacancy_id
            GROUP BY s.vacancy_id
        ),
        funnel_counts AS (
            SELECT v.vacancy_id, COUNT(*) AS viewed,
                   COUNT(s.messenger_user_id) AS started,
                   COUNT(t.messenger_user_id) AS submitted
            FROM first_views v
            LEFT JOIN funnel_starts s
              ON s.messenger_user_id = v.messenger_user_id AND s.vacancy_id = v.vacancy_id
            LEFT JOIN funnel_submitted t
              ON t.messenger_user_id = v.messenger_user_id AND t.vacancy_id = v.vacancy_id
            GROUP BY v.vacancy_id
        )
        SELECT ids.vacancy_id,
               COALESCE(c.started, 0) AS conversion_started,
               COALESCE(c.converted, 0) AS conversion_submitted,
               COALESCE(f.viewed, 0) AS funnel_viewed,
               COALESCE(f.started, 0) AS funnel_started,
               COALESCE(f.submitted, 0) AS funnel_submitted
        FROM (SELECT vacancy_id FROM conversion_counts UNION SELECT vacancy_id FROM funnel_counts) ids
        LEFT JOIN conversion_counts c ON c.vacancy_id = ids.vacancy_id
        LEFT JOIN funnel_counts f ON f.vacancy_id = ids.vacancy_id
    """, params)
    return {int(row["vacancy_id"]): row for row in rows}


def _vacancy_labels(vacancy_ids: set[int]) -> dict[int, str]:
    if not vacancy_ids:
        return {}
    labels: dict[int, str] = {}
    ordered_ids = sorted(vacancy_ids)
    for offset in range(0, len(ordered_ids), 500):
        chunk = tuple(ordered_ids[offset:offset + 500])
        marks = ", ".join("?" for _ in chunk)
        current = db.fetch_all(f"SELECT id, title FROM vacancies WHERE id IN ({marks})", chunk)
        labels.update({int(row["id"]): str(row["title"]) for row in current})
        missing = tuple(vacancy_id for vacancy_id in chunk if vacancy_id not in labels)
        if not missing:
            continue
        missing_marks = ", ".join("?" for _ in missing)
        submitted = db.fetch_all(f"""
            SELECT a.vacancy_id, a.vacancy_title FROM applications a
            JOIN (SELECT vacancy_id, MAX(id) AS last_id FROM applications
                  WHERE vacancy_id IN ({missing_marks}) AND TRIM(COALESCE(vacancy_title, '')) != ''
                  GROUP BY vacancy_id) latest ON latest.last_id = a.id
        """, missing)
        labels.update({int(row["vacancy_id"]): str(row["vacancy_title"]) for row in submitted})
        missing = tuple(vacancy_id for vacancy_id in missing if vacancy_id not in labels)
        if not missing:
            continue
        missing_marks = ", ".join("?" for _ in missing)
        historical = db.fetch_all(f"""
            WITH titles AS (
                SELECT id, vacancy_id,
                       CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.vacancy_title') END AS title
                FROM user_activity_events WHERE vacancy_id IN ({missing_marks}) AND metadata IS NOT NULL
                  AND event_type IN ('vacancy_viewed', 'vacancy_apply_started')
            ), latest AS (
                SELECT vacancy_id, MAX(id) AS last_id FROM titles
                WHERE TYPEOF(title) = 'text' AND TRIM(title) != '' GROUP BY vacancy_id
            )
            SELECT t.vacancy_id, t.title FROM titles t JOIN latest l ON l.last_id = t.id
        """, missing)
        labels.update({int(row["vacancy_id"]): str(row["title"]) for row in historical})
    return labels


def report(period: str = "7d", now: datetime | None = None) -> dict[str, Any]:
    bounds = period_bounds(period, now)
    where, params = _event_scope(bounds)
    summary = db.fetch_one(f"""
        SELECT COUNT(DISTINCT messenger_user_id) AS active_users,
               COUNT(DISTINCT CASE WHEN event_type = 'vacancy_viewed' THEN messenger_user_id END) AS vacancy_viewers_unique,
               SUM(CASE WHEN event_type = 'vacancy_viewed' THEN 1 ELSE 0 END) AS vacancy_views_raw,
               SUM(CASE WHEN event_type = 'vacancy_apply_started' THEN 1 ELSE 0 END) AS apply_started_raw,
               SUM(CASE WHEN event_type = 'vacancy_application_submitted' THEN 1 ELSE 0 END) AS applications_submitted_raw,
               SUM(CASE WHEN event_type = 'question_sent' THEN 1 ELSE 0 END) AS questions_sent,
               SUM(CASE WHEN event_type = 'appeal_sent' THEN 1 ELSE 0 END) AS appeals_sent
        FROM user_activity_events WHERE {where}
    """, params) or {}
    raw_rows = db.fetch_all(f"""
        SELECT vacancy_id,
               SUM(CASE WHEN event_type = 'vacancy_viewed' THEN 1 ELSE 0 END) AS vacancy_views_raw,
               COUNT(DISTINCT CASE WHEN event_type = 'vacancy_viewed' THEN messenger_user_id END) AS vacancy_viewers_unique,
               SUM(CASE WHEN event_type = 'vacancy_apply_started' THEN 1 ELSE 0 END) AS apply_started_raw,
               COUNT(DISTINCT CASE WHEN event_type = 'vacancy_apply_started' THEN messenger_user_id END) AS apply_started_unique_pairs,
               SUM(CASE WHEN event_type = 'vacancy_application_submitted' THEN 1 ELSE 0 END) AS applications_submitted_raw,
               COUNT(DISTINCT CASE WHEN event_type = 'vacancy_application_submitted' THEN messenger_user_id END) AS applications_submitted_unique_pairs
        FROM user_activity_events WHERE {where}
          AND event_type IN ('vacancy_viewed', 'vacancy_apply_started', 'vacancy_application_submitted')
        GROUP BY vacancy_id
    """, params)
    sequences = _sequence_counts(where, params)
    labels = _vacancy_labels({int(row["vacancy_id"]) for row in raw_rows if row["vacancy_id"] is not None})
    vacancies: list[dict[str, Any]] = []
    for raw in raw_rows:
        vacancy_id = raw["vacancy_id"]
        counts = sequences.get(int(vacancy_id), {}) if vacancy_id is not None else {}
        item = {key: (int(value) if isinstance(value, int) else value) for key, value in raw.items()}
        item.update(counts)
        item["label"] = (labels.get(int(vacancy_id), f"Удалённая вакансия #{vacancy_id}")
                         if vacancy_id is not None else "Без привязки к вакансии")
        item["conversion_rate"] = percentage(counts.get("conversion_submitted", 0),
                                               counts.get("conversion_started", 0))
        vacancies.append(item)
    vacancies.sort(key=lambda row: (-row["vacancy_views_raw"], str(row["label"])))
    totals = {key: int(value or 0) for key, value in summary.items()}
    totals["apply_started_unique_pairs"] = sum(row["apply_started_unique_pairs"] for row in vacancies if row["vacancy_id"] is not None)
    totals["applications_submitted_unique_pairs"] = sum(row["applications_submitted_unique_pairs"] for row in vacancies if row["vacancy_id"] is not None)
    funnel = {key: sum(row.get(key, 0) for row in vacancies) for key in
              ("funnel_viewed", "funnel_started", "funnel_submitted")}
    conversion_started = sum(row.get("conversion_started", 0) for row in vacancies)
    conversion_submitted = sum(row.get("conversion_submitted", 0) for row in vacancies)
    totals["conversion_started"] = conversion_started
    totals["conversion_submitted"] = conversion_submitted
    totals["conversion_rate"] = percentage(conversion_submitted, conversion_started)
    funnel["view_to_start_rate"] = percentage(funnel["funnel_started"], funnel["funnel_viewed"])
    funnel["start_to_submit_rate"] = percentage(funnel["funnel_submitted"], funnel["funnel_started"])
    return {"period": period, "period_label": PERIODS[period], "bounds": bounds,
            "summary": totals, "funnel": funnel, "vacancies": vacancies,
            "has_events": bool(totals["active_users"])}
