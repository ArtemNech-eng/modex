"""
Сборка торгового дня одним куском — для аналитика.

ЧТО ЭТО. Модуль складывает уже записанные минуты из разных таблиц в одну
структуру и честно помечает, чего не хватает.

ЧЕГО ЗДЕСЬ НЕТ И НЕ БУДЕТ. Ни одного детектора, порога и вердикта.
Решение принимает аналитик, глядя на числа. Если слой сборки начнёт
подсказывать («всплеск», «пробой»), аналитик будет спорить не с рынком,
а с нашей эвристикой, и ошибка перестанет быть видной.

ТРИ ПРАВИЛА, КОТОРЫЕ ЗДЕСЬ СОБЛЮДАЮТСЯ.

1. Пропуск — это missing, а не ноль. Ноль в обороте читается как «торгов не
   было» — утверждение о рынке, которое мы не проверяли.
2. Рядом с числом едет его норма. Оборот 47 млн ничего не значит, пока не
   сказано, что обычно в эту минуту дня проходит 14 млн.
3. Каждый блок несёт СВОЙ возраст. Стакан может быть свежим, а свечи
   отставать на десять минут; общий штамп времени это бы скрыл.

НОРМА СЧИТАЕТСЯ ПО МИНУТЕ ДНЯ, а не по суткам целиком. В 10:00 и в 14:00
рынок торгует совершенно по-разному, и единая средняя по дню объявила бы
всплеском каждое открытие и штилём каждый обед.

МИНУТЫ И СТРОКИ — РАЗНЫЕ ВЕЩИ. Свечи дают одну строку на минуту, а уровни
стакана — поток событий: 61 615 строк на 1020 минут в один день по SBER.
Если считать строки минутами, полнота ряда превращается в бессмыслицу,
поэтому наружу едут оба числа.

Модуль ЧИСТЫЙ: только стандартная библиотека, ни базы, ни сети. Поэтому
его можно гонять в CI, где нет ни sqlalchemy, ни доступа к бирже.
"""
from datetime import datetime, timedelta

MSK_SHIFT_H = 3
MINUTE_FMT = "%Y-%m-%dT%H:%M"

# Границы сессий в минутах от полуночи по Москве, включительно.
SESSIONS = (
    ("утро", 6 * 60 + 50, 9 * 60 + 59),
    ("основная", 10 * 60, 18 * 60 + 39),
    ("вечер", 19 * 60, 23 * 60 + 49),
)

# Доля заполненных минут, ниже которой ряд считается дырявым.
# 0.9 взята из замера покрытия 05–06.08: основная сессия даёт 99–100%,
# утро и вечер 94–95% из-за тонкой ликвидности, а не из-за сбоев.
THIN_SHARE = 0.9

# Минимальное число дней, на которых норма вообще имеет смысл.
# Норма по одному дню — это не норма, а случайное число.
MIN_DAYS_FOR_NORM = 3


def now_msk(at=None):
    """Сейчас по Москве. Контейнер живёт в UTC, а ключи минут — московские."""
    return at or (datetime.utcnow() + timedelta(hours=MSK_SHIFT_H))


def minute_of_day(ts):
    """Минута дня из ключа '2026-08-06T14:32' → 872."""
    return int(ts[11:13]) * 60 + int(ts[14:16])


def session_of(mm):
    """Имя сессии для минуты дня или None вне торгов."""
    for name, lo, hi in SESSIONS:
        if lo <= mm <= hi:
            return name
    return None


def median(vals):
    """Медиана. Берётся именно она, а не среднее: один выброс на открытии
    сдвигает среднее в разы и делает норму недостижимой."""
    xs = sorted(float(v) for v in vals)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    if n % 2:
        return xs[mid]
    return round((xs[mid - 1] + xs[mid]) / 2, 6)


def expected_minutes(day, at=None):
    """
    Сколько минут МОГЛО быть записано к моменту at, по сессиям.

    Для сегодняшнего дня сессия обрезается текущим временем: иначе в 11:00
    основная сессия выглядела бы заполненной на 12%, хотя всё в порядке.
    """
    at = now_msk(at)
    today = at.strftime("%Y-%m-%d")
    cur = at.hour * 60 + at.minute
    out = {}
    for name, lo, hi in SESSIONS:
        if day > today:
            out[name] = 0
            continue
        end = hi if day < today else min(hi, cur)
        out[name] = max(0, end - lo + 1) if end >= lo else 0
    return out


def minutes_of(rows, day):
    """
    Множество уникальных минут дня, встреченных в строках.

    Ключ режется до 16 символов: у части таблиц в ts прилетают секунды,
    и без обрезки одна минута расползлась бы на шестьдесят.
    """
    out = set()
    for r in rows or []:
        ts = str(r.get("ts") or "")[:16]
        if ts and ts.startswith(day):
            out.add(ts)
    return out


def completeness(rows, day, at=None):
    """
    Сколько минут есть против того, сколько могло быть — по сессиям.

    Дубли по ts не считаются дважды: при перекате деплоя два контейнера
    могут на короткое время писать в одну минуту, а у потоковых таблиц
    на одну минуту приходятся десятки событий.
    """
    exp = expected_minutes(day, at)
    got = {name: 0 for name, _, _ in SESSIONS}
    for ts in minutes_of(rows, day):
        name = session_of(minute_of_day(ts))
        if name:
            got[name] += 1
    out = {}
    for name, _, _ in SESSIONS:
        e = exp[name]
        out[name] = {
            "есть": got[name],
            "могло_быть": e,
            "доля": round(got[name] / e, 3) if e else None,
        }
    return out


