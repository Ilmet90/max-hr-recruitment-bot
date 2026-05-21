from __future__ import annotations

import hashlib
import hmac
import base64
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.max_api import MaxAPI


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

APP_DIR = PROJECT_ROOT / "app"
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
UPLOAD_DIR = STATIC_DIR / "uploads" / "service"

ALLOWED_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SESSION_COOKIE = "max_ik2_admin"
BOOT_SESSION_ID = secrets.token_urlsafe(16)
SESSION_TTL_SECONDS = 8 * 60 * 60

app = FastAPI(title=db.DEFAULT_ORG_SETTINGS["web_admin_title"])
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


def settings() -> dict[str, str]:
    return {
        "login": os.getenv("WEB_ADMIN_LOGIN", "admin"),
        "password": os.getenv("WEB_ADMIN_PASSWORD", "admin"),
        "secret": os.getenv("ADMIN_SECRET", "change-me"),
    }


def sign_value(value: str) -> str:
    secret = settings()["secret"].encode()
    return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()


def make_session(session: dict) -> str:
    session["boot_id"] = BOOT_SESSION_ID
    session["login_at"] = int(time.time())
    payload = base64.urlsafe_b64encode(json.dumps(session, ensure_ascii=False).encode()).decode()
    return f"{payload}:{sign_value(payload)}"


def parse_session(cookie: str | None) -> dict | None:
    if not cookie:
        return None
    try:
        payload, signature = cookie.rsplit(":", 1)
        if not hmac.compare_digest(signature, sign_value(payload)):
            return None
        session = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return None
    if session.get("boot_id") != BOOT_SESSION_ID:
        return None
    if time.time() - float(session.get("login_at", 0)) > SESSION_TTL_SECONDS:
        return None
    if session.get("authenticated") is not True:
        return None
    return session


def is_valid_session(cookie: str | None) -> bool:
    return parse_session(cookie) is not None


def require_admin(request: Request) -> None:
    if not current_admin(request):
        raise HTTPException(
            status_code=303,
            headers={
                "Location": "/admin/login",
                "Set-Cookie": expired_session_cookie(),
            },
        )


def expired_session_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=lax"


def render(request: Request, template: str, context: dict | None = None) -> HTMLResponse:
    current = current_admin(request)
    org_settings = db.get_org_settings()
    data = {
        "request": request,
        "current_admin": current,
        "has_head_rights": has_head_rights(request) if current else False,
        "is_superadmin": is_superadmin(request) if current else False,
        "org_settings": org_settings,
        "title": org_settings["web_admin_title"],
        "role_label": db.role_label,
        "application_status_label": db.application_status_label,
        "service_info_label": db.service_info_label,
        "service_info_help": db.service_info_help,
        "access_label": db.access_label,
        "bool_label": db.bool_label,
    }
    if context:
        data.update(context)
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=data,
    )


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def redirect_with_notice(path: str, message: str = "", error: str = "") -> RedirectResponse:
    params = {key: value for key, value in {"message": message, "error": error}.items() if value}
    suffix = f"?{urlencode(params)}" if params else ""
    return redirect(f"{path}{suffix}")


def form_checkbox(value: str | None) -> int:
    return 1 if value else 0


def current_admin(request: Request) -> dict | None:
    session = parse_session(request.cookies.get(SESSION_COOKIE))
    if not session:
        return None
    if session.get("admin_role") == "superadmin":
        return {
            "id": None,
            "role": "superadmin",
            "name": "Главная учётная запись",
            "web_login": session.get("web_login"),
            "is_superadmin": True,
        }
    admin_id = session.get("admin_id")
    admin = db.get_admin(int(admin_id)) if admin_id else None
    if not admin or admin.get("approved") != 1 or admin.get("is_active") != 1 or admin.get("role") not in {"hr_staff", "hr_head"}:
        return None
    return {
        "id": admin["id"],
        "role": admin["role"],
        "name": db.admin_display_name(admin),
        "web_login": admin.get("web_login"),
        "must_change_password": admin.get("must_change_password"),
        "delegated_until": admin.get("delegated_until"),
        "is_superadmin": False,
        "raw": admin,
    }


