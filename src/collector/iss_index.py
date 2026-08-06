"""
Разбор ответа MOEX ISS по индексу. Чистые функции, без сети и без базы.

ЗАЧЕМ ВООБЩЕ ИНДЕКС. Одно и то же движение бумаги означает разное в
зависимости от того, куда идёт всё остальное. Без рыночного фона аналитик
будет объяснять собственными причинами бумаги каждый день, когда просто
растёт всё сразу.

ЗАМЕР ЗАДЕРЖКИ (06.08.2026, живые данные, две пробы):

    SYSTIME 13:04:22   часы 13:04:44   отставание 22 сек   значение 2275.24
    SYSTIME 13:34:00   часы 13:34:14   отставание 14 сек   значение 2273.74

Между пробами оборот по индексу вырос с 32.5 до 35.7 млрд ₽ — значит это не
кэш и не замороженное число. Пятнадцать минут, о которых говорится в документации,
относятся к МИНУТНЫМ СВЕЧАМ истории, а не к текущему значению индекса.

ВОЗРАСТ СЧИТАЕТСЯ ОТ МЕТКИ БИРЖИ, А НЕ ОТ ВРЕМЕНИ ЗАПРОСА. Эта ошибка уже
ловилась на живых данных 02.08: поле возраста показывало «11 секунд» для
значения, снятого биржей двумя сутками раньше, при закрытой бирже.

НИЧЕГО НЕ ПОДСТАВЛЯЕТСЯ. Если ISS не ответил или ответил мусором, возвращается
НИЧЕГО, а не ноль изменения. Ноль изменения — это «рынок стоит на месте»,
самостоятельное утверждение о рынке, и выдавать за него собственный провал
связи нельзя.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

MSK_SHIFT_H = 3

BASE = "https://iss.moex.com/iss/engines/stock/markets/index/securities"

#  Старше этого значение помечается несвежим. Три минуты при замеренном
#  отставании 14–22 секунды — запас на порядок, чтобы флаг горел при обрыве
#  связи, а не при обычной задержке сети.
STALE_SEC = 180

#  Небольшой минус — не ошибка: часы сервера и биржи расходятся на секунды.
#  Отбрасывать такое значение нельзя — это самое свежее, что у нас есть.
CLOCK_SKEW_SEC = 90


def index_url(sec: str = "IMOEX") -> str:
    """Адрес текущего значения индекса. Токен не нужен."""
    sec = (sec or "IMOEX").upper()
    return f"{BASE}/{sec}.json?iss.meta=off&iss.only=marketdata"


def now_msk() -> datetime:
    """Московское время. Контейнер живёт в UTC, а все метки биржи — московские."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=MSK_SHIFT_H)


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_systime(s) -> Optional[datetime]:
    """Метка биржи "2026-08-06 13:34:00" → datetime. Московское время, без зоны."""
    if not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def age_sec(systime, at: Optional[datetime] = None) -> Optional[int]:
    """Возраст значения по метке БИРЖИ, а не по времени моего запроса."""
    ts = parse_systime(systime) if not isinstance(systime, datetime) else systime
    if ts is None:
        return None
    sec = int(((at or now_msk()) - ts).total_seconds())
    if -CLOCK_SKEW_SEC <= sec < 0:
        return 0
    return sec


def parse_index(payload, at: Optional[datetime] = None,
                name: str = "IMOEX") -> Optional[dict]:
    """
    Ответ ISS → один словарь со значением и его возрастом.

    ISS отдаёт таблицей: отдельно список имён колонок, отдельно строки
    значений. Порядок колонок меняется от запроса к запросу, поэтому берём
    ПО ИМЕНИ, а не по номеру. Чтение по номеру колонки — тот же класс
    ошибки, что неверная таблица FIGI: данные приходят, они чужие.

    CURRENTVALUE — текущее значение. LASTVALUE — ЗАКРЫТИЕ ПРОШЛОГО ДНЯ, и
    подменять им текущее нельзя: получится вчерашний рынок с сегодняшней
    датой. Нет текущего — значит нет ответа.
    """
    md = (payload or {}).get("marketdata") if isinstance(payload, dict) else None
    if not isinstance(md, dict):
        return None
    cols, data = md.get("columns"), md.get("data")
    if not isinstance(cols, list) or not isinstance(data, list) or not data:
        return None
    row = data[0]
    if not isinstance(row, list) or len(row) != len(cols):
        return None
    d = dict(zip(cols, row))

    value = _num(d.get("CURRENTVALUE"))
    if value is None or value <= 0:
        return None

    out = {
        "name": (d.get("SECID") or name or "").upper(),
        "value": round(value, 4),
        "ts": d.get("SYSTIME"),
        "age_sec": age_sec(d.get("SYSTIME"), at=at),
    }
    #  Каждое поле кладётся только если оно есть. Отсутствующее поле должно
    #  отсутствовать, а не равняться нулю.
    for key, col in (("change_pct", "LASTCHANGEPRC"),
                     ("change_to_open_pct", "LASTCHANGETOOPENPRC"),
                     ("open", "OPENVALUE"), ("high", "HIGH"), ("low", "LOW"),
                     ("prev_close", "LASTVALUE"), ("valtoday_rub", "VALTODAY")):
        v = _num(d.get(col))
        if v is not None:
            out[key] = round(v, 4)
    if isinstance(d.get("TRADEDATE"), str):
        out["day"] = d["TRADEDATE"]
    if out["age_sec"] is not None and out["age_sec"] > STALE_SEC:
        out["stale"] = True
    return out
