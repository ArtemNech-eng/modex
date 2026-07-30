"""
Тесты чистых функций интрадей-аналитика (src/agent/intraday_analyst.py):
aggregate_candles и compute_intraday_context. Без сети/зависимостей.
Запуск: `python -m pytest tests/test_intraday_analyst.py` или `python tests/...`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent import intraday_analyst as ia

N = 40


def _base():
    return {
        "open": [10.0] * N, "high": [10.1] * N, "low": [9.9] * N,
        "close": [10.0] * N, "volume": [100] * N,
        "dates": ["2026-07-24T12:00:00+03:00"] * N,
    }


def test_aggregate_1min_to_5min():
    c = {"open": [1, 2, 3, 4, 5, 6], "high": [2, 3, 4, 5, 6, 7],
         "low": [0.5, 1, 2, 3, 4, 5], "close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
         "volume": [10] * 6, "dates": [f"t{i}" for i in range(6)]}
    agg = ia.aggregate_candles(c, 5)
    assert len(agg["close"]) == 2           # 5 + частичная 1
    assert agg["open"][0] == 1 and agg["close"][0] == 5.5
    assert agg["high"][0] == 6 and agg["low"][0] == 0.5
    assert agg["volume"][0] == 50
    assert agg["close"][1] == 6.5           # частичная последняя группа


def test_news_resolution_long_after_spike():
    c = _base()
    c["high"][36], c["low"][36], c["close"][36] = 12.5, 8.0, 10.2   # спайк 4 бара назад
    for i in (37, 38, 39):
        c["high"][i], c["low"][i], c["open"][i], c["close"][i] = 12.9, 12.0, 12.1, 12.8
    ctx = ia.compute_intraday_context(c, 12 * 60, msg_zscore=3.0)
    assert ctx["setup"] == "news_resolution"
    assert ctx["plan"]["signal"] == "long"


def test_spike_now_is_observe():
    c = _base()
    c["high"][-1], c["low"][-1], c["close"][-1] = 12.5, 8.0, 10.1   # вынос прямо сейчас
    ctx = ia.compute_intraday_context(c, 12 * 60, msg_zscore=3.0)
    assert ctx["observe"] is True
    assert ctx["setup"] == "news_observe"


def test_orb_breakout_without_news():
    """СВЕЖИЙ пробой: цена только что вышла за диапазон, поэтому стоп на дальнем
    краю близко и R/R проходит порог 1.5. Так этот сетап и должен работать —
    когда цена ушла далеко, риск до дальнего края становится больше цели."""
    c = _base()
    for i in range(20, N):
        c["high"][i], c["low"][i], c["open"][i], c["close"][i] = 10.35, 10.05, 10.1, 10.12
    ctx = ia.compute_intraday_context(c, 12 * 60, msg_zscore=0.0, opening_range_bars=6)
    assert ctx["setup"] == "orb", ctx.get("orb_blocked") or ctx.get("note")
    assert ctx["signal"] == "long"
    assert ctx["plan"]["risk_reward"] >= 1.5


def test_orb_rejected_when_price_ran_far():
    """Цена далеко от диапазона: стоп на дальнем краю делает риск больше цели.
    Именно так 30.07 в 14:04 появились десять шортов при растущем рынке."""
    c = _base()
    for i in range(20, N):
        c["high"][i], c["low"][i], c["open"][i], c["close"][i] = 10.6, 10.3, 10.35, 10.55
    ctx = ia.compute_intraday_context(c, 12 * 60, msg_zscore=0.0, opening_range_bars=6)
    assert ctx["setup"] != "orb"
    assert ctx.get("orb_blocked") and "R/R" in ctx["orb_blocked"]


def test_late_session_forces_observe():
    ctx = ia.compute_intraday_context(_base(), 23 * 60 + 45, msg_zscore=5.0)
    assert ctx["observe"] is True


def test_pre_session_forces_observe():
    ctx = ia.compute_intraday_context(_base(), 9 * 60 + 55, msg_zscore=5.0)
    assert ctx["observe"] is True


def test_minute_of_day_msk_from_utc_iso():
    # 09:00Z == 12:00 МСК
    assert ia._minute_of_day_msk("2026-07-24T09:00:00+00:00") == 12 * 60


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"✅ {name}")
    print(f"\n{passed} тестов пройдено.")
