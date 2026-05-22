from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from app import db, maintenance, trudvsem_import
from app.admin_bot import notify_admins, notify_approvers
from app.max_api import MaxAPI, build_keyboard


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

PLACEHOLDER_TOKENS = {"", "change-me", "your-token", "MAX_BOT_TOKEN", "put-token-here"}
QUESTION_START_PROMPT = (
    "Напишите ваш вопрос одним сообщением.\n\n"
    "Например:\n"
    "- какие есть вакансии;\n"
    "- какие требования к кандидатам;\n"
    "- какое денежное довольствие;\n"
    "- какие документы нужны;\n"
    "- как проходит оформление на службу."
)
APPLICATION_CONFIRM_PROMPT = (
    "Проверьте данные и подтвердите отправку предварительного отклика.\n\n"
    "Нажимая «Да», вы подтверждаете, что добровольно передаёте указанные данные "
    "для предварительной консультации по вопросу поступления на службу."
)

user_states: dict[str, dict[str, Any]] = {}
PUBLIC_BOT_COMMANDS = [{"name": "start", "description": "Открыть главное меню"}]
STAFF_BOT_COMMANDS = [
    {"name": "start", "description": "Открыть главное меню"},
    {"name": "staff", "description": "Открыть служебное меню"},
]
_PUBLIC_COMMANDS_SYNCED = False

MAIN_MENU_KEYBOARD = build_keyboard(
    [
        ["Актуальные вакансии"],
        ["Условия службы", "Порядок поступления"],
        ["Задать вопрос", "Написать сообщение"],
        ["Контакты"],
    ]
)
MENU_ONLY_KEYBOARD = build_keyboard([["Главное меню"]])
SERVICE_PHOTO_KEYBOARD = build_keyboard([["Просмотреть фотографии"], ["Главное меню"]])
SERVICE_BACK_KEYBOARD = build_keyboard([["Условия службы"], ["Главное меню"]])
SERVICE_PHOTOS_DONE_KEYBOARD = build_keyboard([["Просмотреть фотографии"], ["Условия службы"], ["Главное меню"]])
SERVICE_ALBUM_DONE_KEYBOARD = build_keyboard([["Выбрать другой альбом"], ["Условия службы"], ["Главное меню"]])
YES_NO_CANCEL_KEYBOARD = build_keyboard([["Да", "Нет"], ["Отмена"]])
EDUCATION_KEYBOARD = build_keyboard(
    [
        ["Неполное среднее"],
        ["Полное среднее"],
        ["Среднее профессиональное"],
        ["Высшее"],
        ["Другое / указать вручную"],
        ["Отмена"],
    ]
)
MILITARY_SERVICE_KEYBOARD = build_keyboard(
    [
        ["Да", "Нет"],
        ["Не подлежал / не призывался"],
        ["Указать вручную"],
        ["Отмена"],
    ]
)
PREFERRED_TIME_KEYBOARD = build_keyboard(
    [
        ["В рабочее время"],
        ["До обеда", "После обеда"],
        ["После 17:00"],
        ["В любое время"],
        ["Указать вручную"],
        ["Отмена"],
    ]
)
COMMENT_KEYBOARD = build_keyboard([["Пропустить"], ["Отмена"]])
VACANCY_DETAIL_KEYBOARD = build_keyboard([["Откликнуться на эту вакансию"], ["Назад к вакансиям"], ["Главное меню"]])
STAFF_BACK_KEYBOARD = build_keyboard([["Служебное меню"], ["Меню кандидата"]])
STAFF_MENU_KEYBOARD = build_keyboard(
    [["Новые отклики"], ["Мои отклики в работе"], ["Все отклики"], ["Архив откликов"], ["Вакансии", "Условия службы"], ["О программе"], ["Статистика"], ["Меню кандидата"]]
)
HEAD_STAFF_MENU_KEYBOARD = build_keyboard(
    [["Новые отклики"], ["Мои отклики в работе"], ["Все отклики"], ["Архив откликов"], ["Назначить отклик"], ["Заявки на доступ"], ["Сотрудники отдела кадров"], ["Вакансии", "Условия службы"], ["О программе"], ["Статистика"], ["Меню кандидата"]]
)
STAFF_ABOUT_KEYBOARD = build_keyboard([["Проверить обновления"], ["Обновить из GitHub"], ["Перезапустить MAX-бота"], ["Перезапустить web-панель управления"], ["Служебное меню"]])


def cancel_keyboard() -> dict[str, Any]:
    return build_keyboard([["Отмена"]])


def get_token() -> str:
    return os.getenv("MAX_BOT_TOKEN", "").strip()


def token_is_valid(token: str) -> bool:
    return bool(token) and token not in PLACEHOLDER_TOKENS


def ensure_public_bot_commands(api: MaxAPI) -> None:
    global _PUBLIC_COMMANDS_SYNCED
    if _PUBLIC_COMMANDS_SYNCED:
        return
    if api.set_bot_commands(PUBLIC_BOT_COMMANDS):
        _PUBLIC_COMMANDS_SYNCED = True


def try_set_staff_bot_commands(api: MaxAPI, chat_id: str | None = None, user_id: str | None = None) -> None:
    api.set_bot_commands(STAFF_BOT_COMMANDS, chat_id=chat_id, user_id=user_id)


def try_clear_staff_bot_commands(api: MaxAPI, chat_id: str | None = None, user_id: str | None = None) -> None:
    api.set_bot_commands(PUBLIC_BOT_COMMANDS, chat_id=chat_id, user_id=user_id)


def main_menu() -> str:
    settings = db.get_org_settings()
    return "\n\n".join(
        part.strip()
        for part in (
            settings.get("public_welcome_title", ""),
            settings.get("public_welcome_text", ""),
            settings.get("public_menu_hint", ""),
        )
        if part and part.strip()
    )


def org_text(key: str) -> str:
    settings = db.get_org_settings()
    text = settings.get(key, "")
    try:
        return text.format(**settings)
    except (KeyError, ValueError):
        return text


def application_start_prompt() -> str:
    return (
        "Перед отправкой отклика обратите внимание:\n\n"
        f"{org_text('personal_data_warning')}\n\n"
        "Укажите, пожалуйста, ваше ФИО:"
    )


def normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def extract_message(update: dict[str, Any]) -> dict[str, Any] | None:
    event = update.get("event") if isinstance(update.get("event"), dict) else {}
    message = (
        update.get("message")
        or update.get("payload", {}).get("message")
        or update.get("body", {}).get("message")
        or event.get("message")
    )
    if not isinstance(message, dict):
        return None
    return message


