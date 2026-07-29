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
SCAN_MIN_INTEREST = float(os.getenv("SCAN_MIN_INTEREST", "0.6"))   # порог «интересности» [0..1] (ниже = больше кандидатов к Claude)
SCAN_MAX_CLAUDE = int(os.getenv("SCAN_MAX_CLAUDE", "6"))            # макс. вызовов Claude за цикл (баланс есть → шире охват)

# Визуальный разбор графика (Claude Vision) — ВТОРОЙ вызов Claude на тикер + дорогой
# input-image. По умолчанию ВЫКЛ ради экономии; структурных данных и так достаточно.
CHART_ANALYSIS_ENABLED = os.getenv("CHART_ANALYSIS_ENABLED", "false").lower() == "true"

# ─── База знаний: снимки стакана/потока Tinkoff (market_snapshot_pipeline) ─────
# Сколько тикеров держать «под стаканом». 0 = ВЕСЬ список MOEX_TICKERS (все бумаги).
# Раньше было жёстко [:8]. Мёртвые/переименованные тикеры авто-усыпляются (см. main.py).
SNAPSHOT_MAX = int(os.getenv("SNAPSHOT_MAX", "0"))              # 0 = все тикеры
SNAPSHOT_PACING_SEC = float(os.getenv("SNAPSHOT_PACING_SEC", "0.4"))  # пауза между тикерами (лимиты Tinkoff)
# core+rotating: ЯДРО (ликвидные) опрашиваем КАЖДЫЙ цикл; ХВОСТ — по кругу срезами,
# чтобы не упираться в лимиты Tinkoff и чтобы ликвидные бумаги никогда не «залипали».
SNAPSHOT_CORE = [t.strip().upper() for t in os.getenv(
    "SNAPSHOT_CORE",
    "SBER,GAZP,LKOH,GMKN,NVTK,ROSN,YDEX,TATN,MTSS,MGNT,ALRS,PLZL,VTBR,CHMF,"
    "NLMK,MAGN,SNGS,SNGSP,MOEX,T,OZON,SIBN,TRNFP,PHOR,X5,POSI").split(",") if t.strip()]
SNAPSHOT_TAIL_PER_CYCLE = int(os.getenv("SNAPSHOT_TAIL_PER_CYCLE", "6"))  # тикеров хвоста за цикл

# ─── Приём сделок трейдеров (скрейпер агента → POST /api/ingest/deals) ─────────
# Токен для защиты ingest-эндпоинта. Пусто = приём открыт (внутренний хобби-режим);
# задай INGEST_TOKEN в окружении, чтобы принимать только свои заливки.
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")
INGEST_DEALS_WINDOW_H = int(os.getenv("INGEST_DEALS_WINDOW_H", "72"))  # окно показа сделок на дашборде, ч

# ─── Вето-слой КАЧЕСТВА интрадей-сигналов (№1 окно / №2 анти-чейз / №4 режим+HTF) ─
# Не трогает стоп 1% и решение Claude — только ОТКЛОНЯЕТ слабые входы (→ «наблюдать»,
# не сохраняем). Всё числами, без доп. вызовов Claude. Пороги легко тюнить из .env.
FILTER_NO_ENTRY_FIRST_MIN = int(os.getenv("FILTER_NO_ENTRY_FIRST_MIN", "15"))  # №1: мин после открытия без входов
FILTER_REQUIRE_ORB = os.getenv("FILTER_REQUIRE_ORB", "true").lower() == "true"  # №1: требовать сформированный ORB
FILTER_ENTRY_MAX_ATR = float(os.getenv("FILTER_ENTRY_MAX_ATR", "0.5"))         # №2: макс. расстояние входа до уровня, ×ATR
FILTER_CONFLUENCE_MIN = int(os.getenv("FILTER_CONFLUENCE_MIN", "3"))            # №4: базовый порог конфлюенса
FILTER_CONFLUENCE_COUNTERTREND = int(os.getenv("FILTER_CONFLUENCE_COUNTERTREND", "4"))  # №4: фейд диапазона (range)
FILTER_CONFLUENCE_AGAINST_HTF = int(os.getenv("FILTER_CONFLUENCE_AGAINST_HTF", "5"))    # №4: против HTF / сливающий режим
FILTER_REGIME_MIN_TRADES = int(os.getenv("FILTER_REGIME_MIN_TRADES", "20"))    # №4: выборка для гейта по режиму

