"""
MOODEX — конфигурация
Создай файл .env в корне проекта и заполни переменные.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Telegram API ─────────────────────────────────────────────────────────────
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "moodex_session")
# Строковая сессия для деплоя в контейнерах (генерируется через auth_telegram.py)
# Если задана — используется вместо файла сессии
TELEGRAM_STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "")
# Необязательный прокси для обхода нестабильного доступа к серверам Telegram
# (частая проблема на хостинге в РФ). Формат: socks5://[user:pass@]host:port
# Поддерживаются также socks4:// и http://. Пусто — без прокси.
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")
# Сколько попыток переподключения делает Telethon перед ошибкой
TELEGRAM_CONNECTION_RETRIES = int(os.getenv("TELEGRAM_CONNECTION_RETRIES", "5"))

# ─── AI Агент ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ─── Список каналов для парсинга ──────────────────────────────────────────────
TELEGRAM_CHANNELS = [
    # ── Топ чаты трейдеров MOEX ──
    "markettwits",           # MarketTwits — крупнейший чат трейдеров РФ
    "rdv_investor",          # РынкиДеньгиВласть — аналитика и обсуждения
    "smart_lab_official",    # Smart-lab — профессиональные трейдеры
    "mozgovik",              # Mozgovik Research — аналитика
    "cbrstocks",             # Акции и облигации РФ
    "AlenkaCapital",         # Алёнка Капитал — популярный канал
    "finfeed",               # ФинФид — новости рынка
    "stocktrader_ru",        # Трейдинг РФ
    "invst_ideas",           # Инвестиционные идеи
    "finam_ru",              # Финам — брокер
    "bcs_express",           # БКС Экспресс — аналитика
    "sberinvestments",       # Сбер Инвестиции
    "tinkoff_invest",        # Т-Инвестиции
    "vtb_my_investments",    # ВТБ Мои Инвестиции
    "invest_tinkoff",        # Инвестиции Тинькофф сообщество
    "moex_official",         # Московская биржа официальный
    "russianmacro",          # Русский Макро — макроэкономика
    "helicoptermacro",       # Вертолётный Макро
    "profinance_ru",         # Profinance — биржевые новости
    "akprime",               # АК Прайм — новости экономики
]

# ─── Tinkoff Invest API ────────────────────────────────────────────────────────
# Получить: Т-Инвестиции → Настройки → API токен → Создать (только чтение)
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN", "")

# ─── «Умные деньги»: отслеживаемые трейдеры Пульса ────────────────────────────
# Ники трейдеров, чьи РЕАЛЬНЫЕ сделки (покупки/продажи) используем как сигнал.
# Задаётся через PULSE_TRACKED_AUTHORS в .env (список через запятую).
PULSE_TRACKED_AUTHORS = [
    a.strip() for a in os.getenv("PULSE_TRACKED_AUTHORS", "Rostislavzzz").split(",") if a.strip()
]

# Модели (в порядке приоритета):
# 1. blanchefort/rubert-base-cased-sentiment — точная, 512MB
# 2. cointegrated/rubert-tiny-sentiment-balanced — быстрая, 45MB ✅ рекомендую для старта
NLP_MODEL = os.getenv("NLP_MODEL", "cointegrated/rubert-tiny-sentiment-balanced")
NLP_BATCH_SIZE = int(os.getenv("NLP_BATCH_SIZE", "32"))
NLP_MAX_LENGTH = int(os.getenv("NLP_MAX_LENGTH", "512"))

# Fallback на OpenAI/DeepSeek для сложных случаев (сарказм, мемы)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USE_LLM_FALLBACK = os.getenv("USE_LLM_FALLBACK", "false").lower() == "true"

# ─── База данных ───────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/moodex.db")
# Путь к старому JSON-файлу каналов (для одноразовой миграции в БД)
CHANNELS_FILE = os.getenv("CHANNELS_FILE", "data/channels.json")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ─── API ───────────────────────────────────────────────────────────────────────
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "change-me-in-production")

# ─── Агрегация ─────────────────────────────────────────────────────────────────
# Временное окно для расчёта индекса (в минутах)
SENTIMENT_WINDOW_MINUTES = int(os.getenv("SENTIMENT_WINDOW_MINUTES", "60"))
# Минимальное количество сообщений для значимого индекса
MIN_MESSAGES_FOR_SIGNAL = int(os.getenv("MIN_MESSAGES_FOR_SIGNAL", "5"))
# Порог аномалии (множитель от среднего)
ANOMALY_THRESHOLD = float(os.getenv("ANOMALY_THRESHOLD", "3.0"))

# ─── Интрадей (внутридневная торговля) ────────────────────────────────────────
# Торгуем внутри дня: решение на 5-мин свечах, флэт к закрытию сессии.
INTRADAY_MODE = os.getenv("INTRADAY_MODE", "true").lower() == "true"
INTRADAY_TF_MIN = int(os.getenv("INTRADAY_TF_MIN", "5"))            # таймфрейм решений, мин
INTRADAY_HORIZON_HOURS = int(os.getenv("INTRADAY_HORIZON_HOURS", "2"))  # горизонт прогноза, ч
INTRADAY_OPENING_RANGE_BARS = int(os.getenv("INTRADAY_OPENING_RANGE_BARS", "6"))  # свечей в диапазоне открытия

# ─── Триаж «словари+ML → Claude» (авто-скан) ──────────────────────────────────
# Дешёвые слои постоянно скринят рынок; Claude подтверждает только интересное.
SCAN_MIN_INTEREST = float(os.getenv("SCAN_MIN_INTEREST", "0.7"))   # порог «интересности» [0..1] (выше = реже зовём Claude)
SCAN_MAX_CLAUDE = int(os.getenv("SCAN_MAX_CLAUDE", "2"))            # макс. вызовов Claude за цикл (экономия)

# Визуальный разбор графика (Claude Vision) — ВТОРОЙ вызов Claude на тикер + дорогой
# input-image. По умолчанию ВЫКЛ ради экономии; структурных данных и так достаточно.
CHART_ANALYSIS_ENABLED = os.getenv("CHART_ANALYSIS_ENABLED", "false").lower() == "true"

# Авто-старт live-движка при запуске приложения: сканирование → сигналы по
# плейбуку → оценка созревших прогнозов → обучение. Без него «мозг» простаивает
# до ручного /api/live-signals/start и гаснет при каждом редеплое.
# Сканер (Claude-сигналы) — по умолчанию РУЧНОЙ: включаешь, когда садишься торговать,
# выключаешь при выходе (Claude не зовётся → расход 0). Кнопки: /api/live-signals/start|stop.
LIVE_SIGNALS_AUTOSTART = os.getenv("LIVE_SIGNALS_AUTOSTART", "false").lower() == "true"
LIVE_SIGNALS_INTERVAL_MIN = int(os.getenv("LIVE_SIGNALS_INTERVAL_MIN", "60"))  # период сканера, мин (min 5)
# Learning-цикл (оценка прогнозов, БЕЗ Claude) — работает ВСЕГДА, даже при выкл.
# сканере: самообучение (точность / R / regime-stats) не прерывается.
LEARNING_AUTOSTART = os.getenv("LEARNING_AUTOSTART", "true").lower() == "true"
LEARNING_INTERVAL_MIN = int(os.getenv("LEARNING_INTERVAL_MIN", "30"))  # период оценки, мин (min 5)

# Демо-данные (фейковые сообщения) на старте — только для локальной проверки без
# источников. В проде держать выключенным, иначе «Рынок» показывает выдумку.
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"

# ─── Тикеры Мосбиржи (ТОП-50 IMOEX) ───────────────────────────────────────────
MOEX_TICKERS = {
    "SBER": "Сбербанк",
    "GAZP": "Газпром",
    "LKOH": "Лукойл",
    "GMKN": "Норникель",
    "NVTK": "Новатэк",
    "ROSN": "Роснефть",
    "YNDX": "Яндекс",
    "TATN": "Татнефть",
    "MTSS": "МТС",
    "MGNT": "Магнит",
    "ALRS": "АЛРОСА",
    "POLY": "Polymetal",
    "PLZL": "Полюс",
    "CBOM": "МКБ",
    "VTBR": "ВТБ",
    "AFLT": "Аэрофлот",
    "MAGN": "ММК",
    "NLMK": "НЛМК",
    "CHMF": "Северсталь",
    "PHOR": "ФосАгро",
    "PIKK": "ПИК",
    "FEES": "ФСК ЕЭС",
    "IRAO": "Интер РАО",
    "RUAL": "РусАл",
    "SNGS": "Сургутнефтегаз",
    "SNGSP": "Сургутнефтегаз-п",
    "MTLR": "Мечел",
    "HYDR": "РусГидро",
    "DSKY": "Детский мир",
    "FIVE": "X5 Group",
    "OZON": "Ozon",
    "MOEX": "Мосбиржа",
    "TCSG": "Т-Банк (ТКС)",
    "BSPB": "Банк Санкт-Петербург",
    "SIBN": "Газпромнефть",
    "TRNFP": "Транснефть",
    "UPRO": "Юнипро",
    "AFKS": "АФК Система",
    "MSNG": "Мосэнерго",
    "FLOT": "Совкомфлот",
    "SMLT": "Самолёт",
    "VKCO": "VK",
    "POSI": "Positive Technologies",
    "ASTR": "Астра",
    "DIAS": "Диасофт",
    "HEAD": "HeadHunter",
    "WUSH": "Whoosh",
    "EUTR": "ЮТэйр",
    "NKNC": "Нижнекамскнефтехим",
    "LSRG": "ЛСР",
}
