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
# 900: Claude возвращает ТОЛЬКО сетапы (максимум 5 объектов ≈ 250 токенов), а не
# объект на каждый тикер. На 700 при «объекте на тикер» ответ обрезался, JSON
# рвался и watch всегда выходил 0 — цикл жёг ~4.3₽ вообще без сигналов.
BATCH_SCAN_MAX_TOKENS = int(os.getenv("BATCH_SCAN_MAX_TOKENS", "900"))   # потолок ответа batch-скрина
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
LIVE_SIGNALS_INTERVAL_MIN = int(os.getenv("LIVE_SIGNALS_INTERVAL_MIN", "45"))
# Период сканера, мин. Было 30 с пометкой «бюджет 100₽ живёт всю сессию» —
# ЗАМЕР это опровергает. Фактическая стоимость цикла: batch 2.881₽ + deep
# 5.912₽ = 8.79₽. Основная сессия MOEX 10:00-18:40 это 520 мин, значит:
#   15 мин -> 34 цикла -> 299₽  (бюджет кончится через 2.8 ч)
#   30 мин -> 17 циклов -> 149₽ (кончится через 5.7 ч)
#   45 мин -> 11 циклов ->  97₽ (укладывается)
# При исчерпании бюджет-гард начинает отклонять вызовы, и остаток сессии
# система простаивает молча — то есть сигналов нет именно тогда, когда
# рынок ещё торгуется.
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


# ─── RISK ENGINE: независимый контур управления риском ────────────────────────
# Эти числа НЕ может переопределить Claude: движок в src/risk/ имеет приоритет
# над его решениями. Агент предлагает сценарий — движок решает размер и право.
#
# Почему 0.25% на сделку в фазе обучения. Симуляция 20 000 прогонов, 50 сделок
# в месяц, год: при НУЛЕВОМ эйдже риск 0.25% даёт медиану −0.3% и нулевую
# вероятность потери половины счёта, а 1.33% — медианную просадку 38% и 6.6%
# вероятность потерять половину. При эйдже +0.30R: 0.25% -> +56% за год,
# 1.33% -> +855%. То есть размер ставки — это ставка на существование эйджа,
# а он пока НЕ ИЗМЕРЕН (r_sample = 0). Поднимать после 40-50 закрытых сделок
# с подтверждённым expectancy_r.
RISK_ACCOUNT_RUB = float(os.getenv("RISK_ACCOUNT_RUB", "200000"))
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "0.25"))       # % счёта на сделку
RISK_MAX_POSITION_PCT = float(os.getenv("RISK_MAX_POSITION_PCT", "25"))   # экспозиция на позицию
RISK_MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("RISK_MAX_TOTAL_EXPOSURE_PCT", "50"))
RISK_DAILY_LOSS_R = float(os.getenv("RISK_DAILY_LOSS_R", "3"))            # стоп дня в R
RISK_WEEKLY_LOSS_R = float(os.getenv("RISK_WEEKLY_LOSS_R", "6"))          # стоп недели в R
RISK_MAX_TRADES_DAY = int(os.getenv("RISK_MAX_TRADES_DAY", "3"))
RISK_MAX_OPEN_POSITIONS = int(os.getenv("RISK_MAX_OPEN_POSITIONS", "2"))
RISK_MAX_PER_SECTOR = int(os.getenv("RISK_MAX_PER_SECTOR", "1"))          # неактивно без справочника секторов
RISK_KILL_SWITCH_DD_PCT = float(os.getenv("RISK_KILL_SWITCH_DD_PCT", "5"))