def is_superadmin(request: Request) -> bool:
    admin = current_admin(request)
    return bool(admin and admin["role"] == "superadmin")


def is_hr_head(request: Request) -> bool:
    admin = current_admin(request)
    return bool(admin and admin["role"] == "hr_head")


def is_delegated_head(request: Request) -> bool:
    admin = current_admin(request)
    raw = admin.get("raw") if admin else None
    return bool(raw and raw.get("delegated_until") and str(raw["delegated_until"]) > db.now_iso())


def has_head_rights(request: Request) -> bool:
    return is_superadmin(request) or is_hr_head(request) or is_delegated_head(request)


def is_hr_staff(request: Request) -> bool:
    admin = current_admin(request)
    return bool(admin and admin["role"] == "hr_staff")


def can_manage_content(request: Request) -> bool:
    return current_admin(request) is not None


def can_manage_users(request: Request) -> bool:
    return has_head_rights(request)


def can_assign_applications(request: Request) -> bool:
    return has_head_rights(request)


def can_reset_admin_password(request: Request, target: dict | None) -> bool:
    if not target or not can_manage_users(request):
        return False
    if is_superadmin(request):
        return True
    if has_head_rights(request):
        return target.get("role") != "hr_head"
    return False


def require_content(request: Request) -> None:
    require_admin(request)
    if not can_manage_content(request):
        raise HTTPException(status_code=403)


def require_user_manager(request: Request) -> None:
    require_admin(request)
    if not can_manage_users(request):
        raise HTTPException(status_code=403)


def require_superadmin(request: Request) -> None:
    require_admin(request)
    if not is_superadmin(request):
        raise HTTPException(status_code=403)


def actor_for_audit(request: Request) -> tuple[int | None, str]:
    admin = current_admin(request) or {}
    return admin.get("id"), admin.get("name", "Система")


def max_api_client() -> MaxAPI | None:
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if not token or token in {"change-me", "your-token", "MAX_BOT_TOKEN", "put-token-here"}:
        return None
    return MaxAPI(token)


def send_max_to_admin(admin: dict, text: str) -> bool:
    api = max_api_client()
    if not api:
        return False
    try:
        if admin.get("chat_id"):
            api.send_message(text, chat_id=str(admin["chat_id"]))
        elif admin.get("max_user_id"):
            api.send_message(text, user_id=str(admin["max_user_id"]))
        else:
            return False
        return True
    except Exception as exc:
        print(f"Не удалось отправить MAX-сообщение пользователю {admin.get('max_user_id')}: {exc}")
        return False


def validate_secret_format(secret: str) -> bool:
    return bool(re.fullmatch(r"[\wА-Яа-яЁё-]{4,32}", secret or "", flags=re.UNICODE))


def validate_hex_color(color: str) -> bool:
    return bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", color or ""))


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/")
def root(request: Request) -> RedirectResponse:
    if is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return redirect("/admin")
    result = redirect("/admin/login")
    result.delete_cookie(SESSION_COOKIE)
    return result


@app.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return render(request, "login.html")


