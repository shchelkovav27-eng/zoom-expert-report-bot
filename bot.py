"""
Telegram-бот для групп: по команде /meet спрашивает дату, время (МСК), длительность,
организацию, модуль и номер встречи, создаёт встречу в Zoom (автозапись, отдельная ссылка,
вход всем) и присылает карточку со ссылкой обратно в чат.

Фоном (pipeline.py) забирает готовые записи из облака Zoom, расшифровывает, заполняет
отчёт по форме, кладёт на Google Диск в папку организации и присылает ссылку в группу.

Каждый участник группы оформляет свою встречу независимо: кнопки чужого мастера
не срабатывают.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import gdrive
import pipeline
import registry
import storage
import zoom_client
from config import (
    ALLOWED_CHAT_IDS,
    ORGANIZATIONS,
    TELEGRAM_BASE_URL,
    TELEGRAM_BOT_TOKEN,
    ZOOM_AUTO_RECORDING,
)
from timeutil import MSK, WEEKDAYS, fmt_date, now_msk

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
# httpx на INFO печатает URL с токеном бота — глушим
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

HOURS = range(8, 23)
MINUTES = (0, 15, 30, 45)
DURATIONS = (30, 60, 90, 120, 150, 180, 240, 300, 360)
MODULES = (1, 2, 3, 4)
MEETING_NUMBERS = (1, 2, 3)
DAYS_AHEAD = 15
TOTAL_STEPS = 7


# ---------- форматирование ----------

def fmt_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    parts = []
    if h:
        parts.append(f"{h} ч")
    if m:
        parts.append(f"{m} мин")
    return " ".join(parts)


def fmt_meeting_id(mid) -> str:
    s = str(mid)
    if len(s) == 11:
        return f"{s[:3]} {s[3:7]} {s[7:]}"
    if len(s) == 10:
        return f"{s[:3]} {s[3:6]} {s[6:]}"
    return s


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


def block_label(s: dict) -> str:
    label = f"Модуль {s['module']}, встреча {s['number']}"
    return f"{label} (продолжение {s['part']})" if s.get("part") else label


def earlier_meetings(s: dict) -> list:
    """Уже созданные встречи этой организации с тем же модулем и номером — от старых к новым."""
    same = [info for info in storage.load("meetings").values()
            if info.get("status") != "deleted" and info.get("kind") != "general"
            and info.get("org") == s.get("org") and info.get("module") == s.get("module")
            and info.get("number") == s.get("number")]
    return sorted(same, key=lambda i: i["start"])


def meeting_title(s: dict) -> str:
    """«НКО «Пример» — Модуль 2, встреча 1 — тема». По началу названия обработка записей узнаёт организацию.
    У общих встреч программы (мастер-майнды) название своё и не меняется."""
    if s.get("general_title"):
        return s["general_title"]
    parts = [s["org"], block_label(s)]  # пометка «(продолжение N)» уже внутри block_label
    if s.get("topic"):
        parts.append(s["topic"])
    return " — ".join(parts)


def start_dt(s: dict) -> datetime:
    return datetime.combine(s["date"], datetime.min.time()).replace(hour=s["hour"], minute=s["minute"])


# ---------- занятость ----------

async def load_busy(s: dict) -> None:
    """Занятые интервалы по МСК для каждого аккаунта: [(начало, конец, название, id, аккаунт)]."""
    busy, failed = [], set()
    for acc in zoom_client.ACCOUNTS:
        try:
            items = await zoom_client.list_upcoming(acc)
        except Exception as e:
            # Сбой на резервном аккаунте не мешает работать на основном: пишем одной строкой, без трассировки
            logger.warning("Расписание Zoom недоступно (аккаунт %s): %s", acc, str(e)[:200])
            failed.add(acc)
            continue
        for m in items:
            if not m.get("start_time") or not m.get("duration"):
                continue
            start = datetime.fromisoformat(m["start_time"].replace("Z", "+00:00")).astimezone(MSK).replace(tzinfo=None)
            busy.append((start, start + timedelta(minutes=int(m["duration"])), m.get("topic", ""), str(m.get("id")), acc))
    s["busy"], s["busy_failed"] = sorted(busy), sorted(failed)


def account_of(s: dict) -> str:
    return s.get("account") or zoom_client.MAIN


def conflicts(s: dict, acc: str = None) -> list:
    start = start_dt(s)
    end = start + timedelta(minutes=s["duration"])
    acc = acc or account_of(s)
    return [b for b in s.get("busy", [])
            if b[4] == acc and b[0] < end and start < b[1] and b[3] != s.get("edit_id")]


def backup_is_free(s: dict) -> bool:
    """Занятый на основном слот запомнен, резервный аккаунт настроен и на это время там свободно."""
    slot = s.get("last_slot")
    if not (slot and zoom_client.has_backup() and account_of(s) == zoom_client.MAIN
            and zoom_client.BACKUP not in (s.get("busy_failed") or [])):
        return False
    probe = {**s, **slot}
    return start_dt(probe) > now_msk() and not conflicts(probe, zoom_client.BACKUP)


def fmt_busy(b) -> str:
    return f"{b[0]:%H:%M}–{b[1]:%H:%M} «{html.escape(b[2])}»"


def check_slot(s: dict) -> bool:
    """В режиме изменения после смены даты/времени/длительности — сразу к подтверждению,
    если время в будущем и свободно. Иначе — обратно к выбору часа с объяснением."""
    if start_dt(s) <= now_msk():
        s["notice"] = "⛔ Это время уже прошло — выберите другое."
        s.pop("minute", None)
        s["step"] = "hour"
        return False
    found = conflicts(s)
    if found:
        mark_busy(s, found)
        return False
    s["step"] = "confirm"
    return True


def mark_busy(s: dict, found: list) -> None:
    """Время занято — возвращаем на выбор часа с объяснением. Слот запоминаем: его можно отдать резерву."""
    s["last_slot"] = {"hour": s.get("hour"), "minute": s.get("minute"), "duration": s.get("duration")}
    s["notice"] = ("⛔ <b>Это время занято:</b>\n" + "\n".join(fmt_busy(b) for b in found)
                   + "\nВыберите другое время или дату — или поставьте эту встречу на резервный аккаунт.")
    s.pop("minute", None)
    s["step"] = "hour"


# ---------- разбор ручного ввода ----------

def parse_date(text: str, today: date) -> Optional[date]:
    t = text.strip().lower()
    if t == "сегодня":
        return today
    if t == "завтра":
        return today + timedelta(days=1)
    if t == "послезавтра":
        return today + timedelta(days=2)
    try:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?", t)
        if m:
            day, month = int(m[1]), int(m[2])
            if m[3]:
                year = int(m[3])
                return date(year + 2000 if year < 100 else year, month, day)
            d = date(today.year, month, day)
            # «05.01» в декабре — это следующий год
            return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None
    return None


def parse_time(text: str) -> Optional[Tuple[int, int]]:
    m = re.fullmatch(r"(\d{1,2})(?:[:.\s-]?(\d{2}))?", text.strip())
    if not m:
        return None
    h, mi = int(m[1]), int(m[2] or 0)
    return (h, mi) if h < 24 and mi < 60 else None


def parse_duration(text: str) -> Optional[int]:
    """Возвращает минуты. «1.5», «1,5 ч», «90 мин», «1:30». Число без единиц ≤ 12 — часы."""
    t = text.strip().lower().replace(",", ".")
    minutes = None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        minutes = int(m[1]) * 60 + int(m[2])
    m = m or re.fullmatch(r"(\d+)\s*(м|мин|мин\.|минут|минуты|min)", t)
    if m and minutes is None:
        minutes = int(m[1])
    m = m or re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ч|ч\.|час|часа|часов|h)?", t)
    if m and minutes is None:
        value = float(m[1])
        minutes = round(value * 60) if (m[2] or value <= 12) else round(value)
    if minutes is None or not 15 <= minutes <= 24 * 60:
        return None
    return minutes


# ---------- шаги мастера ----------

def _kb(owner: int, rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text, callback_data=f"m|{owner}|{action}|{value}") for text, action, value in row]
        for row in rows if row
    ])


def _chunks(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def _summary(s: dict) -> str:
    lines = []
    if s.get("date"):
        lines.append(f"📅 {fmt_date(s['date'])}")
    if s.get("hour") is not None and s.get("minute") is not None:
        lines.append(f"🕒 {s['hour']:02d}:{s['minute']:02d} по Москве")
    if s.get("duration"):
        lines.append(f"⏱ {fmt_duration(s['duration'])}")
    if s.get("org"):
        lines.append(f"🏢 {html.escape(s['org'])}")
    if s.get("module"):
        lines.append(f"📚 Модуль {s['module']}" + (f", встреча {s['number']}" if s.get("number") else ""))
    if s.get("number") or s.get("general_title"):
        lines.append(f"📌 {html.escape(meeting_title(s))}")
    if account_of(s) != zoom_client.MAIN:
        lines.append("🗂 Резервный аккаунт Zoom")
    return "\n".join(lines)


def _step(s: dict, n: int) -> str:
    return "" if s.get("edit_id") else f"<b>Шаг {n} из {TOTAL_STEPS}.</b> "


def is_upload(s: dict) -> bool:
    """Сессия «у меня свои записи»: встречу в Zoom не создаём, ждём файлы от эксперта."""
    return s.get("mode") == "upload"


def render(s: dict) -> Tuple[str, InlineKeyboardMarkup]:
    owner, step = s["owner"], s["step"]
    upload = is_upload(s)
    title = ("📼 <b>Своя запись встречи</b>" if upload else
             "✏️ <b>Изменение встречи</b>" if s.get("edit_id") else "🗓 <b>Новая встреча в Zoom</b>")
    head = f"{title} · {s['mention']}"
    summary = _summary(s)
    if summary:
        head += "\n\n" + summary
    if s.get("notice"):
        head += "\n\n" + s.pop("notice")
    cancel = ("✖ Отмена", "cancel", "")

    if step == "date":
        today = now_msk().date()
        days = []
        for i in range(DAYS_AHEAD):
            d = today - timedelta(days=i) if upload else today + timedelta(days=i)
            label = "Сегодня" if i == 0 else ("Вчера" if upload else "Завтра") if i == 1 \
                else f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"
            days.append((label, "date", d.isoformat()))
        rows = _chunks(days, 3) + [[("✏️ Другая дата", "date_in", ""), cancel]]
        question = "Когда прошла встреча?" if upload else "На какую дату поставить встречу?"
        return head + "\n\n" + _step(s, 1) + question, _kb(owner, rows)

    if step == "hour":
        now = now_msk()
        hours = list(HOURS) if upload else [h for h in HOURS if s["date"] > now.date() or h > now.hour]
        buttons = [(f"{h:02d}:__", "hour", str(h)) for h in hours]
        rows = _chunks(buttons, 5)
        if backup_is_free(s):
            slot = s["last_slot"]
            rows.append([(f"📅 {slot['hour']:02d}:{slot['minute']:02d} на резервном аккаунте Zoom",
                          "acct", zoom_client.BACKUP)])
        rows.append([("✏️ Точное время", "time_in", ""), ("◀ Назад", "back", "date")])
        text = _step(s, 2) + ("Во сколько начиналась встреча (по Москве)? Выберите час."
                              if upload else "Во сколько начало (по Москве)? Выберите час.")
        if not hours:
            text += "\nНа сегодня стандартные часы уже прошли — введите время вручную или выберите другую дату."
        if upload:  # своя запись: занятость Zoom тут ни при чём
            return head + "\n\n" + text, _kb(owner, rows)
        day_busy = [b for b in s.get("busy", [])
                    if b[4] == account_of(s) and b[0].date() <= s["date"] <= b[1].date()]
        if account_of(s) in (s.get("busy_failed") or []):
            text += "\n\n⚠️ Не удалось проверить занятость в Zoom — сверьтесь с расписанием сами."
        elif day_busy:
            text += "\n\n🔴 <b>Уже занято в этот день:</b>\n" + "\n".join(fmt_busy(b) for b in day_busy)
        else:
            text += "\n\n🟢 В этот день других встреч нет."
        return head + "\n\n" + text, _kb(owner, rows)

    if step == "minute":
        buttons = [(f"{s['hour']:02d}:{m:02d}", "min", str(m)) for m in MINUTES]
        rows = [buttons, [("◀ Назад", "back", "hour")]]
        return head + "\n\n" + _step(s, 2) + "Уточните минуты:", _kb(owner, rows)

    if step == "duration":
        buttons = [(fmt_duration(d), "dur", str(d)) for d in DURATIONS]
        rows = _chunks(buttons, 3) + [[("✏️ Своя длительность", "dur_in", ""), ("◀ Назад", "back", "hour")]]
        return head + "\n\n" + _step(s, 3) + "На сколько часов поставить встречу?", _kb(owner, rows)

    if step == "org":
        rows = [[(org, "org", str(i))] for i, org in enumerate(ORGANIZATIONS)]
        rows.append([("◀ Назад", "back", "minute" if upload else "duration")])
        return head + "\n\n" + _step(s, 4) + "С какой организацией встреча?", _kb(owner, rows)

    if step == "module":
        rows = [[(f"Модуль {m}", "mod", str(m)) for m in MODULES[:2]],
                [(f"Модуль {m}", "mod", str(m)) for m in MODULES[2:]],
                [("◀ Назад", "back", "org")]]
        return head + "\n\n" + _step(s, 5) + "Какой модуль?", _kb(owner, rows)

    if step == "number":
        rows = [[(f"Встреча {k}", "num", str(k)) for k in MEETING_NUMBERS], [("◀ Назад", "back", "module")]]
        return head + "\n\n" + _step(s, 6) + "Какая по счёту встреча в модуле?", _kb(owner, rows)

    if step == "cont":
        was = "\n".join(f"• {datetime.fromisoformat(i['start']):%d.%m.%Y в %H:%M}" for i in earlier_meetings(s))
        rows = [[("✅ Да, это продолжение", "cont", "1")], [("Нет, отдельная встреча", "cont", "0")],
                [("◀ Назад", "back", "number")]]
        return (head + f"\n\nТакая встреча уже была:\n{was}\n\nЭто её продолжение? "
                "Прошлые отчёт, расшифровка и видео сохранятся, новые лягут рядом с пометкой «продолжение»."), _kb(owner, rows)

    # confirm
    topic_btn = ("✏️ Изменить тему" if s.get("topic") else "✏️ Добавить тему", "topic", "")
    if s.get("edit_id"):
        rows = [
            [("✅ Сохранить изменения", "create", "")],
            [("📅 Дата", "back", "date"), ("🕒 Время", "back", "hour"), ("⏱ Длительность", "back", "duration")],
            [] if s.get("general_title") else [("🏢 Организация и модуль", "back", "org"), topic_btn],
            [cancel],
        ]
        return (head + "\n\nЧто изменить? Выберите, что поправить, и нажмите «Сохранить». "
                "Ссылка на встречу останется прежней."), _kb(owner, rows)
    rows = [
        [("📁 Создать папку для записей", "create", "") if upload else ("✅ Создать встречу", "create", "")],
        [topic_btn, ("◀ Назад", "back", "number")],
        [("🔁 Заново", "restart", ""), cancel],
    ]
    if upload:
        return (head + "\n\nВсё верно? Сделаю на Диске папку для ваших записей и пришлю ссылку. "
                "Можно положить туда несколько файлов — все они пойдут в один отчёт."), _kb(owner, rows)
    return (head + "\n\n<b>Шаг " + str(TOTAL_STEPS) + " из " + str(TOTAL_STEPS) + ".</b> Всё верно? "
            "Можно добавить к названию тему."
            "\nЗапись включится автоматически, вход — для всех по ссылке."), _kb(owner, rows)


PROMPTS = {
    "date": ("напишите дату ответом на это сообщение — например <code>25.09</code> или <code>25.09.2026</code>.", "25.09"),
    "time": ("напишите время начала по Москве ответом на это сообщение — например <code>15:30</code>.", "15:30"),
    "dur": ("напишите длительность ответом на это сообщение — например <code>1.5</code> (часа) или <code>45 мин</code>.", "1.5"),
    "topic": ("напишите тему встречи ответом на это сообщение — она добавится к названию.", "Тема встречи"),
}


# ---------- служебное ----------

def sessions(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.chat_data.setdefault("sessions", {})


def chat_allowed(update: Update) -> bool:
    chat = update.effective_chat
    return chat.type == ChatType.PRIVATE or not ALLOWED_CHAT_IDS or chat.id in ALLOWED_CHAT_IDS


async def _safe_delete(context, chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await context.bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def _send_wizard(context, chat_id: int, s: dict) -> None:
    """Отправляет мастер новым сообщением (после ручного ввода — чтобы он был внизу чата)."""
    await _safe_delete(context, chat_id, s.get("msg_id"))
    text, markup = render(s)
    msg = await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    s["msg_id"] = msg.message_id


async def _ask_input(context, chat_id: int, s: dict, kind: str, error: str = "") -> None:
    await _safe_delete(context, chat_id, s.get("prompt_id"))
    s["await"] = kind
    question, placeholder = PROMPTS[kind]
    text = (error + "\n" if error else "") + f"{s['mention']}, {question}"
    msg = await context.bot.send_message(
        chat_id, text, parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
        reply_parameters=ReplyParameters(message_id=s["origin_id"], allow_sending_without_reply=True),
    )
    s["prompt_id"] = msg.message_id


def card_kb(meeting_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Изменить", callback_data=f"e|{meeting_id}|edit"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"e|{meeting_id}|del"),
    ]])


def final_card(s: dict, meeting: dict, edited: bool = False) -> str:
    start = start_dt(s)
    end = start + timedelta(minutes=s["duration"])
    end_str = f"{end:%H:%M}" + (" (следующего дня)" if end.date() != start.date() else "")
    record = "в облако Zoom" if ZOOM_AUTO_RECORDING == "cloud" else "на компьютер организатора"
    lines = [
        "✏️ <b>Встреча изменена</b>" if edited else "✅ <b>Встреча создана</b>",
        "",
        f"📌 <b>{html.escape(meeting_title(s))}</b>",
        f"📅 {fmt_date(s['date'])}",
        f"🕒 {start:%H:%M}–{end_str} по Москве",
        f"⏱ {fmt_duration(s['duration'])}",
        "",
        f"🔗 {html.escape(meeting['join_url'])}",
        f"🆔 ID: {fmt_meeting_id(meeting['id'])}",
    ]
    if meeting.get("password"):
        lines.append(f"🔑 Код доступа: <code>{html.escape(meeting['password'])}</code>")
    lines += [
        "",
        f"⏺ Запись включится автоматически ({record})",
        "🚪 Вход свободный: без зала ожидания, можно заходить раньше организатора",
        "",
        f"{s['mention']}, " + ("ссылка прежняя — участникам ничего пересылать не нужно."
                               if edited else "ссылка готова — можно пересылать участникам."),
    ]
    return "\n".join(lines)


# ---------- обработчики ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orgs = "\n".join(f"• {html.escape(o)}" for o in ORGANIZATIONS)
    await update.effective_message.reply_text(
        "Я создаю встречи в Zoom прямо из чата.\n\n"
        "Напишите /meet — спрошу дату, время по Москве, длительность, организацию, "
        "модуль и номер встречи, а потом пришлю ссылку сюда.\n\n"
        "После встречи сам заберу запись из Zoom, расшифрую, заполню отчёт эксперта "
        "и пришлю ссылку на него в этот чат.\n\n"
        f"Организации:\n{orgs}\n\n/upload — у меня своя запись встречи (не из этого бота)\n"
        "/mymeetings — ваши встречи: изменить дату, время или удалить\n"
        "/process — обработать готовые записи сейчас\n/cancel — отменить создание встречи.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"ID этого чата: <code>{update.effective_chat.id}</code>",
                                              parse_mode=ParseMode.HTML)


async def cmd_meet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not chat_allowed(update):
        await update.effective_message.reply_text("В этом чате создание встреч не настроено.")
        return
    user, chat = update.effective_user, update.effective_chat
    old = sessions(context).get(user.id)
    if old:
        await _safe_delete(context, chat.id, old.get("msg_id"))
        await _safe_delete(context, chat.id, old.get("prompt_id"))
    s = {
        "owner": user.id,
        "mention": mention(user.id, user.full_name),
        "origin_id": update.effective_message.message_id,
        "step": "date",
    }
    sessions(context)[user.id] = s
    text, markup = render(s)
    msg = await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    s["msg_id"] = msg.message_id


async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«У меня свои записи»: спрашиваем встречу и даём папку на Диске для файлов."""
    if not chat_allowed(update):
        await update.effective_message.reply_text("В этом чате бот не настроен.")
        return
    user, chat = update.effective_user, update.effective_chat
    old = sessions(context).get(user.id)
    if old:
        await _safe_delete(context, chat.id, old.get("msg_id"))
        await _safe_delete(context, chat.id, old.get("prompt_id"))
    s = {"owner": user.id, "mention": mention(user.id, user.full_name), "mode": "upload",
         "origin_id": update.effective_message.message_id, "step": "date"}
    sessions(context)[user.id] = s
    text, markup = render(s)
    msg = await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    s["msg_id"] = msg.message_id


