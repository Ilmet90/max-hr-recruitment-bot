from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Generator

import requests


DEFAULT_BASE_URL = "https://platform-api.max.ru"


class MaxApiError(RuntimeError):
    pass


def build_keyboard(buttons: list[list[str]] | list[str]) -> dict[str, Any]:
    rows = buttons if buttons and isinstance(buttons[0], list) else [buttons]  # type: ignore[index]
    return {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [{"type": "message", "text": str(button)} for button in row]
                for row in rows  # type: ignore[union-attr]
                if row
            ]
        },
    }


class MaxAPI:
    def __init__(self, token: str, base_url: str | None = None, timeout: int = 45) -> None:
        self.token = token
        self.base_url = (base_url or os.getenv("MAX_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        params = kwargs.pop("params", {}) or {}
        headers = kwargs.pop("headers", {}) or {}
        request_timeout = kwargs.pop("request_timeout", self.timeout)
        headers["Authorization"] = self.token
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            headers=headers,
            timeout=request_timeout,
            **kwargs,
        )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def _send_message_payload(self, params: dict[str, str | None], payload: dict[str, Any]) -> Any:
        transient_errors = (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        )
        try:
            return self._request("POST", "/messages", params=params, json=payload)
        except transient_errors:
            return self._request("POST", "/messages", params=params, json=payload)
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code and 500 <= status_code < 600:
                return self._request("POST", "/messages", params=params, json=payload)
            raise

    def send_message(
        self,
        *args: str,
        text: str | None = None,
        chat_id: str | None = None,
        user_id: str | None = None,
        keyboard: dict[str, Any] | None = None,
    ) -> Any:
        if len(args) == 2:
            chat_id, text = args
        elif len(args) == 1:
            text = args[0]
        elif len(args) > 2:
            raise MaxApiError("send_message принимает text и один из chat_id/user_id")
        if not chat_id and not user_id:
            raise MaxApiError("Для отправки сообщения нужен chat_id или user_id")
        if text is None:
            raise MaxApiError("Для отправки сообщения нужен text")
        params = {"chat_id": chat_id} if chat_id else {"user_id": user_id}
        payload = {"text": text}
        if keyboard:
            payload["attachments"] = [keyboard]
        try:
            return self._send_message_payload(params, payload)
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if keyboard and status_code == 400:
                print("MAX API отклонил клавиатуру, повторяю отправку сообщения без кнопок.")
                return self._send_message_payload(params, {"text": text})
            raise

    def upload_image(self, path: str) -> dict[str, Any]:
        image_path = Path(path)
        if not image_path.exists() or not image_path.is_file():
            raise MaxApiError(f"Файл изображения не найден: {path}")
        try:
            upload_info = self._request("POST", "/uploads", params={"type": "image"})
        except Exception as exc:
            print(f"MAX image upload: не удалось получить upload URL: {exc}")
            raise
        upload_url = (upload_info or {}).get("url")
        if not upload_url:
            raise MaxApiError(f"MAX image upload: upload URL отсутствует в ответе: {upload_info}")
        try:
            with image_path.open("rb") as file:
                response = self.session.post(upload_url, files={"data": file}, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            print(f"MAX image upload: не удалось загрузить файл на upload URL: {exc}")
            raise

    def _send_image_attachment(self, params: dict[str, str | None], payload: dict[str, Any]) -> Any:
        delay = 1
        for attempt in range(4):
            try:
                return self._send_message_payload(params, payload)
            except requests.exceptions.HTTPError as exc:
                response = exc.response
                text = response.text if response is not None else ""
                if "attachment.not.ready" in text and attempt < 3:
                    time.sleep(delay)
                    delay += 1
                    continue
                print(f"MAX image send: не удалось отправить attachment: {exc}")
                raise
        return None

    def send_photo(
        self,
        chat_id: str | None = None,
        user_id: str | None = None,
        path: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
    ) -> bool:
        image_path = path or filename
        if not image_path:
            raise MaxApiError("Для отправки фото нужен path")
        if not chat_id and not user_id:
            raise MaxApiError("Для отправки фото нужен chat_id или user_id")
        try:
            upload_payload = self.upload_image(image_path)
            params = {"chat_id": chat_id} if chat_id else {"user_id": user_id}
            message_payload = {
                "text": caption or "",
                "attachments": [{"type": "image", "payload": upload_payload}],
            }
            self._send_image_attachment(params, message_payload)
            return True
        except Exception as exc:
            print(f"MAX image send: fallback после ошибки: {exc}")
            try:
                self.send_message("Не удалось отправить фотографию. Попробуйте позже.", chat_id=chat_id, user_id=user_id)
            except Exception as send_exc:
                print(f"MAX image send: не удалось отправить fallback-сообщение: {send_exc}")
            return False

    def get_updates(self, marker: str | None = None, limit: int = 50, timeout: int = 30) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "timeout": timeout}
        if marker:
            params["marker"] = marker
        return self._request("GET", "/updates", params=params, request_timeout=max(self.timeout, timeout + 10))

    def get_updates_long_polling(self, marker: str | None = None) -> Generator[dict[str, Any], None, None]:
        current_marker = marker
        while True:
            data = self.get_updates(marker=current_marker)
            current_marker = data.get("marker") or current_marker
            for update in data.get("updates", []):
                yield update
