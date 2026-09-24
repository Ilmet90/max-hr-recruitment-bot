"""Candidate conversations. MAX transport is used only at the delivery boundary."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from app import db


SOURCE_TABLES = {"question": "questions", "application": "applications", "appeal": "appeals"}


def ensure_conversations_schema() -> None:
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        statements = (
            """CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                messenger_user_id INTEGER NOT NULL UNIQUE REFERENCES messenger_users(id) ON DELETE RESTRICT,
                reply_token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_message_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS conversation_sources (
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                source_type TEXT NOT NULL CHECK (source_type IN ('question', 'application', 'appeal')),
                source_record_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (source_type, source_record_id))""",
            """CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                messenger TEXT NOT NULL, external_message_id TEXT,
                direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                sender_type TEXT NOT NULL CHECK (sender_type IN ('candidate', 'hr', 'superadmin')),
                sender_admin_id INTEGER REFERENCES admins(id) ON DELETE SET NULL,
                text TEXT NOT NULL CHECK (length(trim(text)) > 0),
                delivery_status TEXT NOT NULL CHECK (delivery_status IN
                    ('received', 'pending', 'sending', 'sent', 'failed', 'uncertain')),
                request_key TEXT UNIQUE, attempt_count INTEGER NOT NULL DEFAULT 0,
                send_started_at TEXT, sent_at TEXT, last_error_code TEXT,
                created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS admin_conversation_state (
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                reader_key TEXT NOT NULL,
                admin_id INTEGER REFERENCES admins(id) ON DELETE CASCADE,
                last_read_inbound_message_id INTEGER REFERENCES conversation_messages(id) ON DELETE SET NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (conversation_id, reader_key))""",
            """CREATE TABLE IF NOT EXISTS processed_max_updates (
                update_key TEXT PRIMARY KEY, processed_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_conversations_status_time ON conversations(status, last_message_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_conversation_messages_timeline ON conversation_messages(conversation_id, id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_messages_external ON conversation_messages(messenger, external_message_id) WHERE external_message_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_conversation_sources_conversation ON conversation_sources(conversation_id)",
            "CREATE INDEX IF NOT EXISTS idx_admin_conversation_reader ON admin_conversation_state(reader_key, conversation_id)",
            """CREATE TRIGGER IF NOT EXISTS trg_conversation_question_deleted AFTER DELETE ON questions
                BEGIN DELETE FROM conversation_sources WHERE source_type = 'question' AND source_record_id = OLD.id; END""",
            """CREATE TRIGGER IF NOT EXISTS trg_conversation_application_deleted AFTER DELETE ON applications
                BEGIN DELETE FROM conversation_sources WHERE source_type = 'application' AND source_record_id = OLD.id; END""",
            """CREATE TRIGGER IF NOT EXISTS trg_conversation_appeal_deleted AFTER DELETE ON appeals
                BEGIN DELETE FROM conversation_sources WHERE source_type = 'appeal' AND source_record_id = OLD.id; END""",
        )
        for statement in statements:
            conn.execute(statement)
        expected = {
            "conversations": {"id", "messenger_user_id", "reply_token", "status", "created_at", "updated_at", "last_message_at"},
            "conversation_sources": {"conversation_id", "source_type", "source_record_id", "created_at"},
            "conversation_messages": {"id", "conversation_id", "messenger", "external_message_id", "direction", "sender_type", "sender_admin_id", "text", "delivery_status", "request_key", "attempt_count", "send_started_at", "sent_at", "last_error_code", "created_at"},
            "admin_conversation_state": {"conversation_id", "reader_key", "admin_id", "last_read_inbound_message_id", "updated_at"},
            "processed_max_updates": {"update_key", "processed_at"},
        }
        for table, columns in expected.items():
            actual = {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual.keys():
                raise RuntimeError(f"Unexpected {table} schema")
            for column in columns - {"id", "update_key", "last_message_at", "external_message_id", "sender_admin_id", "request_key", "send_started_at", "sent_at", "last_error_code", "admin_id", "last_read_inbound_message_id"}:
                if not actual[column]["notnull"]:
                    raise RuntimeError(f"Unexpected {table}.{column} nullable column")
            integer_columns = {"id", "messenger_user_id", "conversation_id", "source_record_id", "sender_admin_id",
                               "attempt_count", "admin_id", "last_read_inbound_message_id"}
            for column in columns:
                expected_type = "INTEGER" if column in integer_columns else "TEXT"
                if actual[column]["type"].upper() != expected_type:
                    raise RuntimeError(f"Unexpected {table}.{column} type")
        foreign_keys = {
            "conversations": {"messenger_user_id": "messenger_users"},
            "conversation_sources": {"conversation_id": "conversations"},
            "conversation_messages": {"conversation_id": "conversations", "sender_admin_id": "admins"},
            "admin_conversation_state": {"conversation_id": "conversations", "admin_id": "admins", "last_read_inbound_message_id": "conversation_messages"},
        }
        for table, keys in foreign_keys.items():
            actual = {row["from"]: row["table"] for row in conn.execute(f"PRAGMA foreign_key_list({table})")}
            if any(actual.get(column) != target for column, target in keys.items()):
                raise RuntimeError(f"Unexpected {table} foreign keys")
        indexes = {
            "conversations": {"idx_conversations_status_time": ["status", "last_message_at", "id"]},
            "conversation_sources": {"idx_conversation_sources_conversation": ["conversation_id"]},
            "conversation_messages": {
                "idx_conversation_messages_timeline": ["conversation_id", "id"],
                "idx_conversation_messages_external": ["messenger", "external_message_id"],
            },
            "admin_conversation_state": {"idx_admin_conversation_reader": ["reader_key", "conversation_id"]},
        }
        for table, specs in indexes.items():
            present = {row["name"] for row in conn.execute(f"PRAGMA index_list({table})")}
            for name, columns in specs.items():
                if name not in present or [row["name"] for row in conn.execute(f"PRAGMA index_info({name})")] != columns:
                    raise RuntimeError(f"Unexpected {name} index")
        for table, required in {
            "conversations": [["messenger_user_id"], ["reply_token"]],
            "conversation_sources": [["source_type", "source_record_id"]],
            "conversation_messages": [["request_key"]],
            "admin_conversation_state": [["conversation_id", "reader_key"]],
        }.items():
            unique = [row["name"] for row in conn.execute(f"PRAGMA index_list({table})") if row["unique"]]
            definitions = [[row["name"] for row in conn.execute(f"PRAGMA index_info({name})")] for name in unique]
            if any(columns not in definitions for columns in required):
                raise RuntimeError(f"Unexpected {table} uniqueness")
        external = next((row for row in conn.execute("PRAGMA index_list(conversation_messages)")
                         if row["name"] == "idx_conversation_messages_external"), None)
        if not external or not external["unique"] or not external["partial"]:
            raise RuntimeError("External MAX message ID must have a partial unique index")


def _conversation_conn(conn: sqlite3.Connection, messenger_user_id: int) -> dict[str, Any]:
    user = conn.execute("SELECT id FROM messenger_users WHERE id = ?", (messenger_user_id,)).fetchone()
    if not user:
        raise ValueError("Candidate identity is not confirmed")
    now = db.utc_now_iso()
    conn.execute("""INSERT OR IGNORE INTO conversations
        (messenger_user_id, reply_token, created_at, updated_at)
        VALUES (?, ?, ?, ?)""", (messenger_user_id, secrets.token_hex(16), now, now))
    return dict(conn.execute("SELECT * FROM conversations WHERE messenger_user_id = ?", (messenger_user_id,)).fetchone())


def _lazy_sources_conn(conn: sqlite3.Connection, conversation: dict[str, Any]) -> None:
    linked = False
    for kind, table in SOURCE_TABLES.items():
        rows = conn.execute(f"SELECT id FROM {table} WHERE messenger_user_id = ?", (conversation["messenger_user_id"],))
        for row in rows:
            inserted = conn.execute("""INSERT OR IGNORE INTO conversation_sources
                (conversation_id, source_type, source_record_id, created_at) VALUES (?, ?, ?, ?)""",
                (conversation["id"], kind, row["id"], db.utc_now_iso()))
            linked = linked or bool(inserted.rowcount)
    if linked:
        conn.execute("UPDATE conversations SET status = 'open', updated_at = ? WHERE id = ?",
                     (db.utc_now_iso(), conversation["id"]))


def link_source_conn(conn: sqlite3.Connection, source_type: str, source_record_id: int,
                     messenger_user_id: int | None, text: str | None = None,
                     external_message_id: str | None = None) -> dict[str, Any] | None:
    if not messenger_user_id:
        return None
    table = SOURCE_TABLES.get(source_type)
    if not table:
        raise ValueError("Unknown source type")
    source = conn.execute(f"SELECT messenger_user_id FROM {table} WHERE id = ?", (source_record_id,)).fetchone()
    if not source or source["messenger_user_id"] != messenger_user_id:
        raise ValueError("Source does not belong to candidate")
    conversation = _conversation_conn(conn, messenger_user_id)
    now = db.utc_now_iso()
    conn.execute("""INSERT OR IGNORE INTO conversation_sources
        (conversation_id, source_type, source_record_id, created_at) VALUES (?, ?, ?, ?)""",
        (conversation["id"], source_type, source_record_id, now))
    if text and text.strip():
        _inbound_conn(conn, conversation["id"], text, external_message_id)
    conn.execute("UPDATE conversations SET status = 'open', updated_at = ? WHERE id = ?", (now, conversation["id"]))
    return conversation


def _inbound_conn(conn: sqlite3.Connection, conversation_id: int, text: str,
                  external_message_id: str | None) -> int | None:
    user = conn.execute("""SELECT u.messenger FROM conversations c JOIN messenger_users u
        ON u.id = c.messenger_user_id WHERE c.id = ?""", (conversation_id,)).fetchone()
    if not user or not text.strip():
        return None
    now = db.utc_now_iso()
    cursor = conn.execute("""INSERT OR IGNORE INTO conversation_messages
        (conversation_id, messenger, external_message_id, direction, sender_type, text,
         delivery_status, created_at) VALUES (?, ?, ?, 'inbound', 'candidate', ?, 'received', ?)""",
        (conversation_id, user["messenger"], external_message_id, text.strip(), now))
    if not cursor.rowcount:
        return None
    conn.execute("""UPDATE conversations SET status = 'open', updated_at = ?, last_message_at = ?
        WHERE id = ?""", (now, now, conversation_id))
    return int(cursor.lastrowid)


def add_inbound(messenger_user_id: int, text: str, external_message_id: str | None) -> tuple[dict[str, Any], bool] | None:
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conversation = conn.execute("SELECT * FROM conversations WHERE messenger_user_id = ?", (messenger_user_id,)).fetchone()
        if not conversation:
            return None
        message_id = _inbound_conn(conn, conversation["id"], text, external_message_id)
        return dict(conversation), message_id is not None


def get_by_user(messenger_user_id: int) -> dict[str, Any] | None:
    return db.fetch_one("SELECT * FROM conversations WHERE messenger_user_id = ?", (messenger_user_id,))


def get_by_id(conversation_id: int, lazy: bool = False) -> dict[str, Any] | None:
    with db.get_connection() as conn:
        if lazy:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row and lazy:
            _lazy_sources_conn(conn, dict(row))
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return dict(row) if row else None


def reader_key(admin: dict[str, Any]) -> str:
    return "superadmin" if admin.get("role") == "superadmin" else f"hr:{int(admin['id'])}"


def list_conversations(admin: dict[str, Any], status: str = "open") -> list[dict[str, Any]]:
    if status not in {"open", "closed"}:
        raise ValueError("Invalid status")
    return db.fetch_all("""SELECT c.*, u.display_name, u.username, u.first_seen_at, u.last_seen_at,
        (SELECT text FROM conversation_messages m WHERE m.conversation_id = c.id ORDER BY id DESC LIMIT 1) AS last_text,
        (SELECT COUNT(*) FROM conversation_messages m WHERE m.conversation_id = c.id
            AND m.direction = 'inbound' AND m.id > COALESCE(s.last_read_inbound_message_id, 0)) AS unread,
        (SELECT COUNT(*) FROM conversation_sources cs WHERE cs.conversation_id = c.id) AS source_count
        FROM conversations c JOIN messenger_users u ON u.id = c.messenger_user_id
        LEFT JOIN admin_conversation_state s ON s.conversation_id = c.id AND s.reader_key = ?
        WHERE c.status = ? ORDER BY MAX(COALESCE(c.last_message_at, ''), c.updated_at) DESC, c.id DESC LIMIT 200""",
        (reader_key(admin), status))


def detail(conversation_id: int) -> dict[str, Any] | None:
    conversation = get_by_id(conversation_id, lazy=True)
    if not conversation:
        return None
    user = db.fetch_one("SELECT * FROM messenger_users WHERE id = ?", (conversation["messenger_user_id"],))
    messages = db.fetch_all("SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))
    sources: dict[str, list[dict[str, Any]]] = {}
    for kind, table in SOURCE_TABLES.items():
        sources[kind] = db.fetch_all(f"""SELECT t.* FROM {table} t JOIN conversation_sources cs
            ON cs.source_record_id = t.id AND cs.source_type = ? WHERE cs.conversation_id = ? ORDER BY t.id DESC""",
            (kind, conversation_id))
    activity = db.fetch_all("""SELECT event_type, created_at, metadata FROM user_activity_events
        WHERE messenger_user_id = ? ORDER BY id DESC LIMIT 10""", (conversation["messenger_user_id"],))
    return {"conversation": conversation, "user": user, "messages": messages,
            "sources": sources, "activity": activity}


def mark_read(conversation_id: int, admin: dict[str, Any], rendered_last_inbound_id: int | None) -> None:
    if rendered_last_inbound_id is None:
        return
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""SELECT id FROM conversation_messages WHERE id = ? AND conversation_id = ?
            AND direction = 'inbound'""", (rendered_last_inbound_id, conversation_id)).fetchone()
        if not row:
            raise ValueError("Read boundary is not an inbound message in this conversation")
        key = reader_key(admin)
        conn.execute("""INSERT INTO admin_conversation_state
            (conversation_id, reader_key, admin_id, last_read_inbound_message_id, updated_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(conversation_id, reader_key) DO UPDATE SET
            last_read_inbound_message_id = MAX(COALESCE(last_read_inbound_message_id, 0), excluded.last_read_inbound_message_id),
            updated_at = excluded.updated_at""",
            (conversation_id, key, admin.get("id"), rendered_last_inbound_id, db.utc_now_iso()))


def set_status(conversation_id: int, status: str) -> bool:
    if status not in {"open", "closed"}:
        raise ValueError("Invalid status")
    with db.get_connection() as conn:
        return bool(conn.execute("UPDATE conversations SET status = ?, updated_at = ? WHERE id = ?",
                                 (status, db.utc_now_iso(), conversation_id)).rowcount)


def _valid_admin(admin: dict[str, Any] | None) -> bool:
    if not admin:
        return False
    if admin.get("role") == "superadmin":
        return True
    if not admin.get("id"):
        return False
    fresh = db.get_admin(int(admin["id"]))
    return bool(fresh and fresh.get("approved") == 1 and fresh.get("is_active") == 1
                and fresh.get("role") in {"hr_staff", "hr_head"})


def create_outbound(messenger_user_id: int, admin: dict[str, Any], text: str,
                    request_key: str) -> tuple[dict[str, Any], bool]:
    if not _valid_admin(admin):
        raise PermissionError("HR access required")
    text = text.strip()
    if not text or len(text) > 4000:
        raise ValueError("Сообщение должно содержать от 1 до 4000 символов")
    if not request_key or len(request_key) > 200:
        raise ValueError("Invalid request key")
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM conversation_messages WHERE request_key = ?", (request_key,)).fetchone()
        if existing:
            if int(existing["conversation_id"]) != int((conn.execute("SELECT id FROM conversations WHERE messenger_user_id = ?", (messenger_user_id,)).fetchone() or {"id": -1})["id"]):
                raise ValueError("Request key belongs to another candidate")
            return dict(existing), False
        user = conn.execute("SELECT * FROM messenger_users WHERE id = ?", (messenger_user_id,)).fetchone()
        if not user or user["messenger"] != "max" or not user["external_user_id"]:
            raise ValueError("Confirmed MAX identity required")
        conversation = _conversation_conn(conn, messenger_user_id)
        _lazy_sources_conn(conn, conversation)
        now = db.utc_now_iso()
        message_id = conn.execute("""INSERT INTO conversation_messages
            (conversation_id, messenger, direction, sender_type, sender_admin_id, text,
             delivery_status, request_key, created_at)
            VALUES (?, 'max', 'outbound', ?, ?, ?, 'pending', ?, ?)""",
            (conversation["id"], "superadmin" if admin.get("role") == "superadmin" else "hr",
             admin.get("id"), text, request_key, now)).lastrowid
        conn.execute("UPDATE conversations SET status = 'open', updated_at = ?, last_message_at = ? WHERE id = ?",
                     (now, now, conversation["id"]))
        # An HR has already contacted this candidate; queued interest notices are stale.
        conn.execute("""UPDATE interest_deliveries SET status = 'cancelled', updated_at = ?
            WHERE status IN ('pending', 'claimed', 'batched', 'failed') AND session_id IN
            (SELECT id FROM user_sessions WHERE messenger_user_id = ?)""", (now, messenger_user_id))
        return dict(conn.execute("SELECT * FROM conversation_messages WHERE id = ?", (message_id,)).fetchone()), True


def send_outbound(api: Any, messenger_user_id: int, admin: dict[str, Any], text: str,
                  request_key: str) -> dict[str, Any]:
    message, created = create_outbound(messenger_user_id, admin, text, request_key)
    if not created:
        return message
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute("""UPDATE conversation_messages SET delivery_status = 'sending',
            attempt_count = attempt_count + 1, send_started_at = ?
            WHERE id = ? AND delivery_status = 'pending'""", (db.utc_now_iso(), message["id"]))
        if claimed.rowcount != 1:
            return dict(conn.execute("SELECT * FROM conversation_messages WHERE id = ?", (message["id"],)).fetchone())
    user = db.fetch_one("""SELECT u.external_user_id FROM messenger_users u JOIN conversations c
        ON c.messenger_user_id = u.id WHERE c.id = ?""", (message["conversation_id"],))
    status, error, external_id = "uncertain", None, None
    try:
        result = api.send_message_once(text, user_id=user["external_user_id"])
        external_id = str(((result or {}).get("message") or {}).get("body", {}).get("mid") or (result or {}).get("message_id") or "") or None
        status = "sent"
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        status = "uncertain" if code is None or code >= 500 else "failed"
        error = f"http_{code}" if code else "http_unknown"
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        error = "network_ambiguous"
    except Exception as exc:
        error = type(exc).__name__
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""UPDATE conversation_messages SET delivery_status = ?, last_error_code = ?,
            external_message_id = ?, sent_at = ? WHERE id = ? AND delivery_status = 'sending'""",
            (status, error, external_id, db.utc_now_iso() if status == "sent" else None, message["id"]))
        return dict(conn.execute("SELECT * FROM conversation_messages WHERE id = ?", (message["id"],)).fetchone())


def recover_interrupted_sends() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sending = conn.execute("""UPDATE conversation_messages SET delivery_status = 'uncertain',
            last_error_code = 'interrupted' WHERE delivery_status = 'sending' AND send_started_at < ?""", (cutoff,)).rowcount
        pending = conn.execute("""UPDATE conversation_messages SET delivery_status = 'failed',
            last_error_code = 'interrupted_before_send' WHERE delivery_status = 'pending' AND created_at < ?""", (cutoff,)).rowcount
        return sending + pending


def retry_failed(api: Any, message_id: int, admin: dict[str, Any], request_key: str) -> dict[str, Any]:
    old = db.fetch_one("""SELECT m.*, c.messenger_user_id FROM conversation_messages m
        JOIN conversations c ON c.id = m.conversation_id WHERE m.id = ?""", (message_id,))
    if not old or old["delivery_status"] != "failed" or old["direction"] != "outbound":
        raise ValueError("Only failed outbound messages can be retried")
    return send_outbound(api, old["messenger_user_id"], admin, old["text"], request_key)


def conversation_for_token(token: str) -> dict[str, Any] | None:
    return db.fetch_one("SELECT * FROM conversations WHERE reply_token = ?", (token,))


def update_was_processed(key: str | None) -> bool:
    return bool(key and db.fetch_one("SELECT 1 FROM processed_max_updates WHERE update_key = ?", (key,)))


def record_processed_update(key: str | None) -> None:
    if key:
        db.execute("INSERT OR IGNORE INTO processed_max_updates (update_key, processed_at) VALUES (?, ?)",
                   (key, db.utc_now_iso()))
