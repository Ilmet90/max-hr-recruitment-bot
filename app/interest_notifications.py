"""Persistent, per-recipient MAX interest notifications."""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import requests

from app import db
from app.max_api import MaxAPI, MaxApiError, callback_keyboard
from app.max_mentions import candidate_identity, escape_markdown

LOG = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")
RETRY_DELAYS = (300, 900, 3600, 10800)
MAX_ATTEMPTS = 5
CLAIM_LIFETIME = timedelta(minutes=2)
EVENT_LABELS = {
    "bot_started": "Открыл бота", "main_menu_opened": "Открыл меню",
    "vacancies_opened": "Открыл вакансии", "vacancy_viewed": "Просмотрел вакансию",
    "vacancy_apply_started": "Начал отклик", "conditions_opened": "Открыл условия службы",
    "question_section_opened": "Открыл раздел вопросов",
    "vacancy_application_submitted": "Отправил отклик",
    "question_sent": "Отправил вопрос", "appeal_sent": "Отправил обращение",
}


def stamp(value: datetime) -> str:
    return db._utc_iso(value)


def parse(value: str) -> datetime:
    return db._utc_datetime(value)


def local_time(value: str) -> str:
    return parse(value).astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def mode_label(mode: str) -> str:
    return {"off": "Выкл", "1h": "Через 1 час", "3h": "Через 3 часа", "daily": "Ежедневная сводка"}.get(mode, "Через 3 часа")


def allowed_admin(admin: dict[str, Any] | None) -> bool:
    return bool(admin and admin.get("approved") == 1 and admin.get("is_active") == 1
                and admin.get("can_receive_notifications") == 1
                and admin.get("role") in {"hr_staff", "hr_head"})


def _cutoff(conn: Any, admin: dict[str, Any]) -> str:
    activation = conn.execute("SELECT value FROM settings WHERE key = 'interest_notifications_activated_at'").fetchone()[0]
    return max(activation, admin.get("interest_mode_changed_at") or activation)


def _has_new_meaningful(conn: Any, session_id: int, cutoff: str) -> bool:
    placeholders = ",".join("?" for _ in db.MEANINGFUL_EVENTS)
    return conn.execute(
        f"SELECT 1 FROM user_activity_events WHERE session_id = ? AND event_type IN ({placeholders}) AND created_at >= ? LIMIT 1",
        (session_id, *sorted(db.MEANINGFUL_EVENTS), cutoff),
    ).fetchone() is not None


def _converted(conn: Any, session: dict[str, Any]) -> bool:
    if session["conversion_type"] is not None or session["converted_at"] is not None:
        return True
    placeholders = ",".join("?" for _ in db.CONVERSION_EVENTS)
    return conn.execute(
        f"SELECT 1 FROM user_activity_events WHERE messenger_user_id = ? AND event_type IN ({placeholders}) AND created_at >= ? LIMIT 1",
        (session["messenger_user_id"], *sorted(db.CONVERSION_EVENTS), session["first_meaningful_at"] or session["started_at"]),
    ).fetchone() is not None


def _due(session: dict[str, Any], mode: str) -> str:
    last = parse(session["last_activity_at"])
    if mode == "daily":
        day = last.astimezone(MSK).date() + timedelta(days=1)
        return stamp(datetime(day.year, day.month, day.day, 9, tzinfo=MSK))
    return stamp(last + timedelta(hours=1 if mode == "1h" else 3))


def _cooldown_until(conn: Any, admin_id: int, user_id: int) -> str | None:
    row = conn.execute("""SELECT MAX(COALESCE(d.sent_at, d.updated_at)) AS last_sent FROM interest_deliveries d
        JOIN user_sessions s ON s.id = d.session_id
        WHERE d.admin_id = ? AND s.messenger_user_id = ? AND d.status IN ('sent', 'uncertain')""", (admin_id, user_id)).fetchone()
    return stamp(parse(row["last_sent"]) + timedelta(hours=24)) if row and row["last_sent"] else None


