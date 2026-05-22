from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app import db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = PROJECT_ROOT / "VERSION"
UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "update_from_github.sh"
MAX_OUTPUT_CHARS = 4000
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")


def get_app_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.1.0"
    except FileNotFoundError:
        return "0.1.0"


def get_installed_commit() -> str:
    return db.get_installed_commit()


def _safe_output(value: str) -> str:
    blocked = ("TOKEN", "PASSWORD", "SECRET", "HASH", ".env")
    lines = []
    for line in (value or "").splitlines():
        if any(marker in line.upper() for marker in blocked):
            lines.append("[строка скрыта]")
        else:
            lines.append(line)
    return "\n".join(lines)[-MAX_OUTPUT_CHARS:]


def _systemctl_path() -> str:
    return shutil.which("systemctl") or "/bin/systemctl"


def _safe_service_name(service: str) -> str:
    service = (service or "").strip()
    if not SERVICE_NAME_RE.fullmatch(service):
        raise ValueError("Некорректное имя systemd-службы.")
    return service


def run_command(args: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return False, f"Команда не найдена: {exc.filename}"
    except subprocess.TimeoutExpired:
        return False, "Команда не завершилась за отведённое время."
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return result.returncode == 0, _safe_output(output)


def get_latest_remote_commit(repo_url: str | None = None, branch: str | None = None) -> tuple[str | None, str | None]:
    repo_url = repo_url or db.get_github_repo_url()
    branch = branch or db.get_github_branch()
    ok, output = run_command(["git", "ls-remote", repo_url, branch], timeout=30)
    if not ok:
        return None, output or "Не удалось получить данные GitHub."
    first_line = output.splitlines()[0] if output.splitlines() else ""
    commit = first_line.split()[0] if first_line else ""
    if not commit:
        return None, "GitHub не вернул commit для указанной ветки."
    return commit, None


def get_local_info() -> dict[str, Any]:
    settings = db.get_update_settings()
    return {
        "version": get_app_version(),
        "installed_commit": settings.get("installed_commit") or "local",
        "latest_commit": "",
        "repo_url": settings["github_repo_url"],
        "branch": settings["github_branch"],
        "update_last_at": settings.get("update_last_at") or "",
        "status": "local_info",
        "message": "Показана локальная информация о версии.",
        "update_available": False,
        "error": "",
    }


def check_updates() -> dict[str, Any]:
    settings = db.get_update_settings()
    installed = settings.get("installed_commit") or "local"
    latest, error = get_latest_remote_commit(settings["github_repo_url"], settings["github_branch"])
    status = "error" if error else "up_to_date"
    update_available = False
    if error:
        message = f"Не удалось проверить обновления: {error}"
    elif installed == "local":
        status = "local"
        update_available = True
        message = "Текущая версия не синхронизирована с GitHub. Можно выполнить обновление из репозитория."
    elif latest and latest != installed:
        status = "update_available"
        update_available = True
        message = "Доступно обновление."
    else:
        message = "Обновлений нет."
    return {
        "version": get_app_version(),
        "installed_commit": installed,
        "latest_commit": latest or "",
        "repo_url": settings["github_repo_url"],
        "branch": settings["github_branch"],
        "update_last_at": settings.get("update_last_at") or "",
        "status": status,
        "message": message,
        "update_available": update_available,
        "error": error or "",
    }


def sudoers_setup_command() -> str:
    try:
        install_path = db.get_install_path()
    except Exception:
        install_path = "/opt/max-hr-recruitment-bot"
    return f"sudo bash {install_path.rstrip('/')}/scripts/setup_maintenance_sudoers.sh"


def sudoers_hint() -> str:
    return (
        "Для обновления и перезапуска служб из web-панели управления выполните на сервере один раз:\n\n"
        f"{sudoers_setup_command()}\n\n"
        "После этого вернитесь в раздел «О программе» и повторите действие."
    )


def _check_sudo_systemctl_status(service: str) -> tuple[bool, str]:
    try:
        service = _safe_service_name(service)
    except ValueError as exc:
        return False, str(exc)
    systemctl = _systemctl_path()
    try:
        result = subprocess.run(
            ["sudo", "-n", systemctl, "status", service],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        return False, f"Команда не найдена: {exc.filename}. {sudoers_hint()}"
    except subprocess.TimeoutExpired:
        return False, f"Проверка sudoers не завершилась за отведённое время. {sudoers_hint()}"
    if result.returncode in {0, 3, 4}:
        return True, ""
    output = _safe_output("\n".join(part for part in (result.stdout, result.stderr) if part))
    return False, f"{sudoers_hint()}\n\n{output}".strip()


def check_maintenance_sudoers() -> dict[str, Any]:
    admin_service = db.get_admin_service_name()
    bot_service = db.get_bot_service_name()
    for service in (admin_service, bot_service):
        ok, message = _check_sudo_systemctl_status(service)
        if not ok:
            return {
                "ok": False,
                "message": message,
                "systemctl": _systemctl_path(),
                "admin_service": admin_service,
                "bot_service": bot_service,
            }
    return {
        "ok": True,
        "message": "Права sudoers для обслуживания настроены.",
        "systemctl": _systemctl_path(),
        "admin_service": admin_service,
        "bot_service": bot_service,
    }


def _can_use_systemctl(service: str) -> tuple[bool, str]:
    return _check_sudo_systemctl_status(service)


def restart_admin_service() -> tuple[bool, str]:
    service = db.get_admin_service_name()
    can_restart, error = _can_use_systemctl(service)
    if not can_restart:
        return False, error
    command = f"sleep 1; sudo -n {shlex.quote(_systemctl_path())} restart {shlex.quote(_safe_service_name(service))}"
    try:
        subprocess.Popen(["nohup", "bash", "-c", command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return False, f"Не удалось запланировать перезапуск web-панели управления: {exc}. {sudoers_hint()}"
    return True, "Web-панель управления будет перезапущена. Обновите страницу через несколько секунд."


def restart_bot_service(deferred: bool = False) -> tuple[bool, str]:
    service = db.get_bot_service_name()
    can_restart, error = _can_use_systemctl(service)
    if not can_restart:
        return False, error
    if deferred:
        command = f"sleep 1; sudo -n {shlex.quote(_systemctl_path())} restart {shlex.quote(_safe_service_name(service))}"
        try:
            subprocess.Popen(["nohup", "bash", "-c", command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            return False, f"Не удалось запланировать перезапуск MAX-бота: {exc}. {sudoers_hint()}"
        return True, "MAX-бот будет перезапущен."
    ok, output = run_command(["sudo", "-n", _systemctl_path(), "restart", _safe_service_name(service)], timeout=20)
    if not ok:
        return False, f"Не удалось перезапустить MAX-бота. {sudoers_hint()}\n{output}"
    return True, "MAX-бот перезапущен."


def run_update_script() -> tuple[bool, str]:
    if not UPDATE_SCRIPT.exists():
        return False, "Скрипт обновления не найден."
    sudoers = check_maintenance_sudoers()
    if not sudoers["ok"]:
        return False, sudoers["message"]
    ok, output = run_command(["bash", str(UPDATE_SCRIPT)], timeout=600)
    if not ok:
        return False, f"Обновление завершилось ошибкой. {sudoers_hint()}\n{output}"
    return True, output or "Обновление выполнено. Службы перезапущены."
