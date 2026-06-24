from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import max_api2_certs


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class MaxApi2CertificateTests(unittest.TestCase):
    def test_expected_archive_names(self) -> None:
        max_api2_certs.validate_archive_names(
            max_api2_certs.ROOT_ARCHIVE_NAME,
            max_api2_certs.SUB_ARCHIVE_NAME,
        )
        with self.assertRaises(ValueError):
            max_api2_certs.validate_archive_names("root.zip", "sub.zip")

    def test_upload_directory_rejects_unsafe_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            safe = max_api2_certs.safe_upload_dir("a" * 32, root)
            self.assertEqual(safe.parent, root.resolve())
            for unsafe in ("../outside", "not-an-id", "a" * 31, "/tmp/outside"):
                with self.assertRaises(ValueError):
                    max_api2_certs.safe_upload_dir(unsafe, root)

    def test_preview_rejects_zip_path_traversal(self) -> None:
        def archive_bytes(filename: str) -> bytes:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr(filename, "not-a-certificate")
            return output.getvalue()

        with tempfile.TemporaryDirectory() as temp_dir:
            upload_root = Path(temp_dir) / "private"
            upload_id = max_api2_certs.create_upload(
                max_api2_certs.ROOT_ARCHIVE_NAME,
                archive_bytes("../outside.pem"),
                max_api2_certs.SUB_ARCHIVE_NAME,
                archive_bytes("inside.pem"),
                upload_root=upload_root,
            )
            preview = max_api2_certs.inspect_upload(upload_id, upload_root=upload_root)
            self.assertFalse(preview["ok"])
            self.assertTrue(any("небезопасный путь" in error for error in preview["errors"]))
            self.assertFalse((Path(temp_dir) / "outside.pem").exists())

    def test_privileged_helper_rejects_path_outside_upload_root(self) -> None:
        helper = PROJECT_ROOT / "scripts" / "admin_install_mincifra_certs.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [str(helper), temp_dir],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 3)
        self.assertIn("вне разрешённой области", result.stdout)

    def test_configure_script_updates_only_required_settings(self) -> None:
        script = PROJECT_ROOT / "scripts" / "configure_max_api2.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            secret_value = "unit-test-secret-value"
            env_file.write_text(
                "MAX_BOT_TOKEN=" + secret_value + "\n"
                "MAX_API_BASE_URL=https://old.invalid\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["ENV_FILE"] = str(env_file)
            result = subprocess.run(
                [str(script)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            content = env_file.read_text(encoding="utf-8")
            backups = list(Path(temp_dir).glob(".env.backup_*"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MAX_API_BASE_URL=https://platform-api2.max.ru", content)
        self.assertIn("REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt", content)
        self.assertIn(secret_value, content)
        self.assertNotIn(secret_value, result.stdout + result.stderr)
        self.assertEqual(len(backups), 1)

    def test_web_routes_are_registered(self) -> None:
        from app.admin_web import app

        routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
        self.assertIn(("/admin/about/max-api2/upload", "POST"), routes)
        self.assertIn(("/admin/about/max-api2/install", "POST"), routes)
        self.assertIn(("/admin/about/max-api2/cleanup", "POST"), routes)


if __name__ == "__main__":
    unittest.main()
