"""
РАСПИСАНИЕ ТОРГОВ MOEX — ОДИН ИСТОЧНИК ПРАВДЫ.

ЗАЧЕМ ФАЙЛ. Границы сессий были записаны в проекте ТРИ РАЗА, и все три
версии расходились:

    config/settings.py    утро 07:00-09:50, основная 10:00-18:50, вечер 19:00-23:50
    day_slice.SESSIONS    утро 06:50-09:59, основная 10:00-18:39, вечер 19:00-23:49
    stream.session_of     всё до 09:50 = morning, до 19:00 = main, дальше evening

Последняя — самая дорогая: метка сессии из неё ПОПАДАЕТ В БАЗУ (колонка
candle_minute.session) и уезжает наружу в /api/candles. Из-за неё

  * аукцион открытия 09:50-09:59 помечался как основная сессия;
  * перерыв 18:50-18:59 помечался как основная сессия;
  * ночь до 06:50 помечалась как УТРЕННЯЯ СЕССИЯ;
  * вечерняя сессия существовала в метке, но её никто не отделял от ночи.

Пока одна и та же минута получает разные метки в разных таблицах, любая
статистика по времени суток считается по трём разным дням.

ЧТО ЗДЕСЬ ЕСТЬ. Чистые функции над «минутой от полуночи по Москве» и
границы, читаемые из окружения теми же именами, что в config/settings.py.

ПОЧЕМУ os.environ, А НЕ config.settings. day_slice обещает в своей шапке
только стандартную библиотеку — его гоняют в CI, где нет ни sqlalchemy,
ни python-dotenv. Импорт config.settings притащил бы dotenv. Источник при
этом ОДИН И ТОТ ЖЕ — переменные окружения; .env их и заполняет.

ФАЗ ШЕСТЬ, А НЕ ТРИ. "pre" и "break" — не мелочь: в аукционе и в перерыве
нет нормального стакана, и входы там запрещены. Смешать их с основной
сессией значит разрешить вход в аукцион.
"""
import os

DAY_MINUTES = 24 * 60


def _minute(name, default):
    """Минута от полуночи из окружения. Мусор и выход за сутки — молча
    в дефолт: расписание не то место, где падать при опечатке в .env."""
    try:
        value = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    if 0 <= value <= DAY_MINUTES:
        return value
    return default


#  Утренний аукцион. Начало дня в данных — 06:50, а не 07:00: первый бар
#  приходит в 06:5x (по MAGN 18.08 это 06:59, 2234 лота). Это НЕ утренняя
#  сессия, входов там нет, но и терять эти минуты нельзя.
AUCTION_OPEN = _minute("SESSION_AUCTION_OPEN", 6 * 60 + 50)

MORNING_OPEN = _minute("SESSION_MORNING_OPEN", 7 * 60)
MORNING_CLOSE = _minute("SESSION_MORNING_CLOSE", 9 * 60 + 50)
MAIN_OPEN = _minute("SESSION_MAIN_OPEN", 10 * 60)
MAIN_CLOSE = _minute("SESSION_MAIN_CLOSE", 18 * 60 + 50)
EVENING_OPEN = _minute("SESSION_EVENING_OPEN", 19 * 60)
EVENING_CLOSE = _minute("SESSION_EVENING_CLOSE", 23 * 60 + 50)

#  Торговые окна: [открытие, закрытие). Закрытие ИСКЛЮЧИТЕЛЬНО — минута
#  18:50 это уже не основная сессия, а аукцион закрытия.
TRADING = (
    ("morning", MORNING_OPEN, MORNING_CLOSE),
    ("main", MAIN_OPEN, MAIN_CLOSE),
    ("evening", EVENING_OPEN, EVENING_CLOSE),
)

RU = {"morning": "утро", "main": "основная", "evening": "вечер"}


def phase(mm, weekday=None):
    """
    Фаза дня для минуты от полуночи: morning | pre | main | break | evening
    | closed.

    weekday — 0..6, как datetime.weekday(). В выходные биржа не торгует при
    любом времени суток, а Tinkoff в субботу отдаёт ДИЛЕРСКИЕ сделки при
    закрытой бирже: 15.08.2026 по SBER так набралось 278 488 акций и 76 млн
    рублей «оборота». Без дня недели такая суббота выглядит торговым днём.
    """
    try:
        mm = int(mm)
    except (TypeError, ValueError):
        return "closed"
    if weekday is not None:
        try:
            if int(weekday) >= 5:
                return "closed"
        except (TypeError, ValueError):
            pass
    if AUCTION_OPEN <= mm < MORNING_OPEN:
        return "pre"
    if MORNING_OPEN <= mm < MORNING_CLOSE:
        return "morning"
    if MORNING_CLOSE <= mm < MAIN_OPEN:
        return "pre"
    if MAIN_OPEN <= mm < MAIN_CLOSE:
        return "main"
    if MAIN_CLOSE <= mm < EVENING_OPEN:
        return "break"
    if EVENING_OPEN <= mm < EVENING_CLOSE:
        return "evening"
    return "closed"


def phase_of_ts(ts, weekday=None):
    """Фаза по ключу минуты '2026-08-18T09:55'. Битый ключ — closed."""
    s = str(ts or "")
    if len(s) < 16:
        return "closed"
    try:
        return phase(int(s[11:13]) * 60 + int(s[14:16]), weekday)
    except (TypeError, ValueError):
        return "closed"


def is_trading(mm, weekday=None):
    """Идут ли непрерывные торги. Аукцион и перерыв — НЕ торги."""
    return phase(mm, weekday) in ("morning", "main", "evening")


def session_open(mm, weekday=None):
    """Открытие ТОЙ сессии, в которой находится минута, или None.

    От этого числа отсчитывается фильтр шума первых минут: у утра, основной
    и вечера открытие своё, и жёсткое 10:00 не отсекало ни утренний,
    ни вечерний шум.
    """
    name = phase(mm, weekday)
    for label, lo, _hi in TRADING:
        if label == name:
            return lo
    return None


def windows_inclusive():
    """Торговые окна с ВКЛЮЧИТЕЛЬНОЙ последней минутой.

    Учёт полноты ряда считает минуты, а не интервалы: последняя минута
    основной сессии — 18:49, потому что бар 18:50 это уже аукцион.
    """
    return tuple((name, lo, hi - 1) for name, lo, hi in TRADING)


def windows_inclusive_ru():
    """То же, но с русскими именами — так их ждёт day_slice."""
    return tuple((RU[name], lo, hi) for name, lo, hi in windows_inclusive())


def bounds():
    """Границы для вывода наружу и для проверок в тестах."""
    return {
        "auction_open": AUCTION_OPEN,
        "morning": [MORNING_OPEN, MORNING_CLOSE],
        "main": [MAIN_OPEN, MAIN_CLOSE],
        "evening": [EVENING_OPEN, EVENING_CLOSE],
    }
