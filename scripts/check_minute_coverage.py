"""
Сколько минут реально записано в минутные таблицы. В БАЗУ НЕ ПИШЕТ.

Запуск:
    python scripts/check_minute_coverage.py [дней]
    TICKERS=SBER,GAZP python scripts/check_minute_coverage.py 1

Зачем этот скрипт. Решено строить срез данных для вердикта вместо новых
детекторов. Срез целиком стоит на том, что минуты действительно есть в базе.
Если из сессии записана половина, то «оборот 3.5× нормы» и «покупок 68%»
будут выглядеть ровно так же убедительно и будут неверны.

Что считается
  * число бумаг с хотя бы одной минутой за день;
  * сколько РАЗНЫХ МИНУТ записано каждой бумаге в каждой сессии;
  * доля от того, сколько минут МОГЛО быть к этому часу;
  * пять самых дырявых бумаг поимённо.

ДВА РЕШЕНИЯ, КОТОРЫЕ ЛЕГКО ПРИНЯТЬ НЕВЕРНО.

1. Считаются МНОЖЕСТВА минут, а не строки. В flow_minute и book_minute ключ включает
   источник (exchange и dealer пишутся в разные строки). Счёт строк показал бы
   до 200% покрытия — и это выглядело бы как отличная новость.

2. Сессии разделены. Перерыв 18:40–18:59 и ночь — это расписание биржи, а не
   потеря данных. На этой же ошибке раньше стоял ключ бара без даты: вечер
   склеивался со следующим утром.

Чего этот скрипт НЕ говорит: верны ли числа внутри строк. Наличие минуты и
правильность её содержимого — разные вопросы; второй проверяет
`check_iss_vs_db.py`, сверяя с независимым источником.
"""
import asyncio
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from statistics import median

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from src import db  # noqa: E402

MSK_SHIFT_H = 3

#  Имена классов берутся через getattr: если таблица ещё не заведена, скрипт
#  должен сказать об этом вслух, а не упасть на импорте целиком.
TABLES = [
    ("свечи  ", "CandleMinute", "OHLC и объём"),
    ("поток  ", "FlowMinute", "сделки, дельта, VWAP"),
    ("стакан ", "BookMinute", "перекос и топ-5"),
    ("секунды", "MicroMinute", "динамика внутри минуты"),
    ("уровни ", "LevelMinute", "съели против сняли"),
]

#  Границы сессий — те же, что в config/settings.py.
SESSIONS = (
    ("утро", 6 * 60 + 50, 9 * 60 + 59),
    ("основная", 10 * 60, 18 * 60 + 39),
    ("вечер", 19 * 60, 23 * 60 + 49),
)


def msk_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=MSK_SHIFT_H)


def weekdays(n: int) -> list:
    """Последние n будних дней, СЕГОДНЯ ВКЛЮЧАЯ — текущий день интереснее всего."""
    out, d = [], msk_now().date()
    while len(out) < max(1, n):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def minute_of_day(ts: str):
    """"2026-08-06T14:32" -> 872. Неразборчивое значение — None, а не ноль."""
    try:
        return int(ts[11:13]) * 60 + int(ts[14:16])
    except (TypeError, ValueError, IndexError):
        return None


def expected(day: str, lo: int, hi: int) -> int:
    """Сколько минут МОГЛО быть к этому часу. Для прошедших дней — сессия целиком.

    Без этого числа любое количество минут выглядит нормально: 120 минут в 11:00
    — полное покрытие, а в 18:00 это потеря двух третей сессии.
    """
    now = msk_now()
    if day < now.date().isoformat():
        return hi - lo + 1
    cur = now.hour * 60 + now.minute
    if cur < lo:
        return 0
    return min(hi, cur) - lo + 1


async def minutes_per_ticker(model, day: str, wanted: set) -> dict:
    """{тикер: {минута дня}}. Множество схлопывает exchange и dealer в одну минуту."""
    async with db.async_session() as session:
        res = await session.execute(
            select(model.ticker, model.ts).where(model.ts.like(f"{day}%")))
        rows = res.all()
    out: dict = {}
    for tk, ts in rows:
        if wanted and tk not in wanted:
            continue
        mm = minute_of_day(ts)
        if mm is None:
            continue
        out.setdefault(tk, set()).add(mm)
    return out


def report(title: str, hint: str, day: str, per_ticker: dict) -> None:
    if not per_ticker:
        print(f"{title} {day}  НИ ОДНОЙ СТРОКИ ({hint})")
        return
    print(f"{title} {day}  бумаг {len(per_ticker):3}  ({hint})")
    for name, lo, hi in SESSIONS:
        want = expected(day, lo, hi)
        if want <= 0:
            continue
        got = {tk: len([m for m in mins if lo <= m <= hi])
               for tk, mins in per_ticker.items()}
        vals = sorted(got.values())
        if not vals or max(vals) == 0:
            print(f"    {name:9} пусто (могло быть {want})")
            continue
        med = int(median(vals))
        print(f"    {name:9} могло быть {want:4}  медиана {med:4} "
              f"({med / want:.0%})  минимум {vals[0]:4}  максимум {vals[-1]:4}")
        worst = sorted(got.items(), key=lambda kv: kv[1])[:5]
        print("              хуже всех: "
              + ", ".join(f"{tk} {n}" for tk, n in worst))


async def main() -> None:
    days_n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 2
    days = weekdays(days_n)
    env = os.getenv("TICKERS", "").strip()
    wanted = {t.strip().upper() for t in env.split(",") if t.strip()} if env else set()

    print(f"покрытие минутных таблиц: {len(days)} дней ({days[0]}..{days[-1]})"
          + (f", только {', '.join(sorted(wanted))}" if wanted else ""))
    print(f"сейчас по МСК {msk_now().strftime('%Y-%m-%d %H:%M')} — сегодняшний день ещё идёт")
    print("считаются РАЗНЫЕ МИНУТЫ, а не строки: exchange и dealer — одна минута")
    print("в базу ничего не записывается\n")

    await db.setup_db()

    for title, class_name, hint in TABLES:
        model = getattr(db, class_name, None)
        if model is None:
            print(f"{title} нет класса {class_name} в src/db.py — пропуск\n")
            continue
        for day in days:
            try:
                per_ticker = await minutes_per_ticker(model, day, wanted)
            except Exception as e:                              # noqa: BLE001
                print(f"{title} {day}  ошибка чтения: {type(e).__name__}: {e}")
                continue
            report(title, hint, day, per_ticker)
        print()

    print("как читать: медиана ниже 90% — срез будет считаться по дырявому ряду")
    print("этот скрипт не проверяет ПРАВИЛЬНОСТЬ чисел — только их НАЛИЧИЕ"))


if __name__ == "__main__":
    asyncio.run(main())
