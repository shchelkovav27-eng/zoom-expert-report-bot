"""Московское время и русские названия дат — общие для бота и обработки записей."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def now_msk() -> datetime:
    return datetime.now(MSK).replace(tzinfo=None)


def fmt_date(d: date) -> str:
    return f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year}"
