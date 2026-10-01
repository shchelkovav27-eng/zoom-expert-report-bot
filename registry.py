"""
Реестр встреч — Google-таблица «Реестр встреч» в общей папке. Одна строка на встречу, ключ — ID встречи Zoom.
Строка всегда собирается целиком из data/meetings.json, поэтому запись идемпотентна: sync_all() можно
вызывать сколько угодно раз.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from googleapiclient.discovery import build

import gdrive
import storage
from config import EXPERTS, GOOGLE_DRIVE_ROOT_ID
from timeutil import now_msk

logger = logging.getLogger(__name__)

TITLE = "Реестр встреч"
SHEET = "Встречи"
HEADERS = ["Дата", "Время (МСК)", "Эксперт", "Организация", "Модуль", "Встреча", "Статус",
           "Отчёт эксперта", "Расшифровка", "Видео на Диске", "Запись Zoom", "Код записи",
           "Аккаунт Zoom", "ID встречи Zoom"]
WIDTHS = [95, 95, 170, 230, 70, 75, 250, 120, 110, 120, 150, 110, 120, 130]
ID_COL = len(HEADERS)  # последний столбец

PLANNED, WAITING, NO_RECORD, DONE, CANCELLED = (
    "🗓 Запланирована", "⏳ Прошла, запись обрабатывается", "⚠️ Прошла, записи нет", "✅ Прошла", "❌ Отменена")
BLOCK_RE = re.compile(r"Модуль\s*(\d+)\D+?встреча\s*(\d+)", re.I)


def _sheets():
    return build("sheets", "v4", credentials=gdrive.credentials(), cache_discovery=False)


def status_of(info: dict) -> str:
    if info.get("status") == "deleted":
        return CANCELLED
    if info.get("report_link") or (info.get("kind") == "general" and info.get("transcript_link")):
        return DONE  # у общих встреч (мастер-майндов) отчёта нет — достаточно расшифровки
    end = datetime.fromisoformat(info["start"]) + timedelta(minutes=int(info.get("duration") or 60))
    if end > now_msk():
        return PLANNED
    return WAITING if now_msk() - end < timedelta(days=1) else NO_RECORD


def _link(url: str, label: str) -> str:
    return f'=HYPERLINK("{url}"; "{label}")' if url else ""


def _row(mid: str, info: dict) -> list:
    start = datetime.fromisoformat(info["start"])
    module, number = info.get("module"), info.get("number")
    if not module:
        found = BLOCK_RE.search(info.get("block") or info.get("topic", ""))
        module, number = (found[1], found[2]) if found else ("", "")
    if info.get("part"):
        number = f"{number} (продолжение {info['part']})"
    return [
        start.strftime("%Y-%m-%d"), start.strftime("%H:%M"),
        info.get("expert_label") or EXPERTS.get(info.get("org", ""), ""),
        info.get("org") or info.get("topic", ""), module, number, status_of(info),
        _link(info.get("report_link", ""), "Отчёт"),
        _link(info.get("transcript_link", ""), "Расшифровка"),
        _link(info.get("video_link", ""), "Видео"),
        "перенесена на Диск" if info.get("zoom_trashed") else _link(info.get("recording_url", ""), "Запись"),
        "" if info.get("zoom_trashed") or not info.get("recording_code") else "'" + info["recording_code"],
        {"backup": "Резервный", "own": "Своя запись"}.get(info.get("account"), "Основной"),
        "'" + mid,  # апостроф — чтобы длинный ID остался текстом, а не превратился в 8,47E+10
    ]


def _sheet_id() -> str:
    """ID таблицы; при первом вызове создаёт её в общей папке и оформляет."""
    saved = storage.load("registry").get("sheet", {}).get("id")
    if saved:
        return saved
    drive = gdrive.service()
    found = drive.files().list(
        q=f"'{GOOGLE_DRIVE_ROOT_ID}' in parents and name = '{TITLE}' and trashed = false "
          "and mimeType = 'application/vnd.google-apps.spreadsheet'", fields="files(id)",
    ).execute().get("files", [])
    if found:
        sid = found[0]["id"]
    else:
        sid = drive.files().create(body={"name": TITLE, "parents": [GOOGLE_DRIVE_ROOT_ID],
                                         "mimeType": "application/vnd.google-apps.spreadsheet"},
                                   fields="id").execute()["id"]
        _format(sid)
    storage.update("registry", "sheet", {"id": sid})
    return sid


def _date_formats(gid: int) -> list:
    """Формат столбцов «Дата» и «Время» — задаём на весь столбец при каждой записи,
    иначе вставленные строки наследуют формат шапки и показывают даты числами."""
    body = {"sheetId": gid, "startRowIndex": 1}
    return [
        {"repeatCell": {"range": {**body, "startColumnIndex": 0, "endColumnIndex": 1},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {**body, "startColumnIndex": 1, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "TIME", "pattern": "hh:mm"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
    ]


def _format(sid: str) -> None:
    api = _sheets().spreadsheets()
    gid = api.get(spreadsheetId=sid).execute()["sheets"][0]["properties"]["sheetId"]
    api.values().update(spreadsheetId=sid, range=f"A1:{chr(64 + ID_COL)}1",
                        valueInputOption="RAW", body={"values": [HEADERS]}).execute()
    col = lambda i: {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1}
    requests = [
        {"updateSpreadsheetProperties": {"properties": {"locale": "ru_RU", "timeZone": "Europe/Moscow"},
                                         "fields": "locale,timeZone"}},
        {"updateSheetProperties": {"properties": {"sheetId": gid, "title": SHEET,
                                                  "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "title,gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {
                            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                            "backgroundColor": {"red": 0.035, "green": 0.247, "blue": 0.294},
                            "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
                        "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment,wrapStrategy)"}},
        {"setBasicFilter": {"filter": {"range": {"sheetId": gid, "startRowIndex": 0,
                                                 "startColumnIndex": 0, "endColumnIndex": ID_COL}}}},
    ] + [{"updateDimensionProperties": {"range": col(i), "properties": {"pixelSize": w}, "fields": "pixelSize"}}
         for i, w in enumerate(WIDTHS)]
    for text, color in ((DONE, (0.85, 0.94, 0.85)), (CANCELLED, (0.93, 0.93, 0.93)),
                        (NO_RECORD, (0.99, 0.91, 0.8)), (WAITING, (1, 0.97, 0.8))):
        requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": gid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": ID_COL}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f'=$G2="{text}"'}]},
                            "format": {"backgroundColor": dict(zip(("red", "green", "blue"), color))}}}}})
    api.batchUpdate(spreadsheetId=sid, body={"requests": requests}).execute()


def _migrate(sid: str, api) -> bool:
    """Если набор столбцов поменялся (добавили новый) — переписываем шапку и все строки заново."""
    head = api.values().get(spreadsheetId=sid, range=f"{SHEET}!1:1").execute().get("values", [[]])[0]
    if head == HEADERS:
        return False
    gid = api.get(spreadsheetId=sid).execute()["sheets"][0]["properties"]["sheetId"]
    api.values().clear(spreadsheetId=sid, range=f"{SHEET}!A1:Z").execute()
    api.values().update(spreadsheetId=sid, range=f"{SHEET}!A1", valueInputOption="RAW",
                        body={"values": [HEADERS]}).execute()
    col = lambda i: {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1}
    api.batchUpdate(spreadsheetId=sid, body={"requests": [
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 0, "endColumnIndex": ID_COL},
                        "cell": {"userEnteredFormat": {
                            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                            "backgroundColor": {"red": 0.035, "green": 0.247, "blue": 0.294},
                            "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
                        "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment,wrapStrategy)"}},
        {"setBasicFilter": {"filter": {"range": {"sheetId": gid, "startRowIndex": 0,
                                                 "startColumnIndex": 0, "endColumnIndex": ID_COL}}}},
    ] + [{"updateDimensionProperties": {"range": col(i), "properties": {"pixelSize": w}, "fields": "pixelSize"}}
         for i, w in enumerate(WIDTHS)]}).execute()
    return True


def sync(mids) -> None:
    """Записывает строки указанных встреч (добавляет новые, обновляет существующие), затем сортирует по дате."""
    meetings = storage.load("meetings")
    mids = [str(m) for m in mids if str(m) in meetings]
    if not mids:
        return
    sid, api = _sheet_id(), _sheets().spreadsheets()
    if _migrate(sid, api):
        mids = list(meetings)
    ids = api.values().get(spreadsheetId=sid, range=f"{SHEET}!{chr(64 + ID_COL)}:{chr(64 + ID_COL)}").execute()
    row_of = {str(r[0]).lstrip("'"): n for n, r in enumerate(ids.get("values", []), 1) if r}
    updates, appends = [], []
    for mid in mids:
        row = _row(mid, meetings[mid])
        if mid in row_of:
            updates.append({"range": f"{SHEET}!A{row_of[mid]}", "values": [row]})
        else:
            appends.append(row)
    if updates:
        api.values().batchUpdate(spreadsheetId=sid, body={"valueInputOption": "USER_ENTERED", "data": updates}).execute()
    if appends:
        api.values().append(spreadsheetId=sid, range=f"{SHEET}!A1", valueInputOption="USER_ENTERED",
                            insertDataOption="OVERWRITE", body={"values": appends}).execute()
    gid = api.get(spreadsheetId=sid).execute()["sheets"][0]["properties"]["sheetId"]
    api.batchUpdate(spreadsheetId=sid, body={"requests": _date_formats(gid) + [{"sortRange": {
        "range": {"sheetId": gid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": ID_COL},
        "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}, {"dimensionIndex": 1, "sortOrder": "ASCENDING"}],
    }}]}).execute()
    for mid in mids:
        storage.update("meetings", mid, {"registry_status": status_of(meetings[mid])})


def sync_changed() -> None:
    """Обновляет строки, у которых сменился статус (например, встреча закончилась). Для фоновой проверки."""
    meetings = storage.load("meetings")
    sync([mid for mid, info in meetings.items() if info.get("registry_status") != status_of(info)])


def sync_all() -> None:
    sync(list(storage.load("meetings")))


def url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{_sheet_id()}"
