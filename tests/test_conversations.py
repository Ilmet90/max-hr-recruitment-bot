from __future__ import annotations

import sqlite3
import re
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests
from starlette.requests import Request

from app import admin_web, bot, conversations as conv, db, interest_notifications as interest
from app.max_api import MaxAPI
from app.max_mentions import candidate_mention, escape_markdown


def http_error(code: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = code
    return requests.exceptions.HTTPError(f"HTTP {code}", response=response)


class API:
    def __init__(self, failure: Exception | None = None):
        self.failure = failure
        self.sent: list[tuple[str, dict]] = []
        self.attempts = 0

    def send_message(self, text: str, **kwargs: object) -> dict:
        self.sent.append((text, kwargs))
        return {"message_id": str(len(self.sent))}

    def set_bot_commands(self, *args: object, **kwargs: object) -> bool:
        return True

    def send_message_once(self, text: str, **kwargs: object) -> dict:
        self.attempts += 1
        if self.failure:
            raise self.failure
        self.sent.append((text, kwargs))
        return {"message": {"body": {"mid": f"out-{secrets.token_hex(8)}"}}}


def update(user: str, text: str, mid: str | None, *, kind: str = "message_created", chat_type: str = "dialog") -> dict:
    body = {"text": text}
    if mid is not None:
        body["mid"] = mid
    return {"update_type": kind, "message": {
        "sender": {"user_id": user, "name": user},
        "recipient": {"chat_id": "chat-" + user, "chat_type": chat_type}, "body": body,
    }}


def realistic_callback(user: str, payload: str, callback_id: str, *, actor_key: str = "user_id") -> dict:
    return {"update_type": "message_callback",
            "callback": {"callback_id": callback_id, "payload": payload,
                         "user": {actor_key: user}},
            "message": {"sender": {"user_id": "bot-user", "is_bot": True},
                        "recipient": {"chat_type": "dialog"},
                        "body": {"mid": "original-bot-message"}}}


class ConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DATABASE_PATH
        db.DATABASE_PATH = Path(self.temp.name) / "test.sqlite3"
        db.init_db()
        bot.user_states.clear()
        bot.reply_states.clear()
        self.api = API()

    def tearDown(self) -> None:
        bot.user_states.clear()
        bot.reply_states.clear()
        db.DATABASE_PATH = self.old_path
        self.temp.cleanup()

    def candidate(self, external: str = "candidate") -> db.ActivityContext:
        return db.touch_candidate("max", external, {"display_name": external})

    def admin(self, external: str = "hr", *, role: str = "hr_staff", bot_access: int = 1) -> dict:
        admin_id = db.execute("""INSERT INTO admins
            (max_user_id, chat_id, role, approved, is_active, can_use_bot_admin, can_receive_notifications)
            VALUES (?, ?, ?, 1, 1, ?, 1)""", (external, "chat-" + external, role, bot_access))
        return db.get_admin(admin_id) or {}

    def process(self, event: dict) -> None:
        bot.process_update_batch(self.api, {"updates": [event], "marker": "100"})

    def test_migration_is_idempotent_concurrent_and_preserves_old_rows(self) -> None:
        ctx = self.candidate()
        qid = db.execute("INSERT INTO questions (max_user_id, question_text, messenger_user_id) VALUES (?, ?, ?)",
                         ("candidate", "old", ctx[0]))
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: db.init_db(), range(3)))
        self.assertEqual(db.fetch_one("SELECT question_text FROM questions WHERE id = ?", (qid,))["question_text"], "old")
        self.assertIsNone(conv.get_by_user(ctx[0]))
        with db.get_connection() as conn:
            tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertTrue({"conversations", "conversation_sources", "conversation_messages", "admin_conversation_state"} <= tables)
            self.assertIn("idx_conversation_messages_external", {r["name"] for r in conn.execute("PRAGMA index_list(conversation_messages)")})

    def test_upgrade_from_v031_shape_does_not_backfill(self) -> None:
        ctx = self.candidate()
        old = db.execute("INSERT INTO questions (max_user_id, question_text, messenger_user_id) VALUES (?, ?, ?)",
                         ("candidate", "Old question", ctx[0]))
        with db.get_connection() as conn:
            for table in ("processed_max_updates", "admin_conversation_state", "conversation_messages",
                          "conversation_sources", "conversations"):
                conn.execute(f"DROP TABLE {table}")
        db.init_db()
        self.assertEqual(db.fetch_one("SELECT question_text FROM questions WHERE id = ?", (old,))["question_text"], "Old question")
        self.assertIsNone(conv.get_by_user(ctx[0]))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 0)

    def test_sources_share_one_conversation_and_application_is_card_only(self) -> None:
        ctx = self.candidate()
        qid = db.create_question("candidate", "Когда?", "нет", ctx, question_mid="q-mid")
        aid = db.create_appeal("candidate", "Иван", "123", "Здравствуйте", ctx, appeal_mid="a-mid")
        app_id = db.create_application({"max_user_id": "candidate", "vacancy_title": "Вакансия", "full_name": "Иван"}, ctx)
        conversation = conv.get_by_user(ctx[0])
        self.assertIsNotNone(conversation)
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversations")["n"], 1)
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_sources")["n"], 3)
        self.assertEqual([r["text"] for r in conv.detail(conversation["id"])["messages"]], ["Когда?", "Здравствуйте"])
        self.assertEqual({qid, aid, app_id}, {r["source_record_id"] for r in db.fetch_all("SELECT source_record_id FROM conversation_sources")})
        other = self.candidate("other")
        db.create_question("other", "Иное", "нет", other)
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversations")["n"], 2)

    def test_unconfirmed_legacy_source_stays_unlinked_and_deletion_keeps_chat(self) -> None:
        ctx = self.candidate()
        db.execute("INSERT INTO questions (max_user_id, question_text) VALUES (?, ?)", ("other", "Unlinked"))
        qid = db.create_question("candidate", "Hello", "нет", ctx)
        cid = conv.get_by_user(ctx[0])["id"]
        conv.detail(cid)
        self.assertEqual(len(db.fetch_all("SELECT * FROM conversation_sources")), 1)
        db.delete_record_permanently("questions", qid)
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_sources")["n"], 0)
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 1)
        appeal = db.create_appeal("candidate", "Name", "123", "Direct delete", ctx)
        db.execute("DELETE FROM appeals WHERE id = ?", (appeal,))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_sources")["n"], 0)
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 2)

    def test_navigation_does_not_create_conversation(self) -> None:
        for index, text in enumerate(("/start", "/menu", "Актуальные вакансии", "Условия службы", "Контакты")):
            self.process(update("candidate", text, f"nav-{index}"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversations")["n"], 0)

    def test_inbound_mid_dedupe_null_and_filter(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Q", "нет", ctx, question_mid="source-q")
        for mid in ("m1", "m1", "m2", None, None):
            self.process(update("candidate", "Спасибо", mid))
        self.process(update("candidate", "Edited", "m3", kind="message_edited"))
        self.process(update("candidate", "Group", "m4", chat_type="chat"))
        texts = [r["text"] for r in db.fetch_all("SELECT text FROM conversation_messages ORDER BY id")]
        self.assertEqual(texts, ["Q", "Спасибо", "Спасибо", "Спасибо", "Спасибо"])
        self.assertEqual(db.fetch_one("SELECT status FROM conversations WHERE messenger_user_id = ?", (ctx[0],))["status"], "open")

    def test_form_precedence_and_free_text_after_contact(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Q", "нет", ctx)
        bot.user_states["chat-candidate"] = {"scenario": "question", "step": "question"}
        self.process(update("candidate", "Inside question", "form-1"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 1)
        self.process(update("candidate", "нет", "form-2"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 2)
        self.process(update("candidate", "Контакты", "nav-1"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 2)
        self.process(update("candidate", "Свободный ответ", "free-1"))
        self.assertEqual(db.fetch_one("SELECT text FROM conversation_messages ORDER BY id DESC LIMIT 1")["text"], "Свободный ответ")

    def test_application_and_appeal_forms_precede_conversation(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Existing", "нет", ctx)
        bot.user_states["chat-candidate"] = {"scenario": "application", "step": "confirm", "data": {
            "max_user_id": "candidate", "vacancy_title": "Role", "full_name": "Name",
        }}
        self.process(update("candidate", "Да", "apply-final"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 1)
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_sources")["n"], 2)
        bot.user_states["chat-candidate"] = {"scenario": "appeal", "step": "text",
                                             "full_name": "Name", "phone": "123"}
        self.process(update("candidate", "Appeal body", "appeal-final"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 2)
        self.assertEqual(db.fetch_one("SELECT text FROM conversation_messages ORDER BY id DESC LIMIT 1")["text"], "Appeal body")

    def test_free_reply_from_vacancy_view_and_recognized_navigation(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Existing", "нет", ctx)
        bot.user_states["chat-candidate"] = {"scenario": "view", "vacancies": []}
        self.process(update("candidate", "Спасибо, а какой график?", "view-free"))
        self.assertEqual(db.fetch_one("SELECT text FROM conversation_messages ORDER BY id DESC LIMIT 1")["text"],
                         "Спасибо, а какой график?")
        self.process(update("candidate", "Контакты", "view-nav"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 2)

    def test_structured_completion_receipt_is_atomic_with_source(self) -> None:
        self.candidate()
        bot.user_states["chat-candidate"] = {"scenario": "question", "step": "contact",
                                             "question": "Question", "question_mid": "q-body"}
        event = update("candidate", "нет", "q-final")
        with patch.object(bot, "notify_admins", side_effect=RuntimeError("after commit")):
            with self.assertRaises(RuntimeError):
                bot.process_update_batch(self.api, {"updates": [event], "marker": "200"})
        self.assertEqual(db.get_setting("max_updates_marker", ""), "")
        self.assertTrue(conv.update_was_processed("message:q-final"))
        bot.process_update_batch(self.api, {"updates": [event], "marker": "200"})
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM questions")["n"], 1)
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_sources")["n"], 1)

    def test_outbound_idempotency_errors_and_explicit_retry(self) -> None:
        ctx = self.candidate()
        admin = self.admin()
        first = conv.send_outbound(self.api, ctx[0], admin, "Здравствуйте", "web-key-1")
        duplicate = conv.send_outbound(self.api, ctx[0], admin, "Здравствуйте", "web-key-1")
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(first["delivery_status"], "sent")
        self.assertEqual(self.api.attempts, 1)
        self.assertEqual(first["text"], "Здравствуйте")
        self.assertEqual(self.api.sent[0][0], conv.candidate_outbound_text("Здравствуйте"))
        self.assertEqual(self.api.sent[0][1]["keyboard"], bot.MENU_ONLY_KEYBOARD)
        for key, failure, expected in (("bad", http_error(400), "failed"),
                                       ("limit", http_error(429), "failed"),
                                       ("server", http_error(503), "uncertain"),
                                       ("timeout", requests.exceptions.Timeout(), "uncertain")):
            api = API(failure)
            result = conv.send_outbound(api, ctx[0], admin, key, key)
            self.assertEqual(result["delivery_status"], expected)
            self.assertEqual(api.attempts, 1)
        failed = db.fetch_one("SELECT id FROM conversation_messages WHERE request_key = 'bad'")
        retry_api = API()
        retried = conv.retry_failed(retry_api, failed["id"], admin, "retry-key")
        self.assertEqual(retried["delivery_status"], "sent")
        self.assertEqual(retried["text"], "bad")
        self.assertEqual(retry_api.sent[0][0], conv.candidate_outbound_text("bad"))
        self.assertEqual(retry_api.sent[0][1]["keyboard"], bot.MENU_ONLY_KEYBOARD)
        self.assertEqual(db.fetch_one("SELECT delivery_status FROM conversation_messages WHERE id = ?", (failed["id"],))["delivery_status"], "failed")
        uncertain = db.fetch_one("SELECT id FROM conversation_messages WHERE request_key = 'timeout'")
        with self.assertRaises(ValueError):
            conv.retry_failed(API(), uncertain["id"], admin, "unsafe")
        empty_response = API()
        empty_response.send_message_once = lambda text, **kwargs: {}
        missing_mid = conv.send_outbound(empty_response, ctx[0], admin, "No MID", "missing-mid")
        self.assertEqual((missing_mid["delivery_status"], missing_mid["last_error_code"], missing_mid["sent_at"]),
                         ("uncertain", "missing_message_id", None))

    def test_validation_and_superadmin(self) -> None:
        ctx = self.candidate()
        with self.assertRaises(PermissionError):
            conv.send_outbound(API(), ctx[0], {"id": 999, "role": "hr_staff"}, "Hello", "denied")
        with self.assertRaises(ValueError):
            conv.send_outbound(API(), ctx[0], {"role": "superadmin"}, " ", "empty")
        sent = conv.send_outbound(API(), ctx[0], {"role": "superadmin", "id": None}, "Hello", "super")
        self.assertEqual(sent["sender_type"], "superadmin")

    def test_source_identity_and_unique_mid_constraints(self) -> None:
        one, two = self.candidate("one"), self.candidate("two")
        question = db.create_question("one", "Q", "нет", one, question_mid="unique-source")
        with db.get_connection() as conn:
            with self.assertRaises(ValueError):
                conv.link_source_conn(conn, "question", question, two[0])
        cid = conv.get_by_user(one[0])["id"]
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO conversation_messages
                (conversation_id, messenger, external_message_id, direction, sender_type, text, delivery_status, created_at)
                VALUES (?, 'max', 'unique-source', 'inbound', 'candidate', 'Duplicate', 'received', ?)""",
                (cid, db.utc_now_iso()))

    def test_interrupted_sends_are_not_retried_automatically(self) -> None:
        ctx = self.candidate()
        admin = self.admin()
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        sending, _ = conv.create_outbound(ctx[0], admin, "Maybe sent", "crash-sending")
        pending, _ = conv.create_outbound(ctx[0], admin, "Never sent", "crash-pending")
        db.execute("UPDATE conversation_messages SET delivery_status = 'sending', send_started_at = ? WHERE id = ?", (old, sending["id"]))
        db.execute("UPDATE conversation_messages SET created_at = ? WHERE id = ?", (old, pending["id"]))
        self.assertEqual(conv.recover_interrupted_sends(), 2)
        self.assertEqual(db.fetch_one("SELECT delivery_status FROM conversation_messages WHERE id = ?", (sending["id"],))["delivery_status"], "uncertain")
        self.assertEqual(db.fetch_one("SELECT delivery_status FROM conversation_messages WHERE id = ?", (pending["id"],))["delivery_status"], "failed")

    def test_per_reader_unread_and_render_boundary(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Q", "нет", ctx)
        cid = conv.get_by_user(ctx[0])["id"]
        hr_a, hr_b = self.admin("a"), self.admin("b")
        superadmin = {"role": "superadmin", "id": None}
        self.assertEqual(conv.list_conversations(hr_a)[0]["unread"], 1)
        self.assertEqual(conv.list_conversations(hr_b)[0]["unread"], 1)
        rendered = conv.detail(cid)
        boundary = rendered["messages"][-1]["id"]
        conv.add_inbound(ctx[0], "arrived during render", "later")
        conv.mark_read(cid, hr_a, boundary)
        self.assertEqual(conv.list_conversations(hr_a)[0]["unread"], 1)
        self.assertEqual(conv.list_conversations(hr_b)[0]["unread"], 2)
        self.assertEqual(conv.list_conversations(superadmin)[0]["unread"], 2)
        with self.assertRaises(ValueError):
            conv.mark_read(cid, hr_a, 999999)
        conv.send_outbound(API(), ctx[0], hr_a, "answer", "unread-out")
        self.assertEqual(conv.list_conversations(hr_a)[0]["unread"], 1)

    def test_status_and_reopen(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Q", "нет", ctx)
        cid = conv.get_by_user(ctx[0])["id"]
        self.assertTrue(conv.set_status(cid, "closed"))
        self.assertEqual(conv.list_conversations(self.admin(), "closed")[0]["id"], cid)
        conv.add_inbound(ctx[0], "Reply", "reopen-1")
        self.assertEqual(conv.get_by_id(cid)["status"], "open")
        conv.set_status(cid, "closed")
        db.create_appeal("candidate", "Name", "123", "Appeal", ctx)
        self.assertEqual(conv.get_by_id(cid)["status"], "open")
        conv.set_status(cid, "closed")
        conv.send_outbound(API(), ctx[0], self.admin("other"), "Hello", "reopen-out")
        self.assertEqual(conv.get_by_id(cid)["status"], "open")

    def test_staff_callback_reply_cancel_expiry_and_duplicate_mid(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Q", "нет", ctx)
        admin = self.admin()
        token = conv.get_by_user(ctx[0])["reply_token"]
        click = {"update_type": "message_callback", "user": {"user_id": "hr"}, "chat_id": "chat-hr",
                 "callback": {"payload": f"cr:{token}", "callback_id": "click-1"}}
        self.process(click)
        self.assertEqual(bot.reply_states["hr"]["messenger_user_id"], ctx[0])
        self.process(update("hr", "Ответ кандидату", "hr-mid"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages WHERE direction = 'outbound'")["n"], 1)
        self.process(update("hr", "Ответ кандидату", "hr-mid"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages WHERE direction = 'outbound'")["n"], 1)
        self.process({**click, "callback": {**click["callback"], "callback_id": "click-2"}})
        self.process(update("hr", "/cancel", "cancel-mid"))
        self.assertNotIn("hr", bot.reply_states)
        self.process({**click, "callback": {**click["callback"], "callback_id": "click-3"}})
        bot.reply_states["hr"]["started_at"] = datetime.now(timezone.utc) - timedelta(minutes=11)
        self.process(update("hr", "Expired", "expired-mid"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages WHERE direction = 'outbound'")["n"], 1)
        self.process({**click, "callback": {**click["callback"], "callback_id": "click-4"}})
        db.set_admin_flags(admin["id"], 0, 1)
        self.process(update("hr", "No permission", "no-rights-mid"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages WHERE direction = 'outbound'")["n"], 1)

    def test_real_callback_actor_opens_reply_and_rejects_non_staff(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Q", "нет", ctx)
        self.admin("hr")
        self.admin("bot-user")  # Even a known sender cannot replace callback.user as actor.
        token = conv.get_by_user(ctx[0])["reply_token"]
        event = realistic_callback("hr", f"cr:{token}", "real-cr")
        self.process(event)
        self.assertEqual(bot.reply_states["hr"]["messenger_user_id"], ctx[0])
        self.assertNotIn("bot-user", bot.reply_states)
        self.assertEqual(self.api.sent[-1][1]["user_id"], "hr")
        self.assertFalse(self.api.sent[-1][1].get("chat_id"))
        self.assertIn("Введите сообщение для", self.api.sent[-1][0])
        self.assertIn("Для отмены: /cancel", self.api.sent[-1][0])
        self.assertTrue(conv.update_was_processed("callback:real-cr"))
        sent_before_replay = len(self.api.sent)
        reply_started_at = bot.reply_states["hr"]["started_at"]
        self.process(event)
        self.assertEqual(len(self.api.sent), sent_before_replay)
        self.assertEqual(bot.reply_states["hr"]["started_at"], reply_started_at)

        bot.reply_states.clear()
        sent_before = len(self.api.sent)
        self.process(realistic_callback("outsider", f"cr:{token}", "non-hr-cr"))
        self.assertEqual(bot.reply_states, {})
        self.assertEqual(len(self.api.sent), sent_before)
        conflicting = realistic_callback("outsider", f"cr:{token}", "non-hr-top-level")
        conflicting["user"] = {"user_id": "hr"}
        self.process(conflicting)
        self.assertEqual(bot.reply_states, {})
        self.assertEqual(len(self.api.sent), sent_before)
        self.process(realistic_callback("hr", "cr:invalid", "invalid-cr"))
        self.assertEqual(bot.reply_states, {})
        self.assertEqual(len(self.api.sent), sent_before)

        self.process(realistic_callback("hr", f"cr:{token}", "real-cr-id", actor_key="id"))
        self.assertEqual(bot.reply_states["hr"]["messenger_user_id"], ctx[0])

    def test_real_callback_actor_interest_history_digest_and_reply(self) -> None:
        base = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        db.set_setting("interest_notifications_activated_at", interest.stamp(base - timedelta(days=1)))
        admin = self.admin("hr")
        db.execute("UPDATE admins SET interest_mode = 'daily', interest_mode_changed_at = ? WHERE id = ?",
                   (interest.stamp(base - timedelta(days=1)), admin["id"]))
        ctx = db.touch_candidate("max", "candidate", {"display_name": "Candidate"}, interest.stamp(base))
        db.record_activity_event(ctx, "vacancies_opened", at=interest.stamp(base))
        interest.run_once(self.api, datetime(2026, 10, 2, 6, tzinfo=timezone.utc))
        digest = db.fetch_one("SELECT * FROM interest_digests WHERE admin_id = ?", (admin["id"],))
        delivery = db.fetch_one("SELECT * FROM interest_deliveries WHERE admin_id = ?", (admin["id"],))
        self.assertEqual(digest["status"], "sent")
        self.assertEqual(delivery["status"], "sent")
        for action, token, expected in (("id", digest["action_token"], "Пользователи сводки"),
                                        ("ih", delivery["action_token"], "История активности")):
            self.process(realistic_callback("hr", f"{action}:{token}", f"real-{action}"))
            self.assertIn(expected, self.api.sent[-1][0])
            self.assertEqual(self.api.sent[-1][1]["user_id"], "hr")
        self.process(realistic_callback("hr", f"ir:{delivery['action_token']}", "real-ir"))
        self.assertEqual(bot.reply_states["hr"]["messenger_user_id"], ctx[0])
        self.assertIn("Введите сообщение для", self.api.sent[-1][0])
        self.assertEqual(self.api.sent[-1][1]["user_id"], "hr")

        bot.reply_states.clear()
        sent_before = len(self.api.sent)
        self.process(realistic_callback("bot-user", f"ir:{delivery['action_token']}", "bot-ir"))
        self.assertEqual(bot.reply_states, {})
        self.assertEqual(len(self.api.sent), sent_before)

    def test_staff_selection_replaces_candidate(self) -> None:
        first, second = self.candidate("first"), self.candidate("second")
        db.create_question("first", "Q1", "нет", first)
        db.create_question("second", "Q2", "нет", second)
        self.admin()
        for index, ctx in enumerate((first, second)):
            token = conv.get_by_user(ctx[0])["reply_token"]
            self.process({"update_type": "message_callback", "user": {"user_id": "hr"}, "chat_id": "chat-hr",
                          "callback": {"payload": f"cr:{token}", "callback_id": f"choose-{index}"}})
        self.process(update("hr", "To second", "second-mid"))
        outbound = db.fetch_one("SELECT conversation_id FROM conversation_messages WHERE direction = 'outbound'")
        self.assertEqual(outbound["conversation_id"], conv.get_by_user(second[0])["id"])

    def test_interest_reply_and_pending_suppression(self) -> None:
        base = datetime.now(timezone.utc) - timedelta(days=1)
        db.set_setting("interest_notifications_activated_at", interest.stamp(base))
        admin = self.admin()
        ctx = db.touch_candidate("max", "candidate", {"display_name": "Candidate"}, interest.stamp(base + timedelta(hours=1)))
        db.record_activity_event(ctx, "vacancies_opened", at=interest.stamp(base + timedelta(hours=1)))
        interest.materialize(base + timedelta(hours=2))
        delivery = db.fetch_one("SELECT * FROM interest_deliveries WHERE admin_id = ?", (admin["id"],))
        self.assertEqual(delivery["status"], "pending")
        conv.send_outbound(API(), ctx[0], admin, "Здравствуйте", "before-due")
        self.assertEqual(db.fetch_one("SELECT status FROM interest_deliveries WHERE id = ?", (delivery["id"],))["status"], "cancelled")
        interest.materialize(base + timedelta(hours=5))
        self.assertEqual(db.fetch_one("SELECT status FROM interest_deliveries WHERE id = ?", (delivery["id"],))["status"], "cancelled")
        self.assertIsNone(interest.candidate_for_token(delivery["action_token"], admin))

    def test_failed_outbound_does_not_suppress_pending_interest(self) -> None:
        base = datetime.now(timezone.utc) - timedelta(days=1)
        db.set_setting("interest_notifications_activated_at", interest.stamp(base))
        admin = self.admin()
        ctx = db.touch_candidate("max", "candidate", {"display_name": "Candidate"},
                                 interest.stamp(base + timedelta(hours=1)))
        db.record_activity_event(ctx, "vacancies_opened", at=interest.stamp(base + timedelta(hours=1)))
        interest.materialize(base + timedelta(hours=2))
        delivery = db.fetch_one("SELECT * FROM interest_deliveries WHERE admin_id = ?", (admin["id"],))
        self.assertEqual(delivery["status"], "pending")
        pending, _ = conv.create_outbound(ctx[0], admin, "Unsent", "not-sent")
        self.assertEqual(db.fetch_one("SELECT status FROM interest_deliveries WHERE id = ?", (delivery["id"],))["status"], "pending")
        interest.materialize(base + timedelta(hours=2))
        self.assertEqual(db.fetch_one("SELECT status FROM interest_deliveries WHERE id = ?", (delivery["id"],))["status"], "pending")
        self.assertEqual(db.fetch_one("SELECT delivery_status FROM conversation_messages WHERE id = ?", (pending["id"],))["delivery_status"], "pending")
        failed = conv.send_outbound(API(http_error(400)), ctx[0], admin, "Rejected", "rejected")
        self.assertEqual(failed["delivery_status"], "failed")
        self.assertEqual(db.fetch_one("SELECT status FROM interest_deliveries WHERE id = ?", (delivery["id"],))["status"], "pending")
        interest.materialize(base + timedelta(hours=2))
        self.assertEqual(db.fetch_one("SELECT status FROM interest_deliveries WHERE id = ?", (delivery["id"],))["status"], "pending")

    def test_interest_single_reply_creates_conversation_only_on_send(self) -> None:
        base = datetime.now(timezone.utc) - timedelta(days=1)
        db.set_setting("interest_notifications_activated_at", interest.stamp(base))
        admin = self.admin()
        ctx = db.touch_candidate("max", "candidate", {"display_name": "Candidate"}, interest.stamp(base + timedelta(hours=1)))
        db.record_activity_event(ctx, "vacancies_opened", at=interest.stamp(base + timedelta(hours=1)))
        interest.run_once(self.api, base + timedelta(hours=5))
        delivery = db.fetch_one("SELECT * FROM interest_deliveries WHERE admin_id = ?", (admin["id"],))
        self.assertEqual(delivery["status"], "sent")
        self.assertIsNone(conv.get_by_user(ctx[0]))
        self.process({"update_type": "message_callback", "user": {"user_id": "hr"}, "chat_id": "chat-hr",
                      "callback": {"payload": f"ir:{delivery['action_token']}", "callback_id": "interest-click"}})
        self.assertIsNone(conv.get_by_user(ctx[0]))
        self.process(update("hr", "Hello from interest", "interest-hr-mid"))
        self.assertEqual(conv.get_by_user(ctx[0])["status"], "open")

    def test_daily_candidate_selection_reaches_reply_state(self) -> None:
        base = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        db.set_setting("interest_notifications_activated_at", interest.stamp(base - timedelta(days=1)))
        admin = self.admin()
        db.execute("UPDATE admins SET interest_mode = 'daily', interest_mode_changed_at = ? WHERE id = ?",
                   (interest.stamp(base - timedelta(days=1)), admin["id"]))
        ctx = db.touch_candidate("max", "candidate", {"display_name": "Candidate"}, interest.stamp(base))
        db.record_activity_event(ctx, "vacancies_opened", at=interest.stamp(base))
        interest.run_once(self.api, datetime(2026, 10, 2, 6, tzinfo=timezone.utc))
        digest = db.fetch_one("SELECT * FROM interest_digests WHERE admin_id = ?", (admin["id"],))
        self.assertEqual(digest["status"], "sent")
        self.process({"update_type": "message_callback", "user": {"user_id": "hr"}, "chat_id": "chat-hr",
                      "callback": {"payload": f"id:{digest['action_token']}", "callback_id": "daily-list"}})
        delivery = db.fetch_one("SELECT * FROM interest_deliveries WHERE admin_id = ?", (admin["id"],))
        self.process({"update_type": "message_callback", "user": {"user_id": "hr"}, "chat_id": "chat-hr",
                      "callback": {"payload": f"ih:{delivery['action_token']}", "callback_id": "daily-candidate"}})
        self.process({"update_type": "message_callback", "user": {"user_id": "hr"}, "chat_id": "chat-hr",
                      "callback": {"payload": f"ir:{delivery['action_token']}", "callback_id": "daily-write"}})
        self.assertEqual(bot.reply_states["hr"]["messenger_user_id"], ctx[0])

    def test_cursor_advances_only_after_complete_batch_and_replay_is_safe(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Q", "нет", ctx)
        event = update("candidate", "Free", "cursor-mid")
        data = {"updates": [event], "marker": 501}
        with patch.object(bot, "handle_message", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                bot.process_update_batch(self.api, data)
        self.assertEqual(db.get_setting("max_updates_marker", ""), "")
        self.assertEqual(bot.process_update_batch(self.api, data), "501")
        self.assertEqual(db.get_setting("max_updates_marker", ""), "501")
        bot.process_update_batch(self.api, data)
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages")["n"], 2)

    def test_web_post_redirect_get_and_read_boundary(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Q", "нет", ctx)
        cid = conv.get_by_user(ctx[0])["id"]
        hr = self.admin()
        request = Request({"type": "http", "method": "POST", "path": "/admin/conversations/1/send",
                           "headers": [], "query_string": b""})
        api = API()
        with patch.object(admin_web, "current_admin", return_value=hr), patch.object(admin_web, "max_api_client", return_value=api):
            response = admin_web.conversation_send(request, cid, "Hello", "web-idempotent")
            again = admin_web.conversation_send(request, cid, "Hello", "web-idempotent")
            self.assertEqual(response.status_code, 303)
            self.assertEqual(again.status_code, 303)
            self.assertEqual(api.attempts, 1)
            self.assertEqual(api.sent[0][0], conv.candidate_outbound_text("Hello"))
            self.assertEqual(api.sent[0][1]["keyboard"], bot.MENU_ONLY_KEYBOARD)
            self.assertEqual(db.fetch_one("SELECT text FROM conversation_messages WHERE request_key = 'web-idempotent'")["text"], "Hello")
            page = admin_web.conversation_page(request, cid)
            self.assertEqual(page.status_code, 200)
        self.assertEqual(conv.list_conversations(hr)[0]["unread"], 0)

    def test_candidate_outbound_menu_navigation_and_free_reply(self) -> None:
        ctx = self.candidate()
        admin = self.admin()
        sent = conv.send_outbound(self.api, ctx[0], admin, "24/7", "menu-ux")
        delivered, kwargs = self.api.sent[-1]
        self.assertIn("Сообщение от отдела кадров", delivered)
        self.assertIn("24/7", delivered)
        self.assertIn("Ответьте обычным сообщением.", delivered)
        self.assertEqual(kwargs["keyboard"], bot.MENU_ONLY_KEYBOARD)
        self.assertEqual(sent["text"], "24/7")
        self.process(update("candidate", "Главное меню", "menu-button"))
        self.assertEqual(self.api.sent[-1][0], bot.main_menu())
        self.process(update("candidate", "/menu", "menu-command"))
        self.assertEqual(self.api.sent[-1][0], bot.main_menu())
        self.process(update("candidate", "Вакансии", "vacancy-nav"))
        self.process(update("candidate", "Условия службы", "conditions-nav"))
        self.assertEqual(db.fetch_one("SELECT COUNT(*) n FROM conversation_messages WHERE direction = 'inbound'")["n"], 0)
        self.process(update("candidate", "Спасибо, понял", "free-reply"))
        self.assertEqual(db.fetch_one("SELECT text FROM conversation_messages ORDER BY id DESC LIMIT 1")["text"], "Спасибо, понял")

    def test_candidate_outbound_respects_max_transport_limit(self) -> None:
        ctx = self.candidate()
        admin = self.admin()
        message = "x" * conv.MAX_OUTBOUND_TEXT_LENGTH
        conv.send_outbound(self.api, ctx[0], admin, message, "max-length")
        self.assertEqual(len(self.api.sent[-1][0]), 4000)
        self.assertEqual(db.fetch_one("SELECT text FROM conversation_messages WHERE request_key = 'max-length'")["text"], message)
        with self.assertRaises(ValueError):
            conv.send_outbound(self.api, ctx[0], admin, message + "x", "too-long")

    def test_staff_send_uses_same_candidate_presentation(self) -> None:
        ctx = self.candidate()
        db.create_question("candidate", "Q", "нет", ctx)
        self.admin()
        token = conv.get_by_user(ctx[0])["reply_token"]
        self.process(realistic_callback("hr", f"cr:{token}", "staff-ux"))
        self.process(update("hr", "Ответ HR", "staff-ux-message"))
        delivered = [(text, kwargs) for text, kwargs in self.api.sent if kwargs.get("user_id") == "candidate" and text.startswith("Сообщение от отдела кадров")]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], conv.candidate_outbound_text("Ответ HR"))
        self.assertEqual(delivered[0][1]["keyboard"], bot.MENU_ONLY_KEYBOARD)
        self.assertEqual(db.fetch_one("SELECT text FROM conversation_messages WHERE direction = 'outbound'")["text"], "Ответ HR")

    def test_candidate_mentions_and_structured_notifications(self) -> None:
        ctx = db.touch_candidate("max", "123", {"first_name": "Валерий", "last_name": "Васкул"})
        self.admin()
        db.create_question("123", "Первый вопрос", "нет", ctx)
        self.process(update("123", "Спасибо [подробнее](max://user/999)", "free-mention"))
        text, kwargs = self.api.sent[-1]
        self.assertIn("[Валерий Васкул](max://user/123)", text)
        self.assertIn(r"\[подробнее\]", text)
        self.assertEqual(kwargs["format"], "markdown")
        self.assertEqual(kwargs["keyboard"]["payload"]["buttons"][0][0]["type"], "callback")
        for scenario, state, answer in (
            ("question", {"scenario": "question", "step": "contact", "question": "Q [unsafe](url)"}, "нет"),
            ("appeal", {"scenario": "appeal", "step": "text", "full_name": "A *bad*", "phone": "123"}, "Hello"),
            ("application", {"scenario": "application", "step": "confirm", "data": {"max_user_id": "123", "vacancy_title": "Role", "full_name": "Name"}}, "Да"),
        ):
            with self.subTest(scenario=scenario):
                bot.user_states["chat-123"] = state
                before = len(self.api.sent)
                self.process(update("123", answer, f"structured-{scenario}"))
                notifications = [(text, kwargs) for text, kwargs in self.api.sent[before:] if kwargs.get("format") == "markdown"]
                self.assertEqual(len(notifications), 1)
                self.assertIn("[Валерий Васкул](max://user/123)", notifications[0][0])
                self.assertTrue(notifications[0][1]["keyboard"])

    def test_web_chat_human_dates_bubbles_and_escaped_content(self) -> None:
        at = "2026-09-24T10:58:11.119Z"
        ctx = db.touch_candidate("max", "123", {"display_name": "Валерий Васкул", "username": "valery"}, at)
        self.admin()
        db.create_question("123", "<script>question</script>", "нет", ctx)
        db.record_activity_event(ctx, "vacancies_opened", at=at)
        conv.add_inbound(ctx[0], "<script>alert(1)</script>\nВторая строка", "web-xss")
        conv.send_outbound(self.api, ctx[0], self.admin("other"), "Ответ HR", "web-view")
        cid = conv.get_by_user(ctx[0])["id"]
        db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (at, cid))
        db.execute("UPDATE conversation_messages SET created_at = ? WHERE conversation_id = ?", (at, cid))
        db.execute("UPDATE questions SET created_at = ? WHERE messenger_user_id = ?", (at, ctx[0]))
        request = Request({"type": "http", "method": "GET", "path": "/admin/conversations", "headers": [], "query_string": b""})
        hr = db.get_admin_by_user_id("hr")
        with patch.object(admin_web, "current_admin", return_value=hr):
            listing = admin_web.conversations_page(request).body.decode()
            detail = admin_web.conversation_page(request, cid).body.decode()
        self.assertIn("24.09.2026 13:58", listing)
        self.assertNotIn(at, listing)
        self.assertIn('class="badge unread-badge"', listing)
        self.assertIn(f'href="/admin/conversations/{cid}"', listing)
        self.assertIn('class="chat-row inbound"', detail)
        self.assertIn('class="chat-row outbound"', detail)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", detail)
        self.assertNotIn("<script>alert(1)</script>", detail)
        self.assertIn("24.09.2026 13:58", detail)
        self.assertNotIn(at, detail)
        self.assertIn("Связанные записи", detail)
        self.assertIn("Последняя активность", detail)
        self.assertIn("Ответ HR", detail)
        self.assertNotIn("Сообщение от отдела кадров", detail)
        self.assertNotIn("Открыть профиль MAX", detail)
        self.assertIn("Отправлено", detail)
        self.assertIn(f'maxlength="{conv.MAX_OUTBOUND_TEXT_LENGTH}"', detail)

    def test_web_delivery_labels_and_retry_only_for_failed_messages(self) -> None:
        ctx = self.candidate()
        hr = self.admin()
        first = conv.send_outbound(API(http_error(400)), ctx[0], hr, "Первый", "failed-one")
        second = conv.send_outbound(API(http_error(400)), ctx[0], hr, "Второй", "failed-two")
        uncertain = conv.send_outbound(API(requests.exceptions.Timeout()), ctx[0], hr, "Третий", "uncertain-one")
        cid = conv.get_by_user(ctx[0])["id"]
        request = Request({"type": "http", "method": "GET", "path": f"/admin/conversations/{cid}", "headers": [], "query_string": b""})
        with patch.object(admin_web, "current_admin", return_value=hr):
            detail = admin_web.conversation_page(request, cid).body.decode()
        self.assertIn("Ошибка отправки", detail)
        self.assertIn("Результат не подтверждён", detail)
        self.assertIn(f'/retry/{first["id"]}', detail)
        self.assertIn(f'/retry/{second["id"]}', detail)
        self.assertNotIn(f'/retry/{uncertain["id"]}', detail)
        retry_keys = re.findall(r'<form method="post" action="[^"]+/retry/\d+" class="chat-retry">\s*<input type="hidden" name="request_key" value="([^"]+)"', detail)
        self.assertEqual(len(retry_keys), 2)
        self.assertEqual(len(set(retry_keys)), 2)


class ConversationUXFormatterTests(unittest.TestCase):
    def test_max_mention_uses_profile_name_and_validated_id(self) -> None:
        self.assertEqual(candidate_mention({"first_name": "Валерий", "last_name": "Васкул", "external_user_id": "123"}),
                         "[Валерий Васкул](max://user/123)")
        self.assertEqual(candidate_mention({"first_name": "Валерий", "external_user_id": "123"}),
                         "[Валерий](max://user/123)")
        self.assertEqual(candidate_mention({"first_name": "Va]lery", "last_name": "A*B", "external_user_id": "123"}),
                         r"[Va\]lery A\*B](max://user/123)")
        self.assertEqual(candidate_mention({"first_name": "Name", "display_name": "Plain", "external_user_id": "123)"}), "Plain")
        self.assertEqual(candidate_mention({"first_name": "Name", "external_user_id": "invalid"}), "Name")
        self.assertEqual(candidate_mention({"first_name": "Name", "display_name": "Plain", "external_user_id": "0"}), "Plain")
        self.assertEqual(candidate_mention({"first_name": "Name", "display_name": "Plain", "external_user_id": "9223372036854775808"}), "Plain")
        self.assertEqual(candidate_mention({"first_name": "Name", "display_name": "Plain", "external_user_id": "9" * 5000}), "Plain")
        self.assertEqual(candidate_mention({"display_name": "[Plain](evil)", "external_user_id": "123"}),
                         r"\[Plain\]\(evil\)")
        self.assertEqual(escape_markdown("[x](max://user/999)"), r"\[x\]\(max://user/999\)")

    def test_moscow_datetime_fallbacks(self) -> None:
        for value in ("2026-09-24T10:58:11.119Z", "2026-09-24T10:58:11Z",
                      "2026-09-24T10:58:11+00:00", "2026-09-24T13:58:11+03:00",
                      "2026-09-24T10:58:11"):
            with self.subTest(value=value):
                self.assertEqual(admin_web.format_msk_datetime(value), "24.09.2026 13:58")
        for value in (None, "", "legacy garbage"):
            self.assertEqual(admin_web.format_msk_datetime(value), "—")

    def test_max_api_sends_markdown_format_with_callback_keyboard(self) -> None:
        api = MaxAPI("unused")
        keyboard = bot.MENU_ONLY_KEYBOARD
        with patch.object(api, "_request", return_value={}) as request:
            api.send_message_once("[Name](max://user/123)", user_id="123", keyboard=keyboard, format="markdown")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["format"], "markdown")
        self.assertEqual(payload["attachments"], [keyboard])


if __name__ == "__main__":
    unittest.main()
