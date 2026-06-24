from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from app import maintenance
from app.max_api import DEFAULT_BASE_URL


PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_ROOT = PROJECT_ROOT / "data" / "runtime" / "cert_uploads"
INSTALL_HELPER = PROJECT_ROOT / "scripts" / "admin_install_mincifra_certs.sh"
OFFICIAL_CERT_URL = "https://www.gosuslugi.ru/crt"
ROOT_ARCHIVE_NAME = "linux_russian_trusted_root_ca_pem.zip"
SUB_ARCHIVE_NAME = "russian_trusted_sub_ca_pem.zip"
EXPECTED_ARCHIVE_NAMES = (ROOT_ARCHIVE_NAME, SUB_ARCHIVE_NAME)
UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{32}$")
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 500
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024


def get_max_api2_status() -> dict[str, str]:
    return {
        "base_url": (os.getenv("MAX_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        "requests_ca_bundle": os.getenv("REQUESTS_CA_BUNDLE") or "не задан",
        "official_cert_url": OFFICIAL_CERT_URL,
    }


def validate_archive_names(root_name: str | None, sub_name: str | None) -> None:
    names = tuple(PurePosixPath((value or "").replace("\\", "/")).name for value in (root_name, sub_name))
    if names != EXPECTED_ARCHIVE_NAMES:
        raise ValueError(
            "Выберите два официальных архива с точными именами: "
            f"{ROOT_ARCHIVE_NAME} и {SUB_ARCHIVE_NAME}."
        )


def safe_upload_dir(upload_id: str, upload_root: Path | None = None) -> Path:
    if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
        raise ValueError("Некорректный идентификатор загрузки.")
    root = (upload_root or UPLOAD_ROOT).resolve()
    candidate = (root / upload_id).resolve()
    if candidate.parent != root:
        raise ValueError("Каталог загрузки находится вне разрешённой области.")
    return candidate


def _write_private_file(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def create_upload(
    root_name: str | None,
    root_content: bytes,
    sub_name: str | None,
    sub_content: bytes,
    upload_root: Path | None = None,
) -> str:
    validate_archive_names(root_name, sub_name)
    for content in (root_content, sub_content):
        if not content:
            raise ValueError("Один из архивов пуст.")
        if len(content) > MAX_ARCHIVE_BYTES:
            raise ValueError("Размер каждого архива не должен превышать 25 МБ.")

    upload_id = secrets.token_hex(16)
    root = upload_root or UPLOAD_ROOT
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    upload_dir = safe_upload_dir(upload_id, root)
    upload_dir.mkdir(mode=0o700)
    _write_private_file(upload_dir / ROOT_ARCHIVE_NAME, root_content)
    _write_private_file(upload_dir / SUB_ARCHIVE_NAME, sub_content)
    return upload_id


def _validate_zip_member(member: zipfile.ZipInfo) -> None:
    raw_name = member.filename.replace("\\", "/")
    path = PurePosixPath(raw_name)
    if not raw_name or raw_name.startswith("/") or ".." in path.parts or re.match(r"^[A-Za-z]:", raw_name):
        raise ValueError(f"Архив содержит небезопасный путь: {member.filename}")
    file_type = (member.external_attr >> 16) & 0o170000
    if file_type == stat.S_IFLNK:
        raise ValueError(f"Архив содержит символическую ссылку: {member.filename}")


def _certificate_details(path: Path) -> dict[str, str] | None:
    for cert_format, args in (
        ("PEM", ["openssl", "x509", "-in", str(path), "-noout", "-subject", "-issuer", "-nameopt", "utf8,oneline"]),
        ("DER", ["openssl", "x509", "-inform", "DER", "-in", str(path), "-noout", "-subject", "-issuer", "-nameopt", "utf8,oneline"]),
    ):
        result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            continue
        details = {"format": cert_format, "subject": "не указан", "issuer": "не указан"}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            normalized = key.strip().lower()
            if separator and normalized in {"subject", "issuer"}:
                details[normalized] = value.strip()
        return details
    return None


def _inspect_archive(archive_path: Path, extract_root: Path) -> tuple[list[dict[str, str]], list[str]]:
    certificates: list[dict[str, str]] = []
    junk_files: list[str] = []
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Файл {archive_path.name} не является корректным ZIP-архивом: {exc}") from exc

    with archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"Архив {archive_path.name} содержит слишком много файлов.")
        extracted_bytes = sum(member.file_size for member in members)
        if extracted_bytes > MAX_EXTRACTED_BYTES:
            raise ValueError(f"Распакованный размер {archive_path.name} превышает 100 МБ.")

        archive_root = extract_root / archive_path.stem
        archive_root.mkdir()
        for member in members:
            _validate_zip_member(member)
            if member.is_dir():
                continue
            relative = PurePosixPath(member.filename.replace("\\", "/"))
            target = archive_root.joinpath(*relative.parts).resolve()
            if archive_root.resolve() not in target.parents:
                raise ValueError(f"Архив содержит небезопасный путь: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)

            details = _certificate_details(target)
            if details:
                certificates.append(
                    {
                        "archive": archive_path.name,
                        "filename": member.filename,
                        **details,
                    }
                )
            else:
                junk_files.append(f"{archive_path.name}: {member.filename}")

    if not certificates:
        raise ValueError(f"В архиве {archive_path.name} не найдено корректных сертификатов X.509.")
    return certificates, junk_files


def inspect_upload(upload_id: str, upload_root: Path | None = None) -> dict[str, Any]:
    upload_dir = safe_upload_dir(upload_id, upload_root)
    errors: list[str] = []
    certificates: list[dict[str, str]] = []
    junk_files: list[str] = []
    files_received = [name for name in EXPECTED_ARCHIVE_NAMES if (upload_dir / name).is_file()]

    if files_received != list(EXPECTED_ARCHIVE_NAMES):
        errors.append("В приватном каталоге загрузки отсутствует один из обязательных архивов.")
    elif not shutil.which("openssl"):
        errors.append("Команда openssl не найдена. Установите пакет openssl.")
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="max-api2-cert-preview-") as temp_dir:
                extract_root = Path(temp_dir)
                for name in EXPECTED_ARCHIVE_NAMES:
                    found, junk = _inspect_archive(upload_dir / name, extract_root)
                    certificates.extend(found)
                    junk_files.extend(junk)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))

    return {
        "ok": not errors,
        "upload_id": upload_id,
        "files_received": files_received,
        "certificates": certificates,
        "junk_files": junk_files,
        "errors": errors,
    }


