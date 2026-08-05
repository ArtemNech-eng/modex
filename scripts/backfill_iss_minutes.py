"""
Дозаливка минутной истории с MOEX ISS — чтобы норма объёма по времени
суток заработала сегодня, а не к середине августа.

Запуск вручную внутри контейнера:

    python scripts/backfill_iss_minutes.py            # 12 прошлых будних дней
    python scripts/backfill_iss_minutes.py 15          # глубже
    python scripts/backfill_iss_minutes.py 12 --dry    # только считать и сверять
    TICKERS=SBER,GAZP python scripts/backfill_iss_minutes.py 3 --dry   # проба

АВТОЗАПУСКА НЕТ НАМЕРЕННО. Дозаливка нужна один раз; фоновая задача,
кладущая чужие единицы в базу каждый час, ломала бы норму молча.

ТРИ ОТКАЗА ВМЕСТО МОЛЧАНИЯ. День пишется только если:
  1) наш пересчёт оборота сошёлся с рублями ISS с точностью MAX_ERR;
  2) баров не меньше MIN_BARS;
  3) СТРАНИЦЫ ЗАКОНЧИЛИСЬ САМИ, а не упёрлись в предел MAX_PAGES.

Третий пункт — из живого случая 05.08: ISS отдаёт по 500 строк за запрос,
а в базе стрима бывает 777 минут. Обрезанный день проходит и порог
баров, и сверку оборота — а теряет всегда ОДИН край сессии, то есть
создаёт ровно тот перекос по времени суток, ради которого вся эта
дозаливка и делалась.

Лучше видимая дыра (profile_note её покажет), чем норма, сдвинутая
незаметно: сканер тогда не упадёт, а будет молча считать любой живой
выброс тишью.

В базу уходит stream_rows(...) — ровно те ключи, что кладёт стрим,
тем же вызовом db.merge_candle_minutes(tk, rows), что в _flush стрима.
"""
import asyncio
import datetime as dt
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from src import db  # noqa: E402
from src.collector.iss_minutes import (  # noqa: E402
    MAX_PAGES, PAGE, bars_of_pages, by_day, candles_url, page_is_full,
    stream_rows, turnover_error,
)

MAX_ERR = 0.01          # доля; ошибка на лотность дала бы порядка 9.0, не 0.01
PACE = 0.25             # секунд между запросами: ISS бесплатен, но не безграничен
MIN_BARS = 200          # тот же порог «день состоялся», что у day_profile
BOARD = "TQBR"
SECURITIES = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
              f"{BOARD}/securities.json?iss.meta=off"
              "&securities.columns=SECID,LOTSIZE")


def weekdays_back(n: int) -> list:
    """Прошлые будние дни, без сегодня.

    Сегодня исключено намеренно: незаконченный день стал бы «коротким»
    и вдобавок вошёл бы в свою же норму. Праздники отсеивает сам ISS:
    ответ будет пустым, и день просто не запишется.
    """
    days, d = [], dt.date.today()
    while len(days) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.isoformat())
    return sorted(days)


async def lots(client: httpx.AsyncClient) -> dict:
    """Лотность с того же ISS. Без неё пересчёт бессмыслен."""
    r = await client.get(SECURITIES, timeout=30)
    r.raise_for_status()
    block = (r.json().get("securities") or {})
    cols = block.get("columns") or []
    out = {}
    for row in block.get("data") or []:
        rec = dict(zip(cols, row))
        try:
            out[str(rec.get("SECID"))] = max(1, int(rec.get("LOTSIZE") or 1))
        except (TypeError, ValueError):
            continue
    return out


def tickers_wanted() -> list:
    env = (os.getenv("TICKERS") or "").strip()
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    from config.settings import MOEX_TICKERS
    return list(MOEX_TICKERS)


async def fetch_day(client, tk, day, lot):
    """Все страницы одного дня → (бары, страниц, обрезано ли)."""
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


async def one_day(client, tk, day, lot, dry):
    """Один тикер за один день. Возвращает (итог, пояснение)."""
    try:
        bars, pages, truncated = await fetch_day(client, tk, day, lot)
    except Exception as e:                      # сеть/формат — не повод ронять всю заливку
        return "error", f"запрос не удался: {type(e).__name__}"

    if not bars:
        return "empty", "нет данных (праздник или бумага не торговалась)"
    if truncated:
        return "truncated", (f"страницы не кончились за {MAX_PAGES} по {PAGE} — "
                             "день неполный, не пишу")
    if len(bars) < MIN_BARS:
        return "short", f"только {len(bars)} баров, порог {MIN_BARS}"

    #  День берётся одним днём, но ISS иногда присылает соседние сутки
    #  краем страницы — пишем только запрошенный день.
    bars = by_day(bars).get(day, [])
    if len(bars) < MIN_BARS:
        return "short", f"за сам {day} только {len(bars)} баров, порог {MIN_BARS}"

    err = turnover_error(bars, lot=lot)
    if err is None:
        return "unchecked", "ISS не отдал value — сверять не с чем, не пишу"
    if err > MAX_ERR:
        return "mismatch", f"расхождение оборота {err:.4f} > {MAX_ERR} — не пишу"

    if dry:
        return "ok-dry", f"{len(bars)} баров с {pages} страниц, сверка {err:.5f}"
    try:
        await db.merge_candle_minutes(tk, stream_rows(bars))
    except Exception as e:
        return "error", f"запись не удалась: {type(e).__name__}: {e}"
    return "ok", f"{len(bars)} баров с {pages} страниц, сверка {err:.5f}"


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    days_n = int(args[0]) if args else 12
    days = weekdays_back(days_n)
    tickers = tickers_wanted()

    print(f"дозаливка: {len(tickers)} бумаг × {len(days)} дней "
          f"({days[0]}..{days[-1]}){' — без записи' if dry else ''}")
    print(f"день берётся целиком: страница ISS = {PAGE} минут, до {MAX_PAGES} страниц")

    if not dry:
        await db.setup_db()

    tally, notes = {}, []
    async with httpx.AsyncClient() as client:
        lot_of = await lots(client)
        missing = [t for t in tickers if t not in lot_of]
        if missing:
            print(f"лотность не найдена, пропускаю: {', '.join(missing)}")

        for tk in tickers:
            lot = lot_of.get(tk)
            if not lot:
                tally["no-lot"] = tally.get("no-lot", 0) + len(days)
                continue
            wrote, bars_seen = 0, []
            for day in days:
                res, why = await one_day(client, tk, day, lot, dry)
                tally[res] = tally.get(res, 0) + 1
                if res in ("ok", "ok-dry"):
                    wrote += 1
                    bars_seen.append(int(why.split()[0]))
                elif res in ("mismatch", "unchecked", "error", "truncated"):
                    notes.append(f"  {tk} {day}: {why}")
                await asyncio.sleep(PACE)
            span = f", минут в дне {min(bars_seen)}-{max(bars_seen)}" if bars_seen else ""
            print(f"{tk:<6} лот {lot:<5} дней годных {wrote}/{len(days)}{span}")

    print("\nитог: " + ", ".join(f"{k} {v}" for k, v in sorted(tally.items())))
    if notes:
        print("отказы и сбои:")
        for n in notes[:60]:
            print(n)
        if len(notes) > 60:
            print(f"  и ещё {len(notes) - 60}")
    if dry:
        print("НИЧЕГО НЕ ЗАПИСАНО (--dry). Убери --dry, когда сверка устроит.")


if __name__ == "__main__":
    asyncio.run(main())