async def _create_upload(query, context, s: dict) -> None:
    """Готовит папку на Диске и ждёт, когда эксперт скажет «загрузил»."""
    chat_id = query.message.chat.id
    await query.answer("Готовлю папку…")
    await query.edit_message_text(_summary(s) + "\n\n⏳ Создаю папку на Google Диске…", parse_mode=ParseMode.HTML)
    name = f"{start_dt(s):%Y-%m-%d} {block_label(s)}"
    try:
        org_folder = await asyncio.to_thread(gdrive.folder_for, s["org"])
        parent = await asyncio.to_thread(gdrive.subfolder, org_folder, "Свои записи")
        folder_id = await asyncio.to_thread(gdrive.subfolder, parent, name)
    except Exception as e:
        logger.exception("Не удалось создать папку для записей")
        text, markup = render(s)
        await query.edit_message_text(f"⚠️ Не получилось создать папку: {html.escape(str(e))}\n\n" + text,
                                      parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    job_id = f"own-{int(datetime.now().timestamp())}"
    storage.update("uploads", job_id, {
        "status": "waiting", "folder_id": folder_id, "topic": meeting_title(s), "org": s["org"],
        "block": block_label(s), "module": s["module"], "number": s["number"], "part": s.get("part"),
        "prev_meeting_id": s.get("prev_meeting_id", ""), "topic_extra": s.get("topic", ""),
        "start": start_dt(s).isoformat(), "chat_id": chat_id, "owner_id": s["owner"], "mention": s["mention"],
    })
    sessions(context).pop(s["owner"], None)
    link = f"https://drive.google.com/drive/folders/{folder_id}"
    await query.edit_message_text("\n".join([
        "📼 <b>Папка для записей готова</b>", "",
        f"📌 <b>{html.escape(meeting_title(s))}</b>",
        f"📅 {fmt_date(s['date'])}, {start_dt(s):%H:%M} по Москве", "",
        f"📁 {link}", "",
        "Положите туда запись встречи — можно несколько файлов, они пойдут в один отчёт.",
        "Когда всё загрузится, нажмите кнопку ниже.",
    ]), parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Я загрузил(а)", callback_data=f"u|{job_id}|ready")],
            [InlineKeyboardButton("✖ Отмена", callback_data=f"u|{job_id}|cancel")]]))


