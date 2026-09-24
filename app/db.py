from __future__ import annotations

import os
import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", PROJECT_ROOT / "data" / "bot.sqlite3"))
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = PROJECT_ROOT / DATABASE_PATH


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SESSION_INACTIVITY = timedelta(hours=3)
MEANINGFUL_EVENTS = frozenset({
    "vacancies_opened", "vacancy_viewed", "vacancy_apply_started",
    "conditions_opened", "question_section_opened",
})
CONVERSION_EVENTS = frozenset({"vacancy_application_submitted", "question_sent", "appeal_sent"})
ACTIVITY_EVENTS = frozenset({"bot_started", "main_menu_opened"}) | MEANINGFUL_EVENTS | CONVERSION_EVENTS
INTEREST_MODES = frozenset({"off", "1h", "3h", "daily"})


def dict_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


ROLE_LABELS = {
    "superadmin": "Главная учётная запись",
    "hr_head": "Начальник отдела кадров",
    "hr_staff": "Сотрудник отдела кадров",
    "pending": "Ожидает подтверждения",
    "disabled": "Отключён",
    "pending_rejected": "Отключён",
}

APPLICATION_STATUS_LABELS = {
    "new": "Новый",
    "in_work": "В работе",
    "done": "Обработан",
    "rejected": "Отклонён",
}

DEFAULT_ORG_SETTINGS = {
    "organization_full_name": "Название организации",
    "organization_short_name": "Организация",
    "parent_organization": "Вышестоящая организация",
    "organization_region": "Регион",
    "bot_display_name": "Кадровый чат-бот",
    "community_name": "Официальное сообщество организации",
    "hr_department_name": "кадровое подразделение",
    "public_welcome_title": "Здравствуйте!",
    "public_welcome_text": (
        "Это кадровый чат-бот организации.\n\n"
        "Здесь вы можете узнать об актуальных вакансиях, условиях работы или службы, порядке оформления, "
        "задать вопрос или направить сообщение."
    ),
    "public_menu_hint": "Выберите нужный раздел:",
    "application_success_text": "Ваш предварительный отклик принят.\n\nСотрудник кадрового подразделения свяжется с вами в рабочее время.",
    "question_success_text": "Ваш вопрос принят. Сотрудник ответит в рабочее время.",
    "appeal_success_text": "Ваше сообщение принято.",
    "personal_data_warning": (
        "Не направляйте через бот паспортные данные, медицинские документы, сведения о третьих лицах "
        "и иную конфиденциальную информацию. Для предварительной консультации достаточно ФИО, возраста, "
        "контактного телефона, образования и интересующей вакансии."
    ),
    "web_admin_title": "Панель управления кадровым чат-ботом",
    "web_admin_header": "Панель управления кадровым чат-ботом",
    "contacts_public_text": "По вопросам трудоустройства вы можете оставить сообщение через бота. Ответственный сотрудник свяжется с вами в рабочее время.",
    "theme_primary_color": "#071a2f",
    "theme_secondary_color": "#0d2f57",
    "theme_accent_color": "#c9a646",
}

ORG_SETTING_GROUPS = {
    "main": [
        "organization_full_name",
        "organization_short_name",
        "parent_organization",
        "organization_region",
        "community_name",
        "bot_display_name",
        "hr_department_name",
    ],
    "public_texts": [
        "public_welcome_title",
        "public_welcome_text",
        "public_menu_hint",
        "application_success_text",
        "question_success_text",
        "appeal_success_text",
        "personal_data_warning",
        "contacts_public_text",
    ],
    "web": ["web_admin_title", "web_admin_header"],
    "theme": ["theme_primary_color", "theme_secondary_color", "theme_accent_color"],
}

DEFAULT_UPDATE_SETTINGS = {
    "github_repo_url": "https://github.com/Ilmet90/max-hr-recruitment-bot.git",
    "installed_commit": "local",
    "auto_update_enabled": "1",
    "admin_service_name": "max-hr-admin.service",
    "bot_service_name": "max-hr-bot.service",
    "install_path": "/opt/max-hr-recruitment-bot",
    "update_last_at": "",
}

DEFAULT_TRUDVSEM_SETTINGS = {
    "trudvsem_enabled": "0",
    "trudvsem_company_code": "",
    "trudvsem_inn": "",
    "trudvsem_api_base": "http://opendata.trudvsem.ru/api/v1",
    "trudvsem_last_sync_at": "",
}

SERVICE_INFO_LABELS = {
    "conditions": "Условия службы",
    "order": "Порядок поступления на службу",
    "warning": "Предупреждение о персональных данных",
    "contacts": "Контакты кадрового подразделения",
}

SERVICE_INFO_HELPS = {
    "conditions": "Используется в MAX-боте в разделе “Условия службы”. Этот текст видят внешние пользователи при просмотре условий службы.",
    "order": "Используется в MAX-боте в разделе “Порядок поступления”. Здесь следует описать общий порядок обращения, консультации и дальнейшего оформления.",
    "warning": "Показывается перед сбором персональных данных: при отклике на вакансию, задании вопроса или отправке сообщения. Здесь следует предупредить пользователя, что не нужно направлять паспортные данные, медицинские документы и сведения о третьих лицах.",
    "contacts": "Используется в MAX-боте в разделе “Контакты”, если отдельные контакты не заполнены или как общий справочный текст.",
}


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(role or "pending", role or "Ожидает подтверждения")


def application_status_label(status: str | None) -> str:
    return APPLICATION_STATUS_LABELS.get(status or "new", status or "Новый")


def service_info_label(key: str | None) -> str:
    return SERVICE_INFO_LABELS.get(key or "", key or "")


def service_info_help(key: str | None) -> str:
    return SERVICE_INFO_HELPS.get(key or "", "")


def access_label(admin: dict[str, Any]) -> str:
    if admin.get("role") == "pending" or int(admin.get("approved") or 0) == 0:
        return "Ожидает подтверждения"
    if admin.get("role") == "disabled" or int(admin.get("is_active") or 0) == 0:
        return "Отключён"
    return "Активен"


def bool_label(value: Any) -> str:
    return "включено" if int(value or 0) == 1 else "отключено"


def validate_web_login(web_login: str) -> bool:
    return bool(re.fullmatch(r"[\w.\-А-Яа-яЁё]+", web_login or "", flags=re.UNICODE))


