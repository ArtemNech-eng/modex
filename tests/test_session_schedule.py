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