async def on_upload_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        _, job_id, action = query.data.split("|", 2)
    except ValueError:
        await query.answer()
        return
    job = storage.load("uploads").get(job_id)
    if not job or job.get("status") not in ("waiting", "failed"):
        await query.answer("Эта загрузка уже в работе или отменена.", show_alert=True)
        return
    if query.from_user.id != job.get("owner_id"):
        await query.answer("Эту загрузку начал другой участник. Свои записи — команда /upload", show_alert=True)
        return

    if action == "cancel":
        storage.update("uploads", job_id, {"status": "cancelled"})
        try:
            await asyncio.to_thread(gdrive.trash, job["folder_id"])
        except Exception:
            logger.exception("Не удалось убрать папку загрузки %s", job_id)
        await query.answer("Отменено.")
        await query.edit_message_text("Загрузка отменена, папка убрана в корзину Диска.")
        return

    await query.answer("Проверяю папку…")
    try:
        files = await asyncio.to_thread(gdrive.list_media, job["folder_id"])
    except Exception as e:
        await query.edit_message_text(f"⚠️ Не получилось заглянуть в папку: {html.escape(str(e))}",
                                      parse_mode=ParseMode.HTML)
        return
    if not files:
        await query.answer("В папке пока нет аудио или видео. Загрузите файлы и нажмите ещё раз.", show_alert=True)
        return
    storage.update("uploads", job_id, {"status": "ready", "files": [f["name"] for f in files]})
    names = "\n".join(f"• {html.escape(f['name'])}" for f in files)
    await query.edit_message_text(
        f"⏳ <b>Взял в работу {len(files)} файл(ов)</b>\n{names}\n\n"
        f"📌 {html.escape(job['topic'])}\nРасшифрую, напишу отчёт и пришлю ссылки сюда.",
        parse_mode=ParseMode.HTML)
    context.application.create_task(pipeline.check_uploads(context.bot))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = sessions(context).pop(update.effective_user.id, None)
    if not s:
        await update.effective_message.reply_text("Нечего отменять. Новая встреча — /meet")
        return
    chat_id = update.effective_chat.id
    await _safe_delete(context, chat_id, s.get("prompt_id"))
    try:
        await context.bot.edit_message_text("Создание встречи отменено.", chat_id, s["msg_id"])
    except TelegramError:
        pass


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        _, owner, action, value = query.data.split("|", 3)
        owner = int(owner)
    except ValueError:
        await query.answer()
        return

    if query.from_user.id != owner:
        await query.answer("Эту встречу оформляет другой участник. Чтобы создать свою — напишите /meet",
                           show_alert=True)
        return
    s = sessions(context).get(owner)
    if not s or s.get("msg_id") != query.message.message_id:
        await query.answer("Это старое меню. Начните заново: /meet", show_alert=True)
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass
        return

    chat_id = query.message.chat.id
    s.pop("await", None)
    await _safe_delete(context, chat_id, s.pop("prompt_id", None))

    if action == "cancel":
        sessions(context).pop(owner, None)
        await query.answer()
        await query.edit_message_text("Изменение отменено — встреча осталась как была."
                                      if s.get("edit_id") else "Создание встречи отменено.")
        return

    if action in ("date_in", "time_in", "dur_in", "topic"):
        await query.answer()
        await _ask_input(context, chat_id, s, {"date_in": "date", "time_in": "time",
                                               "dur_in": "dur", "topic": "topic"}[action])
        return

    if action == "create":
        if is_upload(s):
            await _create_upload(query, context, s)
        else:
            await (_save_edit(query, context, s) if s.get("edit_id") else _create(query, context, s))
        return

    if action == "date":
        s["date"] = date.fromisoformat(value)
        if is_upload(s):
            s["step"] = "hour"
            await query.answer()
            text, markup = render(s)
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        await load_busy(s)
        if s.get("edit_id"):
            check_slot(s)
        else:
            s["step"] = "hour"
    elif action == "hour":
        s["hour"] = int(value)
        s["step"] = "minute"
    elif action == "min":
        s["minute"] = int(value)
        s.pop("last_slot", None)
        if is_upload(s):
            s["step"] = "org"
        elif s.get("edit_id"):
            check_slot(s)
        elif start_dt(s) <= now_msk():
            s.pop("minute")
            s["step"] = "hour"
            await query.answer("Это время уже прошло — выберите другое.", show_alert=True)
        else:
            s["step"] = "duration"
    elif action == "dur":
        s["duration"] = int(value)
        if s.get("edit_id"):
            check_slot(s)
        elif conflicts(s):
            mark_busy(s, conflicts(s))
            await query.answer("Это время занято — выберите другое.", show_alert=True)
        else:
            s["step"] = "org"
    elif action == "org":
        s["org"] = ORGANIZATIONS[int(value)]
        s["step"] = "module"
    elif action == "mod":
        s["module"] = int(value)
        s["step"] = "number"
    elif action == "num":
        s["number"] = int(value)
        s.pop("part", None)
        s["step"] = "cont" if earlier_meetings(s) else "confirm"
    elif action == "acct":
        s["account"] = value
        s.update(s.pop("last_slot", {}))  # возвращаем время, которое было занято на основном
        # Занятость проверяется до выбора организации, поэтому дальше идём обычным порядком
        s["step"] = "confirm" if s.get("number") else "org"
        s["notice"] = ("📅 Встреча будет создана на <b>резервном аккаунте Zoom</b>. "
                       "Для участников ничего не меняется.")
    elif action == "cont":
        earlier = earlier_meetings(s)
        if value == "1":
            s["part"] = len(earlier) + 1
            s["prev_meeting_id"] = earlier[-1].get("meeting_id", "")
        else:
            s.pop("part", None)
        s["step"] = "confirm"
    elif action == "back":
        s["step"] = value
        if value == "hour" and not s.get("edit_id"):
            s.pop("minute", None)
    elif action == "restart":
        for key in ("date", "hour", "minute", "duration", "org", "module", "number", "part", "prev_meeting_id",
                    "topic", "busy", "busy_failed", "account", "last_slot"):
            s.pop(key, None)
        s["step"] = "date"

    await query.answer()
    text, markup = render(s)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramError as e:
        if "not modified" not in str(e):
            raise


