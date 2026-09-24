from __future__ import annotations

import sqlite3
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
        for key, failure, expected in (("bad", http_error(400), "failed"),
                                       ("limit", http_error(429), "failed"),
                                       ("server", http_error(503), "uncertain"),
                                       ("timeout", requests.exceptions.Timeout(), "uncertain")):
            api = API(failure)
            result = conv.send_outbound(api, ctx[0], admin, key, key)
            self.assertEqual(result["delivery_status"], expected)
            self.assertEqual(api.attempts, 1)
        failed = db.fetch_one("SELECT id FROM conversation_messages WHERE request_key = 'bad'")
        retried = conv.retry_failed(API(), failed["id"], admin, "retry-key")
        self.assertEqual(retried["delivery_status"], "sent")
        self.assertEqual(db.fetch_one("SELECT delivery_status FROM conversation_messages WHERE id = ?", (failed["id"],))["delivery_status"], "failed")
        uncertain = db.fetch_one("SELECT id FROM conversation_messages WHERE request_key = 'timeout'")
        with self.assertRaises(ValueError):
            conv.retry_failed(API(), uncertain["id"], admin, "unsafe")

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
            page = admin_web.conversation_page(request, cid)
            self.assertEqual(page.status_code, 200)
        self.assertEqual(conv.list_conversations(hr)[0]["unread"], 0)


if __name__ == "__main__":
    unittest.main()
