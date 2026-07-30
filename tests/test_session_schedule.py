"""Тесты расписания торгов MOEX.

Запуск: python3 tests/test_session_schedule.py

Утренняя сессия 07:00-09:50 раньше попадала в фазу "closed", и почти три часа
реальных торгов система считала нерабочим временем — входы в это время не
открывались вообще. Плюс фильтр «первые N минут шума» был жёстко привязан к
10:00, поэтому шум утреннего и вечернего открытий не отсекался.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.analysis.intraday import (  # noqa: E402
    session_phase, session_open_minute, is_last_minutes,
)


def hm(h, m=0):
    return h * 60 + m


# ─────────────────────────────── фазы ────────────────────────────────────────

def test_morning_session_is_not_closed():
    """ГЛАВНОЕ: 07:00-09:50 это торги, а не 'closed'."""
    for t in (hm(7), hm(8), hm(9), hm(9, 45)):
        assert session_phase(t) == "morning", t


def test_before_morning_open_is_closed():
    assert session_phase(hm(6, 30)) == "closed"
    assert session_phase(hm(3)) == "closed"


def test_pre_auction_between_sessions():
    """09:50-10:00 — аукцион открытия основной сессии, входов нет."""
    assert session_phase(hm(9, 55)) == "pre"


def test_main_and_break_and_evening():
    assert session_phase(hm(10)) == "main"
    assert session_phase(hm(14)) == "main"
    assert session_phase(hm(18, 45)) == "main"
    assert session_phase(hm(18, 55)) == "break"
    assert session_phase(hm(19)) == "evening"
    assert session_phase(hm(22)) == "evening"
    assert session_phase(hm(23, 45)) == "evening"


def test_after_evening_close_is_closed():
    assert session_phase(hm(23, 55)) == "closed"


def test_all_three_sessions_are_distinct_and_tradeable():
    """Три торговые фазы существуют одновременно — не только основная."""
    phases = {session_phase(hm(8)), session_phase(hm(14)), session_phase(hm(21))}
    assert phases == {"morning", "main", "evening"}


# ──────────────── открытие сессии: привязка фильтра шума ─────────────────────

def test_session_open_matches_current_session():
    """Фильтр «первые N минут» должен считать от открытия ТОЙ сессии, в которой
    мы находимся. Раньше было зашито 10:00, поэтому утренний и вечерний шум
    открытия не отсекался вовсе."""
    assert session_open_minute(hm(7, 5)) == hm(7)
    assert session_open_minute(hm(10, 5)) == hm(10)
    assert session_open_minute(hm(19, 5)) == hm(19)


def test_session_open_none_outside_trading():
    for t in (hm(6), hm(9, 55), hm(18, 55), hm(23, 55)):
        assert session_open_minute(t) is None, t


def test_noise_filter_would_catch_all_three_opens():
    """Проверяем именно то, что было сломано: 15 минут после КАЖДОГО открытия."""
    first_min = 15
    for open_min in (hm(7), hm(10), hm(19)):
        inside = open_min + 5
        outside = open_min + first_min + 5
        so = session_open_minute(inside)
        assert so == open_min
        assert so <= inside < so + first_min, ("должен отсечься", inside)
        so2 = session_open_minute(outside)
        assert not (so2 is not None and so2 <= outside < so2 + first_min), \
            ("не должен отсекаться", outside)


# ──────────────────────── конец сессии: флэт к закрытию ──────────────────────

def test_last_minutes_covers_morning_close():
    """У утренней сессии тоже есть конец — перед ним входы не открываем.
    Раньше проверялись только 18:50 и 23:50."""
    assert is_last_minutes(hm(9, 45)) is True
    assert is_last_minutes(hm(9, 20)) is False


def test_last_minutes_covers_main_and_evening_close():
    assert is_last_minutes(hm(18, 45)) is True
    assert is_last_minutes(hm(23, 45)) is True
    assert is_last_minutes(hm(14)) is False


# ────────────────────── горизонт по реальному расписанию ─────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402
from src.agent.external_signal import _default_horizon_hours as _hz  # noqa: E402

MSK = timezone(timedelta(hours=3))


def test_horizon_during_morning_session_covers_today():
    """Сигнал в утреннюю сессию: окно до конца сегодняшнего торгового дня."""
    now = datetime(2026, 7, 30, 8, 0, tzinfo=MSK)
    due = now + timedelta(hours=_hz(now))
    assert due >= datetime(2026, 7, 30, 18, 40, tzinfo=MSK), due
    assert due <= datetime(2026, 7, 31, 1, 0, tzinfo=MSK), due


def test_horizon_late_evening_rolls_to_next_day():
    """После 23:50 торговать уже нечего — переносим на следующий день."""
    now = datetime(2026, 7, 30, 23, 55, tzinfo=MSK)
    due = now + timedelta(hours=_hz(now))
    assert due.day == 31, due


def test_horizon_near_close_rolls_forward():
    """Меньше двух часов до конца дня — сценарий не успеет, переносим."""
    now = datetime(2026, 7, 30, 22, 30, tzinfo=MSK)
    due = now + timedelta(hours=_hz(now))
    assert due >= datetime(2026, 7, 31, 18, 40, tzinfo=MSK), due



# ──────────── окно низкой ликвидности: контекст должен СЧИТАТЬСЯ ──────────────

import src.agent.intraday_analyst as _ia  # noqa: E402

_N = 40


def _candles():
    return {"open": [10.0] * _N, "high": [10.1] * _N, "low": [9.9] * _N,
            "close": [10.0] * _N, "volume": [100] * _N,
            "dates": ["2026-07-30T12:00:00+03:00"] * _N}


def test_low_liquidity_window_does_not_crash():
    """РЕГРЕССИЯ: в ветке «после 22:00 ликвидность падает» переменная h
    затирала список high целым числом (divmod), и построение уровней падало
    с TypeError: 'int' object is not iterable. Ломался КАЖДЫЙ расчёт
    контекста после 22:00 МСК, а не только тест."""
    ctx = _ia.compute_intraday_context(_candles(), hm(22, 30))
    assert ctx is not None
    assert ctx["observe"] is True
    # уровни обязаны быть посчитаны — именно на них падало
    assert ctx["levels"]["session_high"] == 10.1
    assert ctx["levels"]["session_low"] == 9.9


def test_low_liquidity_note_shows_cutoff_time():
    """Время отсечки должно попадать в текст, а не потеряться при переименовании."""
    ctx = _ia.compute_intraday_context(_candles(), hm(22, 30))
    assert ":" in (ctx.get("note") or ""), ctx.get("note")


def test_context_computes_across_whole_trading_day():
    """Ни один торгуемый час не должен падать."""
    for h_ in range(7, 24):
        for m_ in (0, 30):
            ctx = _ia.compute_intraday_context(_candles(), hm(h_, m_))
            assert ctx is None or "levels" in ctx, (h_, m_)


# ───── диапазон открытия: только СВОЯ сессия ─────────────────────────────────

from src.analysis.intraday import opening_range as _or  # noqa: E402


def _bars(times, highs, lows):
    """Свечи по временам МСК вида (час, минута) — как kwargs, чтобы даты не
    попадали в позицию bars."""
    return {"highs": highs, "lows": lows,
            "dates": [f"2026-07-30T{h:02d}:{m:02d}:00+03:00" for h, m in times]}


def test_opening_range_ignores_previous_session():
    """ГЛАВНОЕ. Окно загрузки 8 часов, поэтому в 10:05 первыми свечами идёт
    утренняя сессия. Раньше диапазон считался по ней, цена к основной сессии
    почти всегда уходила за трёхчасовой давности границы, и пробой срабатывал
    сам собой: 30.07 в 10:07 десять тикеров получили одинаковый orb 0.90-1.00.
    Диапазон обязан строиться по свечам ТЕКУЩЕЙ сессии."""
    times = [(7, 0), (7, 5), (7, 10), (7, 15), (7, 20), (7, 25),   # утренняя
             (10, 0), (10, 5), (10, 10), (10, 15), (10, 20), (10, 25)]
    highs = [92.6, 92.4, 92.4, 92.3, 92.3, 92.3] + [93.3] * 6
    lows  = [92.1, 92.2, 92.2, 92.2, 92.2, 92.2] + [93.1] * 6
    r = _or(**_bars(times, highs, lows), bars=6)
    assert r is not None
    # границы основной сессии, а не утренней 92.1..92.6
    assert r["or_low"] == 93.1 and r["or_high"] == 93.3, r
    assert r["session_open_min"] == 10 * 60


def test_no_range_before_enough_bars():
    """В первые 30 минут диапазона открытия ещё НЕТ — и ORB не существует.
    Именно этот случай был в 10:05: два бара основной сессии."""
    times = [(7, h) for h in (0, 5, 10, 15, 20, 25, 30, 35)] + [(10, 0), (10, 5)]
    n = len(times)
    r = _or(**_bars(times, [93.0] * n, [92.0] * n), bars=6)
    assert r is None


def test_range_appears_exactly_at_sixth_bar():
    times = [(10, 5 * i) for i in range(6)]
    r = _or(**_bars(times, [93.0] * 6, [92.0] * 6), bars=6)
    assert r is not None and r["session_bars"] == 6


def test_no_false_breakout_across_sessions():
    """Цена ВЫШЕ утреннего хая, но ВНУТРИ диапазона основной сессии —
    пробоя быть не должно. Раньше был."""
    times = [(7, 5 * i) for i in range(6)] + [(10, 5 * i) for i in range(6)]
    highs = [92.6] * 6 + [93.5] * 6
    lows  = [92.1] * 6 + [93.0] * 6
    r = _or(**_bars(times, highs, lows), bars=6)
    price = 93.2                      # выше 92.6, но внутри 93.0..93.5
    assert price > max(highs[:6]), "цена действительно выше утреннего хая"
    assert not (price > r["or_high"] or price < r["or_low"]), "пробоя нет"


def test_morning_session_has_its_own_range():
    """Утренняя сессия торгуемая, у неё свой диапазон от 07:00."""
    times = [(7, 5 * i) for i in range(6)]
    r = _or(**_bars(times, [92.6] * 6, [92.1] * 6), bars=6)
    assert r is not None and r["session_open_min"] == 7 * 60


def test_evening_session_range_counts_from_1900():
    times = [(14, 5 * i) for i in range(6)] + [(19, 5 * i) for i in range(6)]
    highs = [100.0] * 6 + [105.0] * 6
    lows = [99.0] * 6 + [104.0] * 6
    r = _or(**_bars(times, highs, lows), bars=6)
    assert r["session_open_min"] == 19 * 60
    assert r["or_low"] == 104.0 and r["or_high"] == 105.0


def test_no_dates_means_no_range():
    """Без дат считать нельзя: молчаливый расчёт по чужой сессии и был багом."""
    assert _or([93.0] * 10, [92.0] * 10, bars=6) is None


def test_break_time_falls_back_to_last_traded_session():
    """В перерыве 18:50-19:00 диапазон берём по сессии последней торговой свечи."""
    times = [(10, 5 * i) for i in range(6)] + [(18, 55)]
    n = len(times)
    r = _or(**_bars(times, [93.0] * n, [92.0] * n), bars=6)
    assert r is not None and r["session_open_min"] == 10 * 60

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
