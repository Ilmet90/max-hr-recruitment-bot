from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from app import admin_web, analytics, db


NOW = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


class AnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DATABASE_PATH
        db.DATABASE_PATH = Path(self.temp.name) / "analytics.sqlite3"
        db.init_db()

    def tearDown(self) -> None:
        db.DATABASE_PATH = self.old_path
        self.temp.cleanup()

    def candidate(self, external: str) -> db.ActivityContext:
        return db.touch_candidate("max", external, at="2026-09-01T00:00:00.000Z")

    def event(self, ctx: db.ActivityContext, kind: str, vacancy: int | None, at: str,
              title: str | None = None) -> None:
        db.record_activity_event(ctx, kind, vacancy, {"vacancy_title": title} if title else None, at)

    def page(self, period: str = "all", role: str = "hr_staff", *, active: bool = True) -> str:
        if role == "superadmin":
            session = {"authenticated": True, "admin_role": "superadmin"}
        else:
            admin_id = db.execute("""INSERT INTO admins
                (max_user_id, role, approved, is_active) VALUES (?, ?, 1, ?)""",
                (f"{role}-{active}", role, int(active)))
            session = {"authenticated": True, "admin_id": admin_id, "admin_role": role}
        cookie = admin_web.make_session(session)
        request = Request({"type": "http", "method": "GET", "path": "/admin/analytics",
                           "headers": [(b"cookie", f"{admin_web.SESSION_COOKIE}={cookie}".encode())],
                           "query_string": f"period={period}".encode()})
        return admin_web.analytics_page(request).body.decode()

    def test_period_bounds_moscow_midnight_and_exclusive_end(self) -> None:
        self.assertEqual(analytics.period_bounds("today", NOW),
                         ("2026-09-23T21:00:00.000Z", "2026-09-24T21:00:00.000Z"))
        self.assertEqual(analytics.period_bounds("7d", NOW),
                         ("2026-09-17T21:00:00.000Z", "2026-09-24T21:00:00.000Z"))
        self.assertEqual(analytics.period_bounds("30d", NOW),
                         ("2026-08-25T21:00:00.000Z", "2026-09-24T21:00:00.000Z"))
        self.assertEqual(analytics.period_bounds("all", NOW), (None, None))
        with self.assertRaises(ValueError):
            analytics.period_bounds("bad", NOW)
        ctx = self.candidate("boundary")
        for at in ("2026-09-23T20:59:59.999Z", "2026-09-23T21:00:00.000Z",
                   "2026-09-24T20:59:59.999Z", "2026-09-24T21:00:00.000Z"):
            self.event(ctx, "vacancy_viewed", 10, at)
        self.assertEqual(analytics.report("today", NOW)["summary"]["vacancy_views_raw"], 2)
        self.event(ctx, "vacancy_viewed", 10, "2026-09-17T20:59:59.999Z")
        self.event(ctx, "vacancy_viewed", 10, "2026-09-17T21:00:00.000Z")
        self.assertEqual(analytics.report("7d", NOW)["summary"]["vacancy_views_raw"], 4)

    def test_raw_unique_metrics_and_full_funnel(self) -> None:
        a, b, c = (self.candidate(name) for name in ("a", "b", "c"))
        base = "2026-09-24T10:00:00."
        for ms in ("000", "001", "002"):
            self.event(a, "vacancy_viewed", 10, base + ms + "Z", "Инженер")
        self.event(b, "vacancy_viewed", 10, base + "003Z")
        self.event(a, "vacancy_apply_started", 10, base + "004Z")
        self.event(a, "vacancy_apply_started", 10, base + "005Z")
        self.event(a, "vacancy_application_submitted", 10, base + "006Z")
        self.event(c, "vacancy_apply_started", 10, base + "007Z")
        self.event(c, "vacancy_application_submitted", 10, base + "008Z")
        self.event(a, "question_sent", None, base + "009Z")
        self.event(b, "appeal_sent", None, base + "010Z")
        self.event(a, "vacancy_application_submitted", 10, base + "011Z")
        result = analytics.report("all")
        s, f, row = result["summary"], result["funnel"], result["vacancies"][0]
        self.assertEqual((s["active_users"], s["vacancy_views_raw"], s["vacancy_viewers_unique"]), (3, 4, 2))
        self.assertEqual((s["apply_started_raw"], s["apply_started_unique_pairs"]), (3, 2))
        self.assertEqual((s["applications_submitted_raw"], s["applications_submitted_unique_pairs"]), (3, 2))
        self.assertEqual((s["questions_sent"], s["appeals_sent"], s["conversion_rate"]), (1, 1, "100.0%"))
        self.assertEqual((f["funnel_viewed"], f["funnel_started"], f["funnel_submitted"]), (2, 1, 1))
        self.assertEqual((row["vacancy_views_raw"], row["vacancy_viewers_unique"], row["conversion_rate"]),
                         (4, 2, "100.0%"))

    def test_sequence_requires_view_then_start_then_submit(self) -> None:
        orderings = {
            "full": ("vacancy_viewed", "vacancy_apply_started", "vacancy_application_submitted"),
            "late-view": ("vacancy_apply_started", "vacancy_viewed", "vacancy_application_submitted"),
            "no-start": ("vacancy_viewed", "vacancy_application_submitted"),
            "no-view": ("vacancy_apply_started", "vacancy_application_submitted"),
            "late-start": ("vacancy_application_submitted", "vacancy_apply_started"),
        }
        for user, steps in orderings.items():
            ctx = self.candidate(user)
            for index, kind in enumerate(steps):
                self.event(ctx, kind, 20, f"2026-09-24T10:00:00.{index:03d}Z")
        result = analytics.report("all")
        s, f = result["summary"], result["funnel"]
        self.assertEqual((s["conversion_started"], s["conversion_submitted"]), (4, 3))
        self.assertEqual(s["conversion_rate"], "75.0%")
        self.assertEqual((f["funnel_viewed"], f["funnel_started"], f["funnel_submitted"]), (3, 1, 1))

    def test_sequence_uses_event_id_when_timestamps_match(self) -> None:
        valid, invalid = self.candidate("valid"), self.candidate("invalid")
        at = "2026-09-24T10:00:00.000Z"
        for kind in ("vacancy_viewed", "vacancy_apply_started", "vacancy_application_submitted"):
            self.event(valid, kind, 21, at)
        for kind in ("vacancy_application_submitted", "vacancy_apply_started"):
            self.event(invalid, kind, 21, at)
        result = analytics.report("all")
        self.assertEqual((result["summary"]["conversion_started"], result["summary"]["conversion_submitted"]), (2, 1))
        self.assertEqual((result["funnel"]["funnel_viewed"], result["funnel"]["funnel_started"],
                          result["funnel"]["funnel_submitted"]), (1, 1, 1))

    def test_cross_period_start_and_submission_are_separate(self) -> None:
        before, after = self.candidate("before"), self.candidate("after")
        self.event(before, "vacancy_apply_started", 30, "2026-09-23T20:59:59.999Z")
        self.event(before, "vacancy_application_submitted", 30, "2026-09-23T21:00:00.000Z")
        self.event(after, "vacancy_apply_started", 30, "2026-09-24T20:59:59.999Z")
        self.event(after, "vacancy_application_submitted", 30, "2026-09-24T21:00:00.000Z")
        result = analytics.report("today", NOW)
        self.assertEqual(result["summary"]["applications_submitted_raw"], 1)
        self.assertEqual((result["summary"]["conversion_started"], result["summary"]["conversion_submitted"]), (1, 0))
        self.assertEqual(result["summary"]["conversion_rate"], "0.0%")

    def test_deleted_vacancy_and_unlinked_application(self) -> None:
        ctx = self.candidate("candidate")
        db.execute("INSERT INTO vacancies (id, title) VALUES (40, 'Текущая')")
        self.event(ctx, "vacancy_viewed", 40, "2026-09-24T10:00:00.000Z", "Старое название")
        self.event(ctx, "vacancy_apply_started", 40, "2026-09-24T10:00:00.001Z")
        self.event(ctx, "vacancy_application_submitted", 40, "2026-09-24T10:00:00.002Z")
        db.execute("INSERT INTO applications (vacancy_id, vacancy_title, max_user_id) VALUES (40, 'Историческая', 'candidate')")
        self.assertEqual(analytics.report("all")["vacancies"][0]["label"], "Текущая")
        db.delete_vacancy(40)
        db.execute("INSERT INTO applications (vacancy_id, vacancy_title, max_user_id) VALUES (41, 'Без события', 'legacy')")
        self.event(ctx, "vacancy_viewed", 42, "2026-09-24T10:00:00.003Z", "Название из события")
        self.event(ctx, "vacancy_viewed", 43, "2026-09-24T10:00:00.004Z")
        self.event(ctx, "vacancy_viewed", None, "2026-09-24T10:00:00.005Z")
        rows = {row["vacancy_id"]: row for row in analytics.report("all")["vacancies"]}
        self.assertEqual(rows[40]["label"], "Историческая")
        self.assertEqual(rows[42]["label"], "Название из события")
        self.assertEqual(rows[43]["label"], "Удалённая вакансия #43")
        self.assertEqual(rows[None]["label"], "Без привязки к вакансии")
        self.assertNotIn(41, rows)

    def test_empty_render_auth_privacy_and_escaped_title(self) -> None:
        for role in ("hr_staff", "hr_head", "superadmin"):
            with self.subTest(role=role):
                html = self.page(role=role)
                self.assertIn("За выбранный период данных пока нет.", html)
                self.assertIn("Воронка откликов", html)
                self.assertIn("Статистика по вакансиям", html)
                self.assertIn('href="/admin/analytics?period=today"', html)
                self.assertIn("Активные кандидаты", html)
        with self.assertRaises(HTTPException) as denied:
            self.page(active=False)
        self.assertEqual(denied.exception.status_code, 303)
        request = Request({"type": "http", "method": "GET", "path": "/admin/analytics",
                           "headers": [], "query_string": b""})
        with self.assertRaises(HTTPException) as anonymous:
            admin_web.analytics_page(request)
        self.assertEqual(anonymous.exception.status_code, 303)
        ctx = self.candidate("secret-external-id")
        db.update_messenger_user_chat_id("max", "secret-external-id", 24053553)
        db.create_question("secret-external-id", "PRIVATE QUESTION", "private@example.com", ctx)
        self.event(ctx, "vacancy_viewed", 50, "2026-09-24T10:00:00.000Z", "<script>alert(1)</script>")
        db.execute("INSERT INTO applications (vacancy_id, vacancy_title, phone) VALUES (50, ?, ?)",
                   ("<img src=x onerror=alert(1)>", "+79991234567"))
        html = self.page(role="superadmin")
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        for secret in ("<img src=x", "secret-external-id", "24053553", "+79991234567",
                       "PRIVATE QUESTION", "private@example.com", "https://web.max.ru/"):
            self.assertNotIn(secret, html)

    def test_existing_activity_index_is_used_for_bounded_query(self) -> None:
        ctx = self.candidate("load")
        with db.get_connection() as conn:
            conn.executemany("""INSERT INTO user_activity_events
                (messenger_user_id, session_id, event_type, vacancy_id, created_at)
                VALUES (?, ?, 'vacancy_viewed', 60, ?)""",
                ((ctx[0], ctx[1], f"2026-09-24T10:00:{index % 60:02d}.{index // 60:03d}Z")
                 for index in range(3000)))
            plan = [row["detail"] for row in conn.execute("""EXPLAIN QUERY PLAN SELECT id FROM user_activity_events
                WHERE event_type = 'vacancy_viewed' AND created_at >= ? AND created_at < ?""",
                analytics.period_bounds("today", NOW))]
        self.assertTrue(any("idx_activity_type_time" in detail for detail in plan), plan)
        with patch.object(db, "fetch_all", wraps=db.fetch_all) as fetch:
            self.assertEqual(analytics.report("today", NOW)["summary"]["vacancy_views_raw"], 3000)
        sequence_sql, sequence_params = next(call.args for call in fetch.call_args_list
                                             if "WITH events AS MATERIALIZED" in call.args[0])
        with db.get_connection() as conn:
            full_plan = [row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + sequence_sql,
                                                                  sequence_params)]
        self.assertTrue(any("idx_activity_type_time" in detail for detail in full_plan), full_plan)


if __name__ == "__main__":
    unittest.main()
