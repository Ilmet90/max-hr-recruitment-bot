from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests

from app import db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = PROJECT_ROOT / "VERSION"
UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "update_from_github.sh"
MAX_OUTPUT_CHARS = 4000
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
GITHUB_API_TIMEOUT = 20


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


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = RELEASE_TAG_RE.fullmatch(f"v{value.lstrip('v')}")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def get_current_version() -> str:
    return get_app_version().lstrip("v")


def _github_repo_slug(repo_url: str) -> str:
    value = (repo_url or "").strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+)$", value)
    if not match:
        raise ValueError("Некорректный URL GitHub-репозитория.")
    owner, repo = match.groups()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Некорректный URL GitHub-репозитория.")
    return f"{owner}/{repo}"


def list_github_releases(repo_url: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    repo_url = repo_url or db.get_github_repo_url()
    try:
        slug = _github_repo_slug(repo_url)
        response = requests.get(
            f"https://api.github.com/repos/{slug}/releases",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "MAX-HR-Recruitment-Bot"},
            params={"per_page": 100},
            timeout=GITHUB_API_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("GitHub API вернул неожиданный формат.")
    except Exception as exc:
        print(f"GitHub Releases: не удалось получить список релизов: {exc}")
        return [], "Не удалось получить список релизов GitHub."

    releases: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict) or raw.get("draft") or raw.get("prerelease"):
            continue
        tag_name = str(raw.get("tag_name") or "").strip()
        version_tuple = _version_tuple(tag_name)
        if not version_tuple:
            continue
        releases.append(
            {
                "tag_name": tag_name,
                "version": tag_name.lstrip("v"),
                "version_tuple": version_tuple,
                "name": str(raw.get("name") or tag_name),
                "published_at": str(raw.get("published_at") or ""),
                "html_url": str(raw.get("html_url") or ""),
            }
        )
    releases.sort(key=lambda item: item["version_tuple"], reverse=True)
    return releases, None


def get_latest_release(repo_url: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    releases, error = list_github_releases(repo_url)
    if error:
        return None, error
    if not releases:
        return None, "Опубликованные релизы GitHub не найдены."
    return releases[0], None


def get_published_release(tag_name: str | None = None, repo_url: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    requested = (tag_name or "").strip()
    if requested and not RELEASE_TAG_RE.fullmatch(requested):
        return None, "Недопустимая версия. Разрешены только опубликованные релизы вида vX.Y.Z."
    releases, error = list_github_releases(repo_url)
    if error:
        return None, error
    if not releases:
        return None, "Опубликованные релизы GitHub не найдены."
    if not requested:
        return releases[0], None
    for release in releases:
        if release["tag_name"] == requested:
            return release, None
    return None, "Указанная версия не является опубликованным GitHub Release."


def _release_views(releases: list[dict[str, Any]], current_version: str) -> list[dict[str, Any]]:
    current_tuple = _version_tuple(current_version)
    result: list[dict[str, Any]] = []
    for release in releases:
        item = release.copy()
        item["is_current"] = release["version"] == current_version
        item["is_older"] = bool(current_tuple and release["version_tuple"] < current_tuple)
        item["is_newer"] = bool(current_tuple and release["version_tuple"] > current_tuple)
        result.append(item)
    return result


def get_local_info() -> dict[str, Any]:
    settings = db.get_update_settings()
    return {
        "version": get_current_version(),
        "installed_commit": settings.get("installed_commit") or "local",
        "latest_commit": "",
        "latest_release": None,
        "latest_release_tag": "",
        "releases": [],
        "repo_url": settings["github_repo_url"],
        "branch": "не используется; только GitHub Releases",
        "update_last_at": settings.get("update_last_at") or "",
        "status": "local_info",
        "message": "Показана локальная информация о версии.",
        "update_available": False,
        "error": "",
    }


def check_updates() -> dict[str, Any]:
    settings = db.get_update_settings()
    current_version = get_current_version()
    current_tuple = _version_tuple(current_version)
    releases, error = list_github_releases(settings["github_repo_url"])
    release_views = _release_views(releases, current_version)
    latest = release_views[0] if release_views else None
    update_available = False

    if error:
        status = "error"
        message = "Не удалось получить список релизов GitHub."
    elif not latest:
        status = "no_releases"
        message = "Опубликованные релизы GitHub не найдены."
    elif current_tuple and latest["version_tuple"] > current_tuple:
        status = "update_available"
        update_available = True
        message = f"Доступно обновление до опубликованного релиза {latest['tag_name']}."
    elif latest["version"] == current_version:
        status = "up_to_date"
        message = "Установлена последняя опубликованная релизная версия."
    else:
        status = "ahead"
        message = "Текущая версия новее последнего опубликованного релиза. Обновление не предлагается."

    return {
        "version": current_version,
        "installed_commit": settings.get("installed_commit") or "local",
        "latest_commit": latest["tag_name"] if latest else "",
        "latest_release": latest,
        "latest_release_tag": latest["tag_name"] if latest else "",
        "releases": release_views,
        "repo_url": settings["github_repo_url"],
        "branch": "не используется; только GitHub Releases",
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


def run_update_script(target_version: str | None = None) -> tuple[bool, str]:
    if not UPDATE_SCRIPT.exists():
        return False, "Скрипт обновления не найден."
    release, error = get_published_release(target_version)
    if error or not release:
        return False, error or "Опубликованный релиз не найден."
    sudoers = check_maintenance_sudoers()
    if not sudoers["ok"]:
        return False, sudoers["message"]
    ok, output = run_command(["bash", str(UPDATE_SCRIPT), release["tag_name"]], timeout=600)
    if not ok:
        return False, f"Установка опубликованного релиза завершилась ошибкой. {sudoers_hint()}\n{output}"
    return True, output or f"Опубликованный релиз {release['tag_name']} установлен. Службы перезапущены."
