"""
Сверка минуток ISS с тем, что стрим записал сам. В БАЗУ НЕ ПИШЕТ.

Запуск:
    python scripts/check_iss_vs_db.py [дней]
    TICKERS=SBER,GAZP,UGLD python scripts/check_iss_vs_db.py 2

Почему этот скрипт вообще нужен. Дозаливка сверяет себя с value самого
ISS — и эта сверка НЕ СПОСОБНА поймать ни неверную лотность (лот
сначала делит, потом умножает и сокращается), ни недобранные страницы
(обе стороны сверки берутся из одного и того же ответа). Единицы
проверяет только сравнение с независимым источником — со стримом за
тот же день.

Поэтому брать надо ДНИ, КОГДА СТРИМ УЖЕ РАБОТАЛ (с 01.08). На более
ранних днях в базе пусто, и ответ будет «мало общих минут» — это
честное «не знаю», а не подтверждение.

День берётся ЦЕЛИКОМ, перебором страниц: ISS отдаёт по 500 строк, а
торговый день с утренней и вечерней сессиями длиннее.
"""
import asyncio
import os
import sys
import pathlib
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src import db  # noqa: E402
from src.collector.iss_minutes import (  # noqa: E402
    MAX_PAGES,
    PAGE,
    bars_of_pages,
    by_day,
    candles_url,
    compare_to_db,
    page_is_full,
    turnover_error,
)

BOARD = "TQBR"
PACE = 0.25
SECURITIES = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
              f"{BOARD}/securities.json?iss.meta=off&securities.columns=SECID,LOTSIZE")


def weekdays_back(n: int) -> list:
    """Последние n будних дней, сегодня НЕ включая (день ещё идёт)."""
    out, d = [], date.today() - timedelta(days=1)
    while len(out) < max(1, n):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


async def lots(client) -> dict:
    r = await client.get(SECURITIES, timeout=30)
    r.raise_for_status()
    block = (r.json() or {}).get("securities") or {}
    cols = block.get("columns") or []
    out = {}
    for row in block.get("data") or []:
        d = dict(zip(cols, row))
        sec, lot = d.get("SECID"), d.get("LOTSIZE")
        if sec and lot:
            out[str(sec)] = int(lot)
    return out


async def fetch_day(client, tk, day, lot):
    """Все страницы одного дня. Возвращает (бары, страниц, обрезано).

    Перебор идёт, пока страница набита докрая. Третьим значением
    возвращается признак «страницы закончились раньше дня» — промолчать
    об этом значило бы вернуть ту же тихую ошибку, только глубже.
    """
    payloads, start, truncated = [], 0, False
    for i in range(MAX_PAGES):
        r = await client.get(candles_url(tk, day, BOARD, start=start), timeout=30)
        r.raise_for_status()
        payload = r.json()
        payloads.append(payload)
        if not page_is_full(payload):
            break
        if i == MAX_PAGES - 1:
            truncated = True
            break
        start += PAGE
        await asyncio.sleep(PACE)
    return bars_of_pages(payloads, lot=lot), len(payloads), truncated


def tickers_wanted() -> list:
    env = os.getenv("TICKERS", "").strip()
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    from config.settings import MOEX_TICKERS
    return list(MOEX_TICKERS)


async def main() -> None:
    days_n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 2
    days = weekdays_back(days_n)
    tickers = tickers_wanted()
    print(f"сверка: {len(tickers)} бумаг × {len(days)} дней ({days[0]}..{days[-1]}) — без записи")
    print("сверка по value ловит цены, сверка со стримом — единицы")
    print(f"день берётся целиком: страница ISS = {PAGE} минут, сессия длиннее\n")

    await db.setup_db()
    tally = {"единицы совпали": 0, "РАСХОЖДЕНИЕ": 0, "нет данных": 0}
    cut = 0

    async with httpx.AsyncClient() as client:
        table = await lots(client)
        for tk in tickers:
            lot = table.get(tk)
            if not lot:
                print(f"{tk:6} лотность не найдена — пропуск")
                continue
            for day in days:
                await asyncio.sleep(PACE)
                try:
                    bars, pages, truncated = await fetch_day(client, tk, day, lot)
                except Exception as e:
                    print(f"{tk:6} {day}  ошибка запроса: {e}")
                    continue
                if not bars:
                    print(f"{tk:6} {day}  ISS пуст")
                    continue

                err = turnover_error(bars, lot=lot)
                err_s = "нет value" if err is None else f"{err:.5f}"
                rows = await db.candle_series(tk, day, "1m")
                got = compare_to_db(by_day(bars).get(day, bars), rows)

                mark = ""
                if truncated:
                    cut += 1
                    mark = f"  !! страницы не кончились за {MAX_PAGES} — день неполный"

                print(f"{tk:6} {day}  лот {lot:<5} страниц {pages}  сверка по value {err_s}  "
                      f"ISS {got['iss_minutes']:3} мин / база {got['db_minutes']:3} мин / "
                      f"общих {got['compared']:3}  → {got['verdict']}{mark}")
                if got["ok"] is True:
                    tally["единицы совпали"] += 1
                elif got["ok"] is False:
                    tally["РАСХОЖДЕНИЕ"] += 1
                else:
                    tally["нет данных"] += 1

    print("\nитог: " + ", ".join(f"{k} {v}" for k, v in tally.items() if v))
    if tally["РАСХОЖДЕНИЕ"]:
        print("ЕСТЬ РАСХОЖДЕНИЯ — дозаливку без --dry ЗАПУСКАТЬ НЕЛЬЗЯ")
    if cut:
        print(f"У {cut} дней страницы не кончились — подними MAX_PAGES в iss_minutes.py")
    print("в базу ничего не записано — этот скрипт только читает")


if __name__ == "__main__":
    asyncio.run(main())