def cleanup_upload(upload_id: str, upload_root: Path | None = None) -> None:
    upload_dir = safe_upload_dir(upload_id, upload_root)
    if upload_dir.exists():
        shutil.rmtree(upload_dir)


def manual_install_command(upload_id: str) -> str:
    upload_dir = safe_upload_dir(upload_id)
    return f"sudo {shlex.quote(str(INSTALL_HELPER))} {shlex.quote(str(upload_dir))}"


def install_upload(upload_id: str) -> tuple[bool, str, str]:
    preview = inspect_upload(upload_id)
    if not preview["ok"]:
        return False, "Сначала устраните ошибки, обнаруженные при проверке архивов.", ""
    if not INSTALL_HELPER.is_file():
        return False, "Скрипт установки сертификатов не найден.", ""

    upload_dir = safe_upload_dir(upload_id)
    manual_command = manual_install_command(upload_id)
    ok, output = maintenance.run_command(
        ["sudo", "-n", str(INSTALL_HELPER), str(upload_dir)],
        timeout=300,
    )
    if not ok:
        return (
            False,
            "Автоматическая установка недоступна. Настройте ограниченный sudoers либо выполните указанную команду вручную.",
            manual_command,
        )

    cleanup_upload(upload_id)
    message = "Сертификаты установлены, настройки MAX API2 обновлены, служба MAX-бота перезапущена."
    if output:
        message = f"{message}\n{output[-1500:]}"
    return True, message, ""