@app.post("/admin/login")
def login(response: Response, username: str = Form(""), password: str = Form("")) -> RedirectResponse:
    cfg = settings()
    if username == cfg["login"] and db.verify_superadmin_password(password, cfg["password"]):
        result = redirect("/admin")
        result.set_cookie(
            SESSION_COOKIE,
            make_session(
                {
                    "authenticated": True,
                    "admin_id": None,
                    "admin_role": "superadmin",
                    "admin_name": "Главная учётная запись",
                    "web_login": username,
                }
            ),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return result
    admin = db.get_admin_by_web_login(username)
    if (
        admin
        and admin.get("approved") == 1
        and admin.get("is_active") == 1
        and admin.get("role") in {"hr_staff", "hr_head"}
        and db.verify_password(password, admin.get("password_hash"))
    ):
        db.execute("UPDATE admins SET last_login_at = ?, updated_at = ? WHERE id = ?", (db.now_iso(), db.now_iso(), admin["id"]))
        result = redirect("/admin")
        result.set_cookie(
            SESSION_COOKIE,
            make_session(
                {
                    "authenticated": True,
                    "admin_id": admin["id"],
                    "admin_role": admin["role"],
                    "admin_name": db.admin_display_name(admin),
                    "web_login": username,
                }
            ),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return result
    result = redirect("/admin/login?error=1")
    return result


@app.get("/admin/logout")
def logout() -> RedirectResponse:
    result = redirect("/admin/login")
    result.delete_cookie(SESSION_COOKIE)
    return result


@app.get("/admin/profile", response_class=HTMLResponse)
def profile_page(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(request, "profile.html")


@app.post("/admin/profile")
def profile_update(
    request: Request,
    web_login: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
) -> HTMLResponse:
    require_admin(request)
    admin = current_admin(request)
    messages: list[str] = []
    errors: list[str] = []
    if not admin:
        return render(request, "profile.html", {"messages": messages, "errors": errors})
    if admin["role"] == "superadmin":
        if current_password or new_password or confirm_password:
            ok, message = db.change_superadmin_password(
                current_password,
                new_password,
                confirm_password,
                settings()["password"],
                None,
                "Главная учётная запись",
            )
            (messages if ok else errors).append(message)
        return render(request, "profile.html", {"messages": messages, "errors": errors})
    admin_id = int(admin["id"])
    if web_login.strip() and web_login.strip() != admin.get("web_login"):
        ok, message = db.update_admin_login(admin_id, web_login.strip(), admin_id, admin["name"])
        (messages if ok else errors).append(message)
    if current_password or new_password or confirm_password:
        ok, message = db.change_admin_password(admin_id, current_password, new_password, confirm_password)
        (messages if ok else errors).append(message)
    return render(request, "profile.html", {"messages": messages, "errors": errors})


@app.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    require_admin(request)
    if not has_head_rights(request):
        raise HTTPException(status_code=403)
    has_db_secret = bool(db.get_setting("admin_secret", "").strip())
    return render(request, "settings.html", {"has_db_secret": has_db_secret})


@app.post("/admin/settings/admin-secret")
def settings_admin_secret(request: Request, new_secret: str = Form(""), repeat_secret: str = Form("")) -> HTMLResponse:
    require_admin(request)
    if not has_head_rights(request):
        raise HTTPException(status_code=403)
    new_secret = new_secret.strip()
    repeat_secret = repeat_secret.strip()
    messages: list[str] = []
    errors: list[str] = []
    if new_secret != repeat_secret:
        errors.append("Новый код и повтор не совпадают.")
    elif not validate_secret_format(new_secret):
        errors.append("Код должен быть от 4 до 32 символов: буквы, цифры, дефис или подчёркивание.")
    else:
        actor_id, actor_name = actor_for_audit(request)
        db.set_admin_secret(new_secret, actor_id, actor_name)
        messages.append("Служебный код обновлён.")
    return render(request, "settings.html", {"has_db_secret": bool(db.get_setting("admin_secret", "").strip()), "messages": messages, "errors": errors})


@app.get("/admin/settings/organization", response_class=HTMLResponse)
def organization_settings_page(request: Request) -> HTMLResponse:
    require_superadmin(request)
    return render(
        request,
        "organization_settings.html",
        {
            "settings": db.get_org_settings(),
            "groups": db.ORG_SETTING_GROUPS,
        },
    )


@app.post("/admin/settings/organization", response_class=HTMLResponse)
async def organization_settings_update(request: Request) -> HTMLResponse:
    require_superadmin(request)
    form = await request.form()
    values = {key: str(form.get(key, "")).strip() for key in db.DEFAULT_ORG_SETTINGS}
    messages: list[str] = []
    errors: list[str] = []
    for key in db.ORG_SETTING_GROUPS["theme"]:
        if not validate_hex_color(values.get(key, "")):
            errors.append("Цвета нужно указать в формате HEX, например #071a2f.")
            break
    if errors:
        return render(
            request,
            "organization_settings.html",
            {"settings": values, "groups": db.ORG_SETTING_GROUPS, "messages": messages, "errors": errors},
        )
    actor_id, actor_name = actor_for_audit(request)
    db.update_org_settings(values, actor_id, actor_name)
    messages.append("Настройки организации сохранены.")
    return render(
        request,
        "organization_settings.html",
        {"settings": db.get_org_settings(), "groups": db.ORG_SETTING_GROUPS, "messages": messages, "errors": errors},
    )


@app.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(
        request,
        "dashboard.html",
        {
            "vacancies": len(db.list_vacancies()),
            "applications": len(db.list_applications()),
            "questions": len(db.list_questions()),
            "appeals": len(db.list_appeals()),
            "admins": len(db.list_admins(active_only=True)),
        },
    )


@app.get("/admin/vacancies", response_class=HTMLResponse)
def vacancies(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(request, "vacancies.html", {"items": db.list_vacancies()})


@app.get("/admin/vacancies/new", response_class=HTMLResponse)
def vacancy_new(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(request, "vacancy_form.html", {"item": {}, "action": "/admin/vacancies/new"})


@app.post("/admin/vacancies/new")
def vacancy_create(
    request: Request,
    title: str = Form(...),
    salary: str = Form(""),
    duties: str = Form(""),
    requirements: str = Form(""),
    conditions: str = Form(""),
    note: str = Form(""),
    sort_order: int = Form(100),
    is_active: str | None = Form(None),
) -> RedirectResponse:
    require_content(request)
    data = locals()
    data["is_active"] = form_checkbox(is_active)
    db.save_vacancy(data)
    return redirect("/admin/vacancies")


@app.get("/admin/vacancies/{vacancy_id}/edit", response_class=HTMLResponse)
def vacancy_edit_page(request: Request, vacancy_id: int) -> HTMLResponse:
    require_admin(request)
    item = db.get_vacancy(vacancy_id)
    if not item:
        raise HTTPException(status_code=404)
    return render(request, "vacancy_form.html", {"item": item, "action": f"/admin/vacancies/{vacancy_id}/edit"})


@app.post("/admin/vacancies/{vacancy_id}/edit")
def vacancy_update(
    request: Request,
    vacancy_id: int,
    title: str = Form(...),
    salary: str = Form(""),
    duties: str = Form(""),
    requirements: str = Form(""),
    conditions: str = Form(""),
    note: str = Form(""),
    sort_order: int = Form(100),
    is_active: str | None = Form(None),
) -> RedirectResponse:
    require_content(request)
    data = locals()
    data["is_active"] = form_checkbox(is_active)
    db.save_vacancy(data, vacancy_id)
    return redirect("/admin/vacancies")


@app.post("/admin/vacancies/{vacancy_id}/toggle")
def vacancy_toggle(request: Request, vacancy_id: int) -> RedirectResponse:
    require_content(request)
    db.toggle_vacancy(vacancy_id)
    return redirect("/admin/vacancies")


@app.post("/admin/vacancies/{vacancy_id}/delete")
def vacancy_delete(request: Request, vacancy_id: int) -> RedirectResponse:
    require_content(request)
    db.delete_vacancy(vacancy_id)
    return redirect("/admin/vacancies")


@app.get("/admin/service", response_class=HTMLResponse)
def service_page(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(request, "service.html", {"items": db.list_service_info()})


@app.post("/admin/service")
async def service_update(request: Request) -> RedirectResponse:
    require_content(request)
    form = await request.form()
    actor_id, actor_name = actor_for_audit(request)
    for item in db.list_service_info():
        key = item["key"]
        db.update_service_info(key, str(form.get(f"title_{key}", "")), str(form.get(f"text_{key}", "")))
        db.audit_log(actor_id, actor_name, "service_info_changed", "service_info", item["id"], key)
    return redirect("/admin/service")


def safe_photo_name(original: str) -> str:
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_PHOTO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Допустимы только JPG, PNG или WebP")
    digest = hashlib.sha256(f"{original}:{time.time()}".encode()).hexdigest()[:16]
    return f"{digest}{suffix}"


@app.get("/admin/photos", response_class=HTMLResponse)
def photos_page(request: Request) -> HTMLResponse:
    require_admin(request)
    if not db.list_photo_albums():
        db.ensure_default_photo_album()
    return render(
        request,
        "photos.html",
        {
            "items": db.list_photos(),
            "albums": db.list_photo_albums(with_counts=True),
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/admin/photo-albums/new")
def photo_album_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    sort_order: int = Form(100),
    is_active: str | None = Form(None),
) -> RedirectResponse:
    require_content(request)
    actor_id, actor_name = actor_for_audit(request)
    try:
        album_id = db.save_photo_album(
            {
                "title": title,
                "description": description,
                "sort_order": sort_order,
                "is_active": form_checkbox(is_active),
            }
        )
    except ValueError:
        return redirect_with_notice("/admin/photos", error="Укажите название альбома")
    db.audit_log(actor_id, actor_name, "photo_album_created", "service_photo_album", album_id)
    return redirect_with_notice("/admin/photos", message="Альбом создан")


@app.post("/admin/photo-albums/{album_id}/edit")
def photo_album_update(
    request: Request,
    album_id: int,
    title: str = Form(...),
    description: str = Form(""),
    sort_order: int = Form(100),
    is_active: str | None = Form(None),
) -> RedirectResponse:
    require_content(request)
    actor_id, actor_name = actor_for_audit(request)
    try:
        db.save_photo_album(
            {
                "title": title,
                "description": description,
                "sort_order": sort_order,
                "is_active": form_checkbox(is_active),
            },
            album_id,
        )
    except ValueError:
        return redirect_with_notice("/admin/photos", error="Укажите название альбома")
    db.audit_log(actor_id, actor_name, "photo_album_changed", "service_photo_album", album_id)
    return redirect_with_notice("/admin/photos", message="Альбом обновлён")


@app.post("/admin/photo-albums/{album_id}/toggle")
def photo_album_toggle(request: Request, album_id: int) -> RedirectResponse:
    require_content(request)
    db.toggle_photo_album(album_id)
    actor_id, actor_name = actor_for_audit(request)
    db.audit_log(actor_id, actor_name, "photo_album_toggled", "service_photo_album", album_id)
    return redirect_with_notice("/admin/photos", message="Статус альбома изменён")


@app.post("/admin/photo-albums/{album_id}/delete")
def photo_album_delete(request: Request, album_id: int) -> RedirectResponse:
    require_content(request)
    if not db.delete_photo_album(album_id):
        return redirect_with_notice("/admin/photos", error="Нельзя удалить альбом, пока в нём есть фотографии. Сначала перенесите или удалите фотографии.")
    actor_id, actor_name = actor_for_audit(request)
    db.audit_log(actor_id, actor_name, "photo_album_deleted", "service_photo_album", album_id)
    return redirect_with_notice("/admin/photos", message="Альбом удалён")


@app.post("/admin/photos")
async def photo_upload(
    request: Request,
    photo: UploadFile = File(...),
    album_id: int = Form(0),
    caption: str = Form(""),
    sort_order: int = Form(100),
    is_active: str | None = Form(None),
) -> RedirectResponse:
    require_content(request)
    album_id = album_id or db.ensure_default_photo_album()
    if not db.get_photo_album(album_id):
        return redirect_with_notice("/admin/photos", error="Выберите существующий альбом")
    filename = safe_photo_name(photo.filename or "photo.jpg")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    content = await photo.read()
    if not content:
        raise HTTPException(status_code=400, detail="Файл пустой")
    target = UPLOAD_DIR / filename
    try:
        target.write_bytes(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Не удалось сохранить файл") from exc
    photo_id = db.add_photo(filename, photo.filename or filename, caption, sort_order, form_checkbox(is_active), album_id)
    actor_id, actor_name = actor_for_audit(request)
    db.audit_log(actor_id, actor_name, "photo_uploaded", "service_photo", photo_id, f"album_id={album_id}; filename={filename}")
    return redirect_with_notice("/admin/photos", message="Фотография загружена")


@app.post("/admin/photos/{photo_id}/edit")
def photo_update(
    request: Request,
    photo_id: int,
    album_id: int = Form(0),
    caption: str = Form(""),
    sort_order: int = Form(100),
    is_active: str | None = Form(None),
) -> RedirectResponse:
    require_content(request)
    old = db.get_photo(photo_id)
    if not old:
        raise HTTPException(status_code=404)
    album_id = album_id or db.ensure_default_photo_album()
    if not db.get_photo_album(album_id):
        return redirect_with_notice("/admin/photos", error="Выберите существующий альбом")
    db.update_photo(photo_id, caption, sort_order, form_checkbox(is_active), album_id)
    actor_id, actor_name = actor_for_audit(request)
    action = "photo_moved" if int(old.get("album_id") or 0) != int(album_id) else "photo_changed"
    db.audit_log(actor_id, actor_name, action, "service_photo", photo_id, f"album_id={album_id}")
    return redirect_with_notice("/admin/photos", message="Фотография обновлена")


@app.post("/admin/photos/{photo_id}/toggle")
def photo_toggle(request: Request, photo_id: int) -> RedirectResponse:
    require_content(request)
    db.toggle_photo(photo_id)
    actor_id, actor_name = actor_for_audit(request)
    db.audit_log(actor_id, actor_name, "photo_toggled", "service_photo", photo_id)
    return redirect_with_notice("/admin/photos", message="Статус фотографии изменён")


@app.post("/admin/photos/{photo_id}/delete")
def photo_delete(request: Request, photo_id: int) -> RedirectResponse:
    require_content(request)
    item = db.get_photo(photo_id)
    db.delete_photo(photo_id)
    if item:
        try:
            (UPLOAD_DIR / item["filename"]).unlink(missing_ok=True)
        except OSError:
            pass
    actor_id, actor_name = actor_for_audit(request)
    db.audit_log(actor_id, actor_name, "photo_deleted", "service_photo", photo_id)
    return redirect_with_notice("/admin/photos", message="Фотография удалена")


@app.get("/admin/contacts", response_class=HTMLResponse)
def contacts(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(request, "contacts.html", {"items": db.list_contacts()})


@app.get("/admin/contacts/new", response_class=HTMLResponse)
def contact_new(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(request, "contact_form.html", {"item": {}, "action": "/admin/contacts/new"})


@app.post("/admin/contacts/new")
def contact_create(
    request: Request,
    title: str = Form(...),
    value: str = Form(...),
    note: str = Form(""),
    sort_order: int = Form(100),
    is_active: str | None = Form(None),
) -> RedirectResponse:
    require_content(request)
    data = locals()
    data["is_active"] = form_checkbox(is_active)
    db.save_contact(data)
    return redirect("/admin/contacts")


@app.get("/admin/contacts/{contact_id}/edit", response_class=HTMLResponse)
def contact_edit_page(request: Request, contact_id: int) -> HTMLResponse:
    require_admin(request)
    item = db.get_contact(contact_id)
    if not item:
        raise HTTPException(status_code=404)
    return render(request, "contact_form.html", {"item": item, "action": f"/admin/contacts/{contact_id}/edit"})


@app.post("/admin/contacts/{contact_id}/edit")
def contact_update(
    request: Request,
    contact_id: int,
    title: str = Form(...),
    value: str = Form(...),
    note: str = Form(""),
    sort_order: int = Form(100),
    is_active: str | None = Form(None),
) -> RedirectResponse:
    require_content(request)
    data = locals()
    data["is_active"] = form_checkbox(is_active)
    db.save_contact(data, contact_id)
    return redirect("/admin/contacts")


@app.post("/admin/contacts/{contact_id}/toggle")
def contact_toggle(request: Request, contact_id: int) -> RedirectResponse:
    require_content(request)
    db.toggle_contact(contact_id)
    return redirect("/admin/contacts")


@app.post("/admin/contacts/{contact_id}/delete")
def contact_delete(request: Request, contact_id: int) -> RedirectResponse:
    require_content(request)
    db.delete_contact(contact_id)
    return redirect("/admin/contacts")


@app.get("/admin/applications", response_class=HTMLResponse)
def applications(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(
        request,
        "applications.html",
        {
            "items": db.list_applications(),
            "table": "applications",
            "staff": db.active_staff(),
            "can_assign": can_assign_applications(request),
        },
    )


@app.get("/admin/questions", response_class=HTMLResponse)
def questions(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(request, "questions.html", {"items": db.list_questions(), "table": "questions"})


@app.get("/admin/appeals", response_class=HTMLResponse)
def appeals(request: Request) -> HTMLResponse:
    require_admin(request)
    return render(request, "appeals.html", {"items": db.list_appeals(), "table": "appeals"})


@app.post("/admin/{table}/{item_id}/status")
def status_update(request: Request, table: str, item_id: int, status: str = Form(...)) -> RedirectResponse:
    require_admin(request)
    if table == "applications":
        app_item = db.get_application(item_id)
        admin = current_admin(request)
        if not app_item:
            raise HTTPException(status_code=404)
        if not can_assign_applications(request) and app_item.get("assigned_to_admin_id") != (admin or {}).get("id"):
            raise HTTPException(status_code=403)
        db.update_application_status(item_id, status, (admin or {}).get("raw"))
    else:
        db.update_status(table, item_id, status)
    return redirect(f"/admin/{table}")


@app.post("/admin/applications/{application_id}/take")
def application_take(request: Request, application_id: int) -> RedirectResponse:
    require_admin(request)
    admin = current_admin(request)
    if not admin or admin["role"] == "superadmin":
        raise HTTPException(status_code=403)
    db.take_application(application_id, admin["raw"])
    return redirect("/admin/applications")


@app.post("/admin/applications/{application_id}/assign")
def application_assign(request: Request, application_id: int, assignee_id: int = Form(...)) -> RedirectResponse:
    require_admin(request)
    if not can_assign_applications(request):
        raise HTTPException(status_code=403)
    actor = current_admin(request) or {}
    db.assign_application(application_id, assignee_id, actor.get("raw"), actor.get("name", "superadmin"))
    return redirect("/admin/applications")


@app.post("/admin/applications/{application_id}/release")
def application_release(request: Request, application_id: int) -> RedirectResponse:
    require_admin(request)
    if not can_assign_applications(request):
        raise HTTPException(status_code=403)
    actor = current_admin(request) or {}
    db.release_application(application_id, actor.get("raw"), actor.get("name", "superadmin"))
    return redirect("/admin/applications")


@app.get("/admin/admins", response_class=HTMLResponse)
def admins(request: Request) -> HTMLResponse:
    require_admin(request)
    items = db.list_admins()
    resettable_ids = {int(item["id"]) for item in items if can_reset_admin_password(request, item)}
    return render(request, "admins.html", {"items": items, "can_manage": can_manage_users(request), "resettable_ids": resettable_ids})


@app.post("/admin/admins/{admin_id}/approve")
def admin_approve(request: Request, admin_id: int, role: str = Form("hr_staff")) -> RedirectResponse:
    require_user_manager(request)
    actor_id, actor_name = actor_for_audit(request)
    admin, password = db.approve_admin(admin_id, actor_id, actor_name, role=role)
    if admin:
        send_max_to_admin(
            admin,
            f"Ваш доступ подтверждён.\n\nРоль: {db.role_label(admin.get('role'))}\nЛогин для web-админки: {admin['web_login']}\nВременный пароль: {password}\n\nПосле входа рекомендуется сменить пароль.",
        )
    return redirect("/admin/admins")


@app.post("/admin/admins/{admin_id}/reject")
def admin_reject(request: Request, admin_id: int) -> RedirectResponse:
    require_user_manager(request)
    actor_id, actor_name = actor_for_audit(request)
    admin = db.reject_admin(admin_id, actor_id, actor_name)
    if admin:
        send_max_to_admin(admin, "Ваша заявка на доступ отклонена.")
    return redirect("/admin/admins")


@app.post("/admin/admins/{admin_id}/role")
def admin_role(request: Request, admin_id: int, role: str = Form(...)) -> RedirectResponse:
    require_user_manager(request)
    actor_id, actor_name = actor_for_audit(request)
    db.set_admin_role(admin_id, role, actor_id, actor_name)
    return redirect("/admin/admins")


@app.post("/admin/admins/{admin_id}/login")
def admin_login_update(request: Request, admin_id: int, web_login: str = Form(...)) -> RedirectResponse:
    require_user_manager(request)
    actor_id, actor_name = actor_for_audit(request)
    db.update_admin_login(admin_id, web_login, actor_id, actor_name)
    return redirect("/admin/admins")


@app.post("/admin/admins/{admin_id}/password/reset")
def admin_password_reset(request: Request, admin_id: int) -> HTMLResponse:
    require_user_manager(request)
    actor_id, actor_name = actor_for_audit(request)
    target = db.get_admin(admin_id)
    if not target:
        raise HTTPException(status_code=404)
    if not can_reset_admin_password(request, target):
        raise HTTPException(status_code=403)
    admin, password = db.reset_admin_password(admin_id, actor_id, actor_name)
    sent = False
    if admin:
        sent = send_max_to_admin(
            admin,
            f"Ваш пароль для web-админки был сброшен.\n\nЛогин: {admin.get('web_login')}\nВременный пароль: {password}\n\nПосле входа рекомендуется сменить пароль в разделе «Мой профиль».",
        )
    return render(request, "password_reset_result.html", {"item": admin, "temp_password": "" if sent else password, "sent": sent})


@app.post("/admin/admins/{admin_id}/flags")
def admin_flags(
    request: Request,
    admin_id: int,
    can_use_bot_admin: str | None = Form(None),
    can_receive_notifications: str | None = Form(None),
) -> RedirectResponse:
    require_user_manager(request)
    db.set_admin_flags(admin_id, form_checkbox(can_use_bot_admin), form_checkbox(can_receive_notifications))
    return redirect("/admin/admins")


@app.post("/admin/admins/{admin_id}/delegate")
def admin_delegate(request: Request, admin_id: int, delegated_until: str = Form("")) -> RedirectResponse:
    require_user_manager(request)
    actor_id, actor_name = actor_for_audit(request)
    if delegated_until:
        db.delegate_head(admin_id, delegated_until, actor_id, actor_name)
    return redirect("/admin/admins")


@app.post("/admin/admins/{admin_id}/delegate/clear")
def admin_delegate_clear(request: Request, admin_id: int) -> RedirectResponse:
    require_user_manager(request)
    actor_id, actor_name = actor_for_audit(request)
    db.clear_delegation(admin_id, actor_id, actor_name)
    return redirect("/admin/admins")


@app.post("/admin/admins/{admin_id}/toggle")
def admin_toggle(request: Request, admin_id: int) -> RedirectResponse:
    require_user_manager(request)
    db.toggle_admin(admin_id)
    return redirect("/admin/admins")


@app.post("/admin/admins/{admin_id}/delete")
def admin_delete(request: Request, admin_id: int) -> RedirectResponse:
    require_user_manager(request)
    actor_id, actor_name = actor_for_audit(request)
    db.disable_admin(admin_id, actor_id, actor_name)
    return redirect("/admin/admins")