def dict_value(source: dict[str, Any] | None, *path: str) -> Any:
    current: Any = source
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_text_value(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_text_or_payload(update: dict[str, Any], message: dict[str, Any] | None = None) -> str:
    message = message or extract_message(update) or {}
    body = message.get("body") if isinstance(message, dict) else {}
    payload = update.get("payload") if isinstance(update.get("payload"), dict) else {}
    callback = update.get("callback") if isinstance(update.get("callback"), dict) else {}
    action = update.get("action") if isinstance(update.get("action"), dict) else {}
    event = update.get("event") if isinstance(update.get("event"), dict) else {}
    return first_text_value(
        body.get("text") if isinstance(body, dict) else "",
        message.get("text") if isinstance(message, dict) else "",
        payload.get("text"),
        payload.get("payload"),
        payload.get("action"),
        callback.get("text"),
        callback.get("payload"),
        callback.get("action"),
        action.get("text"),
        action.get("payload"),
        action.get("action"),
        event.get("text"),
        event.get("payload"),
        event.get("action"),
        update.get("text"),
        update.get("payload") if isinstance(update.get("payload"), str) else "",
        update.get("action") if isinstance(update.get("action"), str) else "",
    )


def extract_update_type(update: dict[str, Any]) -> str:
    event = update.get("event") if isinstance(update.get("event"), dict) else {}
    return first_text_value(
        update.get("update_type"),
        update.get("type"),
        update.get("event_type"),
        event.get("type"),
        event.get("event_type"),
        event.get("update_type"),
    )


def extract_text(message: dict[str, Any]) -> str:
    body = message.get("body")
    if isinstance(body, dict) and body.get("text"):
        return str(body["text"]).strip()
    return str(message.get("text") or "").strip()


def extract_chat_id(update: dict[str, Any]) -> str:
    message = extract_message(update) or (update if isinstance(update, dict) else {})
    return str(
        dict_value(update, "chat_id")
        or dict_value(update, "chat", "chat_id")
        or dict_value(update, "chat", "id")
        or dict_value(update, "message", "chat_id")
        or dict_value(update, "message", "chat", "chat_id")
        or dict_value(update, "message", "chat", "id")
        or dict_value(update, "event", "chat_id")
        or dict_value(update, "event", "chat", "chat_id")
        or dict_value(update, "event", "chat", "id")
        or dict_value(message, "recipient", "chat_id")
        or dict_value(message, "chat", "chat_id")
        or dict_value(message, "chat", "id")
        or dict_value(message, "chat_id")
        or ""
    )


def extract_user_id(update: dict[str, Any]) -> str:
    message = extract_message(update) or (update if isinstance(update, dict) else {})
    return str(
        dict_value(update, "user_id")
        or dict_value(update, "user", "user_id")
        or dict_value(update, "user", "id")
        or dict_value(update, "message", "sender", "user_id")
        or dict_value(update, "message", "sender", "id")
        or dict_value(update, "message", "from", "user_id")
        or dict_value(update, "message", "from", "id")
        or dict_value(update, "event", "user_id")
        or dict_value(update, "event", "user", "user_id")
        or dict_value(update, "event", "user", "id")
        or dict_value(message, "sender", "user_id")
        or dict_value(message, "sender", "id")
        or dict_value(message, "from", "user_id")
        or dict_value(message, "from", "id")
        or dict_value(message, "user_id")
        or ""
    )


def extract_display_name(update: dict[str, Any]) -> str:
    message = extract_message(update) or (update if isinstance(update, dict) else {})
    sender = (
        dict_value(update, "user")
        or dict_value(update, "event", "user")
        or dict_value(message, "sender")
        or dict_value(message, "from")
        or {}
    )
    name_parts = [str(sender.get("first_name") or "").strip(), str(sender.get("last_name") or "").strip()]
    display = " ".join(part for part in name_parts if part)
    return display or str(sender.get("name") or sender.get("username") or "")


def send(
    api: MaxAPI,
    chat_id: str,
    text: str,
    user_id: str | None = None,
    keyboard: dict[str, Any] | None = None,
) -> None:
    if chat_id:
        api.send_message(text, chat_id=chat_id, keyboard=keyboard)
        return
    if user_id:
        api.send_message(text, user_id=user_id, keyboard=keyboard)
        return


def format_vacancy(item: dict[str, Any]) -> str:
    return (
        f"{item['title']}\n\n"
        f"Зарплата: {item.get('salary') or 'уточняется'}\n\n"
        f"Обязанности: {item.get('duties') or 'уточняются при консультации'}\n\n"
        f"Требования: {item.get('requirements') or 'уточняются при консультации'}\n\n"
        f"Условия: {item.get('conditions') or 'уточняются при консультации'}\n\n"
        f"Примечание: {item.get('note') or 'информация уточняется сотрудником кадров'}"
    )


def vacancy_buttons(vacancies: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(vacancies) > 12:
        return build_keyboard([["Назад", "Главное меню"]])
    rows = [[vacancy["title"]] for vacancy in vacancies]
    rows.append(["Назад", "Главное меню"])
    return build_keyboard(rows)


def show_main_menu(api: MaxAPI, chat_id: str, user_id: str | None = None) -> None:
    ensure_public_bot_commands(api)
    send(api, chat_id, main_menu(), user_id=user_id, keyboard=MAIN_MENU_KEYBOARD)


def is_delegated_head(admin: dict[str, Any] | None) -> bool:
    if not admin or not admin.get("delegated_until"):
        return False
    return str(admin["delegated_until"]) > db.now_iso()


def has_staff_access(admin: dict[str, Any] | None) -> bool:
    return bool(
        admin
        and admin.get("approved") == 1
        and admin.get("is_active") == 1
        and admin.get("role") in {"hr_staff", "hr_head"}
        and admin.get("can_use_bot_admin") == 1
    )


def has_head_rights(admin: dict[str, Any] | None) -> bool:
    return bool(admin and has_staff_access(admin) and (admin.get("role") == "hr_head" or is_delegated_head(admin)))


def staff_menu_text(admin: dict[str, Any]) -> str:
    return f"Служебный раздел отдела кадров\n\nРоль: {db.role_label(admin.get('role'))}\n\nВыберите действие.\nДля возврата в это меню используйте /staff."


def show_staff_menu(api: MaxAPI, chat_id: str, user_id: str, admin: dict[str, Any]) -> None:
    try_set_staff_bot_commands(api, chat_id=chat_id, user_id=user_id)
    keyboard = HEAD_STAFF_MENU_KEYBOARD if has_head_rights(admin) else STAFF_MENU_KEYBOARD
    send(api, chat_id, staff_menu_text(admin), user_id=user_id, keyboard=keyboard)


def staff_vacancies_keyboard(vacancies: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [["Добавить вакансию"], ["Синхронизировать с Работа России"]]
    rows.extend([[f"Управлять вакансией #{item['id']}"] for item in vacancies[:20]])
    rows.append(["Служебное меню"])
    return build_keyboard(rows)


def vacancy_manage_keyboard(vacancy: dict[str, Any]) -> dict[str, Any]:
    toggle = "Отключить" if vacancy.get("is_active") else "Включить"
    return build_keyboard(
        [
            [toggle],
            ["Изменить название", "Изменить зарплату"],
            ["Изменить обязанности", "Изменить требования"],
            ["Изменить условия", "Изменить примечание"],
            ["Назад к вакансиям"],
            ["Служебное меню"],
        ]
    )


def show_staff_vacancies(api: MaxAPI, chat_id: str, user_id: str, state_id: str) -> None:
    vacancies = db.list_vacancies(active_only=False)
    lines = ["Вакансии\n"]
    for item in vacancies:
        status = "Активна" if item.get("is_active") else "Отключена"
        lines.append(f"#{item['id']} {item['title']} — {status}")
    if not vacancies:
        lines.append("Вакансий пока нет.")
    user_states[state_id] = {"scenario": "staff_vacancies"}
    send(api, chat_id, "\n".join(lines), user_id=user_id, keyboard=staff_vacancies_keyboard(vacancies))


def show_staff_vacancy_card(api: MaxAPI, chat_id: str, user_id: str, state_id: str, vacancy_id: int) -> None:
    vacancy = db.get_vacancy(vacancy_id)
    if not vacancy:
        send(api, chat_id, "Вакансия не найдена.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return
    status = "Активна" if vacancy.get("is_active") else "Отключена"
    text = (
        f"Вакансия #{vacancy['id']}\n\n"
        f"Название: {vacancy.get('title')}\n"
        f"Зарплата: {vacancy.get('salary') or ''}\n"
        f"Статус: {status}\n\n"
        f"Обязанности: {vacancy.get('duties') or ''}\n\n"
        f"Требования: {vacancy.get('requirements') or ''}\n\n"
        f"Условия: {vacancy.get('conditions') or ''}\n\n"
        f"Примечание: {vacancy.get('note') or ''}"
    )
    user_states[state_id] = {"scenario": "staff_vacancy_card", "vacancy_id": vacancy_id}
    send(api, chat_id, text, user_id=user_id, keyboard=vacancy_manage_keyboard(vacancy))


VACANCY_FIELD_COMMANDS = {
    "изменить название": ("title", "Отправьте новое название вакансии."),
    "изменить зарплату": ("salary", "Отправьте новую зарплату."),
    "изменить обязанности": ("duties", "Отправьте новый текст обязанностей."),
    "изменить требования": ("requirements", "Отправьте новый текст требований."),
    "изменить условия": ("conditions", "Отправьте новый текст условий."),
    "изменить примечание": ("note", "Отправьте новое примечание."),
}


def start_vacancy_add(api: MaxAPI, chat_id: str, user_id: str, state_id: str) -> None:
    user_states[state_id] = {"scenario": "staff_vacancy_add", "step": "title", "data": {}}
    send(api, chat_id, "Введите название вакансии.", user_id=user_id, keyboard=cancel_keyboard())


def handle_vacancy_add(api: MaxAPI, chat_id: str, user_id: str, state_id: str, text: str, state: dict[str, Any], admin: dict[str, Any]) -> None:
    order = ["title", "salary", "duties", "requirements", "conditions", "note", "confirm"]
    prompts = {
        "salary": "Введите зарплату.",
        "duties": "Введите обязанности.",
        "requirements": "Введите требования.",
        "conditions": "Введите условия.",
        "note": "Введите примечание.",
        "confirm": "Создать вакансию?\n\nНажмите «Да» для сохранения или «Нет» для отмены.",
    }
    if state["step"] != "confirm":
        state["data"][state["step"]] = text
        next_step = order[order.index(state["step"]) + 1]
        state["step"] = next_step
        keyboard = YES_NO_CANCEL_KEYBOARD if next_step == "confirm" else cancel_keyboard()
        send(api, chat_id, prompts[next_step], user_id=user_id, keyboard=keyboard)
        return
    if normalize(text) not in {"да", "yes"}:
        user_states.pop(state_id, None)
        show_staff_menu(api, chat_id, user_id, admin)
        return
    vacancy_id = db.create_vacancy(state["data"])
    db.audit_log(admin["id"], db.admin_display_name(admin), "vacancy_created", "vacancy", vacancy_id)
    send(api, chat_id, "Вакансия создана.", user_id=user_id)
    show_staff_vacancy_card(api, chat_id, user_id, state_id, vacancy_id)


def handle_staff_vacancy_state(api: MaxAPI, chat_id: str, user_id: str, state_id: str, command: str, text: str, state: dict[str, Any], admin: dict[str, Any]) -> bool:
    scenario = state.get("scenario")
    if scenario == "staff_vacancies":
        if command == "синхронизировать с работа россии":
            settings = db.get_trudvsem_settings()
            company_code = (settings.get("trudvsem_company_code") or "").strip()
            inn = (settings.get("trudvsem_inn") or "").strip()
            if not company_code and not inn:
                send(
                    api,
                    chat_id,
                    "Импорт с портала «Работа России» не настроен. Укажите ИНН работодателя или код работодателя в web-панели управления: Вакансии → Импорт с Работа России.",
                    user_id=user_id,
                    keyboard=STAFF_BACK_KEYBOARD,
                )
                return True
            if str(settings.get("trudvsem_enabled") or "0") != "1":
                send(api, chat_id, "Импорт с портала «Работа России» отключён в настройках web-панели управления.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
                return True
            result = trudvsem_import.import_trudvsem_vacancies(actor_id=admin["id"], actor_name=db.admin_display_name(admin))
            if not result.get("ok"):
                send(
                    api,
                    chat_id,
                    "Не удалось получить вакансии с портала «Работа России». Попробуйте позже или выполните импорт через web-панель управления.",
                    user_id=user_id,
                    keyboard=STAFF_BACK_KEYBOARD,
                )
                return True
            errors = "\n".join(result.get("errors") or []) or "нет"
            send(
                api,
                chat_id,
                (
                    "Импорт завершён.\n"
                    f"Добавлено: {result.get('added', 0)}\n"
                    f"Обновлено: {result.get('updated', 0)}\n"
                    f"Пропущено: {result.get('skipped', 0)}\n"
                    f"Ошибки: {errors}"
                ),
                user_id=user_id,
                keyboard=STAFF_BACK_KEYBOARD,
            )
            return True
        if command == "добавить вакансию":
            start_vacancy_add(api, chat_id, user_id, state_id)
            return True
        if command.startswith("управлять вакансией #"):
            try:
                show_staff_vacancy_card(api, chat_id, user_id, state_id, int(text.split("#", 1)[1]))
            except ValueError:
                send(api, chat_id, "Не удалось определить ID вакансии.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
    if scenario == "staff_vacancy_card":
        vacancy_id = int(state["vacancy_id"])
        vacancy = db.get_vacancy(vacancy_id)
        if not vacancy:
            return False
        if command in {"включить", "отключить"}:
            db.toggle_vacancy(vacancy_id)
            db.audit_log(admin["id"], db.admin_display_name(admin), "vacancy_changed", "vacancy", vacancy_id, command)
            send(api, chat_id, "Вакансия обновлена.", user_id=user_id)
            show_staff_vacancy_card(api, chat_id, user_id, state_id, vacancy_id)
            return True
        if command in VACANCY_FIELD_COMMANDS:
            field, prompt = VACANCY_FIELD_COMMANDS[command]
            user_states[state_id] = {"scenario": "staff_vacancy_edit", "vacancy_id": vacancy_id, "field": field}
            send(api, chat_id, prompt, user_id=user_id, keyboard=cancel_keyboard())
            return True
    if scenario == "staff_vacancy_edit":
        vacancy_id = int(state["vacancy_id"])
        db.update_vacancy_field(vacancy_id, state["field"], text, admin["id"], db.admin_display_name(admin))
        send(api, chat_id, "Вакансия обновлена.", user_id=user_id)
        show_staff_vacancy_card(api, chat_id, user_id, state_id, vacancy_id)
        return True
    if scenario == "staff_vacancy_add":
        handle_vacancy_add(api, chat_id, user_id, state_id, text, state, admin)
        return True
    return False


def require_staff_message(api: MaxAPI, chat_id: str, user_id: str) -> dict[str, Any] | None:
    admin = db.get_admin_by_user_id(user_id)
    if has_staff_access(admin):
        return admin
    if admin and admin.get("role") == "pending":
        send(api, chat_id, "Ваша заявка на доступ ожидает подтверждения.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
    elif admin and admin.get("role") == "disabled":
        send(api, chat_id, "Доступ к служебному разделу отключён.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
    else:
        send(api, chat_id, "Для запроса доступа отправьте команду /admin <код>.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
    return None


def show_vacancies(api: MaxAPI, chat_id: str, state_id: str, mode: str = "view", user_id: str | None = None) -> None:
    vacancies = db.list_vacancies(active_only=True)
    if not vacancies:
        send(
            api,
            chat_id,
            "Сейчас активных вакансий нет. Вы можете задать вопрос или написать сообщение.",
            user_id=user_id,
            keyboard=MAIN_MENU_KEYBOARD,
        )
        return
    user_states[state_id] = {"scenario": mode, "step": "choose_vacancy", "vacancies": vacancies}
    lines = ["Выберите вакансию:"]
    for index, vacancy in enumerate(vacancies, start=1):
        lines.append(f"{index}. {vacancy['title']} — {vacancy.get('salary') or 'зарплата уточняется'}")
    if len(vacancies) > 12:
        lines.append("\nНапишите номер вакансии из списка.")
    send(api, chat_id, "\n".join(lines), user_id=user_id, keyboard=vacancy_buttons(vacancies))


def parse_choice(text: str, choices: list[dict[str, Any]]) -> dict[str, Any] | None:
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(choices):
            return choices[index]
    lowered = normalize(text)
    for item in choices:
        if lowered == normalize(str(item["title"])):
            return item
    return None


def show_vacancy_detail(
    api: MaxAPI,
    chat_id: str,
    state_id: str,
    vacancy: dict[str, Any],
    user_id: str | None = None,
) -> None:
    user_states[state_id] = {"scenario": "vacancy_detail", "vacancy": vacancy}
    send(api, chat_id, format_vacancy(vacancy), user_id=user_id, keyboard=VACANCY_DETAIL_KEYBOARD)


def start_application_for_vacancy(
    api: MaxAPI,
    chat_id: str,
    state_id: str,
    user_id: str,
    vacancy: dict[str, Any],
) -> None:
    state = {
        "scenario": "application",
        "step": "full_name",
        "data": {
            "max_user_id": user_id,
            "vacancy_id": vacancy["id"],
            "vacancy_title": vacancy["title"],
        },
    }
    user_states[state_id] = state
    send(api, chat_id, application_start_prompt(), user_id=user_id, keyboard=cancel_keyboard())


def ask_application_step(api: MaxAPI, chat_id: str, state: dict[str, Any], user_id: str | None = None) -> None:
    prompts = {
        "age": "Укажите возраст.",
        "phone": "Укажите телефон для связи.",
        "education": "Выберите образование или укажите его текстом.",
        "education_manual": "Укажите образование текстом:",
        "military_service": "Проходили ли срочную службу?",
        "military_service_manual": "Укажите информацию о прохождении срочной службы текстом:",
        "preferred_time": "Укажите удобное время для связи.",
        "preferred_time_manual": "Укажите удобное время для связи текстом:",
        "comment": (
            "Добавьте комментарий к отклику, если хотите.\n\n"
            "Например: удобный день для звонка, дополнительная информация, интересующие вопросы.\n\n"
            "Если комментарий не нужен, нажмите «Пропустить»."
        ),
    }
    keyboards = {
        "education": EDUCATION_KEYBOARD,
        "military_service": MILITARY_SERVICE_KEYBOARD,
        "preferred_time": PREFERRED_TIME_KEYBOARD,
        "comment": COMMENT_KEYBOARD,
        "confirm": YES_NO_CANCEL_KEYBOARD,
    }
    step = state["step"]
    prompt = application_confirm_text(state.get("data", {})) if step == "confirm" else prompts[step]
    keyboard = keyboards.get(step) or cancel_keyboard()
    send(api, chat_id, prompt, user_id=user_id, keyboard=keyboard)


def normalize_application_comment(text: str) -> str:
    return "" if normalize(text) in {"нет", "пропустить"} else text


def application_confirm_text(data: dict[str, Any]) -> str:
    comment = data.get("comment") or "Не указан"
    return (
        "Проверьте данные предварительного отклика:\n\n"
        f"Вакансия: {data.get('vacancy_title') or ''}\n"
        f"ФИО: {data.get('full_name') or ''}\n"
        f"Возраст: {data.get('age') or ''}\n"
        f"Телефон: {data.get('phone') or ''}\n"
        f"Образование: {data.get('education') or ''}\n"
        f"Срочная служба: {data.get('military_service') or ''}\n"
        f"Удобное время связи: {data.get('preferred_time') or ''}\n"
        f"Комментарий: {comment}\n\n"
        "Нажимая «Да», вы подтверждаете, что добровольно передаёте указанные данные "
        "для предварительной консультации по вопросу поступления на службу."
    )


def application_notify_text(data: dict[str, Any]) -> str:
    return (
        "Новый отклик на вакансию\n\n"
        f"Вакансия: {data.get('vacancy_title')}\n"
        f"ФИО: {data.get('full_name')}\n"
        f"Возраст: {data.get('age')}\n"
        f"Телефон: {data.get('phone')}\n"
        f"Образование: {data.get('education')}\n"
        f"Срочная служба: {data.get('military_service')}\n"
        f"Удобное время связи: {data.get('preferred_time')}\n"
        f"Комментарий: {data.get('comment')}\n"
        f"Дата: {db.now_iso()}"
    )


def question_notify_text(question: str, contact: str) -> str:
    return f"Новый вопрос от кандидата\n\nВопрос:\n{question}\n\nКонтакт:\n{contact}\n\nДата:\n{db.now_iso()}"


def appeal_notify_text(full_name: str, phone: str, appeal_text: str) -> str:
    return f"Новое сообщение через бота\n\nФИО:\n{full_name}\n\nТелефон:\n{phone}\n\nТекст:\n{appeal_text}\n\nДата:\n{db.now_iso()}"


def handle_application(api: MaxAPI, chat_id: str, state_id: str, user_id: str, text: str, state: dict[str, Any]) -> None:
    if state["step"] == "choose_vacancy":
        vacancy = parse_choice(text, state["vacancies"])
        if not vacancy:
            send(api, chat_id, "Не удалось выбрать вакансию. Напишите номер из списка.", user_id=user_id, keyboard=vacancy_buttons(state["vacancies"]))
            return
        state["data"] = {
            "max_user_id": user_id,
            "vacancy_id": vacancy["id"],
            "vacancy_title": vacancy["title"],
        }
        state["step"] = "full_name"
        send(api, chat_id, application_start_prompt(), user_id=user_id, keyboard=cancel_keyboard())
        return

    command = normalize(text)
    data = state.setdefault("data", {})
    step = state["step"]

    if step in {"full_name", "age", "phone"}:
        data[step] = text
        next_steps = {"full_name": "age", "age": "phone", "phone": "education"}
        state["step"] = next_steps[step]
        ask_application_step(api, chat_id, state, user_id=user_id)
        return

    if step == "education":
        if command in {"другое / указать вручную", "другое", "указать вручную"}:
            state["step"] = "education_manual"
            ask_application_step(api, chat_id, state, user_id=user_id)
            return
        data["education"] = text
        state["step"] = "military_service"
        ask_application_step(api, chat_id, state, user_id=user_id)
        return

    if step == "education_manual":
        data["education"] = text
        state["step"] = "military_service"
        ask_application_step(api, chat_id, state, user_id=user_id)
        return

    if step == "military_service":
        if command in {"указать вручную", "другое", "другое / указать вручную"}:
            state["step"] = "military_service_manual"
            ask_application_step(api, chat_id, state, user_id=user_id)
            return
        data["military_service"] = text
        state["step"] = "preferred_time"
        ask_application_step(api, chat_id, state, user_id=user_id)
        return

    if step == "military_service_manual":
        data["military_service"] = text
        state["step"] = "preferred_time"
        ask_application_step(api, chat_id, state, user_id=user_id)
        return

    if step == "preferred_time":
        if command in {"указать вручную", "другое", "другое / указать вручную"}:
            state["step"] = "preferred_time_manual"
            ask_application_step(api, chat_id, state, user_id=user_id)
            return
        data["preferred_time"] = text
        state["step"] = "comment"
        ask_application_step(api, chat_id, state, user_id=user_id)
        return

    if step == "preferred_time_manual":
        data["preferred_time"] = text
        state["step"] = "comment"
        ask_application_step(api, chat_id, state, user_id=user_id)
        return

    if step == "comment":
        data["comment"] = normalize_application_comment(text)
        state["step"] = "confirm"
        ask_application_step(api, chat_id, state, user_id=user_id)
        return

    if command not in {"да", "yes", "согласен", "согласна"}:
        user_states.pop(state_id, None)
        show_main_menu(api, chat_id, user_id=user_id)
        return
    data["comment"] = normalize_application_comment(str(data.get("comment") or ""))
    application_id = db.create_application(data)
    notify_admins(
        api,
        application_notify_text(data),
        keyboard=build_keyboard([[f"Принять в работу #{application_id}"]]),
    )
    user_states.pop(state_id, None)
    send(
        api,
        chat_id,
        org_text("application_success_text"),
        user_id=user_id,
        keyboard=MENU_ONLY_KEYBOARD,
    )


def handle_question(api: MaxAPI, chat_id: str, state_id: str, user_id: str, text: str, state: dict[str, Any]) -> None:
    if state["step"] == "question":
        state["question"] = text
        state["step"] = "contact"
        send(api, chat_id, "Укажите контакт для связи или напишите «нет».", user_id=user_id, keyboard=cancel_keyboard())
        return
    contact = "" if normalize(text) == "нет" else text
    db.create_question(user_id, state["question"], contact)
    notify_admins(api, question_notify_text(state["question"], contact or "не указан"))
    user_states.pop(state_id, None)
    send(api, chat_id, org_text("question_success_text"), user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)


def handle_appeal(api: MaxAPI, chat_id: str, state_id: str, user_id: str, text: str, state: dict[str, Any]) -> None:
    if state["step"] == "full_name":
        state["full_name"] = text
        state["step"] = "phone"
        send(api, chat_id, "Укажите телефон для связи.", user_id=user_id, keyboard=cancel_keyboard())
        return
    if state["step"] == "phone":
        state["phone"] = text
        state["step"] = "text"
        send(api, chat_id, "Напишите текст сообщения.", user_id=user_id, keyboard=cancel_keyboard())
        return
    db.create_appeal(user_id, state["full_name"], state["phone"], text)
    notify_admins(api, appeal_notify_text(state["full_name"], state["phone"], text))
    user_states.pop(state_id, None)
    send(api, chat_id, org_text("appeal_success_text"), user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)


def service_conditions(api: MaxAPI, chat_id: str, user_id: str | None = None) -> None:
    conditions = db.get_service_info("conditions")
    parts = []
    if conditions:
        parts.append(f"{conditions['title']}\n\n{conditions['text']}")
    albums = db.list_photo_albums_with_active_photos()
    if albums:
        parts.append("Вы можете посмотреть фотографии, добавленные сотрудниками отдела кадров.")
    send(api, chat_id, "\n\n".join(parts) if parts else "Информация об условиях службы пока не заполнена.", user_id=user_id, keyboard=SERVICE_PHOTO_KEYBOARD)


def service_album_keyboard(albums: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [[f"Фотоальбом #{album['id']}"] for album in albums[:20]]
    rows.append(["Условия службы", "Главное меню"])
    return build_keyboard(rows)


def parse_photo_album_choice(text: str, albums: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    command = normalize(text)
    if "#" in command:
        try:
            album_id = int(command.split("#", 1)[1].strip())
        except ValueError:
            album_id = 0
        album = db.get_photo_album(album_id) if album_id else None
        if album and album.get("is_active") and db.list_photos(active_only=True, album_id=album_id):
            return album
    if command.startswith("альбом:"):
        command = normalize(command.split(":", 1)[1])
    for album in albums or db.list_photo_albums_with_active_photos():
        if normalize(str(album.get("title") or "")) == command:
            return album
    return None


def show_service_photo_albums(api: MaxAPI, chat_id: str, state_id: str | None = None, user_id: str | None = None) -> None:
    albums = db.list_photo_albums_with_active_photos()
    if not albums:
        send(api, chat_id, "Сейчас фотографии не добавлены.", user_id=user_id, keyboard=SERVICE_BACK_KEYBOARD)
        return
    if len(albums) == 1:
        send_service_album_photos(api, chat_id, albums[0], user_id=user_id, generic_done=True)
        return
    if state_id:
        user_states[state_id] = {"scenario": "service_photo_albums", "albums": albums}
    lines = ["Выберите альбом для просмотра фотографий:"]
    for album in albums:
        description = f"\n{album['description']}" if album.get("description") else ""
        lines.append(f"\n#{album['id']} {album['title']}{description}")
    send(api, chat_id, "\n".join(lines), user_id=user_id, keyboard=service_album_keyboard(albums))


def send_service_album_photos(api: MaxAPI, chat_id: str, album: dict[str, Any], user_id: str | None = None, generic_done: bool = False) -> None:
    photos = db.list_photos(active_only=True, album_id=int(album["id"]))
    if not photos:
        send(api, chat_id, "Сейчас фотографии не добавлены.", user_id=user_id, keyboard=SERVICE_BACK_KEYBOARD)
        return
    sent_any = False
    failed_any = False
    for photo in photos:
        photo_path = PROJECT_ROOT / "app" / "static" / "uploads" / "service" / photo["filename"]
        try:
            sent = bool(api.send_photo(chat_id=chat_id, filename=str(photo_path), caption=photo.get("caption") or ""))
            sent_any = sent or sent_any
            failed_any = (not sent) or failed_any
        except Exception as exc:
            failed_any = True
            print(f"Не удалось отправить фото условий службы из альбома {album.get('id')}: {exc}")
    if not sent_any:
        if failed_any:
            send(api, chat_id, "Не удалось отправить фотографию. Попробуйте позже.", user_id=user_id, keyboard=SERVICE_ALBUM_DONE_KEYBOARD)
            return
        send(
            api,
            chat_id,
            "Фотографии добавлены, но отправка изображений через MAX API пока требует настройки.",
            user_id=user_id,
            keyboard=SERVICE_ALBUM_DONE_KEYBOARD,
        )
        return
    if failed_any:
        send(api, chat_id, "Не удалось отправить фотографию. Попробуйте позже.", user_id=user_id, keyboard=SERVICE_ALBUM_DONE_KEYBOARD)
    title = album.get("title") or "выбранного альбома"
    done_text = "Фотографии отправлены." if generic_done else f"Фотографии из альбома «{title}» отправлены."
    done_keyboard = SERVICE_PHOTOS_DONE_KEYBOARD if generic_done else SERVICE_ALBUM_DONE_KEYBOARD
    send(api, chat_id, done_text, user_id=user_id, keyboard=done_keyboard)


def service_order(api: MaxAPI, chat_id: str, user_id: str | None = None) -> None:
    item = db.get_service_info("order")
    send(api, chat_id, f"{item['title']}\n\n{item['text']}" if item else "Порядок поступления пока не заполнен.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)


def contacts(api: MaxAPI, chat_id: str, user_id: str | None = None) -> None:
    items = db.list_contacts(active_only=True)
    if items:
        lines = ["Контакты"]
        for item in items:
            note = f"\n{item['note']}" if item.get("note") else ""
            lines.append(f"\n{item['title']}\n{item['value']}{note}")
        send(api, chat_id, "\n".join(lines), user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
        return
    item = db.get_service_info("contacts")
    if item and str(item.get("text") or "").strip():
        send(api, chat_id, f"{item['title']}\n\n{item['text']}", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
        return
    fallback = org_text("contacts_public_text") or "Контакты пока не заполнены."
    send(api, chat_id, fallback, user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)


def handle_admin_command(api: MaxAPI, chat_id: str, user_id: str, display_name: str, text: str) -> None:
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and db.validate_admin_secret(parts[1].strip()):
        admin, created_request = db.request_admin_access(user_id, chat_id, display_name)
        if not created_request and admin.get("approved") == 1:
            send(api, chat_id, "Ваш доступ уже подтверждён. Данные диалога обновлены.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
            return
        send(
            api,
            chat_id,
            "Ваша заявка на доступ к служебному разделу бота принята.\n\nДоступ будет открыт после подтверждения главной учётной записью или начальником отдела кадров.",
            user_id=user_id,
            keyboard=MENU_ONLY_KEYBOARD,
        )
        notify_approvers(api, f"Новая заявка на доступ к служебному разделу\n\nПользователь: {display_name or user_id}\nMAX ID: {user_id}\nДата: {db.now_iso()}\n\nДля одобрения отправьте:\nОдобрить как сотрудника #{admin['id']}\nили\nОдобрить как начальника #{admin['id']}", int(admin["id"]))
    else:
        send(api, chat_id, "Неверный код администратора.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)


def handle_approve_command(api: MaxAPI, chat_id: str, user_id: str, text: str, approve: bool) -> None:
    actor = db.get_admin_by_user_id(user_id)
    if not has_head_rights(actor):
        send(api, chat_id, "Доступ запрещён.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
        return
    try:
        admin_id = int(text.split("#", 1)[1].strip())
    except (IndexError, ValueError):
        send(api, chat_id, "Не удалось определить номер заявки.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
        return
    if approve:
        role = "hr_head" if "начальник" in normalize(text) else "hr_staff"
        admin, password = db.approve_admin(admin_id, actor.get("id"), db.admin_display_name(actor), role=role)
        if not admin:
            send(api, chat_id, "Заявка не найдена.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
            return
        send(api, chat_id, "Пользователь одобрен.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        if admin.get("chat_id") or admin.get("max_user_id"):
            try_set_staff_bot_commands(api, chat_id=str(admin.get("chat_id") or ""), user_id=str(admin.get("max_user_id") or ""))
            send(
                api,
                str(admin.get("chat_id") or ""),
                f"Ваш доступ подтверждён.\n\nРоль: {db.role_label(admin.get('role'))}\nЛогин для web-панели управления: {admin['web_login']}\nВременный пароль: {password}\n\nПосле входа рекомендуется сменить пароль.",
                user_id=str(admin.get("max_user_id") or ""),
                keyboard=MENU_ONLY_KEYBOARD,
            )
    else:
        admin = db.reject_admin(admin_id, actor.get("id"), db.admin_display_name(actor))
        send(api, chat_id, "Заявка отклонена.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        if admin and (admin.get("chat_id") or admin.get("max_user_id")):
            try_clear_staff_bot_commands(api, chat_id=str(admin.get("chat_id") or ""), user_id=str(admin.get("max_user_id") or ""))
            send(api, str(admin.get("chat_id") or ""), "Ваша заявка на доступ отклонена.", user_id=str(admin.get("max_user_id") or ""), keyboard=MENU_ONLY_KEYBOARD)


def handle_take_application_command(api: MaxAPI, chat_id: str, user_id: str, text: str) -> None:
    admin = require_staff_message(api, chat_id, user_id)
    if not admin:
        return
    try:
        application_id = int(text.split("#", 1)[1].strip())
    except (IndexError, ValueError):
        send(api, chat_id, "Не удалось определить номер отклика.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
        return
    ok, app = db.take_application(application_id, admin)
    if ok:
        name = db.admin_display_name(admin)
        send(api, chat_id, f"Отклик #{application_id} принят вами в работу.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
        notify_admins(api, f"Отклик #{application_id} принят в работу.\nОтветственный: {name}")
        return
    if app:
        if int(app.get("is_archived") or 0) == 1:
            send(api, chat_id, f"Отклик #{application_id} находится в архиве.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
            return
        send(api, chat_id, f"Отклик #{application_id} уже в работе.\nОтветственный: {app.get('assigned_to_name') or 'не указан'}", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
    else:
        send(api, chat_id, f"Отклик #{application_id} не найден.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)


def format_application_short(app: dict[str, Any]) -> str:
    return f"#{app['id']} {app.get('vacancy_title') or ''}\n{app.get('full_name') or ''}, {app.get('phone') or ''}\nСтатус: {db.application_status_label(app.get('status'))}\nОтветственный: {app.get('assigned_to_name') or 'нет'}"


def format_application_assignment_line(app: dict[str, Any]) -> str:
    responsible = app.get("assigned_to_name") or "не назначен"
    return (
        f"#{app['id']} — {app.get('full_name') or 'кандидат не указан'}, "
        f"вакансия: {app.get('vacancy_title') or 'не указана'}, "
        f"статус: {db.application_status_label(app.get('status'))}, "
        f"ответственный: {responsible}"
    )


def assignable_applications(limit: int = 20) -> list[dict[str, Any]]:
    apps = [
        item
        for item in db.list_applications(view="active")
        if item.get("status") in {"new", "in_work"}
    ]
    apps.sort(key=lambda item: (1 if item.get("assigned_to_admin_id") else 0, -int(item.get("id") or 0)))
    return apps[:limit]


def show_assignable_applications(api: MaxAPI, chat_id: str, user_id: str, state_id: str) -> None:
    apps = assignable_applications()
    if not apps:
        send(api, chat_id, "Нет откликов для назначения.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return
    user_states[state_id] = {"scenario": "staff_assign_application_list"}
    rows = [[f"Назначить #{item['id']}"] for item in apps]
    rows.append(["Служебное меню"])
    text = "Выберите отклик для назначения ответственного:\n\n" + "\n".join(format_application_assignment_line(item) for item in apps)
    send(api, chat_id, text, user_id=user_id, keyboard=build_keyboard(rows))


def show_assignment_staff(api: MaxAPI, chat_id: str, user_id: str, state_id: str, application_id: int) -> None:
    app = db.get_application(application_id)
    if not app or int(app.get("is_archived") or 0) == 1:
        send(api, chat_id, "Отклик не найден или находится в архиве.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return
    staff = [
        item
        for item in db.active_staff()
        if int(item.get("approved") or 0) == 1
        and int(item.get("is_active") or 0) == 1
        and int(item.get("can_use_bot_admin") or 0) == 1
    ]
    if not staff:
        send(api, chat_id, "Нет сотрудников, доступных для назначения.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return
    user_states[state_id] = {"scenario": "staff_assign_application_staff", "application_id": application_id}
    rows = [[f"Назначить сотруднику #{item['id']}: {db.admin_display_name(item)}"] for item in staff[:20]]
    rows.extend([["Отмена"], ["Служебное меню"]])
    text = f"Отклик для назначения:\n\n{format_application_assignment_line(app)}\n\nВыберите ответственного сотрудника."
    send(api, chat_id, text, user_id=user_id, keyboard=build_keyboard(rows))


def parse_hash_id(text: str) -> int | None:
    match = re.search(r"#\s*(\d+)", text)
    return int(match.group(1)) if match else None


def notify_assigned_application(api: MaxAPI, app: dict[str, Any], assignee: dict[str, Any]) -> None:
    if not (assignee.get("chat_id") or assignee.get("max_user_id")):
        return
    try:
        send(
            api,
            str(assignee.get("chat_id") or ""),
            (
                f"Вам назначен отклик #{app.get('id')}.\n\n"
                f"Кандидат: {app.get('full_name') or 'не указан'}\n"
                f"Вакансия: {app.get('vacancy_title') or 'не указана'}"
            ),
            user_id=str(assignee.get("max_user_id") or ""),
            keyboard=STAFF_BACK_KEYBOARD,
        )
    except Exception as exc:
        print(f"Не удалось отправить уведомление о назначении отклика: {exc}")


def assign_application_via_bot(
    api: MaxAPI,
    chat_id: str,
    user_id: str,
    application_id: int,
    assignee_id: int,
    actor: dict[str, Any],
) -> None:
    if not has_head_rights(actor):
        send(api, chat_id, "Недостаточно прав для назначения ответственного.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return
    ok, message, app, assignee = db.assign_application_to_admin(application_id, assignee_id, actor)
    if not ok:
        send(api, chat_id, message, user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return
    if app and assignee:
        notify_assigned_application(api, app, assignee)
        send(
            api,
            chat_id,
            f"Отклик #{application_id} назначен сотруднику {db.admin_display_name(assignee)}.",
            user_id=user_id,
            keyboard=STAFF_BACK_KEYBOARD,
        )


def handle_staff_assignment_state(
    api: MaxAPI,
    chat_id: str,
    user_id: str,
    state_id: str,
    command: str,
    text: str,
    state: dict[str, Any],
    admin: dict[str, Any],
) -> bool:
    scenario = state.get("scenario")
    if scenario == "staff_assign_application_list":
        if command.startswith("назначить #") or command.startswith("назначить отклик #"):
            application_id = parse_hash_id(text)
            if application_id:
                show_assignment_staff(api, chat_id, user_id, state_id, application_id)
            else:
                send(api, chat_id, "Не удалось определить номер отклика.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
    if scenario == "staff_assign_application_staff":
        if command.startswith("назначить сотруднику"):
            assignee_id = parse_hash_id(text)
            if assignee_id:
                application_id = int(state.get("application_id") or 0)
                user_states.pop(state_id, None)
                assign_application_via_bot(api, chat_id, user_id, application_id, assignee_id, admin)
            else:
                send(api, chat_id, "Не удалось определить сотрудника.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
    return False


def format_maintenance_info(info: dict[str, Any], include_remote: bool = True) -> str:
    lines = [
        "О программе",
        "",
        f"Версия: {info.get('version')}",
        f"Установленный commit: {info.get('installed_commit')}",
    ]
    if include_remote:
        lines.extend(
            [
                f"GitHub: {info.get('repo_url')}",
                f"Ветка: {info.get('branch')}",
                f"Последний commit: {info.get('latest_commit') or 'не получен'}",
                f"Статус: {info.get('message')}",
            ]
        )
    return "\n".join(lines)


def show_staff_about(api: MaxAPI, chat_id: str, user_id: str, admin: dict[str, Any]) -> None:
    can_maintain = has_head_rights(admin)
    info = maintenance.check_updates() if can_maintain else maintenance.get_local_info()
    keyboard = STAFF_ABOUT_KEYBOARD if can_maintain else STAFF_BACK_KEYBOARD
    send(api, chat_id, format_maintenance_info(info, include_remote=can_maintain), user_id=user_id, keyboard=keyboard)


def handle_staff_maintenance_state(
    api: MaxAPI,
    chat_id: str,
    user_id: str,
    state_id: str,
    command: str,
    state: dict[str, Any],
    admin: dict[str, Any],
) -> bool:
    if state.get("scenario") != "maintenance_confirm":
        return False
    if command in {"отмена", "/cancel", "нет"}:
        user_states.pop(state_id, None)
        show_staff_menu(api, chat_id, user_id, admin)
        return True
    if command != "да, обновить":
        send(api, chat_id, "Подтвердите действие кнопкой или нажмите «Отмена».", user_id=user_id, keyboard=build_keyboard([["Да, обновить"], ["Отмена"]]))
        return True
    user_states.pop(state_id, None)
    ok, output = maintenance.run_update_script()
    text = "Обновление выполнено. Службы перезапущены." if ok else "Не удалось выполнить обновление."
    if output:
        text += f"\n\n{output}"
    send(api, chat_id, text[-3500:], user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
    return True


def handle_staff_text(api: MaxAPI, chat_id: str, user_id: str, state_id: str, command: str) -> bool:
    admin = db.get_admin_by_user_id(user_id)
    if command in {"/staff", "служебное меню"}:
        checked = require_staff_message(api, chat_id, user_id)
        if checked:
            show_staff_menu(api, chat_id, user_id, checked)
        return True
    if not has_staff_access(admin):
        return False
    state = user_states.get(state_id)
    if state and handle_staff_assignment_state(api, chat_id, user_id, state_id, command, command, state, admin):
        return True
    if state and handle_staff_maintenance_state(api, chat_id, user_id, state_id, command, state, admin):
        return True
    direct_assign = re.fullmatch(r"назначить\s+#?(\d+)\s+сотруднику\s+#?(\d+)", command)
    if direct_assign:
        assign_application_via_bot(api, chat_id, user_id, int(direct_assign.group(1)), int(direct_assign.group(2)), admin)
        return True
    if command.startswith("назначить отклик #") or command.startswith("назначить #"):
        if not has_head_rights(admin):
            send(api, chat_id, "Назначение ответственного доступно начальнику кадрового подразделения.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
        application_id = parse_hash_id(command)
        if application_id:
            show_assignment_staff(api, chat_id, user_id, state_id, application_id)
        else:
            send(api, chat_id, "Не удалось определить номер отклика.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return True
    if command in {"новые отклики", "мои отклики в работе", "все отклики"}:
        apps = db.list_applications()
        if command == "новые отклики":
            apps = [item for item in apps if item.get("status") == "new" and not item.get("assigned_to_admin_id")]
        elif command == "мои отклики в работе":
            apps = [item for item in apps if item.get("assigned_to_admin_id") == admin["id"]]
        text = "\n\n".join(format_application_short(item) for item in apps[:10]) or "Откликов нет."
        send(api, chat_id, text, user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return True
    if command == "архив откликов":
        apps = db.list_applications(view="archive")
        rows = [[f"Вернуть отклик #{item['id']}"] for item in apps[:10]]
        rows.append(["Служебное меню"])
        text = "\n\n".join(format_application_short(item) for item in apps[:10]) or "В архиве откликов нет."
        send(api, chat_id, text, user_id=user_id, keyboard=build_keyboard(rows))
        return True
    if command.startswith("вернуть отклик #"):
        try:
            application_id = int(command.split("#", 1)[1].strip())
        except (IndexError, ValueError):
            send(api, chat_id, "Не удалось определить номер отклика.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
        db.unarchive_record("applications", application_id, admin["id"], db.admin_display_name(admin))
        send(api, chat_id, f"Отклик #{application_id} возвращён из архива.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return True
    if command == "назначить отклик":
        if has_head_rights(admin):
            show_assignable_applications(api, chat_id, user_id, state_id)
        else:
            send(api, chat_id, "Назначение ответственного доступно начальнику кадрового подразделения.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return True
    if command == "заявки на доступ":
        if not has_head_rights(admin):
            send(api, chat_id, "Доступ запрещён.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
        pending = [item for item in db.list_admins() if item.get("role") == "pending" or not item.get("approved")]
        if not pending:
            send(api, chat_id, "Заявок на доступ нет.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
        rows = []
        lines = ["Заявки на доступ"]
        for item in pending[:10]:
            lines.append(f"\n#{item['id']}\nИмя: {item.get('display_name') or ''}\nMAX ID: {item.get('max_user_id')}\nДата заявки: {item.get('created_at')}")
            rows.extend([[f"Одобрить как сотрудника #{item['id']}"], [f"Одобрить как начальника #{item['id']}"], [f"Отклонить #{item['id']}"]])
        rows.append(["Служебное меню"])
        send(api, chat_id, "\n".join(lines), user_id=user_id, keyboard=build_keyboard(rows))
        return True
    if command == "вакансии":
        show_staff_vacancies(api, chat_id, user_id, chat_id or user_id)
        return True
    if command == "сотрудники отдела кадров":
        if has_head_rights(admin):
            staff = db.active_staff()
            text = "\n".join(f"#{item['id']} {db.admin_display_name(item)} — {db.role_label(item.get('role'))}" for item in staff) or "Сотрудников нет."
            send(api, chat_id, text, user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        else:
            send(api, chat_id, "Доступ запрещён.", user_id=user_id, keyboard=MENU_ONLY_KEYBOARD)
        return True
    if command == "статистика":
        apps = db.list_applications()
        send(api, chat_id, f"Отклики всего: {len(apps)}\nНовые: {len([a for a in apps if a.get('status') == 'new'])}", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return True
    if command == "о программе":
        show_staff_about(api, chat_id, user_id, admin)
        return True
    if command == "проверить обновления":
        if not has_head_rights(admin):
            send(api, chat_id, "Доступ запрещён.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
        show_staff_about(api, chat_id, user_id, admin)
        return True
    if command == "обновить из github":
        if not has_head_rights(admin):
            send(api, chat_id, "Доступ запрещён.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
        user_states[state_id] = {"scenario": "maintenance_confirm", "action": "update"}
        send(api, chat_id, "Подтвердите обновление из GitHub.", user_id=user_id, keyboard=build_keyboard([["Да, обновить"], ["Отмена"]]))
        return True
    if command == "перезапустить max-бота":
        if not has_head_rights(admin):
            send(api, chat_id, "Доступ запрещён.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
        send(api, chat_id, "MAX-бот будет перезапущен.", user_id=user_id)
        ok, message = maintenance.restart_bot_service(deferred=True)
        if not ok:
            send(api, chat_id, message, user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return True
    if command == "перезапустить web-панель управления":
        if not has_head_rights(admin):
            send(api, chat_id, "Доступ запрещён.", user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
            return True
        ok, message = maintenance.restart_admin_service()
        send(api, chat_id, message, user_id=user_id, keyboard=STAFF_BACK_KEYBOARD)
        return True
    if command in {"главное меню кандидата", "меню кандидата"}:
        show_main_menu(api, chat_id, user_id=user_id)
        return True
    return False


START_EVENT_TYPES = {"bot_started", "botstarted", "chat_started", "dialog_started", "user_started_bot"}


def is_bot_started_update(update: dict[str, Any]) -> bool:
    return normalize(extract_update_type(update)).replace(" ", "_").lower() in START_EVENT_TYPES


def log_unknown_update(update: dict[str, Any]) -> None:
    update_type = extract_update_type(update) or "unknown"
    keys = sorted(str(key) for key in update.keys())
    has_chat = "да" if extract_chat_id(update) else "нет"
    has_user = "да" if extract_user_id(update) else "нет"
    print(f"Нераспознанное событие MAX: type={update_type}, chat_id={has_chat}, user_id={has_user}, keys={keys}")


def handle_bot_started(api: MaxAPI, update: dict[str, Any]) -> None:
    chat_id = extract_chat_id(update)
    user_id = extract_user_id(update)
    if not (chat_id or user_id):
        log_unknown_update(update)
        return
    state_id = chat_id or user_id
    user_states.pop(state_id, None)
    show_main_menu(api, chat_id, user_id=user_id)


def handle_message(api: MaxAPI, message: dict[str, Any], update: dict[str, Any] | None = None) -> None:
    source = update or message
    text = extract_text_or_payload(source, message)
    chat_id = extract_chat_id(source)
    user_id = extract_user_id(source)
    display_name = extract_display_name(source)
    if not text or not (chat_id or user_id):
        return
    state_id = chat_id or user_id

    command = normalize(text)
    if command in {"/cancel", "отмена"}:
        state = user_states.get(state_id)
        admin = db.get_admin_by_user_id(user_id)
        user_states.pop(state_id, None)
        if admin and has_staff_access(admin) and str((state or {}).get("scenario") or "").startswith(("staff_", "maintenance_")):
            show_staff_menu(api, chat_id, user_id, admin)
        else:
            show_main_menu(api, chat_id, user_id=user_id)
        return
    if command in {"/start", "/menu", "меню", "главное меню", "начать", "start", "старт", "bot_start"}:
        user_states.pop(state_id, None)
        show_main_menu(api, chat_id, user_id=user_id)
        return
    if command.startswith("/admin"):
        handle_admin_command(api, chat_id, user_id, display_name, text)
        return
    if command.startswith("одобрить доступ #") or command.startswith("одобрить как сотрудника #") or command.startswith("одобрить как начальника #"):
        handle_approve_command(api, chat_id, user_id, text, approve=True)
        return
    if command.startswith("отклонить доступ #") or command.startswith("отклонить #"):
        handle_approve_command(api, chat_id, user_id, text, approve=False)
        return
    if command.startswith("принять в работу #"):
        handle_take_application_command(api, chat_id, user_id, text)
        return
    if handle_staff_text(api, chat_id, user_id, state_id, command):
        return

    state = user_states.get(state_id)
    if state:
        admin = db.get_admin_by_user_id(user_id)
        if admin and handle_staff_vacancy_state(api, chat_id, user_id, state_id, command, text, state, admin):
            return
        scenario = state.get("scenario")
        if command in {"назад", "назад к вакансиям"}:
            show_vacancies(api, chat_id, state_id, "view", user_id=user_id)
            return
        if scenario == "view":
            vacancy = parse_choice(text, state["vacancies"])
            if vacancy:
                show_vacancy_detail(api, chat_id, state_id, vacancy, user_id=user_id)
            else:
                send(api, chat_id, "Напишите номер вакансии из списка.", user_id=user_id, keyboard=vacancy_buttons(state["vacancies"]))
            return
        if scenario == "vacancy_detail":
            if command in {"откликнуться на эту вакансию", "отклик"}:
                start_application_for_vacancy(api, chat_id, state_id, user_id, state["vacancy"])
            else:
                send(api, chat_id, "Выберите действие кнопкой или напишите команду.", user_id=user_id, keyboard=VACANCY_DETAIL_KEYBOARD)
            return
        if scenario == "application":
            handle_application(api, chat_id, state_id, user_id, text, state)
            return
        if scenario == "question":
            handle_question(api, chat_id, state_id, user_id, text, state)
            return
        if scenario == "appeal":
            handle_appeal(api, chat_id, state_id, user_id, text, state)
            return
        if scenario == "service_photo_albums":
            if command in {"условия службы", "условия"}:
                user_states.pop(state_id, None)
                service_conditions(api, chat_id, user_id=user_id)
                return
            if command in {"выбрать другой альбом", "просмотреть фотографии"}:
                show_service_photo_albums(api, chat_id, state_id, user_id=user_id)
                return
            album = parse_photo_album_choice(text, state.get("albums", []))
            if album:
                send_service_album_photos(api, chat_id, album, user_id=user_id)
            else:
                send(api, chat_id, "Выберите альбом кнопкой или напишите «Фотоальбом #номер».", user_id=user_id, keyboard=service_album_keyboard(state.get("albums", [])))
            return

    if command in {"1", "актуальные вакансии", "вакансии"}:
        show_vacancies(api, chat_id, state_id, "view", user_id=user_id)
    elif command in {"2", "откликнуться на вакансию", "откликнуться", "отклик"}:
        show_vacancies(api, chat_id, state_id, "application", user_id=user_id)
    elif command in {"3", "задать вопрос", "вопрос"}:
        user_states[state_id] = {"scenario": "question", "step": "question"}
        send(api, chat_id, QUESTION_START_PROMPT, user_id=user_id, keyboard=cancel_keyboard())
    elif command in {"4", "написать сообщение", "сообщение"}:
        user_states[state_id] = {"scenario": "appeal", "step": "full_name"}
        send(api, chat_id, "Укажите ФИО.", user_id=user_id, keyboard=cancel_keyboard())
    elif command in {"5", "условия службы", "условия"}:
        service_conditions(api, chat_id, user_id=user_id)
    elif command in {"просмотреть фотографии", "выбрать другой альбом"}:
        show_service_photo_albums(api, chat_id, state_id, user_id=user_id)
    elif command.startswith("фотоальбом #") or command.startswith("альбом:"):
        album = parse_photo_album_choice(text)
        if album:
            send_service_album_photos(api, chat_id, album, user_id=user_id)
        else:
            show_service_photo_albums(api, chat_id, state_id, user_id=user_id)
    elif command in {"6", "порядок поступления", "порядок"}:
        service_order(api, chat_id, user_id=user_id)
    elif command in {"7", "контакты"}:
        contacts(api, chat_id, user_id=user_id)
    else:
        show_main_menu(api, chat_id, user_id=user_id)


def run_polling() -> None:
    token = get_token()
    if not token_is_valid(token):
        print("MAX_BOT_TOKEN отсутствует или равен заглушке. Long Polling не запущен.")
        return

    db.init_db()
    api = MaxAPI(token)
    marker: str | None = None
    print("MAX-бот запущен. Для остановки нажмите Ctrl+C.")
    while True:
        try:
            data = api.get_updates(marker=marker)
            marker = data.get("marker") or marker
            for update in data.get("updates", []):
                if is_bot_started_update(update):
                    handle_bot_started(api, update)
                    continue
                message = extract_message(update)
                if message:
                    handle_message(api, message, update)
                elif extract_text_or_payload(update):
                    handle_message(api, update, update)
                else:
                    log_unknown_update(update)
        except KeyboardInterrupt:
            print("MAX-бот остановлен.")
            break
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as exc:
            print(f"Ошибка polling: {exc}")
            time.sleep(5)


def main() -> None:
    run_polling()


if __name__ == "__main__":
    main()