async def _create(query, context, s: dict) -> None:
    missing = next((step for step in ("org", "module", "number") if not s.get(step)), None)
    if missing:  # страховка: без организации, модуля и номера встречу не создаём
        s["step"] = missing
        s["notice"] = "Не хватает данных о встрече — закончим по шагам."
        await query.answer()
        text, markup = render(s)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return
    if start_dt(s) <= now_msk():
        s.pop("minute", None)
        s["step"] = "hour"
        await query.answer("Время начала уже прошло — выберите новое.", show_alert=True)
        text, markup = render(s)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    # Проверяем ещё раз прямо перед созданием — пока оформляли, время могли занять
    await load_busy(s)
    found = conflicts(s)
    if found:
        mark_busy(s, found)
        await query.answer("Это время уже занято — выберите другое.", show_alert=True)
        text, markup = render(s)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    await query.answer("Создаю встречу…")
    await query.edit_message_text(_summary(s) + "\n\n⏳ Создаю встречу в Zoom…", parse_mode=ParseMode.HTML)
    try:
        meeting = await zoom_client.create_meeting(meeting_title(s), start_dt(s), s["duration"], account_of(s))
    except Exception as e:  # сеть, авторизация, ошибки Zoom — показываем и даём повторить
        logger.exception("Не удалось создать встречу")
        text, markup = render(s)
        await query.edit_message_text(
            f"⚠️ Не получилось создать встречу: {html.escape(str(e))}\n\n" + text,
            parse_mode=ParseMode.HTML, reply_markup=markup,
        )
        return

    chat_id = query.message.chat.id
    sessions(context).pop(s["owner"], None)
    logger.info("Создана встреча %s (%s): %s", meeting.get("id"), zoom_client.LABELS[account_of(s)], meeting_title(s))
    # Запоминаем, чья это встреча: по ней обработка записей заполнит шапку отчёта и отметит эксперта
    await _safe_delete(context, chat_id, s["msg_id"])
    card = await context.bot.send_message(
        chat_id, final_card(s, meeting), parse_mode=ParseMode.HTML, reply_markup=card_kb(meeting["id"]),
        reply_parameters=ReplyParameters(message_id=s["origin_id"], allow_sending_without_reply=True),
    )
    storage.update("meetings", str(meeting["id"]), {
        **_meeting_fields(s), "meeting_id": str(meeting["id"]), "account": account_of(s), "chat_id": chat_id, "owner_id": s["owner"], "mention": s["mention"],
        "join_url": meeting.get("join_url", ""), "password": meeting.get("password", ""),
        "card_msg_id": card.message_id, "status": "active",
    })
    await pipeline.sync_registry(registry.sync, [str(meeting["id"])])