# ─── РАСПИСАНИЕ ТОРГОВ MOEX (МСК, минуты от полуночи) ─────────────────────────
# Зашитые границы были неполными: код знал только основную сессию 10:00-18:50 и
# вечернюю 19:00-23:50, а УТРЕННЯЯ сессия 07:00-09:50 попадала в фазу "closed" —
# почти три часа реальных торгов система считала нерабочим временем и не входила.
# Выносим в конфиг: биржа меняет часы, и это не должно требовать правки кода.
SESSION_MORNING_OPEN = int(os.getenv("SESSION_MORNING_OPEN", str(7 * 60)))        # 07:00
SESSION_MORNING_CLOSE = int(os.getenv("SESSION_MORNING_CLOSE", str(9 * 60 + 50)))  # 09:50
SESSION_MAIN_OPEN = int(os.getenv("SESSION_MAIN_OPEN", str(10 * 60)))            # 10:00
SESSION_MAIN_CLOSE = int(os.getenv("SESSION_MAIN_CLOSE", str(18 * 60 + 50)))     # 18:50
SESSION_EVENING_OPEN = int(os.getenv("SESSION_EVENING_OPEN", str(19 * 60)))      # 19:00
SESSION_EVENING_CLOSE = int(os.getenv("SESSION_EVENING_CLOSE", str(23 * 60 + 50)))  # 23:50

# Разрешать входы в утреннюю сессию. По опыту владельца активные торги идут с
# 07:00 до 22:00, и ликвидность там рабочая. Но полагаться на часы нельзя:
# фактическую торгуемость проверяет гейт ликвидности по спреду и глубине стакана,
# а расписание отвечает только на вопрос «биржа открыта».
SESSION_ALLOW_MORNING_ENTRY = os.getenv("SESSION_ALLOW_MORNING_ENTRY", "true").lower() == "true"

# После этого времени ликвидность падает — новые входы не открываем даже при
# формально открытой сессии (по умолчанию 22:00 МСК).
SESSION_LOW_LIQUIDITY_AFTER = int(os.getenv("SESSION_LOW_LIQUIDITY_AFTER", str(22 * 60)))


# Максимальный возраст последней интрадей-свечи (минуты). Старше — сетапы не
# строим: источник может молча отдать вчерашнюю серию с пометкой «свежая».
# ISS по факту отстаёт на 12-15 минут, поэтому 40 даёт запас, но вчерашние
# данные (возраст ~1000 минут) отсекаются наверняка.
INTRADAY_MAX_AGE_MIN = int(os.getenv("INTRADAY_MAX_AGE_MIN", "40"))


# Допустимое расхождение интрадей-цены с дневной (проценты). Больше — считаем,
# что пришли свечи другого инструмента, и сетапы не строим. Реальные расхождения
# при подмене FIGI были в разы (50-1700%), поэтому 15 отсекает их с запасом и не
# срабатывает на честном внутридневном движении.
INTRADAY_PRICE_MISMATCH_PCT = float(os.getenv("INTRADAY_PRICE_MISMATCH_PCT", "15"))


# ── Что считается НОВОСТЬЮ для детектора событий ──────────────────────────────
# Снимки стакана и сделки трейдеров лежат в том же хранилище событий и идут 2157
# в час против 21 новости, поэтому любой недостаточно узкий фильтр превращает
# опрос Tinkoff в «новостной поток». Держим политику здесь, а не в слое БД:
# это решение о смысле данных, и его нужно уметь прочитать и поменять.
NEWS_KINDS = ("news", "message")
NEWS_EXCLUDE_SOURCES = ("tinkoff", "pulse_deal")

# Окно причинности новости относительно свечи выноса (минуты). Новость может
# заметно опережать движение (рынок переваривает) и может слегка отставать: у RSS
# есть задержка публикации, а лента иногда двигается раньше заголовка. Признак
# «была ли новость за последний час» негоден — заголовок через сорок минут ПОСЛЕ
# выноса его не объясняет.
NEWS_BEFORE_SPIKE_MIN = float(os.getenv("NEWS_BEFORE_SPIKE_MIN", "30"))
NEWS_AFTER_SPIKE_MIN = float(os.getenv("NEWS_AFTER_SPIKE_MIN", "10"))


# ── Годность сетапа ───────────────────────────────────────────────────────────
# Минимальный R/R, при котором план входа вообще выдаётся. R/R считался и раньше,
# но никто на него не смотрел: замер 30.07 в 14:04 показал, что из 36 сетапов ORB
# у 35 он был ниже 1.0, медиана около 0.27 (MGNT 0.12, SMLT 0.22 при риске 5.1%).
SETUP_MIN_RR = float(os.getenv("SETUP_MIN_RR", "1.5"))

