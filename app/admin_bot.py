"""Helpers for notifying MAX admins from bot scenarios."""

from __future__ import annotations

import requests

from app import db
from app.max_api import MaxAPI, build_keyboard


def notify_admins(api: MaxAPI, text: str, keyboard: dict | None = None) -> None:
    for admin in db.notification_admins():
        user_id = str(admin.get("max_user_id") or "")
        chat_id = str(admin.get("chat_id") or "")
        sent = False
        if chat_id:
            try:
                api.send_message(text, chat_id=chat_id, keyboard=keyboard)
                print(f"Уведомление администратору {user_id} отправлено через chat_id={chat_id}.")
                sent = True
            except requests.exceptions.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code != 404 or not user_id:
                    print(f"Не удалось отправить уведомление администратору {user_id} через chat_id={chat_id}: {exc}")
                    continue
                print(f"chat_id={chat_id} для администратора {user_id} вернул 404, пробую user_id.")
            except Exception as exc:
                print(f"Не удалось отправить уведомление администратору {user_id} через chat_id={chat_id}: {exc}")
                continue
        if sent:
            continue
        if user_id:
            try:
                api.send_message(text, user_id=user_id, keyboard=keyboard)
                print(f"Уведомление администратору {user_id} отправлено через user_id.")
            except Exception as exc:
                print(f"Не удалось отправить уведомление администратору {user_id} через user_id: {exc}")


def notify_approvers(api: MaxAPI, text: str, admin_id: int) -> None:
    keyboard = build_keyboard(
        [
            [f"Одобрить как сотрудника #{admin_id}"],
            [f"Одобрить как начальника #{admin_id}"],
            [f"Отклонить #{admin_id}"],
        ]
    )
    for admin in db.approver_admins():
        chat_id = str(admin.get("chat_id") or "")
        user_id = str(admin.get("max_user_id") or "")
        try:
            if chat_id:
                api.send_message(text, chat_id=chat_id, keyboard=keyboard)
            elif user_id:
                api.send_message(text, user_id=user_id, keyboard=keyboard)
        except Exception as exc:
            print(f"Не удалось отправить заявку на доступ администратору {user_id}: {exc}")
