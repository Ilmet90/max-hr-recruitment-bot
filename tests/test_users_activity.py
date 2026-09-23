from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import bot, db


T0 = "2026-01-01T00:00:00.000Z"
T1 = "2026-01-01T02:59:59.000Z"
T2 = "2026-01-01T05:59:59.000Z"


class FakeMaxAPI:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, text: str, **kwargs: object) -> None:
        self.messages.append(text)

    def set_bot_commands(self, *args: object, **kwargs: object) -> bool:
        return True


def update(text: str, user_id: str | None = "123", chat_id: str = "chat") -> dict:
    sender = {"user_id": user_id, "first_name": "Имя", "last_name": "Фамилия", "username": "name"} if user_id else {}
    return {"message": {"sender": sender, "recipient": {"chat_id": chat_id}, "body": {"text": text}}}


class DBFixture:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DATABASE_PATH
        db.DATABASE_PATH = Path(self.temp.name) / "test.sqlite3"
        db.init_db()

    def tearDown(self) -> None:
        db.DATABASE_PATH = self.old_path
        self.temp.cleanup()

    def rows(self, table: str) -> list[dict]:
        return db.fetch_all(f"SELECT * FROM {table} ORDER BY id")


class ActivityDBTests(DBFixture, unittest.TestCase):

    def test_fresh_schema_repeated_init_and_constraints(self) -> None:
        db.init_db()
        with db.get_connection() as conn:
            for table in ("applications", "questions", "appeals"):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertIn("messenger_user_id", columns)
                foreign_keys = list(conn.execute(f"PRAGMA foreign_key_list({table})"))
                self.assertTrue(any(row["from"] == "messenger_user_id" and row["on_delete"] == "RESTRICT" for row in foreign_keys))
                indexes = {row["name"] for row in conn.execute(f"PRAGMA index_list({table})")}
                self.assertIn(f"idx_{table}_messenger_user", indexes)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertIn("idx_user_sessions_one_open", {row["name"] for row in conn.execute("PRAGMA index_list(user_sessions)")})
            for name in ("idx_activity_user_time", "idx_activity_session_time", "idx_activity_type_time"):
                self.assertIn(name, {row["name"] for row in conn.execute("PRAGMA index_list(user_activity_events)")})
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO applications (messenger_user_id) VALUES (999999)")

    def test_existing_rows_and_columns_survive_without_mass_backfill(self) -> None:
        db.execute("INSERT INTO questions (max_user_id, question_text, created_at) VALUES (?, ?, ?)", ("old", "text", "2025-01-01 12:00:00"))
        db.execute("INSERT INTO applications (max_user_id, full_name) VALUES (?, ?)", ("old", "Legacy"))
        db.execute("INSERT INTO appeals (max_user_id, appeal_text) VALUES (?, ?)", ("old", "Legacy"))
        db.init_db()
        self.assertEqual(self.rows("questions")[0]["created_at"], "2025-01-01 12:00:00")
        for table in ("questions", "applications", "appeals"):
            self.assertIsNone(self.rows(table)[0]["messenger_user_id"])

    def test_two_near_init_calls(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: db.init_db(), range(2)))
        self.assertEqual(len(self.rows("messenger_users")), 0)

    def test_v029_style_database_preserves_rows_and_extra_columns(self) -> None:
        db.DATABASE_PATH.unlink()
        with sqlite3.connect(db.DATABASE_PATH) as conn:
            conn.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY AUTOINCREMENT, max_user_id TEXT, question_text TEXT, contact TEXT, status TEXT DEFAULT 'new', created_at TEXT, source_note TEXT)")
            conn.execute("INSERT INTO questions (max_user_id, question_text, created_at, source_note) VALUES (?, ?, ?, ?)", ("old", "original", "2025-01-01 12:00:00", "keep"))
            conn.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY AUTOINCREMENT, max_user_id TEXT, vacancy_id INTEGER, created_at TEXT)")
            conn.execute("INSERT INTO applications (max_user_id, vacancy_id) VALUES (?, ?)", ("old", 17))
            conn.execute("CREATE TABLE appeals (id INTEGER PRIMARY KEY AUTOINCREMENT, max_user_id TEXT, appeal_text TEXT, created_at TEXT)")
            conn.execute("INSERT INTO appeals (max_user_id, appeal_text) VALUES (?, ?)", ("old", "original"))
        db.init_db()
        db.init_db()
        question = self.rows("questions")[0]
        self.assertEqual((question["question_text"], question["source_note"], question["created_at"]),
                         ("original", "keep", "2025-01-01 12:00:00"))
        self.assertIsNone(question["messenger_user_id"])
        self.assertEqual(self.rows("applications")[0]["vacancy_id"], 17)
        self.assertEqual(self.rows("appeals")[0]["appeal_text"], "original")

    def test_two_near_fresh_init_calls(self) -> None:
        db.DATABASE_PATH.unlink()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: db.init_db(), range(2)))
        with db.get_connection() as conn:
            self.assertIn("messenger_user_id", {row["name"] for row in conn.execute("PRAGMA table_info(questions)")})

    def test_identity_upsert_profiles_and_messenger_namespace(self) -> None:
        first = db.touch_candidate("max", "123", {"first_name": "First", "username": "handle"}, T0)
        second = db.touch_candidate("max", "123", {"first_name": "  ", "username": "new", "display_name": "New"}, T1)
        db.touch_candidate("max", "123", {"username": None}, T0)
        other = db.touch_candidate("telegram", "123", at=T0)
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[0], other[0])
        user = db.fetch_one("SELECT * FROM messenger_users WHERE id = ?", (first[0],))
        self.assertEqual((user["first_seen_at"], user["last_seen_at"]), (T0, T1))
        self.assertEqual((user["first_name"], user["username"], user["display_name"]), ("First", "new", "New"))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO messenger_users (messenger, external_user_id, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)", ("max", "123", T0, T0))
        with self.assertRaises(ValueError):
            db.touch_candidate("max", "  ")

    def test_session_boundary_flags_and_reopened_connection(self) -> None:
        first = db.touch_candidate("max", "123", at=T0)
        same = db.touch_candidate("max", "123", at=T1)
        self.assertEqual(first[1], same[1])
        db.record_activity_event(same, "vacancies_opened", at=T1)
        db.record_activity_event(same, "question_sent", at=T1)
        db.record_activity_event(same, "appeal_sent", at=T1)
        old = db.fetch_one("SELECT * FROM user_sessions WHERE id = ?", (first[1],))
        self.assertEqual(old["meaningful_activity"], 1)
        self.assertEqual(old["first_meaningful_at"], T1)
        self.assertEqual(old["conversion_type"], "question_sent")
        self.assertEqual(old["converted_at"], T1)
        self.assertIsNone(old["interest_notification_due_at"])
        self.assertIsNone(old["interest_notification_sent_at"])
        new = db.touch_candidate("max", "123", at=T2)
        self.assertNotEqual(first[1], new[1])
        self.assertEqual(db.fetch_one("SELECT closed_at FROM user_sessions WHERE id = ?", (first[1],))["closed_at"], T2)
        self.assertEqual(len([row for row in self.rows("user_sessions") if row["closed_at"] is None]), 1)
        self.assertEqual(len(self.rows("user_activity_events")), 3)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO user_sessions (messenger_user_id, started_at, last_activity_at) VALUES (?, ?, ?)", (first[0], T2, T2))

    def test_every_event_type_and_metadata(self) -> None:
        context = db.touch_candidate("max", "123", at=T0)
        for event_type in sorted(db.ACTIVITY_EVENTS):
            vacancy_id = 42 if event_type in {"vacancy_viewed", "vacancy_apply_started"} else None
            metadata = {"vacancy_title": "Test"} if vacancy_id else None
            db.record_activity_event(context, event_type, vacancy_id, metadata, T0)
        events = self.rows("user_activity_events")
        self.assertEqual(len(events), 10)
        self.assertTrue(all(event["messenger_user_id"] == context[0] and event["session_id"] == context[1] for event in events))
        self.assertTrue(all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", event["created_at"]) for event in events))
        viewed = next(event for event in events if event["event_type"] == "vacancy_viewed")
        self.assertEqual(viewed["vacancy_id"], 42)
        self.assertEqual(json.loads(viewed["metadata"]), {"vacancy_title": "Test"})
        with self.assertRaises(ValueError):
            db.record_activity_event(context, "invalid")
        with self.assertRaises(ValueError):
            db.record_activity_event(context, "vacancy_viewed", metadata={"phone": "secret"})

    def test_lazy_link_only_question_and_appeal(self) -> None:
        for table in ("questions", "appeals", "applications"):
            db.execute(f"INSERT INTO {table} (max_user_id) VALUES (?)", ("123",))
            db.execute(f"INSERT INTO {table} (max_user_id) VALUES (?)", ("unrelated",))
        context = db.touch_candidate("max", "123", at=T0)
        db.touch_candidate("max", "123", at=T1)
        for table in ("questions", "appeals"):
            self.assertEqual(self.rows(table)[0]["messenger_user_id"], context[0])
            self.assertIsNone(self.rows(table)[1]["messenger_user_id"])
        self.assertIsNone(self.rows("applications")[0]["messenger_user_id"])

    def test_lazy_link_does_not_override_existing_link(self) -> None:
        other = db.touch_candidate("max", "other", at=T0)
        db.execute("INSERT INTO questions (max_user_id, messenger_user_id) VALUES (?, ?)", ("123", other[0]))
        db.touch_candidate("max", "123", at=T1)
        self.assertEqual(self.rows("questions")[0]["messenger_user_id"], other[0])

    def test_other_messenger_does_not_link_max_history(self) -> None:
        db.execute("INSERT INTO questions (max_user_id) VALUES (?)", ("123",))
        db.touch_candidate("telegram", "123", at=T0)
        self.assertIsNone(self.rows("questions")[0]["messenger_user_id"])

    def test_shared_chat_application_is_not_linked_to_different_finisher(self) -> None:
        finisher = db.touch_candidate("max", "finisher", at=T0)
        db.create_application({"max_user_id": "starter", "vacancy_id": 1}, finisher)
        self.assertIsNone(self.rows("applications")[0]["messenger_user_id"])
        self.assertEqual(self.rows("user_activity_events"), [])

    def test_submission_and_event_rollback_together(self) -> None:
        context = db.touch_candidate("max", "123", at=T0)
        cases = (
            ("applications", lambda: db.create_application({"max_user_id": "123", "vacancy_id": 1}, context)),
            ("questions", lambda: db.create_question("123", "text", "contact", context)),
            ("appeals", lambda: db.create_appeal("123", "Name", "phone", "text", context)),
        )
        for table, create in cases:
            with self.subTest(table=table):
                existing_events = len(self.rows("user_activity_events"))
                with patch.object(db, "_record_activity_event_conn", side_effect=RuntimeError("event failed")):
                    with self.assertRaises(RuntimeError):
                        create()
                self.assertEqual(self.rows(table), [])
                self.assertEqual(len(self.rows("user_activity_events")), existing_events)
                create()
                self.assertEqual(len(self.rows(table)), 1)
        self.assertEqual([row["event_type"] for row in self.rows("user_activity_events")],
                         ["vacancy_application_submitted", "question_sent", "appeal_sent"])


class BotActivityTests(DBFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        bot.user_states.clear()
        bot._PUBLIC_COMMANDS_SYNCED = False
        self.api = FakeMaxAPI()

    def send(self, text: str, user_id: str | None = "123", chat_id: str = "chat") -> None:
        item = update(text, user_id, chat_id)
        bot.handle_message(self.api, item["message"], item)

    def test_profile_partial_payload_and_no_avatar_guess(self) -> None:
        profile = bot.extract_user_profile(update("/start"))
        self.assertEqual(profile["display_name"], "Имя Фамилия")
        self.assertEqual(profile["username"], "name")
        self.assertIsNone(profile["avatar_url"])
        self.assertEqual(bot.extract_user_profile({"payload": "text", "chat_id": "c"})["first_name"], "")
        mixed = update("/start")
        mixed["user"] = {"user_id": "123", "username": "updated"}
        self.assertEqual(bot.extract_user_profile(mixed)["first_name"], "Имя")
        self.assertEqual(bot.extract_user_profile(mixed)["username"], "updated")

    def test_candidate_navigation_and_form_steps(self) -> None:
        self.send("/start")
        self.send("/menu")
        self.send("Актуальные вакансии")
        self.send("1")
        self.send("Откликнуться на эту вакансию")
        for value in ("Имя", "30", "+70000000000", "Высшее", "Да", "В любое время", "Пропустить", "Да"):
            self.send(value)
        self.send("Задать вопрос")
        self.send("Вопрос")
        self.send("нет")
        self.send("Написать сообщение")
        self.send("Имя")
        self.send("Телефон")
        self.send("Текст")
        self.send("Условия службы")
        events = [row["event_type"] for row in self.rows("user_activity_events")]
        self.assertEqual(events, [
            "bot_started", "main_menu_opened", "vacancies_opened", "vacancy_viewed",
            "vacancy_apply_started", "vacancy_application_submitted", "question_section_opened",
            "question_sent", "appeal_sent", "conditions_opened",
        ])
        for table in ("applications", "questions", "appeals"):
            self.assertIsNotNone(self.rows(table)[0]["messenger_user_id"])
        self.assertEqual(self.rows("applications")[0]["max_user_id"], "123")
        self.assertEqual(self.rows("questions")[0]["max_user_id"], "123")
        self.assertEqual(self.rows("appeals")[0]["max_user_id"], "123")
        self.assertEqual(self.rows("messenger_users")[0]["first_name"], "Имя")
        self.assertTrue(self.api.messages)

    def test_intermediate_form_message_touches_session_without_event(self) -> None:
        self.send("Задать вопрос")
        before = self.rows("user_sessions")[0]["last_activity_at"]
        self.send("Вопрос")
        self.assertEqual([row["event_type"] for row in self.rows("user_activity_events")], ["question_section_opened"])
        self.assertGreaterEqual(self.rows("user_sessions")[0]["last_activity_at"], before)

    def test_system_start_is_one_event_and_admin_command_is_excluded(self) -> None:
        item = update("", "123")
        item["update_type"] = "bot_started"
        bot.handle_bot_started(self.api, item)
        self.assertEqual([row["event_type"] for row in self.rows("user_activity_events")], ["bot_started"])
        self.send("/admin code")
        self.assertEqual([row["event_type"] for row in self.rows("user_activity_events")], ["bot_started"])

    def test_missing_sender_keeps_legacy_submission_without_activity(self) -> None:
        self.send("Актуальные вакансии", None)
        self.send("1", None)
        self.send("Откликнуться на эту вакансию", None)
        for value in ("Имя", "30", "+70000000000", "Высшее", "Да", "В любое время", "Пропустить", "Да"):
            self.send(value, None)
        self.send("Задать вопрос", None)
        self.send("Вопрос", None)
        self.send("нет", None)
        self.send("Написать сообщение", None)
        self.send("Имя", None)
        self.send("Телефон", None)
        self.send("Текст", None)
        self.assertEqual(len(self.rows("applications")), 1)
        self.assertEqual(len(self.rows("questions")), 1)
        self.assertEqual(len(self.rows("appeals")), 1)
        self.assertIsNone(self.rows("applications")[0]["messenger_user_id"])
        self.assertIsNone(self.rows("questions")[0]["messenger_user_id"])
        self.assertIsNone(self.rows("appeals")[0]["messenger_user_id"])
        self.assertEqual(self.rows("messenger_users"), [])
        self.assertEqual(self.rows("user_sessions"), [])
        self.assertEqual(self.rows("user_activity_events"), [])

    def test_approved_staff_in_candidate_menu_does_not_create_activity(self) -> None:
        db.execute("INSERT INTO admins (max_user_id, role, approved, is_active, can_use_bot_admin) VALUES (?, 'hr_staff', 1, 1, 1)", ("staff",))
        self.send("меню кандидата", "staff")
        self.send("/start", "staff")
        self.send("Актуальные вакансии", "staff")
        self.send("Условия службы", "staff")
        self.assertEqual(self.rows("messenger_users"), [])
        self.assertEqual(self.rows("user_activity_events"), [])


if __name__ == "__main__":
    unittest.main()