def _meeting_fields(s: dict) -> dict:
    """То, что меняется при редактировании: для отчёта (org, block, topic) и для повторного мастера."""
    if s.get("general_title"):
        return {"topic": s["general_title"], "start": start_dt(s).isoformat(), "duration": s["duration"]}
    return {
        "org": s["org"], "block": block_label(s), "topic": meeting_title(s),
        "start": start_dt(s).isoformat(), "duration": s["duration"],
        "module": s["module"], "number": s["number"], "topic_extra": s.get("topic", ""),
        "part": s.get("part"), "prev_meeting_id": s.get("prev_meeting_id", ""),
    }


async def _save_edit(query, context, s: dict) -> None:
    mid = s["edit_id"]
    await load_busy(s)
    if not check_slot(s):
        await query.answer("Время прошло или занято — выберите другое.", show_alert=True)
        text, markup = render(s)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return
    await query.answer("Сохраняю…")
    await query.edit_message_text(_summary(s) + "\n\n⏳ Сохраняю изменения в Zoom…", parse_mode=ParseMode.HTML)
    try:
        await zoom_client.update_meeting(mid, meeting_title(s), start_dt(s), s["duration"], account_of(s))
    except Exception as e:
        logger.exception("Не удалось изменить встречу %s", mid)
        text, markup = render(s)
        await query.edit_message_text(f"⚠️ Не получилось изменить встречу: {html.escape(str(e))}\n\n" + text,
                                      parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    chat_id = query.message.chat.id
    sessions(context).pop(s["owner"], None)
    info = storage.load("meetings").get(mid, {})
    logger.info("Изменена встреча %s: %s", mid, meeting_title(s))
    await _safe_delete(context, chat_id, s["msg_id"])
    try:  # старая карточка больше не актуальна
        await context.bot.edit_message_text("✏️ Эта встреча изменена — актуальная карточка ниже.",
                                            chat_id, info.get("card_msg_id"))
    except TelegramError:
        pass
    meeting = {"id": mid, "join_url": info.get("join_url", ""), "password": info.get("password", "")}
    card = await context.bot.send_message(chat_id, final_card(s, meeting, edited=True),
                                          parse_mode=ParseMode.HTML, reply_markup=card_kb(mid))
    storage.update("meetings", mid, {**_meeting_fields(s), "card_msg_id": card.message_id})
    await pipeline.sync_registry(registry.sync, [mid])


# ---------- изменение и удаление созданных встреч

async def _can_manage(context, chat_id: int, user_id: int, info: dict) -> bool:
    if info.get("owner_id") == user_id:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


def _when(info: dict) -> str:
    start = datetime.fromisoformat(info["start"])
    return f"{start:%d.%m} в {start:%H:%M} по Москве"


def _short(info: dict) -> str:
    return f"«{html.escape(info.get('topic', ''))}», {_when(info)}"


async def on_meeting_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        _, mid, action = query.data.split("|", 2)
    except ValueError:
        await query.answer()
        return
    user, chat_id = query.from_user, query.message.chat.id
    info = storage.load("meetings").get(mid)
    if not info or info.get("status") == "deleted":
        await query.answer("Эта встреча уже удалена.", show_alert=True)
        return
    if not await _can_manage(context, chat_id, user.id, info):
        await query.answer("Изменить или удалить встречу может тот, кто её создал, или администратор группы.",
                           show_alert=True)
        return
    if action in ("edit", "del", "delyes") and datetime.fromisoformat(info["start"]) <= now_msk():
        await query.answer("Встреча уже началась или прошла — её не изменить.", show_alert=True)
        return

    if action == "edit":
        await query.answer()
        start = datetime.fromisoformat(info["start"])
        old = sessions(context).get(user.id)
        if old:
            await _safe_delete(context, chat_id, old.get("msg_id"))
            await _safe_delete(context, chat_id, old.get("prompt_id"))
        s = {
            "owner": user.id, "mention": mention(user.id, user.full_name),
            "origin_id": query.message.message_id, "edit_id": mid, "step": "confirm",
            "date": start.date(), "hour": start.hour, "minute": start.minute,
            "duration": int(info.get("duration") or 60), "org": info.get("org", ""),
            "module": info.get("module") or 1, "number": info.get("number") or 1,
            "part": info.get("part"), "prev_meeting_id": info.get("prev_meeting_id", ""),
            "account": info.get("account") or zoom_client.MAIN,
            "topic": info.get("topic_extra", ""),
        }
        if info.get("kind") == "general":
            s["general_title"] = info["topic"]
        await load_busy(s)
        sessions(context)[user.id] = s
        text, markup = render(s)
        msg = await context.bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup,
            reply_parameters=ReplyParameters(message_id=query.message.message_id, allow_sending_without_reply=True),
        )
        s["msg_id"] = msg.message_id

    elif action == "del":
        await query.answer()
        await context.bot.send_message(
            chat_id, f"🗑 Удалить встречу {_short(info)}?\nОна будет отменена в Zoom, ссылка перестанет работать.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Да, удалить", callback_data=f"e|{mid}|delyes"),
                InlineKeyboardButton("Нет, оставить", callback_data=f"e|{mid}|keep"),
            ]]),
            reply_parameters=ReplyParameters(message_id=query.message.message_id, allow_sending_without_reply=True),
        )

    elif action == "keep":
        await query.answer("Встреча остаётся.")
        await _safe_delete(context, chat_id, query.message.message_id)

    elif action == "delyes":
        try:
            await zoom_client.delete_meeting(mid, info.get("account") or zoom_client.MAIN)
        except Exception as e:
            logger.exception("Не удалось удалить встречу %s", mid)
            await query.answer("Не получилось удалить встречу.", show_alert=True)
            await query.edit_message_text(f"⚠️ Не получилось удалить встречу: {html.escape(str(e))}",
                                          parse_mode=ParseMode.HTML)
            return
        await query.answer("Встреча удалена.")
        storage.update("meetings", mid, {"status": "deleted"})
        logger.info("Удалена встреча %s", mid)
        await pipeline.sync_registry(registry.sync, [mid])
        await _safe_delete(context, chat_id, query.message.message_id)
        try:
            await context.bot.edit_message_text(
                f"❌ <b>Встреча отменена</b>\n\n<s>{html.escape(info.get('topic', ''))}</s>\n"
                f"{_when(info)}\n\nОтменил(а): {mention(user.id, user.full_name)}",
                chat_id, info.get("card_msg_id"), parse_mode=ParseMode.HTML)
        except TelegramError:
            await context.bot.send_message(chat_id, f"❌ Встреча {_short(info)} отменена.",
                                           parse_mode=ParseMode.HTML)


