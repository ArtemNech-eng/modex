"""Тесты свежести и происхождения интрадей-данных.

Запуск: python3 tests/test_data_freshness.py

30.07 в 10:20 МСК скрин выдал пятнадцать тикеров с «диапазоном открытия», хотя
основная сессия шла двадцать минут. Разбор показал три независимые причины:

  1) ISS публикует время свечей ПО МОСКВЕ, а коллектор помечал его как UTC —
     каждая свеча уезжала на +3 часа, и утренняя сессия выглядела основной;
  2) интрадей запрашивался from_date=вчера, ISS отдаёт ответ порциями (~500
     строк), поэтому минутный запрос за двое суток обрывался внутри вчерашнего
     дня и до сегодня не доходил вовсе — по OZON приходила серия за 29.07;
  3) серия за вчера была помечена «задержка ~15 минут», то есть свежей, и по ней
     строились сетапы против сегодняшней цены (OZON: диапазон 2892-2924 против
     цены 3174.5, мнимый пробой на 8.6%).
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.collector.moex_price_collector import parse_iss_timestamp  # noqa: E402
from src.agent import intraday_analyst as ia                        # noqa: E402
from src.analysis.intraday import opening_range                     # noqa: E402

MSK = timezone(timedelta(hours=3))


# ───────────────────── 1) часовой пояс ISS ───────────────────────────────────

def test_iss_timestamp_is_moscow_not_utc():
    """ГЛАВНОЕ. Сверено с Tinkoff по тому же бару: ISS 06:59 close=92.41 ==
    Tinkoff 03:55Z H=92.41."""
    assert parse_iss_timestamp("2026-07-30 06:59:00") == \
        datetime(2026, 7, 30, 3, 59, tzinfo=timezone.utc)


def test_iss_morning_bar_stays_in_morning_session():
    """Свеча 07:00 МСК обязана остаться утренней сессией, а не стать 10:00."""
    ts = parse_iss_timestamp("2026-07-30 07:00:00")
    msk = ts.astimezone(MSK)
    assert (msk.hour, msk.minute) == (7, 0), msk


def test_iss_main_session_bar_not_shifted_to_evening():
    ts = parse_iss_timestamp("2026-07-30 18:40:00").astimezone(MSK)
    assert ts.hour == 18, ts


def test_iss_garbage_returns_none_not_crash():
    assert parse_iss_timestamp("не время") is None
    assert parse_iss_timestamp(None) is None


# ───────────────────── 2) возраст последней свечи ─────────────────────────────

def test_age_of_fresh_bar_is_near_zero():
    now = datetime.now(timezone.utc).isoformat()
    assert ia._last_bar_age_min([now]) < 1.0


def test_age_of_yesterday_bar_is_hours():
    old = (datetime.now(timezone.utc) - timedelta(hours=16)).isoformat()
    assert 950 < ia._last_bar_age_min([old]) < 1010


def test_age_none_without_dates():
    assert ia._last_bar_age_min([]) is None


def test_msk_today_not_utc_today():
    """До 03:00 МСК дата по UTC ещё вчерашняя — торговый день считаем по Москве."""
    assert ia._msk_today() == (datetime.now(timezone.utc) + timedelta(hours=3)).date()


# ───────────────────── 3) заслон по свежести ─────────────────────────────────

def _series(last_msk: datetime, bars: int = 40, step_min: int = 5):
    """Серия свечей, заканчивающаяся указанным моментом МСК."""
    dates, o, h, l, c, v = [], [], [], [], [], []
    for i in range(bars - 1, -1, -1):
        t = last_msk - timedelta(minutes=step_min * i)
        dates.append(t.astimezone(timezone.utc).isoformat())
        o.append(10.0); h.append(10.1); l.append(9.9); c.append(10.0); v.append(100)
    return {"dates": dates, "open": o, "high": h, "low": l, "close": c, "volume": v,
            "_source": "moex_iss", "_delayed": True}


def _ctx_with(series):
    """build_intraday_context поверх подменённой загрузки."""
    orig = ia.fetch_intraday
    async def fake(ticker, tf_min=5, hours=8):
        return series
    ia.fetch_intraday = fake
    try:
        return asyncio.get_event_loop().run_until_complete(
            ia.build_intraday_context("TEST", tf_min=5))
    finally:
        ia.fetch_intraday = orig


def test_stale_series_blocks_setups():
    """Серия за вчера обязана быть помечена stale и НЕ давать сетапов."""
    yesterday = datetime.now(MSK).replace(hour=18, minute=14) - timedelta(days=1)
    ctx = _ctx_with(_series(yesterday))
    assert ctx["stale"] is True, ctx.get("age_min")
    assert ctx["setup"] == "none" and ctx["plan"] is None
    assert ctx["observe"] is True
    assert "устарели" in ctx["note"]


def test_fresh_series_not_marked_stale():
    ctx = _ctx_with(_series(datetime.now(MSK) - timedelta(minutes=10)))
    assert ctx["stale"] is False
    assert ctx["age_min"] is not None and ctx["age_min"] < 40


def test_age_reported_even_when_fresh():
    """Возраст показываем всегда — чтобы задержку источника было видно."""
    ctx = _ctx_with(_series(datetime.now(MSK) - timedelta(minutes=18)))
    assert 15 < ctx["age_min"] < 25, ctx["age_min"]


# ───────────── 4) диапазон открытия не берёт вчерашние бары ───────────────────

def test_opening_range_ignores_yesterday_same_minutes():
    """Бары ВЧЕРА 10:00-10:30 и СЕГОДНЯ 10:00-10:10: минуты дня совпадают,
    поэтому сравнения по одной минуте было недостаточно — нужна дата."""
    dates = [f"2026-07-29T{10 + i // 12:02d}:{(i * 5) % 60:02d}:00+03:00" for i in range(6)]
    dates += [f"2026-07-30T10:{i * 5:02d}:00+03:00" for i in range(2)]
    highs = [92.6] * 6 + [93.3] * 2
    lows = [92.1] * 6 + [93.1] * 2
    r = opening_range(highs, lows, bars=6, dates=dates)
    assert r is None, f"вчерашние бары не должны давать диапазон сегодня: {r}"


def test_opening_range_uses_today_when_complete():
    dates = [f"2026-07-29T10:{i * 5:02d}:00+03:00" for i in range(6)]
    dates += [f"2026-07-30T10:{i * 5:02d}:00+03:00" for i in range(6)]
    highs = [92.6] * 6 + [93.5] * 6
    lows = [92.1] * 6 + [93.0] * 6
    r = opening_range(highs, lows, bars=6, dates=dates)
    assert r["or_low"] == 93.0 and r["or_high"] == 93.5, r


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ПАДАЕТ {name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed += 1
            print(f"  ОШИБКА {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} из {len(tests)} пройдено")
    sys.exit(1 if failed else 0)