# Сколько минут после формирования диапазона открытия его пробой ещё считается
# сетапом. Это техника первого часа: в 14:04 «пробой» диапазона 10:00-10:30 — уже
# не пробой, а констатация того, что рынок ниже утреннего. Именно так 30.07
# появились десять шортов при рынке, выросшем на 0.89% (32 бумаги вверх, 15 вниз),
# включая шорт по DIAS, прибавившему 2.54% и стоявшему ВЫШЕ VWAP.
ORB_VALID_MIN = int(os.getenv("ORB_VALID_MIN", "90"))


# ── Пробой внутридневной консолидации ─────────────────────────────────────────
# Закрывает дырку: пробой диапазона открытия живёт только первые 90 минут, и в
# середине дня у системы не было ни одной техники входа. 30.07 Мечел прошёл 7.1%,
# а в момент правильного входа дневная техника рекомендовала ШОРТ.
#
# Порог ширины 1.2 выбран по проверке на 12 бумагах и 18 торговых днях июля 2026:
# только он держит положительное ожидание в ОБЕИХ половинах выборки и после
# издержек (+0.291R и +0.149R). При 2.0 знак между половинами меняется.
BREAKOUT_WINDOW_BARS = int(os.getenv("BREAKOUT_WINDOW_BARS", "6"))
BREAKOUT_MAX_WIDTH_ATR = float(os.getenv("BREAKOUT_MAX_WIDTH_ATR", "1.2"))
BREAKOUT_VOL_MULT = float(os.getenv("BREAKOUT_VOL_MULT", "1.5"))
BREAKOUT_TARGET_R = float(os.getenv("BREAKOUT_TARGET_R", "2.0"))
BREAKOUT_MAX_RISK_PCT = float(os.getenv("BREAKOUT_MAX_RISK_PCT", "3.0"))
BREAKOUT_ENABLED = os.getenv("BREAKOUT_ENABLED", "true").lower() == "true"

# РЕЖИМ СЕТАПА. Проверка на 18 днях июля дала +0.264R, и я зашил сетап как рабочий.
# Проверка на 181 торговом дне (январь-июль 2026, где март-июнь падали) показала, что
# ЛОНГОВАЯ сторона убыточна во ВСЕХ конфигурациях после издержек: лучшая -0.113R.
# Те +0.264R были эффектом режима на 55 наблюдениях.
#
#   "observe" — считается и записывается, но НЕ является сигналом к сделке.
#               Исходы оцениваются автоматически, чтобы решать по живым числам.
#   "signal"  — полноценный сетап (включать только после подтверждения на живых данных)
#   "off"     — не считается вовсе, наблюдений не будет
#
# По умолчанию observe: выключить целиком значит потерять данные, а считать сигналом
# значит торговать без доказанного преимущества.
BREAKOUT_MODE = os.getenv("BREAKOUT_MODE", "observe").lower()

# Через сколько минут оценивать исход наблюдения. Полтора часа — компромисс: успевает
# разрешиться большинство внутридневных сетапов, и оценка не тянется до конца дня.
SETUP_OUTCOME_AFTER_MIN = int(os.getenv("SETUP_OUTCOME_AFTER_MIN", "90"))

# КАКИЕ СТОРОНЫ РАЗРЕШЕНЫ В КАКОЙ ФАЗЕ. Разбивка по сессиям на 181 торговом дне
# показала, что утренняя сессия ведёт себя ПРОТИВОПОЛОЖНО основной:
#
#   утро 07:00-09:50   лонг  -0.402R   ШОРТ +0.302R -> +0.210R после издержек
#   основная           лонг  +0.076R   шорт -0.089R
#   вечер              обе стороны отрицательно
#
# Утренний шорт проверен отдельно: половины выборки +0.200R и +0.204R, 6 месяцев из 7
# положительны, 10 бумаг из 12 положительны. Лучший месяц — ЯНВАРЬ (+0.600R), когда
# рынок РОС, то есть это не эффект падающего рынка. Механизм: утренняя ликвидность
# тонкая, пробой вверх не находит продолжения, пробой вниз идёт дальше.
BREAKOUT_LONG_PHASES = os.getenv("BREAKOUT_LONG_PHASES", "main")
BREAKOUT_SHORT_PHASES = os.getenv("BREAKOUT_SHORT_PHASES", "morning")