async def cmd_mymeetings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not chat_allowed(update):
        return
    user, chat_id = update.effective_user, update.effective_chat.id
    now = now_msk()
    is_admin = False
    try:
        is_admin = (await context.bot.get_chat_member(chat_id, user.id)).status in ("administrator", "creator")
    except TelegramError:
        pass
    items = sorted(
        ((mid, info) for mid, info in storage.load("meetings").items()
         if info.get("status") != "deleted" and info.get("chat_id") == chat_id
         and datetime.fromisoformat(info["start"]) > now and (is_admin or info.get("owner_id") == user.id)),
        key=lambda x: x[1]["start"],
    )
    if not items:
        await update.effective_message.reply_text("Запланированных встреч нет. Создать — /meet")
        return
    lines = ["📋 <b>" + ("Запланированные встречи группы" if is_admin else "Ваши запланированные встречи") + "</b>", ""]
    rows = []
    for i, (mid, info) in enumerate(items[:20], 1):
        start = datetime.fromisoformat(info["start"])
        lines.append(f"{i}. {start:%d.%m %H:%M} — {html.escape(info.get('topic', ''))}")
        rows.append([InlineKeyboardButton(f"✏️ {i}. {start:%d.%m %H:%M}", callback_data=f"e|{mid}|edit"),
                     InlineKeyboardButton(f"🗑 {i}", callback_data=f"e|{mid}|del")])
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                              reply_markup=InlineKeyboardMarkup(rows))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    s = sessions(context).get(user.id) if user else None
    if not s or not s.get("await"):
        return
    # В группе принимаем только ответ на вопрос бота, чтобы не перехватывать обычную переписку
    if chat.type != ChatType.PRIVATE:
        reply_to = msg.reply_to_message
        if not reply_to or reply_to.message_id != s.get("prompt_id"):
            return

    kind, text = s["await"], msg.text.strip()
    today = now_msk().date()

    if kind == "date":
        d = parse_date(text, today)
        if not d or d < today:
            await _ask_input(context, chat.id, s, kind, "Не понял дату или она уже прошла.")
            return
        s["date"] = d
        await load_busy(s)
        if s.get("edit_id"):
            check_slot(s)
        else:
            s["step"] = "hour"
    elif kind == "time":
        t = parse_time(text)
        if not t:
            await _ask_input(context, chat.id, s, kind, "Не понял время.")
            return
        s["hour"], s["minute"] = t
        if start_dt(s) <= now_msk():
            s.pop("minute")
            await _ask_input(context, chat.id, s, kind, "Это время уже прошло.")
            return
        if s.get("edit_id"):
            check_slot(s)
        else:
            s["step"] = "duration"
    elif kind == "dur":
        minutes = parse_duration(text)
        if not minutes:
            await _ask_input(context, chat.id, s, kind, "Не понял длительность (от 15 минут до 24 часов).")
            return
        s["duration"] = minutes
        if s.get("edit_id"):
            check_slot(s)
        elif conflicts(s):
            mark_busy(s, conflicts(s))
        else:
            s["step"] = "org"
    elif kind == "topic":
        s["topic"] = text[:100]
        s["step"] = "confirm"

    s.pop("await", None)
    await _safe_delete(context, chat.id, s.pop("prompt_id", None))
    await _send_wizard(context, chat.id, s)


