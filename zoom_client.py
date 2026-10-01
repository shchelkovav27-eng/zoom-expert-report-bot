"""
Клиент Zoom API: получение токена (Server-to-Server OAuth) и создание встречи.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta

import httpx

from config import (
    JOIN_BEFORE_MIN,
    ZOOM_ACCOUNT_ID,
    ZOOM_AUTO_RECORDING,
    ZOOM_BACKUP_ACCOUNT_ID,
    ZOOM_BACKUP_CLIENT_ID,
    ZOOM_BACKUP_CLIENT_SECRET,
    ZOOM_BACKUP_HOST,
    ZOOM_CLIENT_ID,
    ZOOM_CLIENT_SECRET,
    ZOOM_HOST,
)

_API = "https://api.zoom.us/v2"

MAIN, BACKUP = "main", "backup"
LABELS = {MAIN: "Основной", BACKUP: "Резервный"}
ACCOUNTS = {MAIN: {"account": ZOOM_ACCOUNT_ID, "client": ZOOM_CLIENT_ID,
                   "secret": ZOOM_CLIENT_SECRET, "host": ZOOM_HOST}}
if ZOOM_BACKUP_ACCOUNT_ID and ZOOM_BACKUP_CLIENT_ID and ZOOM_BACKUP_CLIENT_SECRET:
    ACCOUNTS[BACKUP] = {"account": ZOOM_BACKUP_ACCOUNT_ID, "client": ZOOM_BACKUP_CLIENT_ID,
                        "secret": ZOOM_BACKUP_CLIENT_SECRET, "host": ZOOM_BACKUP_HOST}
_tokens = {key: {"value": "", "expires": 0.0} for key in (MAIN, BACKUP)}


def has_backup() -> bool:
    return BACKUP in ACCOUNTS


def host(acc: str = MAIN) -> str:
    return ACCOUNTS[acc]["host"]


class ZoomError(Exception):
    pass


async def _get_token(client: httpx.AsyncClient, acc: str = MAIN) -> str:
    # Токен живёт час — держим в памяти (для каждого аккаунта свой) и обновляем за минуту до истечения
    cfg, tok = ACCOUNTS[acc], _tokens[acc]
    if tok["value"] and time.time() < tok["expires"] - 60:
        return tok["value"]
    basic = base64.b64encode(f"{cfg['client']}:{cfg['secret']}".encode()).decode()
    r = await client.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials", "account_id": cfg["account"]},
        headers={"Authorization": f"Basic {basic}"},
    )
    if r.status_code != 200:
        raise ZoomError(f"Не удалось авторизоваться в Zoom, аккаунт «{LABELS[acc]}» "
                        f"({r.status_code}): {r.text[:300]}")
    data = r.json()
    tok["value"] = data["access_token"]
    tok["expires"] = time.time() + int(data.get("expires_in", 3600))
    return tok["value"]


async def list_upcoming(acc: str = MAIN) -> list:
    """Все предстоящие встречи организатора (включая созданные вручную)."""
    meetings, page_token = [], ""
    async with httpx.AsyncClient(timeout=20) as client:
        token = await _get_token(client, acc)
        while True:
            r = await client.get(
                f"{_API}/users/{host(acc)}/meetings",
                params={"type": "upcoming", "page_size": 300, "next_page_token": page_token},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                raise ZoomError(f"Не удалось получить список встреч, аккаунт «{LABELS[acc]}» "
                                f"({r.status_code}): {r.text[:300]}")
            data = r.json()
            meetings += data.get("meetings", [])
            page_token = data.get("next_page_token", "")
            if not page_token:
                return meetings


async def list_recordings(days: int, acc: str = MAIN) -> list:
    """Встречи с облачными записями за последние `days` дней (по одной на каждое проведение)."""
    today = datetime.utcnow().date()
    params = {"from": (today - timedelta(days=days)).isoformat(),
              "to": (today + timedelta(days=1)).isoformat(), "page_size": 300}
    meetings, page_token = [], ""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _get_token(client, acc)
        while True:
            r = await client.get(
                f"{_API}/users/{host(acc)}/recordings",
                params={**params, "next_page_token": page_token},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                raise ZoomError(f"Не удалось получить записи, аккаунт «{LABELS[acc]}» "
                                f"({r.status_code}): {r.text[:300]}")
            data = r.json()
            meetings += data.get("meetings", [])
            page_token = data.get("next_page_token", "")
            if not page_token:
                return meetings


async def download(url: str, dest: str, acc: str = MAIN) -> None:
    """Скачивает файл записи по download_url потоком на диск."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=600), follow_redirects=True) as client:
        token = await _get_token(client, acc)
        async with client.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status_code != 200:
                raise ZoomError(f"Не удалось скачать запись ({r.status_code})")
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)


