"""
Настройки бота. Всё берётся из .env (шаблон — .env.example), секреты в коде не храним.
"""

import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Необязательно: адрес прокси для Bot API, если api.telegram.org недоступен (…/bot)
TELEGRAM_BASE_URL = os.getenv("TELEGRAM_BASE_URL", "")

# В каких группах боту разрешено создавать встречи (id через запятую).
# Пусто — в любых. Узнать id группы: написать в ней /chatid.
ALLOWED_CHAT_IDS = {
    int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
}

# Zoom: Server-to-Server OAuth приложение (marketplace.zoom.us → Develop → Build App)
ZOOM_ACCOUNT_ID = os.getenv("ZOOM_ACCOUNT_ID", "")
ZOOM_CLIENT_ID = os.getenv("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET", "")
# Чей аккаунт будет организатором встреч: e-mail пользователя Zoom
ZOOM_HOST = os.getenv("ZOOM_HOST") or "me"
# cloud — запись в облако Zoom (нужен платный тариф); local — на компьютер организатора
ZOOM_AUTO_RECORDING = os.getenv("ZOOM_AUTO_RECORDING", "cloud")
# За сколько минут до начала участники могут зайти без организатора (Zoom допускает 0 — в любое время, 5, 10, 15)
JOIN_BEFORE_MIN = int(os.getenv("JOIN_BEFORE_MIN", "15"))
# Записи короче этого — случайный заход (зашли заранее и вышли), а не встреча: не обрабатываем
MIN_RECORDING_MIN = int(os.getenv("MIN_RECORDING_MIN", "10"))

# Резервный аккаунт Zoom: на него бот ставит встречу, когда на основном время занято.
# Пусто — резерв выключен, бот просто скажет «это время занято».
ZOOM_BACKUP_ACCOUNT_ID = os.getenv("ZOOM_BACKUP_ACCOUNT_ID", "")
ZOOM_BACKUP_CLIENT_ID = os.getenv("ZOOM_BACKUP_CLIENT_ID", "")
ZOOM_BACKUP_CLIENT_SECRET = os.getenv("ZOOM_BACKUP_CLIENT_SECRET", "")
ZOOM_BACKUP_HOST = os.getenv("ZOOM_BACKUP_HOST") or "me"
# Убирать ли записи со второго аккаунта в корзину Zoom через ZOOM_TRASH_AFTER_DAYS (как на основном)
ZOOM_BACKUP_TRASH = os.getenv("ZOOM_BACKUP_TRASH", "0") == "1"

# Организации программы — варианты названия встречи. Через точку с запятой.
ORGANIZATIONS = [
    s.strip()
    for s in os.getenv(
        "ORGANIZATIONS",
        "НКО «Пример»;АНО «Пример-2»;Фонд «Пример-3»",
    ).split(";")
    if s.strip()
]

# Эксперт каждой организации — для шапки отчёта
EXPERTS = dict(
    pair.split("=", 1) for pair in os.getenv("EXPERTS", "").split(";") if "=" in pair
)
EXPERTS = {k.strip(): v.strip() for k, v in EXPERTS.items()}

# ---------- Обработка записей ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORT_TEMPLATE = os.path.join(BASE_DIR, "templates", "report_template.docx")
RECORDINGS_CHECK_MIN = int(os.getenv("RECORDINGS_CHECK_MIN", "10"))
# Окно просмотра облака Zoom: с запасом больше срока, через который запись удаляется из Zoom
RECORDINGS_LOOKBACK_DAYS = int(os.getenv("RECORDINGS_LOOKBACK_DAYS", "14"))
# Через сколько дней после загрузки видео на Диск отправлять запись в корзину Zoom (0 — не удалять)
ZOOM_TRASH_AFTER_DAYS = int(os.getenv("ZOOM_TRASH_AFTER_DAYS", "7"))
# Записи встреч раньше этой даты (ГГГГ-ММ-ДД, по Москве) не трогаем
PROCESS_SINCE = os.getenv("PROCESS_SINCE", "")

# Google Диск: папка «Встречи», внутри — подпапки с названиями организаций
GOOGLE_DRIVE_ROOT_ID = os.getenv("GOOGLE_DRIVE_ROOT_ID", "")
# Файл входа в Google (client_id, client_secret, refresh_token)
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE") or os.path.join(BASE_DIR, "google_token.json")

# Расшифровка и заполнение отчёта
TRANSCRIPTOR_API_KEY = os.getenv("TRANSCRIPTOR_API_KEY", "")
# Нейросеть для отчёта — Claude (Anthropic API)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL") or "claude-opus-5"
