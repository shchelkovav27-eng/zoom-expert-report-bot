"""
Расшифровка через Transkriptor API (https://api.tor.app):
get_upload_url → PUT файла → initiate_transcription → опрос content.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import time

import requests

from config import TRANSCRIPTOR_API_KEY

logger = logging.getLogger(__name__)

BASE = "https://api.tor.app/developer"


class TranscribeError(Exception):
    pass


def _dig(obj, *names):
    """Первое непустое из полей names — на верхнем уровне или внутри body."""
    for src in (obj, obj.get("body") if isinstance(obj, dict) else None):
        if isinstance(src, dict):
            for n in names:
                if src.get(n) not in (None, ""):
                    return src[n]
    return None


def _check(r: requests.Response, what: str) -> dict:
    if r.status_code >= 400:
        raise TranscribeError(f"Transkriptor: не удалось {what} ({r.status_code}): {r.text[:300]}")
    return r.json() if r.content else {}


def transcribe(path: str, lang: str = "ru-RU", timeout_min: int = 120) -> list:
    """Файл → список реплик [{"speaker": str|None, "text": str}]."""
    if not TRANSCRIPTOR_API_KEY:
        raise TranscribeError("Не задан TRANSCRIPTOR_API_KEY в .env")
    auth = {"Authorization": f"Bearer {TRANSCRIPTOR_API_KEY}"}
    name = os.path.basename(path)

    j = _check(requests.post(f"{BASE}/transcription/local_file/get_upload_url", headers=auth,
                             json={"file_name": name}, timeout=60), "получить адрес загрузки")
    upload_url, public_url = _dig(j, "upload_url", "uploadUrl", "url"), _dig(j, "public_url", "publicUrl")
    if not upload_url or not public_url:
        raise TranscribeError("Transkriptor: в ответе нет адреса загрузки")

    logger.info("Transkriptor: загружаю %s (%d МБ)", name, os.path.getsize(path) >> 20)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        r = requests.put(upload_url, data=f, headers={"Content-Type": ctype}, timeout=1800)
    if r.status_code >= 400:
        raise TranscribeError(f"Transkriptor: файл не загрузился ({r.status_code})")

    j = _check(requests.post(f"{BASE}/transcription/local_file/initiate_transcription", headers=auth,
                             json={"url": public_url, "language": lang, "service": "Standard"}, timeout=60),
               "запустить расшифровку")
    order_id = _dig(j, "order_id", "orderId", "id")
    if not order_id:
        raise TranscribeError("Transkriptor: в ответе нет номера заказа")
    logger.info("Transkriptor: заказ %s, жду результат", order_id)

    deadline, delay = time.time() + timeout_min * 60, 15
    while time.time() < deadline:
        time.sleep(delay)
        delay = min(delay + 5, 60)
        r = requests.get(f"{BASE}/files/{order_id}/content",
                         headers={**auth, "Accept": "application/json"}, timeout=60)
        if r.status_code != 200:
            continue
        content = _dig(r.json(), "content")
        if not content:
            continue
        segments = []
        for seg in content:
            if isinstance(seg, dict):
                text = str(seg.get("text", "")).strip()
                speaker = seg.get("Speaker") or seg.get("speaker")
            else:
                text, speaker = str(seg).strip(), None
            if text:
                segments.append({"speaker": speaker, "text": text})
        if not segments:
            raise TranscribeError("Transkriptor вернул пустую расшифровку (речь не распознана)")
        return segments
    raise TranscribeError("Transkriptor: расшифровка не пришла вовремя")


def as_text(segments: list) -> str:
    """Реплики одного спикера подряд склеиваем в абзац."""
    lines, last = [], object()
    for s in segments:
        if s["speaker"] and s["speaker"] != last:
            lines.append(f"\n{s['speaker']}: {s['text']}")
        else:
            lines.append(s["text"])
        last = s["speaker"]
    return "\n".join(lines).strip()