# Порог ширины сжатия РАЗНЫЙ для сторон. У утреннего шорта устойчивое преимущество
# именно на 1.5: 275 входов, половины выборки +0.200R и +0.204R, 6 месяцев из 7
# положительны. У лонга лучший из плохих вариантов — 1.2 (и он всё равно в минусе
# после издержек). Один общий порог означал бы либо потерять проверенный шорт, либо
# впустить непроверенный лонг.
BREAKOUT_MAX_WIDTH_ATR_SHORT = float(os.getenv("BREAKOUT_MAX_WIDTH_ATR_SHORT", "1.5"))

# ── НАБЛЮДЕНИЯ ШИРЕ, ЧЕМ СИГНАЛЫ ──────────────────────────────────────────────
# Сигнальные пороги узкие: только проверенное. Но при них за день не набирается ни
# одного наблюдения, а значит через месяц решать будет не по чему — в этом и была
# проблема с «сигналов мало».
#
# Наблюдение — это строка в базе, а не сделка: оно ничего не стоит и ничем не
# рискует. Поэтому сеть наблюдений шире: обе стороны, все торговые фазы, порог
# ширины 2.0 вместо 1.2/1.5. По бэктесту это даёт порядка 15 наблюдений в день на
# 48 бумагах, то есть около 300 в месяц — уже выборка, на которой видно, какие
# срезы работают.
#
# Рекомендацией к сделке наблюдение НЕ является: у него validated=False, и в
# журнал оно идёт отдельным видом события.
BREAKOUT_OBSERVE_MAX_WIDTH_ATR = float(os.getenv("BREAKOUT_OBSERVE_MAX_WIDTH_ATR", "2.0"))
BREAKOUT_OBSERVE_VOL_MULT = float(os.getenv("BREAKOUT_OBSERVE_VOL_MULT", "1.5"))
BREAKOUT_OBSERVE_PHASES = os.getenv("BREAKOUT_OBSERVE_PHASES", "morning,main,evening")


# ── Быстрый наблюдатель сетапов (без Claude) ──────────────────────────────────
# Сканер с подтверждением Claude ходит раз в 45 минут. Сетап пробоя по Мечелу 30.07
# сработал в 13:25, был бы увиден в 14:10 при цене уже 39.16, и опоздание стоило
# половины прибыли: вход 13:25 дал бы 8 056₽ против фактических 4 088₽.
#
# Интервал 5 минут, а не чаще: сетапы считаются по ЗАКРЫТИЮ пятиминутного бара,
# поэтому один проход на бар — и максимальная осмысленная скорость, и минимальная
# нагрузка на лимиты Tinkoff.
SETUP_WATCH_ENABLED = os.getenv("SETUP_WATCH_ENABLED", "false").lower() == "true"
SETUP_WATCH_INTERVAL_MIN = int(os.getenv("SETUP_WATCH_INTERVAL_MIN", "5"))
SETUP_WATCH_DEDUP_MIN = int(os.getenv("SETUP_WATCH_DEDUP_MIN", "30"))
SETUP_WATCH_PACING_SEC = float(os.getenv("SETUP_WATCH_PACING_SEC", "0.2"))
# Пауза перед ПЕРВЫМ проходом. Проход — это 48 бумаг и около двух сотен запросов за
# 18 секунд; в момент старта контейнера он конкурирует с healthcheck (curl
# /api/stats, таймаут 10 сек, три попытки) и с подъёмом FIGI по всем бумагам.
SETUP_WATCH_WARMUP_SEC = int(os.getenv("SETUP_WATCH_WARMUP_SEC", "45"))