async def _call(method: str, url: str, acc: str = MAIN, **kwargs) -> httpx.Response:
    """Запрос к Zoom. Если ключ устарел или выдан до добавления новых прав — берём свежий и повторяем."""
    async with httpx.AsyncClient(timeout=20) as client:
        for attempt in (1, 2):
            token = await _get_token(client, acc)
            r = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
            if attempt == 1 and (r.status_code == 401 or "does not contain scopes" in r.text):
                _tokens[acc]["value"] = ""
                continue
            return r


def _uuid_path(uuid: str) -> str:
    """UUID со слэшем в начале или двойным слэшем Zoom требует кодировать дважды."""
    from urllib.parse import quote
    return quote(quote(uuid, safe=""), safe="") if uuid.startswith("/") or "//" in uuid else uuid


async def recording_details(uuid: str, acc: str = MAIN) -> dict:
    """share_url и код записи (password) для одного проведения встречи."""
    r = await _call("GET", f"{_API}/meetings/{_uuid_path(uuid)}/recordings", acc)
    if r.status_code != 200:
        raise ZoomError(f"Не удалось получить данные записи ({r.status_code}): {_error_message(r)}")
    return r.json()


async def trash_recordings(uuid: str, acc: str = MAIN) -> None:
    """Отправляет все файлы записи этого проведения встречи в корзину Zoom (там они хранятся ещё 30 дней)."""
    r = await _call("DELETE", f"{_API}/meetings/{_uuid_path(uuid)}/recordings", acc, params={"action": "trash"})
    if r.status_code not in (200, 204, 404):
        raise ZoomError(f"Zoom не дал удалить запись ({r.status_code}): {_error_message(r)}")


def _error_message(r: httpx.Response) -> str:
    try:
        return r.json().get("message", r.text)[:300]
    except ValueError:
        return r.text[:300]


async def update_meeting(meeting_id, topic: str, start_msk: datetime, duration_min: int, acc: str = MAIN) -> None:
    """Меняет название, время и длительность. Ссылка и ID встречи остаются прежними."""
    payload = {"topic": topic[:200], "start_time": start_msk.strftime("%Y-%m-%dT%H:%M:%S"),
               "timezone": "Europe/Moscow", "duration": duration_min}
    r = await _call("PATCH", f"{_API}/meetings/{meeting_id}", acc, json=payload)
    if r.status_code != 204:
        raise ZoomError(f"Zoom не дал изменить встречу ({r.status_code}): {_error_message(r)}")


async def update_settings(meeting_id, settings: dict, acc: str = MAIN) -> None:
    """Меняет настройки уже созданной встречи (например, как рано можно заходить без организатора)."""
    r = await _call("PATCH", f"{_API}/meetings/{meeting_id}", acc, json={"settings": settings})
    if r.status_code != 204:
        raise ZoomError(f"Zoom не дал изменить настройки встречи ({r.status_code}): {_error_message(r)}")


async def delete_meeting(meeting_id, acc: str = MAIN) -> None:
    """Удаляет встречу. Если её уже нет в Zoom — считаем, что удалена."""
    r = await _call("DELETE", f"{_API}/meetings/{meeting_id}", acc, params={"schedule_for_reminder": "false"})
    if r.status_code not in (204, 404):
        raise ZoomError(f"Zoom не дал удалить встречу ({r.status_code}): {_error_message(r)}")


async def create_meeting(topic: str, start_msk: datetime, duration_min: int, acc: str = MAIN) -> dict:
    """Создаёт запланированную встречу. start_msk — время по Москве (без tzinfo).

    Возвращает ответ Zoom: id, join_url, password, start_time и т.д.
    """
    payload = {
        "topic": topic[:200],
        "type": 2,  # запланированная встреча с отдельным ID (не личная комната)
        "start_time": start_msk.strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": "Europe/Moscow",
        "duration": duration_min,
        "settings": {
            "use_pmi": False,
            "auto_recording": ZOOM_AUTO_RECORDING,
            # Вход всем: без зала ожидания, без регистрации и авторизации,
            # можно зайти раньше организатора
            "waiting_room": False,
            "join_before_host": True,
            "jbh_time": JOIN_BEFORE_MIN,  # не раньше чем за 15 минут: ранний заход включал автозапись
            "meeting_authentication": False,
            "approval_type": 2,
            "host_video": True,
            "participant_video": True,
            "mute_upon_entry": False,
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        token = await _get_token(client, acc)
        r = await client.post(
            f"{_API}/users/{host(acc)}/meetings",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code != 201:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise ZoomError(f"Zoom вернул ошибку, аккаунт «{LABELS[acc]}» ({r.status_code}): {msg[:300]}")
    return r.json()
