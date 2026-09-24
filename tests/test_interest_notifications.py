from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from fastapi import HTTPException
from starlette.requests import Request

from app import admin_web, bot, db, interest_notifications as interest
from app.max_api import MaxAPI

BASE = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return interest.stamp(value)


def http_error(code: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = code
    return requests.exceptions.HTTPError(f"HTTP {code}", response=response)


class FakeAPI:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.attempts = 0
        self.sent: list[tuple[str, dict]] = []
        self.messages: list[str] = []

    def send_message_once(self, text: str, **kwargs: object) -> dict:
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((text, kwargs))
        return {"message_id": "accepted"}

    def send_message(self, text: str, **kwargs: object) -> dict:
        self.messages.append(text)
        return {"message_id": "accepted"}

    def set_bot_commands(self, *args: object, **kwargs: object) -> bool:
        return True


class InterestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DATABASE_PATH
        db.DATABASE_PATH = Path(self.temp.name) / "test.sqlite3"
        db.init_db()
        db.set_setting("interest_notifications_activated_at", iso(BASE - timedelta(days=1)))
        self.api = FakeAPI()
        bot.user_states.clear()

    def tearDown(self) -> None:
        db.DATABASE_PATH = self.old_path
        self.temp.cleanup()

    def admin(self, name: str, mode: str = "3h", **flags: int | str) -> dict:
        admin_id = db.execute("""INSERT INTO admins
            (max_user_id, chat_id, role, approved, is_active, can_use_bot_admin, can_receive_notifications,
             interest_mode, interest_mode_changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, "chat-" + name, flags.get("role", "hr_staff"), flags.get("approved", 1),
             flags.get("is_active", 1), flags.get("can_use_bot_admin", 1),
             flags.get("can_receive_notifications", 1), mode, iso(BASE - timedelta(days=1))))
        return db.get_admin(admin_id) or {}

    def candidate(self, name: str = "candidate", at: datetime = BASE, meaningful: bool = True, event: str = "vacancies_opened") -> db.ActivityContext:
        ctx = db.touch_candidate("max", name, {"display_name": "Иван", "username": "ivan"}, iso(at))
        if meaningful:
            db.record_activity_event(ctx, event, at=iso(at))
        return ctx

    def delivery(self, admin: dict, ctx: db.ActivityContext) -> dict | None:
        return db.fetch_one("SELECT * FROM interest_deliveries WHERE admin_id = ? AND session_id = ?", (admin["id"], ctx[1]))

    def test_schema_repeat_concurrent_and_no_retroactive_generation(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: db.init_db(), range(2)))
        admin = self.admin("hr")
        old = self.candidate(at=BASE - timedelta(days=2))
        db.set_setting("interest_notifications_activated_at", iso(BASE))
        interest.run_once(self.api, BASE + timedelta(days=1))
        self.assertIsNone(self.delivery(admin, old))
        self.assertIsNone(db.fetch_one("SELECT interest_notification_sent_at FROM user_sessions WHERE id = ?", (old[1],))["interest_notification_sent_at"])
        with db.get_connection() as conn:
            self.assertIn("interest_mode", {row["name"] for row in conn.execute("PRAGMA table_info(admins)")})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM interest_deliveries").fetchone()[0], 0)

    def test_upgrade_from_v030_shape_preserves_existing_rows(self) -> None:
        admin = self.admin("legacy")
        ctx = self.candidate(at=BASE - timedelta(days=30))
        db.execute("INSERT INTO questions (max_user_id, question_text, messenger_user_id) VALUES (?, ?, ?)", ("candidate", "old question", ctx[0]))
        with db.get_connection() as conn:
            conn.execute("DROP TABLE interest_deliveries")
            conn.execute("DROP TABLE interest_digests")
            conn.execute("DELETE FROM settings WHERE key = 'interest_notifications_activated_at'")
            conn.execute("ALTER TABLE admins DROP COLUMN interest_mode_changed_at")
            conn.execute("ALTER TABLE admins DROP COLUMN interest_mode")
        db.init_db()
        activated_at = db.get_setting("interest_notifications_activated_at")
        changed_at = db.get_admin(admin["id"])["interest_mode_changed_at"]
        self.assertEqual(changed_at, activated_at)
        db.init_db()
        self.assertEqual(db.get_admin(admin["id"])["interest_mode"], "3h")
        self.assertEqual(db.get_admin(admin["id"])["interest_mode_changed_at"], changed_at)
        self.assertEqual(db.fetch_one("SELECT question_text FROM questions")["question_text"], "old question")
        self.assertEqual(db.fetch_one("SELECT COUNT(*) AS n FROM user_activity_events")["n"], 1)
        interest.run_once(self.api, BASE + timedelta(hours=5))
        self.assertEqual(self.api.sent, [])

    def test_modes_independence_low_value_due_and_activity_extension(self) -> None:
        off = self.admin("off", "off")
        one = self.admin("one", "1h")
        three = self.admin("three", "3h")
        daily = self.admin("daily", "daily")
        low = self.candidate("low", meaningful=False)
        db.record_activity_event(low, "bot_started", at=iso(BASE))
        interest.run_once(self.api, BASE + timedelta(hours=4))
        self.assertEqual(db.fetch_all("SELECT * FROM interest_deliveries"), [])
        ctx = self.candidate()
        interest.run_once(self.api, BASE + timedelta(minutes=59, seconds=59))
        self.assertIsNone(self.delivery(off, ctx))
        self.assertEqual(self.delivery(one, ctx)["status"], "pending")
        self.assertEqual(self.delivery(three, ctx)["status"], "pending")
        self.assertEqual(self.delivery(daily, ctx)["status"], "pending")
        db.touch_candidate("max", "candidate", at=iso(BASE + timedelta(minutes=30)))
        interest.run_once(self.api, BASE + timedelta(hours=1))
        self.assertEqual(self.api.sent, [])
        interest.run_once(self.api, BASE + timedelta(hours=1, minutes=30))
        self.assertEqual(self.delivery(one, ctx)["status"], "sent")
        self.assertEqual(self.delivery(three, ctx)["status"], "pending")
        interest.run_once(self.api, BASE + timedelta(hours=3, minutes=30))
        self.assertEqual(self.delivery(three, ctx)["status"], "sent")
        self.assertEqual(len(self.api.sent), 2)

    def test_recipient_flags_and_mode_change_cutoff(self) -> None:
        valid = self.admin("valid", "1h")
        disabled = self.admin("disabled", "1h", is_active=0)
        unapproved = self.admin("unapproved", "1h", approved=0)
        muted = self.admin("muted", "1h", can_receive_notifications=0)
        wrong_role = self.admin("pending", "1h", role="pending")
        ctx = self.candidate()
        interest.run_once(self.api, BASE + timedelta(hours=1))
        self.assertEqual(self.delivery(valid, ctx)["status"], "sent")
        for admin in (disabled, unapproved, muted, wrong_role):
            self.assertIsNone(self.delivery(admin, ctx))
        db.set_interest_mode(valid["id"], "off")
        self.assertEqual(db.get_admin(valid["id"])["interest_mode"], "off")
        changed = db.get_admin(valid["id"])["interest_mode_changed_at"]
        self.assertTrue(changed)
        db.set_interest_mode(valid["id"], "3h")
        self.assertEqual(db.get_admin(valid["id"])["interest_mode"], "3h")
        self.assertEqual(len(self.api.sent), 1)

    def test_mode_change_cancels_pending_and_requires_new_interest(self) -> None:
        admin = self.admin("hr", "3h")
        ctx = self.candidate()
        interest.materialize(BASE)
        self.assertEqual(self.delivery(admin, ctx)["status"], "pending")
        with patch.object(db, "utc_now_iso", return_value=iso(BASE + timedelta(minutes=1))):
            db.set_interest_mode(admin["id"], "off")
        self.assertEqual(self.delivery(admin, ctx)["status"], "cancelled")
        with patch.object(db, "utc_now_iso", return_value=iso(BASE + timedelta(minutes=2))):
            db.set_interest_mode(admin["id"], "1h")
        interest.run_once(self.api, BASE + timedelta(hours=2))
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.delivery(admin, ctx)["status"], "cancelled")
        db.touch_candidate("max", "candidate", at=iso(BASE + timedelta(hours=2)))
        db.record_activity_event(ctx, "conditions_opened", at=iso(BASE + timedelta(hours=2)))
        interest.run_once(self.api, BASE + timedelta(hours=3))
        self.assertEqual(self.delivery(admin, ctx)["status"], "sent")

    def test_conversion_after_due_before_send_and_while_rendering(self) -> None:
        admin = self.admin("hr", "1h")
        ctx = self.candidate()
        interest.materialize(BASE)
        db.record_activity_event(ctx, "appeal_sent", at=iso(BASE + timedelta(hours=1)))
        interest.run_once(self.api, BASE + timedelta(hours=1))
        self.assertEqual(self.api.sent, [])
        newer = self.candidate("newer", BASE + timedelta(days=1))
        original = interest._single_message

        def convert_during_render(delivery: dict, session: dict) -> tuple[str, dict]:
            result = original(delivery, session)
            db.record_activity_event(newer, "question_sent", at=iso(BASE + timedelta(days=1, hours=1)))
            return result

        with patch.object(interest, "_single_message", side_effect=convert_during_render):
            interest.run_once(self.api, BASE + timedelta(days=1, hours=1))
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.delivery(admin, newer)["status"], "cancelled")

    def test_conversion_suppression_all_types_and_newer_session(self) -> None:
        admin = self.admin("hr", "1h")
        for index, event in enumerate(sorted(db.CONVERSION_EVENTS)):
            ctx = self.candidate(str(index), BASE + timedelta(days=index))
            interest.materialize(BASE + timedelta(days=index))
            db.record_activity_event(ctx, event, at=iso(BASE + timedelta(days=index, minutes=10)))
            interest.run_once(self.api, BASE + timedelta(days=index, hours=2))
            self.assertNotEqual(self.delivery(admin, ctx)["status"], "sent")
        old = self.candidate("later", BASE + timedelta(days=4))
        interest.materialize(BASE + timedelta(days=4))
        newer_at = BASE + timedelta(days=4, hours=4)
        new = self.candidate("later", newer_at)
        db.record_activity_event(new, "question_sent", at=iso(newer_at))
        interest.run_once(self.api, newer_at + timedelta(hours=1))
        self.assertNotEqual(self.delivery(admin, old)["status"], "sent")
        self.assertEqual(self.api.sent, [])

    def test_cooldown_and_supersession(self) -> None:
        admin = self.admin("hr", "1h")
        first = self.candidate()
        interest.run_once(self.api, BASE + timedelta(hours=1))
        self.assertEqual(self.delivery(admin, first)["status"], "sent")
        second_at = BASE + timedelta(hours=4)
        second = self.candidate(at=second_at)
        third_at = BASE + timedelta(hours=8)
        third = self.candidate(at=third_at)
        interest.run_once(self.api, third_at + timedelta(hours=1))
        self.assertEqual(self.delivery(admin, second)["status"], "superseded")
        self.assertEqual(self.delivery(admin, third)["status"], "pending")
        self.assertEqual(len(self.api.sent), 1)
        interest.run_once(self.api, BASE + timedelta(hours=25))
        self.assertEqual(self.delivery(admin, third)["status"], "sent")
        self.assertEqual(len(self.api.sent), 2)

    def test_reboot_claim_recovery_and_no_replay_of_sending(self) -> None:
        admin = self.admin("hr", "1h")
        ctx = self.candidate()
        interest.materialize(BASE)
        delivery = self.delivery(admin, ctx)
        now = iso(BASE + timedelta(hours=1))
        token = interest._claim("interest_deliveries", delivery["id"], now)
        self.assertIsNotNone(token)
        self.assertIsNone(interest._claim("interest_deliveries", delivery["id"], now))
        interest._repair_claims(iso(BASE + timedelta(hours=1, minutes=3)))
        self.assertEqual(self.delivery(admin, ctx)["status"], "pending")
        token = interest._claim("interest_deliveries", delivery["id"], iso(BASE + timedelta(hours=1, minutes=3)))
        self.assertIsNotNone(interest._prepare_single(delivery["id"], token, iso(BASE + timedelta(hours=1, minutes=3))))
        interest._repair_claims(iso(BASE + timedelta(hours=1, minutes=6)))
        self.assertEqual(self.delivery(admin, ctx)["status"], "uncertain")
        interest.run_once(self.api, BASE + timedelta(hours=2))
        self.assertEqual(self.api.sent, [])

    def test_two_workers_only_one_claims(self) -> None:
        admin = self.admin("hr", "1h")
        ctx = self.candidate()
        interest.materialize(BASE)
        delivery = self.delivery(admin, ctx)
        now = iso(BASE + timedelta(hours=1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: interest._claim("interest_deliveries", delivery["id"], now), range(2)))
        self.assertEqual(sum(token is not None for token in results), 1)

    def test_retry_429_then_success_and_ambiguous_results(self) -> None:
        admin = self.admin("hr", "1h")
        ctx = self.candidate()
        api = FakeAPI([http_error(429)])
        interest.run_once(api, BASE + timedelta(hours=1))
        self.assertEqual(self.delivery(admin, ctx)["status"], "failed")
        self.assertEqual(self.delivery(admin, ctx)["next_attempt_at"], iso(BASE + timedelta(hours=1, minutes=5)))
        interest.run_once(api, BASE + timedelta(hours=1, minutes=4))
        self.assertEqual(api.attempts, 1)
        interest.run_once(api, BASE + timedelta(hours=1, minutes=5))
        self.assertEqual(api.attempts, 2)
        self.assertEqual(self.delivery(admin, ctx)["status"], "sent")
        interest.run_once(api, BASE + timedelta(hours=2))
        self.assertEqual(api.attempts, 2)
        for name, failure in (("timeout", requests.exceptions.Timeout()), ("server", http_error(503))):
            other = self.candidate(name, BASE + timedelta(days=2 if name == "timeout" else 3))
            failed_api = FakeAPI([failure])
            interest.run_once(failed_api, BASE + timedelta(days=2 if name == "timeout" else 3, hours=1))
            self.assertEqual(self.delivery(admin, other)["status"], "uncertain")
            interest.run_once(failed_api, BASE + timedelta(days=4))
            self.assertEqual(failed_api.attempts, 1)

    def test_uncertain_send_blocks_repeat_during_cooldown(self) -> None:
        admin = self.admin("hr", "1h")
        first = self.candidate()
        api = FakeAPI([requests.exceptions.Timeout()])
        interest.run_once(api, BASE + timedelta(hours=1))
        self.assertEqual(self.delivery(admin, first)["status"], "uncertain")
        second = self.candidate(at=BASE + timedelta(hours=4))
        interest.run_once(api, BASE + timedelta(hours=5))
        self.assertEqual(api.attempts, 1)
        self.assertEqual(self.delivery(admin, second)["due_at"], iso(BASE + timedelta(hours=25)))
        interest.run_once(api, BASE + timedelta(hours=24, minutes=59))
        self.assertEqual(api.attempts, 1)
        interest.run_once(api, BASE + timedelta(hours=25))
        self.assertEqual(api.attempts, 2)

    def test_429_backoff_stops_after_five_attempts(self) -> None:
        admin = self.admin("hr", "1h")
        ctx = self.candidate()
        api = FakeAPI([http_error(429) for _ in range(5)])
        attempts = [BASE + timedelta(hours=1), BASE + timedelta(hours=1, minutes=5),
                    BASE + timedelta(hours=1, minutes=20), BASE + timedelta(hours=2, minutes=20),
                    BASE + timedelta(hours=5, minutes=20)]
        expected_next = [attempts[1], attempts[2], attempts[3], attempts[4], None]
        for index, when in enumerate(attempts):
            interest.run_once(api, when)
            row = self.delivery(admin, ctx)
            self.assertEqual(row["attempt_count"], index + 1)
            self.assertEqual(row["next_attempt_at"], iso(expected_next[index]) if expected_next[index] else None)
        interest.run_once(api, BASE + timedelta(days=2))
        self.assertEqual(api.attempts, 5)

    def test_daily_boundary_digest_and_history_tokens(self) -> None:
        admin = self.admin("hr", "daily")
        first = self.candidate("first")
        second = self.candidate("second", BASE + timedelta(hours=4))
        before = datetime(2026, 10, 2, 5, 59, tzinfo=timezone.utc)
        interest.run_once(self.api, before)
        self.assertEqual(self.api.sent, [])
        due = before + timedelta(minutes=1)
        interest.run_once(self.api, due)
        self.assertEqual(len(self.api.sent), 1)
        digest = db.fetch_one("SELECT * FROM interest_digests WHERE admin_id = ?", (admin["id"],))
        self.assertEqual(digest["status"], "sent")
        self.assertEqual(self.delivery(admin, first)["status"], "sent")
        self.assertEqual(self.delivery(admin, second)["status"], "sent")
        self.assertIsNotNone(interest.digest_for_token(digest["action_token"], admin))
        self.assertNotIn(digest["action_token"], self.api.sent[0][0])
        self.assertEqual(self.api.sent[0][1]["keyboard"]["payload"]["buttons"][0][0]["text"], "Открыть список")
        self.assertNotIn(self.delivery(admin, first)["action_token"],
                         interest.digest_for_token(digest["action_token"], admin)[0])
        self.assertIsNotNone(interest.history_for_token(self.delivery(admin, first)["action_token"], admin))
        wrong = self.admin("wrong", "off")
        self.assertIsNone(interest.digest_for_token(digest["action_token"], wrong))
        self.assertIsNone(interest.history_for_token(self.delivery(admin, first)["action_token"], wrong))
        interest.run_once(self.api, due + timedelta(hours=1))
        self.assertEqual(len(self.api.sent), 1)

    def test_daily_grouping_cap_remainder_and_downtime(self) -> None:
        admin = self.admin("hr", "daily")
        first = self.candidate("repeat", BASE)
        second = self.candidate("repeat", BASE + timedelta(hours=4))
        for index in range(20):
            self.candidate(f"extra-{index}", BASE + timedelta(minutes=index))
        due = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
        interest.run_once(self.api, due)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.api.sent[0][0].count("Иван"), 20)
        self.assertIn("Ещё пользователей в очереди: 1", self.api.sent[0][0])
        self.assertEqual(self.delivery(admin, first)["status"], "sent")
        self.assertEqual(self.delivery(admin, second)["status"], "sent")
        pending = db.fetch_one("SELECT COUNT(*) AS n FROM interest_deliveries WHERE status = 'pending' AND admin_id = ?", (admin["id"],))
        self.assertEqual(pending["n"], 1)
        interest.run_once(self.api, due + timedelta(hours=1))
        self.assertEqual(len(self.api.sent), 1)
        interest.run_once(self.api, due + timedelta(days=1))
        self.assertEqual(len(self.api.sent), 2)

    def test_daily_429_retries_and_mode_change_cancels_pending_digest(self) -> None:
        admin = self.admin("hr", "daily")
        ctx = self.candidate()
        due = datetime(2026, 10, 2, 6, tzinfo=timezone.utc)
        api = FakeAPI([http_error(429)])
        interest.run_once(api, due)
        digest = db.fetch_one("SELECT * FROM interest_digests WHERE admin_id = ?", (admin["id"],))
        self.assertEqual(digest["status"], "failed")
        self.assertEqual(digest["next_attempt_at"], iso(due + timedelta(minutes=5)))
        interest.run_once(api, due + timedelta(minutes=4))
        self.assertEqual(api.attempts, 1)
        interest.run_once(api, due + timedelta(minutes=5))
        self.assertEqual(db.fetch_one("SELECT status FROM interest_digests WHERE id = ?", (digest["id"],))["status"], "sent")
        self.assertEqual(self.delivery(admin, ctx)["status"], "sent")

    def test_expired_digest_sending_becomes_uncertain_without_replay(self) -> None:
        admin = self.admin("hr", "daily")
        ctx = self.candidate()
        due = datetime(2026, 10, 2, 6, tzinfo=timezone.utc)
        with patch.object(interest, "_confirm_digest_sending", side_effect=RuntimeError("process stopped")):
            with self.assertRaises(RuntimeError):
                interest.run_once(self.api, due)
        digest = db.fetch_one("SELECT * FROM interest_digests WHERE admin_id = ?", (admin["id"],))
        self.assertEqual(digest["status"], "sending")
        interest.run_once(self.api, due + timedelta(minutes=3))
        self.assertEqual(db.fetch_one("SELECT status FROM interest_digests WHERE id = ?", (digest["id"],))["status"], "uncertain")
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.delivery(admin, ctx)["status"], "uncertain")

    def test_bot_setting_and_history_action(self) -> None:
        admin = self.admin("hr", "1h")
        ctx = self.candidate()
        interest.run_once(self.api, BASE + timedelta(hours=1))
        token = self.delivery(admin, ctx)["action_token"]
        self.assertNotIn(token, self.api.sent[0][0])
        buttons = self.api.sent[0][1]["keyboard"]["payload"]["buttons"]
        self.assertEqual([row[0]["text"] for row in buttons], ["История активности", "Написать кандидату"])
        self.assertIn(token, buttons[0][0]["payload"])

        def message(text: str, user_id: str) -> None:
            update = {"message": {"sender": {"user_id": user_id}, "recipient": {"chat_id": "chat-" + user_id},
                                  "body": {"text": text}}}
            bot.handle_message(self.api, update["message"], update)

        message("Уведомления об интересе", "hr")
        self.assertIn("Через 1 час", self.api.messages[-1])
        message("История активности " + token, "hr")
        self.assertIn("История активности", self.api.messages[-1])
        self.assertIn("Открыл вакансии", self.api.messages[-1])
        self.admin("other", "off")
        message("История активности " + token, "other")
        self.assertEqual(self.api.messages[-1], "История недоступна.")
        message("Интерес: через 3 часа", "hr")
        self.assertEqual(db.get_admin(admin["id"])["interest_mode"], "3h")

    def test_permanent_http_error_and_timeout_do_not_replay(self) -> None:
        admin = self.admin("hr", "1h")
        ctx = self.candidate()
        api = FakeAPI([http_error(403)])
        interest.run_once(api, BASE + timedelta(hours=1))
        row = self.delivery(admin, ctx)
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["next_attempt_at"])
        interest.run_once(api, BASE + timedelta(hours=5))
        self.assertEqual(api.attempts, 1)

    def test_summary_and_web_owner_preference(self) -> None:
        admin = self.admin("hr", "1h")
        ctx = db.touch_candidate("max", "viewer", {"display_name": "", "username": ""}, iso(BASE))
        db.record_activity_event(ctx, "vacancy_viewed", vacancy_id=999, metadata={"vacancy_title": "Инженер"}, at=iso(BASE))
        db.record_activity_event(ctx, "vacancy_viewed", vacancy_id=999, metadata={"vacancy_title": "Инженер"}, at=iso(BASE))
        db.record_activity_event(ctx, "question_section_opened", at=iso(BASE))
        interest.run_once(self.api, BASE + timedelta(hours=1))
        text = self.api.sent[0][0]
        self.assertIn("Инженер — 2 просм.", text)
        self.assertIn("Открыл раздел вопросов", text)
        self.assertNotIn("Задал вопрос", text)
        with (patch.object(admin_web, "require_admin"),
              patch.object(admin_web, "current_admin", return_value={"id": admin["id"], "role": "hr_staff"})):
            response = admin_web.profile_interest_mode(SimpleNamespace(), "daily")
            self.assertEqual(response.status_code, 303)
            self.assertEqual(db.get_admin(admin["id"])["interest_mode"], "daily")
            with self.assertRaises(HTTPException):
                admin_web.profile_interest_mode(SimpleNamespace(), "invalid")
        with (patch.object(admin_web, "require_admin"),
              patch.object(admin_web, "current_admin", return_value=None)):
            with self.assertRaises(HTTPException):
                admin_web.profile_interest_mode(SimpleNamespace(), "off")
        self.assertIsNone(interest.history_for_token("bad", admin))
        self.assertIsNone(interest.history_for_token(self.delivery(admin, ctx)["action_token"], {**admin, "is_active": 0}))

    def test_web_profile_route_authentication_and_owner(self) -> None:
        owner = self.admin("owner", "3h")
        other = self.admin("other", "3h")
        with patch.object(admin_web, "settings", return_value={"login": "admin", "password": "unused", "secret": "test-only"}):
            unauthenticated = Request({"type": "http", "method": "POST", "path": "/admin/profile/interest-mode", "headers": []})
            with self.assertRaises(HTTPException) as denied:
                admin_web.profile_interest_mode(unauthenticated, "off")
            self.assertEqual(denied.exception.status_code, 303)
            self.assertEqual(db.get_admin(owner["id"])["interest_mode"], "3h")
            cookie = admin_web.make_session({"authenticated": True, "admin_id": owner["id"], "admin_role": "hr_staff"})
            request = Request({"type": "http", "method": "POST", "path": "/admin/profile/interest-mode",
                               "headers": [(b"cookie", f"{admin_web.SESSION_COOKIE}={cookie}".encode())]})
            response = admin_web.profile_interest_mode(request, "daily")
            self.assertEqual(response.status_code, 303)
            self.assertEqual(db.get_admin(owner["id"])["interest_mode"], "daily")
            self.assertEqual(db.get_admin(other["id"])["interest_mode"], "3h")
            with self.assertRaises(HTTPException) as invalid:
                admin_web.profile_interest_mode(request, "invalid")
            self.assertEqual(invalid.exception.status_code, 400)

    def test_max_one_shot_does_not_retry(self) -> None:
        api = MaxAPI("test-token")
        with patch.object(api, "_request", side_effect=requests.exceptions.Timeout()) as request:
            with self.assertRaises(requests.exceptions.Timeout):
                api.send_message_once("text", user_id="hr")
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