# ─── Управление сделкой №3 (безубыток + частичная фиксация + трейл) ────────────
# Ведём позицию МНОГОНОГО в рамках фикс-риска 1% (начальный риск НЕ расширяем):
# при +BE_TRIGGER_R → стоп в безубыток; на цели фиксируем PARTIAL_FRAC; остаток
# трейлим (peak − TRAIL_ATR×ATR, не ниже BE) до конца сессии. Итоговый R взвешенный.
MGMT_ENABLED = os.getenv("MGMT_ENABLED", "true").lower() == "true"
MGMT_BE_TRIGGER_R = float(os.getenv("MGMT_BE_TRIGGER_R", "1.0"))   # при скольки R двигаем стоп в безубыток
MGMT_PARTIAL_FRAC = float(os.getenv("MGMT_PARTIAL_FRAC", "0.5"))   # доля позиции, фиксируемая на цели T1
MGMT_TRAIL_ATR = float(os.getenv("MGMT_TRAIL_ATR", "1.5"))         # чандельер-трейл остатка, ×ATR

# ─── Моментум-режим (продолжение тренда) — ловит сильные однонаправленные движения ─
# Вход по продолжению тренда (не только на откате), СТРУКТУРНЫЙ стоп (с потолком),
# мягче гейт R:R (прибыль трейлим). Ниже win-rate, но крупнее победители — мерим
# отдельно под тегом режима trend_momentum. Пуллбэк-режим не трогаем.
MOMENTUM_ENABLED = os.getenv("MOMENTUM_ENABLED", "true").lower() == "true"
MOMENTUM_STOP_CAP_PCT = float(os.getenv("MOMENTUM_STOP_CAP_PCT", "0.01"))  # потолок структурного стопа (1%)
MOMENTUM_EXT_ATR = float(os.getenv("MOMENTUM_EXT_ATR", "2.0"))     # макс. растяжение входа от VWAP, ×ATR (анти-пик)
MOMENTUM_MIN_CONFLUENCE = int(os.getenv("MOMENTUM_MIN_CONFLUENCE", "3"))   # мин. конфлюенс для моментума

# ─── Batch-скрин Claude: ОДИН запрос по ВСЕМ тикерам («общая картина») ─────────
# Дёшево: Claude бегло судит все тикеры за 1 вызов (~5₽) → шортлист реальных
# сетапов → глубокий разбор только по лучшим (BATCH_SCAN_MAX_DEEP вызовов).
# Втрое дешевле поштучного триажа и покрывает ВСЕ бумаги (ничего не «теряется»).
BATCH_SCAN_ENABLED = os.getenv("BATCH_SCAN_ENABLED", "true").lower() == "true"
BATCH_SCAN_MAX_DEEP = int(os.getenv("BATCH_SCAN_MAX_DEEP", "1"))     # глубоких разборов после batch
BATCH_SCAN_MAX_TOKENS = int(os.getenv("BATCH_SCAN_MAX_TOKENS", "700"))   # потолок ответа batch-скрина
BATCH_SCAN_MAX_TICKERS = int(os.getenv("BATCH_SCAN_MAX_TICKERS", "30"))  # сколько брифов слать в batch (топ по интересу)

# ─── БЮДЖЕТ Claude (жёсткий дневной лимит) ────────────────────────────────────
# Считаем стоимость КАЖДОГО вызова и не даём выйти за дневной лимит: когда
# остаток мал — глубокие разборы отключаются (batch-скрин дешёвый идёт дальше),
# при нуле Claude не зовётся вообще. Так 100₽ гарантированно живут весь день.
AI_DAILY_BUDGET_RUB = float(os.getenv("AI_DAILY_BUDGET_RUB", "100"))
AI_PRICE_IN_RUB_1K = float(os.getenv("AI_PRICE_IN_RUB_1K", "0.5"))    # ₽ за 1К входных токенов
AI_PRICE_OUT_RUB_1K = float(os.getenv("AI_PRICE_OUT_RUB_1K", "2.5"))  # ₽ за 1К выходных токенов
AI_DEEP_MIN_RESERVE_RUB = float(os.getenv("AI_DEEP_MIN_RESERVE_RUB", "12"))  # ниже — только batch

# Авто-старт live-движка при запуске приложения: сканирование → сигналы по
# плейбуку → оценка созревших прогнозов → обучение. Без него «мозг» простаивает
# до ручного /api/live-signals/start и гаснет при каждом редеплое.
# Сканер (Claude-сигналы) — по умолчанию РУЧНОЙ: включаешь, когда садишься торговать,
# выключаешь при выходе (Claude не зовётся → расход 0). Кнопки: /api/live-signals/start|stop.
LIVE_SIGNALS_AUTOSTART = os.getenv("LIVE_SIGNALS_AUTOSTART", "false").lower() == "true"
LIVE_SIGNALS_INTERVAL_MIN = int(os.getenv("LIVE_SIGNALS_INTERVAL_MIN", "30"))  # период сканера, мин (30 = бюджет 100₽ живёт всю сессию)
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
    "YDEX": "Яндекс",
    "TATN": "Татнефть",
    "MTSS": "МТС",
    "MGNT": "Магнит",
    "ALRS": "АЛРОСА",
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
    "X5": "X5 Group",
    "OZON": "Ozon",
    "MOEX": "Мосбиржа",
    "T": "Т-Технологии (ТКС)",
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