@contextmanager
def get_connection() -> Iterable[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        schema_sql = """
            CREATE TABLE IF NOT EXISTS vacancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                salary TEXT,
                duties TEXT,
                requirements TEXT,
                conditions TEXT,
                note TEXT,
                is_active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 100,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS service_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS service_photo_albums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                is_active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 100,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS service_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER,
                filename TEXT NOT NULL,
                original_name TEXT,
                caption TEXT,
                is_active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 100,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                value TEXT NOT NULL,
                note TEXT,
                is_active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 100
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_user_id TEXT,
                vacancy_id INTEGER,
                vacancy_title TEXT,
                full_name TEXT,
                age TEXT,
                phone TEXT,
                education TEXT,
                military_service TEXT,
                preferred_time TEXT,
                comment TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_user_id TEXT,
                question_text TEXT,
                contact TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS appeals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_user_id TEXT,
                full_name TEXT,
                phone TEXT,
                appeal_text TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_user_id TEXT UNIQUE,
                chat_id TEXT,
                display_name TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                role TEXT DEFAULT 'pending',
                approved INTEGER DEFAULT 0,
                web_login TEXT UNIQUE,
                password_hash TEXT,
                must_change_password INTEGER DEFAULT 1,
                can_use_bot_admin INTEGER DEFAULT 0,
                can_receive_notifications INTEGER DEFAULT 0,
                delegated_until TEXT,
                delegated_by_admin_id INTEGER,
                last_login_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                admin_name TEXT,
                action TEXT,
                target_type TEXT,
                target_id INTEGER,
                details TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        # executescript() commits an open transaction; execute each fixed DDL statement
        # so concurrent web/bot startups cannot race the legacy ALTER TABLE checks.
        for statement in schema_sql.split(";"):
            if statement.strip():
                conn.execute(statement)
        ensure_admins_schema(conn)
        ensure_applications_schema(conn)
        ensure_archive_schema(conn)
        ensure_vacancies_external_schema(conn)
        ensure_service_photos_schema(conn)
        seed_default_org_settings(conn)
        seed_default_update_settings(conn)
        seed_default_trudvsem_settings(conn)
        seed_db(conn)
    ensure_activity_schema()
    ensure_interest_notifications_schema()
    from app.conversations import ensure_conversations_schema
    ensure_conversations_schema()


def ensure_activity_schema() -> None:
    """Apply the additive v0.3.0 schema in one SQLite write transaction."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messenger_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                messenger TEXT NOT NULL CHECK (length(trim(messenger)) > 0),
                external_user_id TEXT NOT NULL CHECK (length(trim(external_user_id)) > 0),
                external_chat_id TEXT,
                display_name TEXT, first_name TEXT, last_name TEXT,
                username TEXT, avatar_url TEXT,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                UNIQUE (messenger, external_user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                messenger_user_id INTEGER NOT NULL REFERENCES messenger_users(id) ON DELETE RESTRICT,
                started_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
                meaningful_activity INTEGER NOT NULL DEFAULT 0 CHECK (meaningful_activity IN (0, 1)),
                first_meaningful_at TEXT,
                conversion_type TEXT CHECK (conversion_type IS NULL OR conversion_type IN
                    ('vacancy_application_submitted', 'question_sent', 'appeal_sent')),
                converted_at TEXT, interest_notification_due_at TEXT,
                interest_notification_sent_at TEXT, closed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                messenger_user_id INTEGER NOT NULL REFERENCES messenger_users(id) ON DELETE RESTRICT,
                session_id INTEGER NOT NULL REFERENCES user_sessions(id) ON DELETE RESTRICT,
                event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
                vacancy_id INTEGER, metadata TEXT, created_at TEXT NOT NULL
            )
        """)
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messenger_users)")}
        if "external_chat_id" not in user_columns:
            conn.execute("ALTER TABLE messenger_users ADD COLUMN external_chat_id TEXT")
        expected = {
            "messenger_users": {"id", "messenger", "external_user_id", "external_chat_id", "display_name", "first_name", "last_name", "username", "avatar_url", "first_seen_at", "last_seen_at"},
            "user_sessions": {"id", "messenger_user_id", "started_at", "last_activity_at", "meaningful_activity", "first_meaningful_at", "conversion_type", "converted_at", "interest_notification_due_at", "interest_notification_sent_at", "closed_at"},
            "user_activity_events": {"id", "messenger_user_id", "session_id", "event_type", "vacancy_id", "metadata", "created_at"},
        }
        for table, columns in expected.items():
            actual = {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual.keys():
                raise RuntimeError(f"Unexpected {table} schema: missing {sorted(columns - actual.keys())}")
            required = {
                "messenger_users": {"messenger": "TEXT", "external_user_id": "TEXT", "first_seen_at": "TEXT", "last_seen_at": "TEXT"},
                "user_sessions": {"messenger_user_id": "INTEGER", "started_at": "TEXT", "last_activity_at": "TEXT", "meaningful_activity": "INTEGER"},
                "user_activity_events": {"messenger_user_id": "INTEGER", "session_id": "INTEGER", "event_type": "TEXT", "created_at": "TEXT"},
            }[table]
            if any(actual[name]["type"].upper() != kind or actual[name]["notnull"] != 1 for name, kind in required.items()):
                raise RuntimeError(f"Unexpected {table} required column definition")
        chat_column = {row["name"]: row for row in conn.execute("PRAGMA table_info(messenger_users)")}["external_chat_id"]
        if chat_column["type"].upper() != "TEXT" or chat_column["notnull"]:
            raise RuntimeError("Unexpected messenger_users.external_chat_id definition")
        for table, targets in {
            "user_sessions": {"messenger_user_id": "messenger_users"},
            "user_activity_events": {"messenger_user_id": "messenger_users", "session_id": "user_sessions"},
        }.items():
            foreign_keys = {row["from"]: (row["table"], row["on_delete"]) for row in conn.execute(f"PRAGMA foreign_key_list({table})")}
            if any(foreign_keys.get(column) != (target, "RESTRICT") for column, target in targets.items()):
                raise RuntimeError(f"Unexpected {table} foreign keys")
        unique_indexes = [row["name"] for row in conn.execute("PRAGMA index_list(messenger_users)") if row["unique"]]
        if not any(
            [row["name"] for row in conn.execute(f"PRAGMA index_info({index})")] == ["messenger", "external_user_id"]
            for index in unique_indexes
        ):
            raise RuntimeError("messenger_users identity key is not unique")
        for table in ("applications", "questions", "appeals"):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "messenger_user_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN messenger_user_id INTEGER REFERENCES messenger_users(id) ON DELETE RESTRICT")
            foreign_keys = list(conn.execute(f"PRAGMA foreign_key_list({table})"))
            if not any(row["from"] == "messenger_user_id" and row["table"] == "messenger_users" for row in foreign_keys):
                raise RuntimeError(f"Unexpected {table}.messenger_user_id foreign key")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_messenger_user ON {table}(messenger_user_id)")
        for table in ("questions", "appeals"):
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_unlinked_max_user ON {table}(max_user_id) "
                "WHERE messenger_user_id IS NULL AND max_user_id IS NOT NULL"
            )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_sessions_one_open ON user_sessions(messenger_user_id) WHERE closed_at IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user_activity ON user_sessions(messenger_user_id, last_activity_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_user_time ON user_activity_events(messenger_user_id, created_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_session_time ON user_activity_events(session_id, created_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_type_time ON user_activity_events(event_type, created_at, id)")


def ensure_interest_notifications_schema() -> None:
    """Add per-HR delivery state without changing the v0.3.0 activity tables."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(admins)")}
        if "interest_mode" not in columns:
            conn.execute("ALTER TABLE admins ADD COLUMN interest_mode TEXT NOT NULL DEFAULT '3h' CHECK (interest_mode IN ('off', '1h', '3h', 'daily'))")
        if "interest_mode_changed_at" not in columns:
            conn.execute("ALTER TABLE admins ADD COLUMN interest_mode_changed_at TEXT")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('interest_notifications_activated_at', ?)", (utc_now_iso(),))
        conn.execute("""UPDATE admins SET interest_mode_changed_at =
            (SELECT value FROM settings WHERE key = 'interest_notifications_activated_at')
            WHERE interest_mode_changed_at IS NULL""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS interest_digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
                local_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                    ('pending', 'claimed', 'sending', 'sent', 'failed', 'uncertain', 'cancelled')),
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                next_attempt_at TEXT, claim_token TEXT, claim_expires_at TEXT,
                sent_at TEXT, last_error_code TEXT, action_token TEXT NOT NULL UNIQUE,
                remaining_count INTEGER NOT NULL DEFAULT 0 CHECK (remaining_count >= 0),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE (admin_id, local_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS interest_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
                session_id INTEGER NOT NULL REFERENCES user_sessions(id) ON DELETE RESTRICT,
                mode TEXT NOT NULL CHECK (mode IN ('1h', '3h', 'daily')),
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                    ('pending', 'claimed', 'sending', 'batched', 'sent', 'failed', 'uncertain', 'cancelled', 'superseded')),
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                next_attempt_at TEXT, claim_token TEXT, claim_expires_at TEXT,
                sent_at TEXT, last_error_code TEXT,
                digest_id INTEGER REFERENCES interest_digests(id) ON DELETE RESTRICT,
                action_token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE (admin_id, session_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interest_sessions_due ON user_sessions(last_activity_at, id) WHERE meaningful_activity = 1 AND conversion_type IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interest_deliveries_due ON interest_deliveries(status, due_at, next_attempt_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interest_deliveries_claim ON interest_deliveries(status, claim_expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interest_deliveries_session ON interest_deliveries(session_id, admin_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interest_deliveries_sent ON interest_deliveries(admin_id, status, sent_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interest_digests_due ON interest_digests(status, next_attempt_at, local_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_interest_digests_claim ON interest_digests(status, claim_expires_at)")
        expected = {
            "interest_digests": {"id", "admin_id", "local_date", "status", "attempt_count", "next_attempt_at", "claim_token", "claim_expires_at", "sent_at", "last_error_code", "action_token", "remaining_count", "created_at", "updated_at"},
            "interest_deliveries": {"id", "admin_id", "session_id", "mode", "due_at", "status", "attempt_count", "next_attempt_at", "claim_token", "claim_expires_at", "sent_at", "last_error_code", "digest_id", "action_token", "created_at", "updated_at"},
        }
        for table, required in expected.items():
            actual = {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            if not required <= actual.keys():
                raise RuntimeError(f"Unexpected {table} schema: missing {sorted(required - actual.keys())}")
            if any(actual[name]["notnull"] != 1 for name in ("admin_id", "status", "attempt_count", "action_token")):
                raise RuntimeError(f"Unexpected {table} required columns")
            foreign_keys = {row["from"]: row["table"] for row in conn.execute(f"PRAGMA foreign_key_list({table})")}
            required_foreign_keys = {"admin_id": "admins"}
            if table == "interest_deliveries":
                required_foreign_keys.update({"session_id": "user_sessions", "digest_id": "interest_digests"})
            if any(foreign_keys.get(column) != target for column, target in required_foreign_keys.items()):
                raise RuntimeError(f"Unexpected {table} foreign keys")
            unique_indexes = [row["name"] for row in conn.execute(f"PRAGMA index_list({table})") if row["unique"]]
            required_uniques = (["admin_id", "local_date"] if table == "interest_digests" else ["admin_id", "session_id"])
            if not any([row["name"] for row in conn.execute(f"PRAGMA index_info({index})")] == required_uniques for index in unique_indexes):
                raise RuntimeError(f"Unexpected {table} uniqueness")
        admin_columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(admins)")}
        if not {"interest_mode", "interest_mode_changed_at"} <= admin_columns.keys():
            raise RuntimeError("Unexpected admins interest preference schema")
        mode_column = admin_columns["interest_mode"]
        if mode_column["type"].upper() != "TEXT" or mode_column["notnull"] != 1 or mode_column["dflt_value"] != "'3h'":
            raise RuntimeError("Unexpected admins.interest_mode definition")
        index_specs = {
            "user_sessions": {"idx_interest_sessions_due": ["last_activity_at", "id"]},
            "interest_deliveries": {
                "idx_interest_deliveries_due": ["status", "due_at", "next_attempt_at"],
                "idx_interest_deliveries_claim": ["status", "claim_expires_at"],
                "idx_interest_deliveries_session": ["session_id", "admin_id"],
                "idx_interest_deliveries_sent": ["admin_id", "status", "sent_at"],
            },
            "interest_digests": {
                "idx_interest_digests_due": ["status", "next_attempt_at", "local_date"],
                "idx_interest_digests_claim": ["status", "claim_expires_at"],
            },
        }
        for table, specs in index_specs.items():
            indexes = {row["name"] for row in conn.execute(f"PRAGMA index_list({table})")}
            for index, columns in specs.items():
                if index not in indexes or [row["name"] for row in conn.execute(f"PRAGMA index_info({index})")] != columns:
                    raise RuntimeError(f"Unexpected {index} definition")


def ensure_admins_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(admins)").fetchall()}
    migrations = {
        "chat_id": "ALTER TABLE admins ADD COLUMN chat_id TEXT",
        "role": "ALTER TABLE admins ADD COLUMN role TEXT DEFAULT 'pending'",
        "approved": "ALTER TABLE admins ADD COLUMN approved INTEGER DEFAULT 0",
        "web_login": "ALTER TABLE admins ADD COLUMN web_login TEXT",
        "password_hash": "ALTER TABLE admins ADD COLUMN password_hash TEXT",
        "must_change_password": "ALTER TABLE admins ADD COLUMN must_change_password INTEGER DEFAULT 1",
        "can_use_bot_admin": "ALTER TABLE admins ADD COLUMN can_use_bot_admin INTEGER DEFAULT 0",
        "can_receive_notifications": "ALTER TABLE admins ADD COLUMN can_receive_notifications INTEGER DEFAULT 0",
        "delegated_until": "ALTER TABLE admins ADD COLUMN delegated_until TEXT",
        "delegated_by_admin_id": "ALTER TABLE admins ADD COLUMN delegated_by_admin_id INTEGER",
        "last_login_at": "ALTER TABLE admins ADD COLUMN last_login_at TEXT",
        "updated_at": "ALTER TABLE admins ADD COLUMN updated_at TEXT",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)
    conn.execute(
        """
        UPDATE admins
        SET role = COALESCE(NULLIF(role, ''), 'hr_staff'),
            approved = 1,
            can_use_bot_admin = 1,
            can_receive_notifications = 1,
            updated_at = COALESCE(updated_at, ?)
        WHERE is_active = 1 AND COALESCE(approved, 0) = 0
        """,
        (now_iso(),),
    )


def ensure_applications_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(applications)").fetchall()}
    migrations = {
        "assigned_to_admin_id": "ALTER TABLE applications ADD COLUMN assigned_to_admin_id INTEGER",
        "assigned_to_name": "ALTER TABLE applications ADD COLUMN assigned_to_name TEXT",
        "assigned_at": "ALTER TABLE applications ADD COLUMN assigned_at TEXT",
        "taken_at": "ALTER TABLE applications ADD COLUMN taken_at TEXT",
        "status_comment": "ALTER TABLE applications ADD COLUMN status_comment TEXT",
        "taken_by_admin_id": "ALTER TABLE applications ADD COLUMN taken_by_admin_id INTEGER",
        "taken_by_name": "ALTER TABLE applications ADD COLUMN taken_by_name TEXT",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)



def ensure_archive_schema(conn: sqlite3.Connection) -> None:
    migrations = {
        "is_archived": "INTEGER DEFAULT 0",
        "archived_at": "TEXT",
        "archived_by_admin_id": "INTEGER",
        "archived_by_name": "TEXT",
    }
    for table in ("applications", "questions", "appeals"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in migrations.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_vacancies_external_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(vacancies)").fetchall()}
    migrations = {
        "external_source": "ALTER TABLE vacancies ADD COLUMN external_source TEXT",
        "external_id": "ALTER TABLE vacancies ADD COLUMN external_id TEXT",
        "external_url": "ALTER TABLE vacancies ADD COLUMN external_url TEXT",
        "external_updated_at": "ALTER TABLE vacancies ADD COLUMN external_updated_at TEXT",
        "external_raw_json": "ALTER TABLE vacancies ADD COLUMN external_raw_json TEXT",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vacancies_external_source_id ON vacancies (external_source, external_id)")

def ensure_default_photo_album_conn(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM service_photo_albums WHERE title = ? ORDER BY id LIMIT 1", ("Фотографии со службы",)).fetchone()
    if row:
        return int(row["id"])
    created = now_iso()
    cur = conn.execute(
        """
        INSERT INTO service_photo_albums (title, description, is_active, sort_order, created_at, updated_at)
        VALUES (?, ?, 1, 100, ?, ?)
        """,
        ("Фотографии со службы", "Общий альбом фотографий условий службы", created, created),
    )
    return int(cur.lastrowid)


def ensure_service_photos_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_photo_albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            is_active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 100,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    album_columns = {row["name"] for row in conn.execute("PRAGMA table_info(service_photo_albums)").fetchall()}
    album_migrations = {
        "description": "ALTER TABLE service_photo_albums ADD COLUMN description TEXT",
        "is_active": "ALTER TABLE service_photo_albums ADD COLUMN is_active INTEGER DEFAULT 1",
        "sort_order": "ALTER TABLE service_photo_albums ADD COLUMN sort_order INTEGER DEFAULT 100",
        "created_at": "ALTER TABLE service_photo_albums ADD COLUMN created_at TEXT",
        "updated_at": "ALTER TABLE service_photo_albums ADD COLUMN updated_at TEXT",
    }
    for column, sql in album_migrations.items():
        if column not in album_columns:
            conn.execute(sql)

    photo_columns = {row["name"] for row in conn.execute("PRAGMA table_info(service_photos)").fetchall()}
    if "album_id" not in photo_columns:
        conn.execute("ALTER TABLE service_photos ADD COLUMN album_id INTEGER")

    photos_without_album = conn.execute("SELECT COUNT(*) FROM service_photos WHERE album_id IS NULL").fetchone()[0]
    if photos_without_album:
        album_id = ensure_default_photo_album_conn(conn)
        conn.execute("UPDATE service_photos SET album_id = ? WHERE album_id IS NULL", (album_id,))


def seed_default_org_settings(conn: sqlite3.Connection) -> None:
    for key, value in DEFAULT_ORG_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))


def seed_default_update_settings(conn: sqlite3.Connection) -> None:
    for key, value in DEFAULT_UPDATE_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))



def seed_default_trudvsem_settings(conn: sqlite3.Connection) -> None:
    for key, value in DEFAULT_TRUDVSEM_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

def seed_db(conn: sqlite3.Connection) -> None:
    created = now_iso()
    if conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 0:
        vacancies = [
            (
                "Специалист кадрового подразделения",
                "уточняется при консультации",
                "работа с документами, консультация кандидатов, сопровождение кадровых процедур.",
                "ответственность, внимательность, грамотная устная и письменная речь.",
                "официальное оформление, стабильная занятость, социальные гарантии.",
                "подробные условия уточняются при консультации.",
                10,
            ),
            (
                "Специалист службы обеспечения",
                "уточняется при консультации",
                "выполнение задач по направлению деятельности, участие в обеспечении работы организации.",
                "дисциплинированность, ответственность, готовность к обучению.",
                "официальное оформление, стабильная занятость, профессиональное развитие.",
                "требования зависят от конкретной должности.",
                20,
            ),
            (
                "Водитель",
                "уточняется при консультации",
                "управление транспортным средством, выполнение служебных поручений по направлению деятельности.",
                "водительское удостоверение соответствующей категории, ответственность, аккуратность.",
                "официальное оформление, стабильная занятость.",
                "категория водительского удостоверения уточняется по конкретной должности.",
                30,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO vacancies
            (title, salary, duties, requirements, conditions, note, is_active, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            [(*item, created, created) for item in vacancies],
        )

    service_items = {
        "conditions": (
            "Условия работы или службы",
            "Организация предлагает официальное оформление, стабильную занятость, социальные гарантии, обучение и возможность профессионального развития. Подробные условия зависят от должности и уточняются при консультации.",
        ),
        "order": (
            "Порядок оформления",
            "Для предварительной консультации оставьте отклик или задайте вопрос через бота. Сотрудник кадрового подразделения свяжется с вами, уточнит интересующую должность, требования, перечень документов и дальнейший порядок оформления.",
        ),
        "warning": (
            "Предупреждение о персональных данных",
            DEFAULT_ORG_SETTINGS["personal_data_warning"],
        ),
        "contacts": (
            "Контакты кадрового подразделения",
            DEFAULT_ORG_SETTINGS["contacts_public_text"],
        ),
    }
    for key, (title, text) in service_items.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO service_info (key, title, text, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (key, title, text, created),
        )


def get_setting(key: str, default: str | None = "") -> str:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row and row.get("value") is not None:
        return str(row["value"])
    if default is None:
        return ""
    return str(default)


def set_setting(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def get_org_settings() -> dict[str, str]:
    settings = DEFAULT_ORG_SETTINGS.copy()
    if not DEFAULT_ORG_SETTINGS:
        return settings
    placeholders = ", ".join("?" for _ in DEFAULT_ORG_SETTINGS)
    rows = fetch_all(f"SELECT key, value FROM settings WHERE key IN ({placeholders})", tuple(DEFAULT_ORG_SETTINGS.keys()))
    for row in rows:
        value = row.get("value")
        if value is not None:
            settings[str(row["key"])] = str(value)
    return settings


def update_org_settings(data: dict[str, Any], actor_id: int | None = None, actor_name: str = "Система") -> None:
    for key in DEFAULT_ORG_SETTINGS:
        value = str(data.get(key, DEFAULT_ORG_SETTINGS[key]) or "").strip()
        set_setting(key, value)
    audit_log(actor_id, actor_name, "organization_settings_updated", "settings", None, "Настройки организации сохранены.")


def get_update_settings() -> dict[str, str]:
    settings = DEFAULT_UPDATE_SETTINGS.copy()
    placeholders = ", ".join("?" for _ in DEFAULT_UPDATE_SETTINGS)
    rows = fetch_all(f"SELECT key, value FROM settings WHERE key IN ({placeholders})", tuple(DEFAULT_UPDATE_SETTINGS.keys()))
    for row in rows:
        if row.get("value") is not None:
            settings[str(row["key"])] = str(row["value"])
    return settings


def set_installed_commit(commit: str) -> None:
    set_setting("installed_commit", (commit or "local").strip() or "local")
    set_setting("update_last_at", now_iso())


def get_installed_commit() -> str:
    return get_setting("installed_commit", DEFAULT_UPDATE_SETTINGS["installed_commit"]).strip() or "local"


def get_github_repo_url() -> str:
    return get_setting("github_repo_url", DEFAULT_UPDATE_SETTINGS["github_repo_url"]).strip()


def get_admin_service_name() -> str:
    return get_setting("admin_service_name", DEFAULT_UPDATE_SETTINGS["admin_service_name"]).strip() or DEFAULT_UPDATE_SETTINGS["admin_service_name"]


def get_bot_service_name() -> str:
    return get_setting("bot_service_name", DEFAULT_UPDATE_SETTINGS["bot_service_name"]).strip() or DEFAULT_UPDATE_SETTINGS["bot_service_name"]


def get_install_path() -> str:
    return get_setting("install_path", DEFAULT_UPDATE_SETTINGS["install_path"]).strip() or DEFAULT_UPDATE_SETTINGS["install_path"]



def get_trudvsem_settings() -> dict[str, str]:
    settings = DEFAULT_TRUDVSEM_SETTINGS.copy()
    placeholders = ", ".join("?" for _ in DEFAULT_TRUDVSEM_SETTINGS)
    rows = fetch_all(f"SELECT key, value FROM settings WHERE key IN ({placeholders})", tuple(DEFAULT_TRUDVSEM_SETTINGS.keys()))
    for row in rows:
        if row.get("value") is not None:
            settings[str(row["key"])] = str(row["value"])
    return settings


def update_trudvsem_settings(data: dict[str, Any]) -> None:
    for key in DEFAULT_TRUDVSEM_SETTINGS:
        value = str(data.get(key, DEFAULT_TRUDVSEM_SETTINGS[key]) or "").strip()
        set_setting(key, value)


def set_trudvsem_last_sync_at() -> None:
    set_setting("trudvsem_last_sync_at", now_iso())

def get_admin_secret() -> str:
    saved = get_setting("admin_secret", "").strip()
    if saved:
        return saved
    return os.getenv("ADMIN_SECRET", "change-me").strip()


def set_admin_secret(new_secret: str, changed_by_admin_id: int | None = None, changed_by_name: str | None = None) -> None:
    set_setting("admin_secret", new_secret.strip())
    audit_log(changed_by_admin_id, changed_by_name or "Система", "admin_secret_changed", "settings", None, "Служебный код регистрации изменён.")


def validate_admin_secret(code: str) -> bool:
    expected = get_admin_secret()
    return bool(expected) and expected != "change-me" and secrets.compare_digest((code or "").strip(), expected)


def get_superadmin_password_hash() -> str:
    return get_setting("superadmin_password_hash", "").strip()


def verify_superadmin_password(password: str, fallback_password: str) -> bool:
    saved_hash = get_superadmin_password_hash()
    if saved_hash:
        return verify_password(password, saved_hash)
    return secrets.compare_digest(password or "", fallback_password or "")


def change_superadmin_password(
    current_password: str,
    new_password: str,
    confirm_password: str,
    fallback_password: str,
    actor_id: int | None = None,
    actor_name: str = "Главная учётная запись",
) -> tuple[bool, str]:
    if not verify_superadmin_password(current_password, fallback_password):
        return False, "Текущий пароль указан неверно."
    if len(new_password or "") < 8:
        return False, "Новый пароль должен быть не короче 8 символов."
    if new_password != confirm_password:
        return False, "Новый пароль и повтор не совпадают."
    set_setting("superadmin_password_hash", hash_password(new_password))
    audit_log(actor_id, actor_name, "superadmin_password_changed", "settings", None, "Пароль главной учётной записи изменён")
    return True, "Пароль главной учётной записи изменён."


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with get_connection() as conn:
        return dict_row(conn.execute(query, params).fetchone())


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with get_connection() as conn:
        cur = conn.execute(query, params)
        return int(cur.lastrowid or 0)


ActivityContext = tuple[int, int, str]  # messenger_user_id, session_id, external_user_id


def _utc_datetime(value: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value):
        raise ValueError("Activity timestamps must use UTC milliseconds")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def touch_candidate(
    messenger: str, external_user_id: str, profile: dict[str, Any] | None = None,
    at: str | None = None,
) -> ActivityContext:
    if messenger is None or external_user_id is None:
        raise ValueError("Confirmed messenger and user ID are required")
    messenger = str(messenger).strip()
    external_user_id = str(external_user_id).strip()
    if not messenger or not external_user_id:
        raise ValueError("Confirmed messenger and user ID are required")
    seen_at = at or utc_now_iso()
    _utc_datetime(seen_at)
    profile = profile or {}
    values = {
        key: str(profile[key]).strip() if profile.get(key) is not None else None
        for key in ("display_name", "first_name", "last_name", "username", "avatar_url")
    }
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            INSERT INTO messenger_users
                (messenger, external_user_id, display_name, first_name, last_name,
                 username, avatar_url, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(messenger, external_user_id) DO UPDATE SET
                display_name = COALESCE(NULLIF(excluded.display_name, ''), messenger_users.display_name),
                first_name = COALESCE(NULLIF(excluded.first_name, ''), messenger_users.first_name),
                last_name = COALESCE(NULLIF(excluded.last_name, ''), messenger_users.last_name),
                username = COALESCE(NULLIF(excluded.username, ''), messenger_users.username),
                avatar_url = COALESCE(NULLIF(excluded.avatar_url, ''), messenger_users.avatar_url),
                last_seen_at = MAX(messenger_users.last_seen_at, excluded.last_seen_at)
        """, (messenger, external_user_id, *(values[key] for key in values), seen_at, seen_at))
        user = conn.execute(
            "SELECT id FROM messenger_users WHERE messenger = ? AND external_user_id = ?",
            (messenger, external_user_id),
        ).fetchone()
        user_id = int(user["id"])
        if messenger == "max":
            for table in ("questions", "appeals"):
                conn.execute(
                    f"UPDATE {table} SET messenger_user_id = ? WHERE messenger_user_id IS NULL AND max_user_id = ?",
                    (user_id, external_user_id),
                )
        session = conn.execute(
            "SELECT id, last_activity_at FROM user_sessions WHERE messenger_user_id = ? AND closed_at IS NULL",
            (user_id,),
        ).fetchone()
        if session and _utc_datetime(seen_at) - _utc_datetime(session["last_activity_at"]) >= SESSION_INACTIVITY:
            closed_at = _utc_iso(_utc_datetime(session["last_activity_at"]) + SESSION_INACTIVITY)
            conn.execute("UPDATE user_sessions SET closed_at = ? WHERE id = ?", (closed_at, session["id"]))
            session = None
        if session:
            session_id = int(session["id"])
            conn.execute(
                "UPDATE user_sessions SET last_activity_at = MAX(last_activity_at, ?) WHERE id = ?",
                (seen_at, session_id),
            )
        else:
            session_id = int(conn.execute(
                "INSERT INTO user_sessions (messenger_user_id, started_at, last_activity_at) VALUES (?, ?, ?)",
                (user_id, seen_at, seen_at),
            ).lastrowid)
    return user_id, session_id, external_user_id


