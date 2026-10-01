"""
Простое хранилище в JSON-файлах (data/*.json):
- meetings  — встречи, созданные ботом: id встречи → организация, эксперт, чат;
- processed — обработанные записи: uuid записи → статус, ссылки на документы.
"""

from __future__ import annotations

import json
import os
import threading

from config import DATA_DIR

_lock = threading.Lock()


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, f"{name}.json")


def load(name: str) -> dict:
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update(name: str, key: str, value: dict) -> None:
    """Записывает одну запись; файл перезаписывается атомарно."""
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        data = load(name)
        data[key] = {**data.get(key, {}), **value}
        tmp = _path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _path(name))