async def cmd_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверить облако Zoom прямо сейчас, не дожидаясь планового запуска."""
    if not chat_allowed(update):
        return
    msg = await update.effective_message.reply_text("🔎 Проверяю записи в облаке Zoom…")
    try:
        stats = await pipeline.check_recordings(context.bot)
    except Exception as e:
        logger.exception("Ручная проверка записей упала")
        await msg.edit_text(f"⚠️ Не получилось проверить записи: {e}")
        return
    if stats.get("busy"):
        await msg.edit_text("⏳ Записи уже обрабатываются — отчёт придёт сюда, как будет готов.")
        return
    parts = [f"готово отчётов: {stats['done']}"]
    if stats["waiting"]:
        parts.append(f"Zoom ещё готовит записей: {stats['waiting']}")
    if stats["failed"]:
        parts.append(f"с ошибкой: {stats['failed']}")
    await msg.edit_text("✅ Проверка закончена — " + ", ".join(parts) + ".")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбои связи с Telegram (Bad Gateway, таймауты) бывают и проходят сами — не пугаем журнал трассировкой."""
    if isinstance(context.error, NetworkError):
        logger.warning("Временный сбой связи с Telegram: %s", context.error)
    else:
        logger.error("Ошибка при обработке обновления", exc_info=context.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("meet", "Создать встречу в Zoom"),
        BotCommand("mymeetings", "Мои встречи: изменить или удалить"),
        BotCommand("upload", "У меня своя запись встречи"),
        BotCommand("process", "Обработать готовые записи сейчас"),
        BotCommand("cancel", "Отменить создание встречи"),
        BotCommand("help", "Как пользоваться"),
    ])
    app.create_task(pipeline.run_loop(app))


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN в .env")
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init)
    if TELEGRAM_BASE_URL:
        builder = builder.base_url(TELEGRAM_BASE_URL)
    app = builder.build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler(["meet", "zoom"], cmd_meet))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("process", cmd_process))
    app.add_handler(CommandHandler("mymeetings", cmd_mymeetings))
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^m\|"))
    app.add_handler(CallbackQueryHandler(on_meeting_action, pattern=r"^e\|"))
    app.add_handler(CallbackQueryHandler(on_upload_action, pattern=r"^u\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
