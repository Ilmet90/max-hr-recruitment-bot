from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from app import admin_web


class UpdateRestartPageTests(unittest.TestCase):
    def test_success_returns_standalone_page_without_rendering_templates(self) -> None:
        with (
            patch.object(admin_web, "require_admin"),
            patch.object(admin_web, "has_head_rights", return_value=True),
            patch.object(admin_web.maintenance, "run_update_script", return_value=(True, "done")) as update,
            patch.object(admin_web, "render", side_effect=AssertionError("template rendered")),
            patch.object(admin_web.maintenance, "check_updates", side_effect=AssertionError("new code loaded")),
        ):
            response = admin_web.about_update(SimpleNamespace(), "v0.2.9")

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response, HTMLResponse)
        html = response.body.decode("utf-8")
        self.assertIn("Обновление установлено", html)
        self.assertIn("перезапускается", html)
        self.assertIn('content="12;url=/admin/about"', html)
        self.assertIn('<a href="/admin/about">Обновить страницу</a>', html)
        update.assert_called_once_with("v0.2.9")

    def test_failure_keeps_existing_error_page(self) -> None:
        expected = HTMLResponse("failed")
        with (
            patch.object(admin_web, "require_admin"),
            patch.object(admin_web, "has_head_rights", return_value=True),
            patch.object(admin_web.maintenance, "run_update_script", return_value=(False, "update error")),
            patch.object(admin_web.maintenance, "check_updates", return_value={}),
            patch.object(admin_web, "render", return_value=expected) as render,
        ):
            response = admin_web.about_update(SimpleNamespace(), "v0.2.9")

        self.assertIs(response, expected)
        self.assertEqual(render.call_args.args[1], "about.html")
        context = render.call_args.args[2]
        self.assertEqual(context["errors"], ["Не удалось установить опубликованный релиз."])
        self.assertEqual(context["command_output"], "update error")

    def test_about_template_renders_without_max_api2_status(self) -> None:
        env = Environment(loader=FileSystemLoader(admin_web.TEMPLATE_DIR))
        html = env.get_template("about.html").render(
            org_settings={"organization_full_name": "Организация"},
            info={"version": "0.2.8", "installed_commit": "local", "releases": []},
            can_maintain=True,
            maintenance_sudoers_command="sudo bash scripts/setup_maintenance_sudoers.sh",
        )

        self.assertIn("О программе", html)
        self.assertNotIn("MAX API2 и сертификаты", html)


if __name__ == "__main__":
    unittest.main()