def thin_sessions(comp):
    """Имена сессий, где ряд дырявый. Пустые сессии не считаются
    дырявыми: если торги ещё не начались, дыры нет."""
    out = []
    for name, block in (comp or {}).items():
        share = block.get("доля")
        if share is not None and share < THIN_SHARE:
            out.append(name)
    return out


def last_row(rows, day=None):
    """Последняя по времени строка или None. Порядок входа не доверяется."""
    best = None
    for r in rows or []:
        ts = r.get("ts")
        if not ts or (day and not ts.startswith(day)):
            continue
        if best is None or ts > best["ts"]:
            best = r
    return best


def age_sec(ts, at=None):
    """
    Возраст минутной строки в секундах от КОНЦА её минуты.

    От конца, а не от начала: минута 14:32 закрывается в 14:33, и до этого
    момента она не устарела вовсе. Иначе свежая строка всегда выглядела бы
    протухшей на полминуты.

    Отрицательный возраст обрезается в ноль: штамп биржи иногда идёт
    вперёд наших часов, и минус здесь смутил бы читателя.
    """
    if not ts:
        return None
    at = now_msk(at)
    try:
        cell = datetime.strptime(ts[:16], MINUTE_FMT)
    except ValueError:
        return None
    delta = (at - (cell + timedelta(minutes=1))).total_seconds()
    return int(delta) if delta > 0 else 0


def minute_norm(history, mm, key="turnover_rub"):
    """
    Норма значения для КОНКРЕТНОЙ минуты дня по прошлым дням.

    Возвращает (медиана, число_дней). Медиана равна None, пока дней
    меньше MIN_DAYS_FOR_NORM: лучше честное «нормы нет», чем число, которому
    нельзя верить.

    Нули и пустоты в выборку не идут: ноль оборота у нас означает
    «не посчитано», а не «торгов не было».
    """
    vals = []
    days = set()
    for r in history or []:
        ts = r.get("ts")
        if not ts or minute_of_day(ts) != mm:
            continue
        v = r.get(key)
        if v in (None, ""):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        vals.append(v)
        days.add(ts[:10])
    if len(days) < MIN_DAYS_FOR_NORM:
        return None, len(days)
    return median(vals), len(days)


def block(rows, day, at=None):
    """
    Общая справка по одной таблице: сколько минут покрыто, сколько строк
    пришло, какая минута последняя, какой у неё возраст и насколько ряд полон.

    «минут» и «строк» специально разведены. У свечей они совпадают, у уровней
    стакана расходятся в шестьдесят раз, и если бы наружу ехало одно число,
    полнота ряда врала бы на порядок.
    """
    rows = [r for r in (rows or []) if str(r.get("ts", "")).startswith(day)]
    last = last_row(rows, day)
    comp = completeness(rows, day, at)
    return {
        "минут": len(minutes_of(rows, day)),
        "строк": len(rows),
        "последняя": last.get("ts") if last else None,
        "возраст_сек": age_sec(last.get("ts"), at) if last else None,
        "полнота": comp,
        "дырявые_сессии": thin_sessions(comp),
        "значения": last,
    }


def assemble(ticker, day, blocks, history=None, market=None, at=None):
    """
    Собрать день по одной бумаге.

    blocks  — {"свечи": [...], "поток": [...], "стакан": [...], ...}, строки
              как их отдают читатели базы (словари с ключом ts).
    history — минуты ПРОШЛЫХ дней той же бумаги для нормы.
    market  — строки фона рынка (IMOEX) за тот же день.

    В missing попадает всё, чего нет или чему нельзя верить. Пустой missing
    — единственное условие, при котором срез можно читать как есть.
    """
    at = now_msk(at)
    out_blocks = {}
    missing = []
    for name, rows in (blocks or {}).items():
        b = block(rows, day, at)
        out_blocks[name] = b
        if not b["минут"]:
            missing.append(f"{name}: нет данных за день")
            continue
        for sess in b["дырявые_сессии"]:
            share = b["полнота"][sess]["доля"]
            missing.append(f"{name}: {sess} заполнена на {int(share * 100)}%")

    candles = [r for r in (blocks or {}).get("свечи", [])
               if str(r.get("ts", "")).startswith(day)]
    last_candle = last_row(candles, day)

    norm = {}
    if last_candle:
        mm = minute_of_day(last_candle["ts"])
        value, days = minute_norm(history, mm, "turnover_rub")
        norm = {
            "оборот_руб_обычно": value,
            "дней_в_норме": days,
            "минута_дня": mm,
        }
        fact = last_candle.get("turnover_rub")
        if fact in (None, "", 0, 0.0):
            missing.append("оборот в рублях не посчитан для последней минуты")
        elif value:
            norm["оборот_к_норме"] = round(float(fact) / value, 3)
        else:
            missing.append(f"нормы оборота нет: дней в истории {days}")

    market_last = last_row(market, day)
    market_out = None
    if market_last:
        market_out = {
            "имя": market_last.get("name"),
            "значение": market_last.get("value"),
            "изменение_проц": market_last.get("change_pct"),
            "минута": market_last.get("ts"),
            "возраст_сек": market_last.get("age_sec"),
            "штамп_биржи": market_last.get("exch_ts"),
        }
    else:
        missing.append("фон рынка: нет данных за день")

    return {
        "бумага": ticker.upper(),
        "день": day,
        "собрано": at.strftime(MINUTE_FMT),
        "единицы": {
            "turnover_rub": "рубли за минуту",
            "volume": "лоты",
            "возраст_сек": "секунды от конца минуты",
        },
        "блоки": out_blocks,
        "норма": norm,
        "рынок": market_out,
        "missing": missing,
    }
