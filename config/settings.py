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
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./moodex.db")
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