def update_messenger_user_chat_id(messenger: str, external_user_id: str, external_chat_id: object) -> bool:
    from app.max_chat_ids import canonical_chat_id

    chat_id = canonical_chat_id(external_chat_id)
    if not chat_id or not messenger or not external_user_id:
        return False
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE messenger_users SET external_chat_id = ? WHERE messenger = ? AND external_user_id = ?",
            (chat_id, messenger, str(external_user_id)),
        )
        return bool(cursor.rowcount)


def _record_activity_event_conn(
    conn: sqlite3.Connection, activity: ActivityContext, event_type: str,
    vacancy_id: int | None = None, metadata: dict[str, Any] | None = None,
    at: str | None = None,
) -> int:
    if event_type not in ACTIVITY_EVENTS:
        raise ValueError("Unsupported activity event")
    if metadata is not None:
        if not isinstance(metadata, dict) or set(metadata) - {"vacancy_title"}:
            raise ValueError("Unsupported activity metadata")
        if "vacancy_title" in metadata and not isinstance(metadata["vacancy_title"], str):
            raise ValueError("Invalid vacancy title")
        encoded_metadata = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        encoded_metadata = None
    user_id, session_id, _ = activity
    if conn.execute(
        "SELECT 1 FROM user_sessions WHERE id = ? AND messenger_user_id = ?", (session_id, user_id)
    ).fetchone() is None:
        raise ValueError("Activity session does not belong to messenger user")
    created = at or utc_now_iso()
    _utc_datetime(created)
    event_id = int(conn.execute("""
        INSERT INTO user_activity_events
            (messenger_user_id, session_id, event_type, vacancy_id, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, session_id, event_type, vacancy_id, encoded_metadata, created)).lastrowid)
    if event_type in MEANINGFUL_EVENTS or event_type in CONVERSION_EVENTS:
        conn.execute("""
            UPDATE user_sessions SET meaningful_activity = 1,
                first_meaningful_at = COALESCE(first_meaningful_at, ?)
            WHERE id = ?
        """, (created, session_id))
    if event_type in CONVERSION_EVENTS:
        conn.execute("""
            UPDATE user_sessions SET conversion_type = COALESCE(conversion_type, ?),
                converted_at = COALESCE(converted_at, ?)
            WHERE id = ?
        """, (event_type, created, session_id))
    return event_id


def record_activity_event(
    activity: ActivityContext, event_type: str, vacancy_id: int | None = None,
    metadata: dict[str, Any] | None = None, at: str | None = None,
) -> int:
    with get_connection() as conn:
        return _record_activity_event_conn(conn, activity, event_type, vacancy_id, metadata, at)


def list_vacancies(active_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE is_active = 1" if active_only else ""
    return fetch_all(f"SELECT * FROM vacancies {where} ORDER BY sort_order, id")


def vacancy_application_counts() -> dict[int, dict[str, int]]:
    rows = fetch_all(
        """
        SELECT vacancy_id,
               SUM(CASE WHEN COALESCE(is_archived, 0) = 0 THEN 1 ELSE 0 END) AS active_count,
               SUM(CASE WHEN COALESCE(is_archived, 0) = 1 THEN 1 ELSE 0 END) AS archived_count
        FROM applications
        WHERE vacancy_id IS NOT NULL
        GROUP BY vacancy_id
        """
    )
    return {
        int(row["vacancy_id"]): {
            "active": int(row.get("active_count") or 0),
            "archive": int(row.get("archived_count") or 0),
        }
        for row in rows
    }


def get_vacancy(vacancy_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,))


def save_vacancy(data: dict[str, Any], vacancy_id: int | None = None) -> int:
    is_active = 1 if data.get("is_active") in (1, "1", True, "on") else 0
    values = (
        data["title"],
        data.get("salary", ""),
        data.get("duties", ""),
        data.get("requirements", ""),
        data.get("conditions", ""),
        data.get("note", ""),
        is_active,
        int(data.get("sort_order") or 100),
        now_iso(),
    )
    if vacancy_id:
        execute(
            """
            UPDATE vacancies
            SET title = ?, salary = ?, duties = ?, requirements = ?, conditions = ?, note = ?,
                is_active = ?, sort_order = ?, updated_at = ?
            WHERE id = ?
            """,
            (*values, vacancy_id),
        )
        return vacancy_id
    created = now_iso()
    return execute(
        """
        INSERT INTO vacancies
        (title, salary, duties, requirements, conditions, note, is_active, sort_order, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*values[:-1], created, values[-1]),
    )


def create_vacancy(data: dict[str, Any]) -> int:
    return save_vacancy({**data, "is_active": 1, "sort_order": data.get("sort_order") or 100})


def update_vacancy_field(vacancy_id: int, field: str, value: str, actor_id: int | None = None, actor_name: str = "Система") -> None:
    allowed = {"title", "salary", "duties", "requirements", "conditions", "note"}
    if field not in allowed:
        raise ValueError("Unsupported vacancy field")
    execute(f"UPDATE vacancies SET {field} = ?, updated_at = ? WHERE id = ?", (value, now_iso(), vacancy_id))
    audit_log(actor_id, actor_name, "vacancy_changed", "vacancy", vacancy_id, field)


def toggle_vacancy(vacancy_id: int) -> None:
    execute("UPDATE vacancies SET is_active = CASE is_active WHEN 1 THEN 0 ELSE 1 END, updated_at = ? WHERE id = ?", (now_iso(), vacancy_id))


def delete_vacancy(vacancy_id: int) -> None:
    execute("DELETE FROM vacancies WHERE id = ?", (vacancy_id,))


def list_service_info() -> list[dict[str, Any]]:
    return fetch_all("SELECT * FROM service_info ORDER BY CASE key WHEN 'conditions' THEN 1 WHEN 'order' THEN 2 WHEN 'warning' THEN 3 WHEN 'contacts' THEN 4 ELSE 9 END")


def get_service_info(key: str) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM service_info WHERE key = ?", (key,))


def update_service_info(key: str, title: str, text: str) -> None:
    execute("UPDATE service_info SET title = ?, text = ?, updated_at = ? WHERE key = ?", (title, text, now_iso(), key))


def ensure_default_photo_album() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM service_photo_albums ORDER BY sort_order, id LIMIT 1").fetchone()
        if row:
            return int(row["id"])
        return ensure_default_photo_album_conn(conn)


def list_photo_albums(active_only: bool = False, with_counts: bool = False) -> list[dict[str, Any]]:
    where = "WHERE a.is_active = 1" if active_only else ""
    if with_counts:
        return fetch_all(
            f"""
            SELECT a.*, COUNT(p.id) AS photo_count
            FROM service_photo_albums a
            LEFT JOIN service_photos p ON p.album_id = a.id
            {where}
            GROUP BY a.id
            ORDER BY a.sort_order, a.id
            """
        )
    return fetch_all(f"SELECT * FROM service_photo_albums a {where} ORDER BY a.sort_order, a.id")


def list_photo_albums_with_active_photos() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT a.*, COUNT(p.id) AS photo_count
        FROM service_photo_albums a
        JOIN service_photos p ON p.album_id = a.id AND p.is_active = 1
        WHERE a.is_active = 1
        GROUP BY a.id
        HAVING COUNT(p.id) > 0
        ORDER BY a.sort_order, a.id
        """
    )


def get_photo_album(album_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM service_photo_albums WHERE id = ?", (album_id,))


def save_photo_album(data: dict[str, Any], album_id: int | None = None) -> int:
    values = (
        str(data.get("title") or "").strip(),
        str(data.get("description") or "").strip(),
        1 if data.get("is_active") in (1, "1", True, "on") else 0,
        int(data.get("sort_order") or 100),
        now_iso(),
    )
    if not values[0]:
        raise ValueError("Album title is required")
    if album_id:
        execute(
            """
            UPDATE service_photo_albums
            SET title = ?, description = ?, is_active = ?, sort_order = ?, updated_at = ?
            WHERE id = ?
            """,
            (*values, album_id),
        )
        return album_id
    created = now_iso()
    return execute(
        """
        INSERT INTO service_photo_albums (title, description, is_active, sort_order, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (*values[:-1], created, values[-1]),
    )


def toggle_photo_album(album_id: int) -> None:
    execute("UPDATE service_photo_albums SET is_active = CASE is_active WHEN 1 THEN 0 ELSE 1 END, updated_at = ? WHERE id = ?", (now_iso(), album_id))


def album_photo_count(album_id: int) -> int:
    row = fetch_one("SELECT COUNT(*) AS count FROM service_photos WHERE album_id = ?", (album_id,))
    return int(row["count"] if row else 0)


def delete_photo_album(album_id: int) -> bool:
    if album_photo_count(album_id) > 0:
        return False
    execute("DELETE FROM service_photo_albums WHERE id = ?", (album_id,))
    return True


def list_photos(active_only: bool = False, album_id: int | None = None) -> list[dict[str, Any]]:
    conditions = []
    params: list[Any] = []
    if active_only:
        conditions.append("p.is_active = 1")
        conditions.append("COALESCE(a.is_active, 1) = 1")
    if album_id is not None:
        conditions.append("p.album_id = ?")
        params.append(album_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return fetch_all(
        f"""
        SELECT p.*, a.title AS album_title, a.is_active AS album_is_active, a.sort_order AS album_sort_order
        FROM service_photos p
        LEFT JOIN service_photo_albums a ON a.id = p.album_id
        {where}
        ORDER BY COALESCE(a.sort_order, 100), a.id, p.sort_order, p.id
        """,
        tuple(params),
    )


def get_photo(photo_id: int) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT p.*, a.title AS album_title
        FROM service_photos p
        LEFT JOIN service_photo_albums a ON a.id = p.album_id
        WHERE p.id = ?
        """,
        (photo_id,),
    )


def add_photo(filename: str, original_name: str, caption: str, sort_order: int, is_active: int = 1, album_id: int | None = None) -> int:
    album_id = album_id or ensure_default_photo_album()
    return execute(
        """
        INSERT INTO service_photos (album_id, filename, original_name, caption, is_active, sort_order, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (album_id, filename, original_name, caption, int(is_active), int(sort_order or 100), now_iso()),
    )


def update_photo(photo_id: int, caption: str, sort_order: int, is_active: int, album_id: int | None) -> None:
    album_id = album_id or ensure_default_photo_album()
    execute(
        """
        UPDATE service_photos
        SET album_id = ?, caption = ?, sort_order = ?, is_active = ?
        WHERE id = ?
        """,
        (album_id, caption, int(sort_order or 100), int(is_active), photo_id),
    )


def toggle_photo(photo_id: int) -> None:
    execute("UPDATE service_photos SET is_active = CASE is_active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (photo_id,))


def delete_photo(photo_id: int) -> None:
    execute("DELETE FROM service_photos WHERE id = ?", (photo_id,))


def list_contacts(active_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE is_active = 1" if active_only else ""
    return fetch_all(f"SELECT * FROM contacts {where} ORDER BY sort_order, id")


def get_contact(contact_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM contacts WHERE id = ?", (contact_id,))


def save_contact(data: dict[str, Any], contact_id: int | None = None) -> int:
    is_active = 1 if data.get("is_active") in (1, "1", True, "on") else 0
    values = (
        data["title"],
        data["value"],
        data.get("note", ""),
        is_active,
        int(data.get("sort_order") or 100),
    )
    if contact_id:
        execute("UPDATE contacts SET title = ?, value = ?, note = ?, is_active = ?, sort_order = ? WHERE id = ?", (*values, contact_id))
        return contact_id
    return execute(
        "INSERT INTO contacts (title, value, note, is_active, sort_order) VALUES (?, ?, ?, ?, ?)",
        values,
    )


def toggle_contact(contact_id: int) -> None:
    execute("UPDATE contacts SET is_active = CASE is_active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (contact_id,))


def delete_contact(contact_id: int) -> None:
    execute("DELETE FROM contacts WHERE id = ?", (contact_id,))


def _matching_activity(max_user_id: Any, activity: ActivityContext | None) -> ActivityContext | None:
    # In a shared chat the person completing an in-memory form can differ from its starter.
    return activity if activity and str(max_user_id or "") == activity[2] else None


def _record_completion_mid_conn(conn: sqlite3.Connection, completion_mid: str | None) -> None:
    if completion_mid:
        conn.execute("INSERT OR IGNORE INTO processed_max_updates (update_key, processed_at) VALUES (?, ?)",
                     (f"message:{completion_mid}", utc_now_iso()))


def create_application(data: dict[str, Any], activity: ActivityContext | None = None,
                       completion_mid: str | None = None) -> int:
    activity = _matching_activity(data.get("max_user_id"), activity)
    with get_connection() as conn:
        application_id = int(conn.execute(
            """
            INSERT INTO applications
            (max_user_id, vacancy_id, vacancy_title, full_name, age, phone, education,
             military_service, preferred_time, comment, status, created_at, messenger_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                data.get("max_user_id"), data.get("vacancy_id"), data.get("vacancy_title"),
                data.get("full_name"), data.get("age"), data.get("phone"), data.get("education"),
                data.get("military_service"), data.get("preferred_time"), data.get("comment"),
                now_iso(), activity[0] if activity else None,
            ),
        ).lastrowid)
        if activity:
            _record_activity_event_conn(conn, activity, "vacancy_application_submitted", data.get("vacancy_id"))
            from app.conversations import link_source_conn
            link_source_conn(conn, "application", application_id, activity[0])
        _record_completion_mid_conn(conn, completion_mid)
        return application_id


def create_question(
    max_user_id: str, question_text: str, contact: str,
    activity: ActivityContext | None = None,
    question_mid: str | None = None,
    completion_mid: str | None = None,
) -> int:
    activity = _matching_activity(max_user_id, activity)
    with get_connection() as conn:
        question_id = int(conn.execute(
            "INSERT INTO questions (max_user_id, question_text, contact, status, created_at, messenger_user_id) VALUES (?, ?, ?, 'new', ?, ?)",
            (max_user_id, question_text, contact, now_iso(), activity[0] if activity else None),
        ).lastrowid)
        if activity:
            _record_activity_event_conn(conn, activity, "question_sent")
            from app.conversations import link_source_conn
            link_source_conn(conn, "question", question_id, activity[0], question_text, question_mid)
        _record_completion_mid_conn(conn, completion_mid)
        return question_id


def create_appeal(
    max_user_id: str, full_name: str, phone: str, appeal_text: str,
    activity: ActivityContext | None = None,
    appeal_mid: str | None = None,
    completion_mid: str | None = None,
) -> int:
    activity = _matching_activity(max_user_id, activity)
    with get_connection() as conn:
        appeal_id = int(conn.execute(
            "INSERT INTO appeals (max_user_id, full_name, phone, appeal_text, status, created_at, messenger_user_id) VALUES (?, ?, ?, ?, 'new', ?, ?)",
            (max_user_id, full_name, phone, appeal_text, now_iso(), activity[0] if activity else None),
        ).lastrowid)
        if activity:
            _record_activity_event_conn(conn, activity, "appeal_sent")
            from app.conversations import link_source_conn
            link_source_conn(conn, "appeal", appeal_id, activity[0], appeal_text, appeal_mid)
        _record_completion_mid_conn(conn, completion_mid)
        return appeal_id


def _archive_where(view: str, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    if view == "archive":
        return f"COALESCE({prefix}is_archived, 0) = 1"
    if view == "all":
        return "1 = 1"
    return f"COALESCE({prefix}is_archived, 0) = 0"


def list_applications(view: str = "active", vacancy_id: int | None = None) -> list[dict[str, Any]]:
    conditions = [_archive_where(view)]
    params: list[Any] = []
    if vacancy_id:
        conditions.append("vacancy_id = ?")
        params.append(vacancy_id)
    where = f"WHERE {' AND '.join(conditions)}"
    return fetch_all(f"SELECT * FROM applications {where} ORDER BY id DESC", tuple(params))


def list_questions(view: str = "active") -> list[dict[str, Any]]:
    return fetch_all(f"SELECT * FROM questions WHERE {_archive_where(view)} ORDER BY id DESC")


def list_appeals(view: str = "active") -> list[dict[str, Any]]:
    return fetch_all(f"SELECT * FROM appeals WHERE {_archive_where(view)} ORDER BY id DESC")


def update_status(table: str, item_id: int, status: str) -> None:
    if table not in {"applications", "questions", "appeals"}:
        raise ValueError("Unsupported table")
    if status not in {"new", "in_work", "done"}:
        raise ValueError("Unsupported status")
    execute(f"UPDATE {table} SET status = ? WHERE id = ?", (status, item_id))


ARCHIVE_TARGETS = {
    "applications": "application",
    "questions": "question",
    "appeals": "appeal",
}


def _archive_target(table: str) -> str:
    if table not in ARCHIVE_TARGETS:
        raise ValueError("Unsupported table")
    return ARCHIVE_TARGETS[table]


def archive_record(table: str, item_id: int, actor_id: int | None = None, actor_name: str = "Система") -> None:
    target_type = _archive_target(table)
    execute(
        f"""
        UPDATE {table}
        SET is_archived = 1, archived_at = ?, archived_by_admin_id = ?, archived_by_name = ?
        WHERE id = ?
        """,
        (now_iso(), actor_id, actor_name, item_id),
    )
    audit_log(actor_id, actor_name, f"{target_type}_archived", target_type, item_id)


def unarchive_record(table: str, item_id: int, actor_id: int | None = None, actor_name: str = "Система") -> None:
    target_type = _archive_target(table)
    execute(
        f"""
        UPDATE {table}
        SET is_archived = 0, archived_at = NULL, archived_by_admin_id = NULL, archived_by_name = NULL
        WHERE id = ?
        """,
        (item_id,),
    )
    audit_log(actor_id, actor_name, f"{target_type}_unarchived", target_type, item_id)


def delete_record_permanently(table: str, item_id: int, actor_id: int | None = None, actor_name: str = "Система") -> None:
    target_type = _archive_target(table)
    details = {
        "application": "Отклик удалён полностью",
        "question": "Вопрос удалён полностью",
        "appeal": "Сообщение удалено полностью",
    }[target_type]
    audit_log(actor_id, actor_name, f"{target_type}_deleted_permanently", target_type, item_id, details)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM conversation_sources WHERE source_type = ? AND source_record_id = ?", (target_type, item_id))
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))


def hash_password(password: str) -> str:
    iterations = 260_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        algorithm, iterations_text, salt, expected = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations_text)).hex()
        return secrets.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def generate_temp_password(length: int = 12) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def audit_log(
    admin_id: int | None,
    admin_name: str,
    action: str,
    target_type: str,
    target_id: int | None = None,
    details: str = "",
) -> None:
    execute(
        """
        INSERT INTO admin_audit_log (admin_id, admin_name, action, target_type, target_id, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (admin_id, admin_name, action, target_type, target_id, details, now_iso()),
    )


def list_admins(active_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE is_active = 1" if active_only else ""
    return fetch_all(f"SELECT * FROM admins {where} ORDER BY id DESC")


def get_admin(admin_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM admins WHERE id = ?", (admin_id,))


def get_admin_by_user_id(max_user_id: str) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM admins WHERE max_user_id = ?", (max_user_id,))


def get_admin_by_web_login(web_login: str) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM admins WHERE web_login = ?", (web_login,))


def admin_display_name(admin: dict[str, Any] | None) -> str:
    if not admin:
        return "Система"
    return str(admin.get("display_name") or admin.get("web_login") or admin.get("max_user_id") or f"admin #{admin.get('id')}")


def request_admin_access(max_user_id: str, chat_id: str = "", display_name: str = "") -> tuple[dict[str, Any], bool]:
    existing = get_admin_by_user_id(max_user_id)
    updated = now_iso()
    if existing and existing.get("approved") == 1 and existing.get("role") not in {"pending", "disabled", "pending_rejected"}:
        execute(
            "UPDATE admins SET chat_id = ?, display_name = COALESCE(NULLIF(?, ''), display_name), updated_at = ? WHERE id = ?",
            (chat_id, display_name, updated, existing["id"]),
        )
        return get_admin(existing["id"]) or existing, False
    if existing:
        execute(
            """
            UPDATE admins
            SET chat_id = ?, display_name = COALESCE(NULLIF(?, ''), display_name),
                role = 'pending', approved = 0, is_active = 0,
                can_use_bot_admin = 0, can_receive_notifications = 0, updated_at = ?
            WHERE id = ?
            """,
            (chat_id, display_name, updated, existing["id"]),
        )
        audit_log(existing["id"], display_name or max_user_id, "access_requested", "admin", existing["id"], "Повторная заявка")
        return get_admin(existing["id"]) or existing, True
    admin_id = execute(
        """
        INSERT INTO admins
        (max_user_id, chat_id, display_name, is_active, created_at, role, approved,
         can_use_bot_admin, can_receive_notifications, updated_at)
        VALUES (?, ?, ?, 0, ?, 'pending', 0, 0, 0, ?)
        """,
        (max_user_id, chat_id, display_name, updated, updated),
    )
    audit_log(admin_id, display_name or max_user_id, "access_requested", "admin", admin_id, "Новая заявка")
    return get_admin(admin_id) or {}, True


def add_admin(max_user_id: str, chat_id: str = "", display_name: str = "") -> int:
    admin, _ = request_admin_access(max_user_id, chat_id, display_name)
    return int(admin.get("id") or 0)


def approve_admin(admin_id: int, actor_id: int | None = None, actor_name: str = "Система", role: str = "hr_staff") -> tuple[dict[str, Any] | None, str]:
    admin = get_admin(admin_id)
    if not admin:
        return None, ""
    if role not in {"hr_staff", "hr_head"}:
        role = "hr_staff"
    password = generate_temp_password()
    web_login = admin.get("web_login") or f"max_{admin['max_user_id']}"
    execute(
        """
        UPDATE admins
        SET role = ?,
            approved = 1, is_active = 1, can_use_bot_admin = 1, can_receive_notifications = 1,
            interest_mode_changed_at = ?, web_login = ?, password_hash = ?, must_change_password = 1, updated_at = ?
        WHERE id = ?
        """,
        (role, utc_now_iso(), web_login, hash_password(password), now_iso(), admin_id),
    )
    audit_log(actor_id, actor_name, "admin_approved", "admin", admin_id, f"web_login={web_login}; role={role}")
    return get_admin(admin_id), password


def reject_admin(admin_id: int, actor_id: int | None = None, actor_name: str = "Система") -> dict[str, Any] | None:
    execute(
        """
        UPDATE admins
        SET role = 'disabled', approved = 0, is_active = 0, can_use_bot_admin = 0,
            can_receive_notifications = 0, updated_at = ?
        WHERE id = ?
        """,
        (now_iso(), admin_id),
    )
    audit_log(actor_id, actor_name, "admin_rejected", "admin", admin_id)
    return get_admin(admin_id)


def set_admin_role(admin_id: int, role: str, actor_id: int | None = None, actor_name: str = "Система") -> None:
    if role not in {"hr_staff", "hr_head"}:
        raise ValueError("Unsupported role")
    execute("UPDATE admins SET role = ?, updated_at = ? WHERE id = ?", (role, now_iso(), admin_id))
    audit_log(actor_id, actor_name, "admin_role_changed", "admin", admin_id, role)


def set_admin_flags(admin_id: int, can_use_bot_admin: int, can_receive_notifications: int) -> None:
    execute(
        """UPDATE admins SET can_use_bot_admin = ?, can_receive_notifications = ?,
           interest_mode_changed_at = CASE WHEN can_receive_notifications != ? AND ? = 1 THEN ? ELSE interest_mode_changed_at END,
           updated_at = ? WHERE id = ?""",
        (int(can_use_bot_admin), int(can_receive_notifications), int(can_receive_notifications), int(can_receive_notifications), utc_now_iso(), now_iso(), admin_id),
    )


def set_interest_mode(admin_id: int, mode: str) -> None:
    if mode not in INTEREST_MODES:
        raise ValueError("Unsupported interest notification mode")
    changed_at = utc_now_iso()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        admin = conn.execute("SELECT interest_mode FROM admins WHERE id = ?", (admin_id,)).fetchone()
        if admin is None:
            raise ValueError("Unknown admin")
        if admin["interest_mode"] == mode:
            return
        conn.execute("UPDATE admins SET interest_mode = ?, interest_mode_changed_at = ?, updated_at = ? WHERE id = ?", (mode, changed_at, now_iso(), admin_id))
        conn.execute("""UPDATE interest_deliveries SET status = 'cancelled', updated_at = ?
            WHERE admin_id = ? AND (status IN ('pending', 'failed', 'claimed') OR
                (status = 'batched' AND digest_id IN
                    (SELECT id FROM interest_digests WHERE status IN ('pending', 'failed', 'claimed'))))""", (changed_at, admin_id))
        conn.execute("""UPDATE interest_digests SET status = 'cancelled', updated_at = ?
            WHERE admin_id = ? AND status IN ('pending', 'failed', 'claimed')""", (changed_at, admin_id))


def update_admin_login(admin_id: int, web_login: str, actor_id: int | None = None, actor_name: str = "Система") -> tuple[bool, str]:
    web_login = (web_login or "").strip()
    if not web_login:
        return False, "Логин не может быть пустым."
    if not validate_web_login(web_login):
        return False, "Логин может содержать буквы, цифры, точку, дефис и подчёркивание."
    existing = get_admin_by_web_login(web_login)
    if existing and int(existing["id"]) != int(admin_id):
        return False, "Этот логин уже занят."
    execute("UPDATE admins SET web_login = ?, updated_at = ? WHERE id = ?", (web_login, now_iso(), admin_id))
    audit_log(actor_id, actor_name, "admin_login_changed", "admin", admin_id, web_login)
    return True, "Логин обновлён."


def change_admin_password(admin_id: int, current_password: str, new_password: str, confirm_password: str) -> tuple[bool, str]:
    admin = get_admin(admin_id)
    if not admin:
        return False, "Пользователь не найден."
    if not verify_password(current_password, admin.get("password_hash")):
        return False, "Текущий пароль указан неверно."
    if len(new_password or "") < 8:
        return False, "Новый пароль должен быть не короче 8 символов."
    if new_password != confirm_password:
        return False, "Новый пароль и повтор не совпадают."
    execute(
        "UPDATE admins SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
        (hash_password(new_password), now_iso(), admin_id),
    )
    audit_log(admin_id, admin_display_name(admin), "password_changed", "admin", admin_id)
    return True, "Пароль изменён."


def reset_admin_password(admin_id: int, actor_id: int | None = None, actor_name: str = "Система") -> tuple[dict[str, Any] | None, str]:
    admin = get_admin(admin_id)
    if not admin:
        return None, ""
    password = generate_temp_password()
    web_login = admin.get("web_login") or f"max_{admin.get('max_user_id') or admin_id}"
    execute(
        "UPDATE admins SET web_login = ?, password_hash = ?, must_change_password = 1, updated_at = ? WHERE id = ?",
        (web_login, hash_password(password), now_iso(), admin_id),
    )
    audit_log(actor_id, actor_name, "admin_password_reset", "admin", admin_id, "Пароль пользователя сброшен")
    return get_admin(admin_id), password


def disable_admin(admin_id: int, actor_id: int | None = None, actor_name: str = "Система") -> None:
    execute(
        "UPDATE admins SET role = 'disabled', is_active = 0, approved = 0, can_use_bot_admin = 0, can_receive_notifications = 0, updated_at = ? WHERE id = ?",
        (now_iso(), admin_id),
    )
    audit_log(actor_id, actor_name, "admin_disabled", "admin", admin_id)


def delegate_head(admin_id: int, delegated_until: str, actor_id: int | None, actor_name: str) -> None:
    execute(
        "UPDATE admins SET delegated_until = ?, delegated_by_admin_id = ?, updated_at = ? WHERE id = ?",
        (delegated_until, actor_id, now_iso(), admin_id),
    )
    audit_log(actor_id, actor_name, "head_delegated", "admin", admin_id, delegated_until)


def clear_delegation(admin_id: int, actor_id: int | None, actor_name: str) -> None:
    execute(
        "UPDATE admins SET delegated_until = NULL, delegated_by_admin_id = NULL, updated_at = ? WHERE id = ?",
        (now_iso(), admin_id),
    )
    audit_log(actor_id, actor_name, "head_delegation_cleared", "admin", admin_id)


def notification_admins() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT * FROM admins
        WHERE approved = 1 AND is_active = 1 AND can_receive_notifications = 1
          AND role IN ('hr_staff', 'hr_head')
        ORDER BY id DESC
        """
    )


def approver_admins() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT * FROM admins
        WHERE approved = 1 AND is_active = 1 AND can_receive_notifications = 1
          AND (role = 'hr_head' OR (delegated_until IS NOT NULL AND delegated_until > ?))
        ORDER BY id DESC
        """,
        (now_iso(),),
    )


def active_staff() -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT * FROM admins
        WHERE approved = 1 AND is_active = 1 AND role IN ('hr_staff', 'hr_head')
        ORDER BY display_name, id
        """
    )


def get_application(application_id: int) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM applications WHERE id = ?", (application_id,))


def take_application(application_id: int, admin: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    app = get_application(application_id)
    if not app:
        return False, None
    if int(app.get("is_archived") or 0) == 1:
        return False, app
    if app.get("assigned_to_admin_id"):
        return False, app
    name = admin_display_name(admin)
    ts = now_iso()
    execute(
        """
        UPDATE applications
        SET assigned_to_admin_id = ?, assigned_to_name = ?, assigned_at = ?,
            taken_at = ?, taken_by_admin_id = ?, taken_by_name = ?, status = 'in_work'
        WHERE id = ? AND assigned_to_admin_id IS NULL
        """,
        (admin["id"], name, ts, ts, admin["id"], name, application_id),
    )
    audit_log(admin["id"], name, "application_taken", "application", application_id)
    return True, get_application(application_id)


def assign_application(application_id: int, assignee_id: int, actor: dict[str, Any] | None, actor_name: str = "Система") -> None:
    assignee = get_admin(assignee_id)
    if not assignee:
        raise ValueError("Assignee not found")
    name = admin_display_name(assignee)
    ts = now_iso()
    execute(
        """
        UPDATE applications
        SET assigned_to_admin_id = ?, assigned_to_name = ?, assigned_at = ?, status = 'in_work'
        WHERE id = ?
        """,
        (assignee_id, name, ts, application_id),
    )
    audit_log(actor.get("id") if actor else None, actor_name, "application_assigned", "application", application_id, name)


def assign_application_to_admin(
    application_id: int,
    admin_id: int,
    assigned_by_admin: dict[str, Any] | None,
) -> tuple[bool, str, dict[str, Any] | None, dict[str, Any] | None]:
    app = get_application(application_id)
    if not app:
        return False, "Отклик не найден.", None, None
    if int(app.get("is_archived") or 0) == 1:
        return False, "Отклик находится в архиве.", app, None
    assignee = get_admin(admin_id)
    if not assignee:
        return False, "Сотрудник не найден.", app, None
    if not (
        int(assignee.get("approved") or 0) == 1
        and int(assignee.get("is_active") or 0) == 1
        and assignee.get("role") in {"hr_staff", "hr_head"}
        and int(assignee.get("can_use_bot_admin") or 0) == 1
    ):
        return False, "Этого сотрудника нельзя назначить ответственным.", app, assignee
    actor_name = admin_display_name(assigned_by_admin)
    assignee_name = admin_display_name(assignee)
    ts = now_iso()
    execute(
        """
        UPDATE applications
        SET assigned_to_admin_id = ?, assigned_to_name = ?, assigned_at = ?,
            status = CASE WHEN status = 'new' THEN 'in_work' ELSE status END
        WHERE id = ?
        """,
        (admin_id, assignee_name, ts, application_id),
    )
    audit_log(
        assigned_by_admin.get("id") if assigned_by_admin else None,
        actor_name,
        "application_assigned",
        "application",
        application_id,
        f"Ответственный: {assignee_name}",
    )
    return True, "Отклик назначен.", get_application(application_id), assignee


def release_application(application_id: int, actor: dict[str, Any] | None, actor_name: str = "Система") -> None:
    execute(
        """
        UPDATE applications
        SET assigned_to_admin_id = NULL, assigned_to_name = NULL, assigned_at = NULL,
            taken_at = NULL, taken_by_admin_id = NULL, taken_by_name = NULL, status = 'new'
        WHERE id = ?
        """,
        (application_id,),
    )
    audit_log(actor.get("id") if actor else None, actor_name, "application_released", "application", application_id)


def update_application_status(application_id: int, status: str, actor: dict[str, Any] | None = None, comment: str = "") -> None:
    if status not in {"new", "in_work", "done", "rejected"}:
        raise ValueError("Unsupported status")
    execute("UPDATE applications SET status = ?, status_comment = ? WHERE id = ?", (status, comment, application_id))
    audit_log(actor.get("id") if actor else None, admin_display_name(actor), "application_status_changed", "application", application_id, status)


def toggle_admin(admin_id: int) -> None:
    execute("""UPDATE admins SET is_active = CASE is_active WHEN 1 THEN 0 ELSE 1 END,
        interest_mode_changed_at = CASE WHEN is_active = 0 THEN ? ELSE interest_mode_changed_at END WHERE id = ?""", (utc_now_iso(), admin_id))


def delete_admin(admin_id: int) -> None:
    execute("DELETE FROM admins WHERE id = ?", (admin_id,))