def _eligible(conn: Any, admin: dict[str, Any], session: dict[str, Any]) -> bool:
    session_id = session.get("session_id") or session["id"]
    contacted = conn.execute("""SELECT 1 FROM conversation_messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE c.messenger_user_id = ? AND m.direction = 'outbound'
        AND m.delivery_status IN ('sent', 'uncertain')
        AND m.created_at >= ? LIMIT 1""", (session["messenger_user_id"], session["started_at"])).fetchone()
    return (allowed_admin(admin) and admin["interest_mode"] != "off"
            and session["meaningful_activity"] == 1 and not _converted(conn, session)
            and not contacted and _has_new_meaningful(conn, session_id, _cutoff(conn, admin)))


def materialize(at: datetime | None = None) -> None:
    now = stamp(at or datetime.now(timezone.utc))
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        admins = [dict(row) for row in conn.execute("""SELECT * FROM admins WHERE approved = 1 AND is_active = 1
            AND can_receive_notifications = 1 AND role IN ('hr_staff', 'hr_head') AND interest_mode != 'off'""")]
        sessions = [dict(row) for row in conn.execute("""SELECT s.* FROM user_sessions s
            JOIN messenger_users u ON u.id = s.messenger_user_id WHERE u.messenger = 'max'
            AND s.meaningful_activity = 1 AND s.conversion_type IS NULL
            AND s.last_activity_at >= (SELECT value FROM settings WHERE key = 'interest_notifications_activated_at')
            AND NOT EXISTS (SELECT 1 FROM conversation_messages m JOIN conversations c ON c.id = m.conversation_id
                WHERE c.messenger_user_id = s.messenger_user_id AND m.direction = 'outbound'
                AND m.delivery_status IN ('sent', 'uncertain')
                AND m.created_at >= s.started_at)""")]
        for admin in admins:
            for session in sessions:
                if not _eligible(conn, admin, session):
                    continue
                due = _due(session, admin["interest_mode"])
                if admin["interest_mode"] != "daily":
                    cool = _cooldown_until(conn, admin["id"], session["messenger_user_id"])
                    due = max(due, cool or due)
                conn.execute("""INSERT OR IGNORE INTO interest_deliveries
                    (admin_id, session_id, mode, due_at, status, action_token, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
                    (admin["id"], session["id"], admin["interest_mode"], due, secrets.token_hex(8), now, now))
                conn.execute("""UPDATE interest_deliveries SET mode = ?, due_at = ?,
                    attempt_count = CASE WHEN status = 'cancelled' THEN 0 ELSE attempt_count END,
                    next_attempt_at = CASE WHEN status = 'cancelled' THEN NULL ELSE next_attempt_at END,
                    last_error_code = CASE WHEN status = 'cancelled' THEN NULL ELSE last_error_code END,
                    digest_id = CASE WHEN status = 'cancelled' THEN NULL ELSE digest_id END,
                    status = CASE WHEN status = 'cancelled' THEN 'pending' ELSE status END, updated_at = ?
                    WHERE admin_id = ? AND session_id = ? AND status IN ('pending', 'failed', 'cancelled')""",
                    (admin["interest_mode"], due, now, admin["id"], session["id"]))
        active = conn.execute("""SELECT id, admin_id, session_id FROM interest_deliveries
            WHERE status IN ('pending', 'failed')""").fetchall()
        for item in active:
            admin_row = conn.execute("SELECT * FROM admins WHERE id = ?", (item["admin_id"],)).fetchone()
            session_row = conn.execute("SELECT * FROM user_sessions WHERE id = ?", (item["session_id"],)).fetchone()
            if not admin_row or not session_row or not _eligible(conn, dict(admin_row), dict(session_row)):
                conn.execute("UPDATE interest_deliveries SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, item["id"]))
        # The newest useful session replaces older unsent sessions for this HR/user.
        rows = conn.execute("""SELECT d.id, d.admin_id, s.messenger_user_id, s.started_at
            FROM interest_deliveries d JOIN user_sessions s ON s.id = d.session_id
            WHERE d.status IN ('pending', 'failed') AND d.mode != 'daily'
            ORDER BY s.started_at DESC, s.id DESC""").fetchall()
        newest: set[tuple[int, int]] = set()
        for row in rows:
            key = (row["admin_id"], row["messenger_user_id"])
            if key in newest:
                conn.execute("UPDATE interest_deliveries SET status = 'superseded', updated_at = ? WHERE id = ?", (now, row["id"]))
            else:
                newest.add(key)


def _repair_claims(now: str) -> None:
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("interest_deliveries", "interest_digests"):
            conn.execute(f"""UPDATE {table} SET status = 'pending', claim_token = NULL, claim_expires_at = NULL,
                updated_at = ? WHERE status = 'claimed' AND claim_expires_at <= ?""", (now, now))
            conn.execute(f"""UPDATE {table} SET status = 'uncertain', last_error_code = 'expired_sending',
                claim_token = NULL, claim_expires_at = NULL, updated_at = ?
                WHERE status = 'sending' AND claim_expires_at <= ?""", (now, now))
        conn.execute("""UPDATE interest_deliveries SET status = 'uncertain', last_error_code = 'expired_sending', updated_at = ?
            WHERE status = 'batched' AND digest_id IN
                (SELECT id FROM interest_digests WHERE status = 'uncertain' AND last_error_code = 'expired_sending')""", (now,))


def _claim(table: str, row_id: int, now: str) -> str | None:
    token = secrets.token_hex(16)
    expiry = stamp(parse(now) + CLAIM_LIFETIME)
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = conn.execute(f"""UPDATE {table} SET status = 'claimed', claim_token = ?, claim_expires_at = ?, updated_at = ?
            WHERE id = ? AND (status = 'pending' OR (status = 'failed' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?))""",
            (token, expiry, now, row_id, now))
        return token if result.rowcount == 1 else None


def _prepare_single(delivery_id: int, token: str, now: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM interest_deliveries WHERE id = ? AND status = 'claimed' AND claim_token = ?", (delivery_id, token)).fetchone()
        if row is None:
            return None
        delivery = dict(row)
        admin_row = conn.execute("SELECT * FROM admins WHERE id = ?", (delivery["admin_id"],)).fetchone()
        session_row = conn.execute("SELECT * FROM user_sessions WHERE id = ?", (delivery["session_id"],)).fetchone()
        admin, session = dict(admin_row) if admin_row else None, dict(session_row) if session_row else None
        if not admin or not session or not _eligible(conn, admin, session) or admin["interest_mode"] != delivery["mode"]:
            conn.execute("UPDATE interest_deliveries SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, delivery_id))
            return None
        due = _due(session, delivery["mode"])
        cool = _cooldown_until(conn, admin["id"], session["messenger_user_id"])
        ready = max(due, cool or due)
        if ready > now:
            conn.execute("UPDATE interest_deliveries SET status = 'pending', due_at = ?, claim_token = NULL, claim_expires_at = NULL, updated_at = ? WHERE id = ?", (ready, now, delivery_id))
            return None
        newer = conn.execute("""SELECT s.id FROM user_sessions s WHERE s.messenger_user_id = ? AND s.id > ?
            AND s.meaningful_activity = 1 AND s.conversion_type IS NULL LIMIT 1""", (session["messenger_user_id"], session["id"])).fetchone()
        if newer:
            conn.execute("UPDATE interest_deliveries SET status = 'superseded', updated_at = ? WHERE id = ?", (now, delivery_id))
            return None
        conn.execute("UPDATE interest_deliveries SET status = 'sending', updated_at = ? WHERE id = ?", (now, delivery_id))
        return delivery, admin, session


def _confirm_single_sending(delivery_id: int, token: str, now: str) -> bool:
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM interest_deliveries WHERE id = ? AND status = 'sending' AND claim_token = ?", (delivery_id, token)).fetchone()
        if row is None:
            return False
        admin_row = conn.execute("SELECT * FROM admins WHERE id = ?", (row["admin_id"],)).fetchone()
        session_row = conn.execute("SELECT * FROM user_sessions WHERE id = ?", (row["session_id"],)).fetchone()
        admin = dict(admin_row) if admin_row else None
        session = dict(session_row) if session_row else None
        if not admin or not session or admin["interest_mode"] != row["mode"] or not _eligible(conn, admin, session):
            conn.execute("UPDATE interest_deliveries SET status = 'cancelled', claim_token = NULL, claim_expires_at = NULL, updated_at = ? WHERE id = ?", (now, delivery_id))
            return False
        due = _due(session, row["mode"])
        cool = _cooldown_until(conn, admin["id"], session["messenger_user_id"])
        ready = max(due, cool or due)
        if ready > now:
            conn.execute("UPDATE interest_deliveries SET status = 'pending', due_at = ?, claim_token = NULL, claim_expires_at = NULL, updated_at = ? WHERE id = ?", (ready, now, delivery_id))
            return False
        newer = conn.execute("""SELECT 1 FROM user_sessions WHERE messenger_user_id = ? AND id > ?
            AND meaningful_activity = 1 AND conversion_type IS NULL LIMIT 1""", (session["messenger_user_id"], session["id"])).fetchone()
        if newer:
            conn.execute("UPDATE interest_deliveries SET status = 'superseded', claim_token = NULL, claim_expires_at = NULL, updated_at = ? WHERE id = ?", (now, delivery_id))
            return False
        conn.execute("UPDATE interest_deliveries SET attempt_count = attempt_count + 1, updated_at = ? WHERE id = ?", (now, delivery_id))
        return True


def _send_once(api: MaxAPI, admin: dict[str, Any], text: str,
               keyboard: dict[str, Any] | None = None, format: str | None = None) -> Any:
    chat_id = str(admin.get("chat_id") or "")
    user_id = str(admin.get("max_user_id") or "")
    if not chat_id and not user_id:
        raise ValueError("missing_recipient")
    try:
        return api.send_message_once(text, chat_id=chat_id or None, user_id=None if chat_id else user_id,
                                     keyboard=keyboard, format=format)
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        if code == 404 and chat_id and user_id:
            return api.send_message_once(text, user_id=user_id, keyboard=keyboard, format=format)
        if code == 400 and keyboard:
            return api.send_message_once(text, chat_id=chat_id or None, user_id=None if chat_id else user_id,
                                         format=format)
        raise


def _finish(table: str, row_id: int, token: str, error: Exception | None, now: str) -> str:
    status = "sent"
    code = None
    next_attempt = None
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT attempt_count FROM {table} WHERE id = ? AND status = 'sending' AND claim_token = ?", (row_id, token)).fetchone()
        if row is None:
            return "stale"
        if error is not None:
            code = type(error).__name__
            if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
                code = f"http_{error.response.status_code}"
                if error.response.status_code == 429 and row["attempt_count"] < MAX_ATTEMPTS:
                    status = "failed"
                    next_attempt = stamp(parse(now) + timedelta(seconds=RETRY_DELAYS[row["attempt_count"] - 1]))
                elif error.response.status_code == 408 or error.response.status_code >= 500:
                    status = "uncertain"
                else:
                    status = "failed"
            elif isinstance(error, MaxApiError) or (isinstance(error, ValueError) and str(error) == "missing_recipient"):
                status = "failed"
            else:
                status = "uncertain"
        conn.execute(f"""UPDATE {table} SET status = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
            next_attempt_at = ?, last_error_code = ?, claim_token = NULL, claim_expires_at = NULL, updated_at = ?
            WHERE id = ? AND status = 'sending' AND claim_token = ?""",
            (status, status, now, next_attempt, code, now, row_id, token))
        if table == "interest_digests":
            item_status = ("sent" if status == "sent" else "uncertain" if status == "uncertain"
                           else "failed" if status == "failed" and next_attempt is None else "batched")
            conn.execute("""UPDATE interest_deliveries SET status = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                updated_at = ? WHERE digest_id = ? AND status = 'batched'""", (item_status, status, now, now, row_id))
    LOG.info("Interest delivery result id=%s kind=%s status=%s error=%s", row_id, table, status, code)
    return status


def _event_title(event: dict[str, Any]) -> str:
    metadata = {}
    try:
        metadata = json.loads(event.get("metadata") or "{}")
    except (TypeError, ValueError):
        pass
    if not isinstance(metadata, dict):
        metadata = {}
    title = str(metadata.get("vacancy_title") or "").strip()
    if not title and event.get("vacancy_id"):
        vacancy = db.get_vacancy(int(event["vacancy_id"]))
        title = str(vacancy.get("title") or "") if vacancy else ""
    return title


def _identity(user: dict[str, Any]) -> str:
    name = str(user.get("display_name") or "").strip()
    username = str(user.get("username") or "").strip()
    parts = [name] if name else []
    if username and username != name:
        parts.append("@" + username.lstrip("@"))
    return " · ".join(parts) or "Пользователь MAX"


def _candidate_markdown(text: str, user: dict[str, Any]) -> str:
    heading, separator, rest = text.partition("\n")
    identity, next_separator, tail = rest.partition("\n")
    if separator and identity == _identity(user):
        return (escape_markdown(heading) + "\n" + candidate_identity(user)
                + ("\n" + escape_markdown(tail) if next_separator else ""))
    return escape_markdown(text)


def _clip_markdown(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 2].rsplit("\n", 1)[0] + "\n…"


def _session_summary(session: dict[str, Any], user: dict[str, Any]) -> str:
    events = db.fetch_all("SELECT * FROM user_activity_events WHERE session_id = ? ORDER BY created_at, id", (session["id"],))
    views: dict[str, int] = {}
    labels: list[str] = []
    for event in events:
        kind = event["event_type"]
        if kind == "vacancy_viewed":
            title = _event_title(event) or "Вакансия (название недоступно)"
            views[title] = views.get(title, 0) + 1
        elif kind in {"vacancies_opened", "vacancy_apply_started", "conditions_opened", "question_section_opened"}:
            label = EVENT_LABELS[kind]
            if kind == "vacancy_apply_started":
                title = _event_title(event)
                if title:
                    label += ": " + title
            if label not in labels:
                labels.append(label)
    lines = ["Пользователь проявил интерес", _identity(user),
             "Впервые: " + local_time(user["first_seen_at"]),
             "Последняя активность: " + local_time(session["last_activity_at"]), "", "Интересовался:"]
    lines += ["• " + label for label in labels]
    lines += [f"• {title} — {count} просм." for title, count in views.items()]
    lines.append("\nОтклик, вопрос или обращение не оставлены.")
    return "\n".join(lines)[:3500]


def _single_message(delivery: dict[str, Any], session: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    user = db.fetch_one("SELECT * FROM messenger_users WHERE id = ?", (session["messenger_user_id"],)) or {}
    token = delivery["action_token"]
    return _clip_markdown(_candidate_markdown(_session_summary(session, user), user), 3350), callback_keyboard([
        [("История активности", f"ih:{token}")],
        [("Написать кандидату", f"ir:{token}")],
    ])


def process_singles(api: MaxAPI, now: str, limit: int = 20) -> None:
    rows = db.fetch_all("""SELECT id FROM interest_deliveries WHERE mode IN ('1h', '3h') AND due_at <= ?
        AND (status = 'pending' OR (status = 'failed' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?))
        ORDER BY due_at, id LIMIT ?""", (now, now, limit))
    for row in rows:
        token = _claim("interest_deliveries", row["id"], now)
        if not token:
            continue
        prepared = _prepare_single(row["id"], token, now)
        if not prepared:
            continue
        delivery, admin, session = prepared
        message, keyboard = _single_message(delivery, session)
        if not _confirm_single_sending(row["id"], token, now):
            continue
        error = None
        try:
            _send_once(api, admin, message, keyboard, format="markdown")
        except Exception as exc:
            error = exc
        _finish("interest_deliveries", row["id"], token, error, now)


def _daily_cutoff(now: datetime) -> str:
    local = now.astimezone(MSK)
    return stamp(datetime(local.year, local.month, local.day, tzinfo=MSK))


def _confirm_digest_sending(digest_id: int, token: str, admin_id: int, start: str, now: str) -> bool:
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        digest = conn.execute("SELECT * FROM interest_digests WHERE id = ? AND status = 'sending' AND claim_token = ?", (digest_id, token)).fetchone()
        if digest is None:
            return False
        admin_row = conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
        admin = dict(admin_row) if admin_row else None
        if not allowed_admin(admin) or admin["interest_mode"] != "daily":
            conn.execute("UPDATE interest_deliveries SET status = 'cancelled', updated_at = ? WHERE digest_id = ? AND status = 'batched'", (now, digest_id))
            conn.execute("UPDATE interest_digests SET status = 'cancelled', claim_token = NULL, claim_expires_at = NULL, updated_at = ? WHERE id = ?", (now, digest_id))
            return False
        items = [dict(row) for row in conn.execute("""SELECT d.*, s.messenger_user_id, s.last_activity_at,
            s.meaningful_activity, s.conversion_type, s.converted_at, s.first_meaningful_at, s.started_at
            FROM interest_deliveries d JOIN user_sessions s ON s.id = d.session_id
            WHERE d.digest_id = ? AND d.status = 'batched'""", (digest_id,))]
        for item in items:
            cool = _cooldown_until(conn, admin_id, item["messenger_user_id"])
            if (not _eligible(conn, admin, item) or item["last_activity_at"] >= start
                    or parse(now) - parse(item["last_activity_at"]) < timedelta(hours=3)
                    or (cool is not None and cool > now)):
                conn.execute("UPDATE interest_deliveries SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, item["id"]))
        remaining = conn.execute("SELECT COUNT(*) FROM interest_deliveries WHERE digest_id = ? AND status = 'batched'", (digest_id,)).fetchone()[0]
        if remaining != len(items):
            status = "pending" if remaining else "cancelled"
            conn.execute("UPDATE interest_digests SET status = ?, claim_token = NULL, claim_expires_at = NULL, updated_at = ? WHERE id = ?", (status, now, digest_id))
            return False
        conn.execute("UPDATE interest_digests SET attempt_count = attempt_count + 1, updated_at = ? WHERE id = ?", (now, digest_id))
        return True


def process_daily(api: MaxAPI, at: datetime) -> None:
    now = stamp(at)
    local = at.astimezone(MSK)
    if local.hour < 9:
        return
    local_date = local.date().isoformat()
    start = _daily_cutoff(at)
    for admin in db.notification_admins():
        if admin.get("interest_mode") != "daily":
            continue
        with db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("""SELECT id FROM interest_digests WHERE admin_id = ? AND local_date < ?
                AND (status IN ('pending', 'claimed') OR (status = 'failed' AND next_attempt_at <= ?))""",
                (admin["id"], local_date, now)).fetchall()
            for stale in old:
                conn.execute("UPDATE interest_deliveries SET status = 'pending', digest_id = NULL, updated_at = ? WHERE digest_id = ? AND status = 'batched'", (now, stale["id"]))
                conn.execute("UPDATE interest_digests SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, stale["id"]))
            sent_today = conn.execute("""SELECT 1 FROM interest_digests WHERE admin_id = ? AND status = 'sent'
                AND sent_at >= ? LIMIT 1""", (admin["id"], start)).fetchone()
            unresolved = conn.execute("""SELECT 1 FROM interest_digests WHERE admin_id = ? AND local_date < ?
                AND (status = 'sending' OR (status = 'uncertain' AND updated_at > ?)
                    OR (status = 'failed' AND next_attempt_at > ?)) LIMIT 1""",
                (admin["id"], local_date, stamp(at - timedelta(hours=24)), now)).fetchone()
        if sent_today or unresolved:
            continue
        digest = db.fetch_one("SELECT * FROM interest_digests WHERE admin_id = ? AND local_date = ?", (admin["id"], local_date))
        if digest and digest["status"] in {"sent", "sending", "uncertain", "cancelled"}:
            continue
        if digest is None:
            with db.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT * FROM interest_digests WHERE admin_id = ? AND local_date = ?", (admin["id"], local_date)).fetchone()
                if existing:
                    digest = dict(existing)
                else:
                    candidates = [dict(row) for row in conn.execute("""SELECT d.*, s.messenger_user_id, s.last_activity_at, s.meaningful_activity,
                    s.conversion_type, s.converted_at, s.first_meaningful_at, s.started_at
                    FROM interest_deliveries d JOIN user_sessions s ON s.id = d.session_id
                    WHERE d.admin_id = ? AND d.mode = 'daily' AND d.status = 'pending' AND d.due_at <= ?
                    AND s.last_activity_at < ? ORDER BY s.last_activity_at DESC, d.id DESC""", (admin["id"], now, start))]
                    selected: list[dict[str, Any]] = []
                    users: set[int] = set()
                    overflow: set[int] = set()
                    for item in candidates:
                        if not _eligible(conn, admin, item):
                            conn.execute("UPDATE interest_deliveries SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, item["id"]))
                            continue
                        if parse(now) - parse(item["last_activity_at"]) < timedelta(hours=3):
                            continue
                        cool = _cooldown_until(conn, admin["id"], item["messenger_user_id"])
                        if cool and cool > now:
                            continue
                        if item["messenger_user_id"] not in users and len(users) >= 20:
                            overflow.add(item["messenger_user_id"])
                            continue
                        users.add(item["messenger_user_id"])
                        selected.append(item)
                    if not selected:
                        continue
                    action = secrets.token_hex(8)
                    digest_id = conn.execute("""INSERT INTO interest_digests
                    (admin_id, local_date, status, action_token, remaining_count, created_at, updated_at)
                    VALUES (?, ?, 'pending', ?, ?, ?, ?)""", (admin["id"], local_date, action, len(overflow), now, now)).lastrowid
                    for item in selected:
                        conn.execute("UPDATE interest_deliveries SET status = 'batched', digest_id = ?, updated_at = ? WHERE id = ?", (digest_id, now, item["id"]))
                    digest = {"id": digest_id, "action_token": action, "remaining_count": len(overflow)}
        token = _claim("interest_digests", digest["id"], now)
        if not token:
            continue
        with db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM interest_digests WHERE id = ? AND status = 'claimed' AND claim_token = ?", (digest["id"], token)).fetchone()
            if not current:
                continue
            fresh_admin = conn.execute("SELECT * FROM admins WHERE id = ?", (admin["id"],)).fetchone()
            if not fresh_admin or not allowed_admin(dict(fresh_admin)) or fresh_admin["interest_mode"] != "daily":
                conn.execute("UPDATE interest_digests SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, digest["id"]))
                conn.execute("UPDATE interest_deliveries SET status = 'cancelled', updated_at = ? WHERE digest_id = ? AND status = 'batched'", (now, digest["id"]))
                continue
            items = [dict(row) for row in conn.execute("""SELECT d.*, s.messenger_user_id, s.last_activity_at, s.meaningful_activity,
                s.conversion_type, s.converted_at, s.first_meaningful_at, s.started_at
                FROM interest_deliveries d JOIN user_sessions s ON s.id = d.session_id
                WHERE d.digest_id = ? AND d.status = 'batched'""", (digest["id"],))]
            valid = []
            for item in items:
                cool = _cooldown_until(conn, admin["id"], item["messenger_user_id"])
                if (_eligible(conn, dict(fresh_admin), item) and item["last_activity_at"] < start
                        and parse(now) - parse(item["last_activity_at"]) >= timedelta(hours=3)
                        and (cool is None or cool <= now)):
                    valid.append(item)
                else:
                    conn.execute("UPDATE interest_deliveries SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, item["id"]))
            if not valid:
                conn.execute("UPDATE interest_digests SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, digest["id"]))
                continue
            conn.execute("UPDATE interest_digests SET status = 'sending', updated_at = ? WHERE id = ?", (now, digest["id"]))
        unique_users: dict[int, dict[str, Any]] = {}
        for item in valid:
            uid = item["messenger_user_id"]
            if uid not in unique_users or item["last_activity_at"] > unique_users[uid]["last_activity_at"]:
                unique_users[uid] = item
        lines = [f"Сводка интереса за {local_date}", ""]
        for item in unique_users.values():
            user = db.fetch_one("SELECT * FROM messenger_users WHERE id = ?", (item["messenger_user_id"],)) or {}
            lines.append(f"• {candidate_identity(user)} — {escape_markdown(local_time(item['last_activity_at']))}")
        if digest.get("remaining_count"):
            lines.append(f"\nЕщё пользователей в очереди: {digest['remaining_count']}")
        lines.append("\nОтклики, вопросы или обращения не оставлены.")
        keyboard = callback_keyboard([[("Открыть список", f"id:{digest['action_token']}")]])
        if not _confirm_digest_sending(digest["id"], token, admin["id"], start, now):
            continue
        error = None
        try:
            _send_once(api, admin, _clip_markdown("\n".join(lines), 3350), keyboard, format="markdown")
        except Exception as exc:
            error = exc
        _finish("interest_digests", digest["id"], token, error, now)


def run_once(api: MaxAPI, at: datetime | None = None) -> None:
    now_dt = at or datetime.now(timezone.utc)
    now = stamp(now_dt)
    _repair_claims(now)
    materialize(now_dt)
    process_singles(api, now)
    process_daily(api, now_dt)


def history_for_token(token: str, admin: dict[str, Any]) -> tuple[str, dict[str, Any] | None] | None:
    if not allowed_admin(admin):
        return None
    row = db.fetch_one("SELECT * FROM interest_deliveries WHERE action_token = ? AND admin_id = ? AND status = 'sent'", (token, admin["id"]))
    if not row:
        return None
    session = db.fetch_one("SELECT * FROM user_sessions WHERE id = ?", (row["session_id"],))
    if not session:
        return None
    user = db.fetch_one("SELECT * FROM messenger_users WHERE id = ?", (session["messenger_user_id"],)) or {}
    events = db.fetch_all("""SELECT * FROM user_activity_events WHERE messenger_user_id = ?
        ORDER BY created_at DESC, id DESC LIMIT 25""", (session["messenger_user_id"],))
    lines = ["История активности", _identity(user), "Впервые: " + local_time(user["first_seen_at"]),
             "Последняя активность: " + local_time(user["last_seen_at"]), ""]
    for event in events:
        label = EVENT_LABELS.get(event["event_type"], event["event_type"])
        title = _event_title(event) if event["event_type"] in {"vacancy_viewed", "vacancy_apply_started"} else ""
        lines.append(f"{local_time(event['created_at'])}: {label}{' — ' + title if title else ''}")
    return _clip_markdown(_candidate_markdown("\n".join(lines), user), 3500), callback_keyboard([[("Написать кандидату", f"ir:{token}")]])


def digest_for_token(token: str, admin: dict[str, Any]) -> tuple[str, dict[str, Any] | None] | None:
    if not allowed_admin(admin):
        return None
    digest = db.fetch_one("SELECT * FROM interest_digests WHERE action_token = ? AND admin_id = ? AND status = 'sent'", (token, admin["id"]))
    if not digest:
        return None
    items = db.fetch_all("""SELECT d.action_token, s.messenger_user_id, MAX(s.last_activity_at) AS last_activity_at
        FROM interest_deliveries d JOIN user_sessions s ON s.id = d.session_id
        WHERE d.digest_id = ? AND d.status = 'sent' GROUP BY s.messenger_user_id ORDER BY last_activity_at DESC LIMIT 20""", (digest["id"],))
    lines = ["Пользователи сводки", ""]
    buttons = []
    for item in items:
        user = db.fetch_one("SELECT * FROM messenger_users WHERE id = ?", (item["messenger_user_id"],)) or {}
        lines.append(f"• {candidate_identity(user)} — {escape_markdown(local_time(item['last_activity_at']))}")
        buttons.append([(_identity(user)[:50], f"ih:{item['action_token']}")])
    return _clip_markdown("\n".join(lines), 3500), callback_keyboard(buttons) if buttons else None


def candidate_for_token(token: str, admin: dict[str, Any] | None) -> dict[str, Any] | None:
    if not allowed_admin(admin):
        return None
    return db.fetch_one("""SELECT u.* FROM interest_deliveries d
        JOIN user_sessions s ON s.id = d.session_id
        JOIN messenger_users u ON u.id = s.messenger_user_id
        WHERE d.action_token = ? AND d.admin_id = ? AND d.status = 'sent'""",
        (token, admin["id"]))
