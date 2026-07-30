"""
Тесты чистой интрадей-логики (src/analysis/intraday.py).
Запуск: `python -m pytest tests/test_intraday.py`  или  `python tests/test_intraday.py`.
Зависимостей нет — только stdlib.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis import intraday as iv


def test_vwap_equal_volumes_is_cumulative_typical_avg():
    h, l, c, v = [10, 11, 12], [8, 9, 10], [9, 10, 11], [100, 100, 100]
    vw = iv.vwap(h, l, c, v)
    # типичные цены: 9,10,11 -> кумулятивные средние 9, 9.5, 10
    assert vw == [9.0, 9.5, 10.0]


def test_vwap_zero_volume_falls_back_to_typical():
    vw = iv.vwap([10], [8], [9], [0])
    assert vw == [9.0]


def test_intraday_atr_simple_mean_of_true_ranges():
    assert iv.intraday_atr([10, 11, 12], [8, 9, 10], [9, 10, 11], period=3) == 2.0


def test_opening_range_and_none_when_too_few_bars():
    assert iv.opening_range([10, 11, 12, 9], [8, 9, 10, 7], bars=2, assume_scoped=True) == {
        "or_high": 11, "or_low": 8, "bars": 2}
    assert iv.opening_range([10], [8], bars=6) is None


def test_orb_long_short_none():
    """Направление пробоя. min_rr=0 задан явно: геометрия здесь синтетическая
    (стоп на другом краю диапазона даёт R/R 0.75), а проверяется именно выбор
    стороны."""
    assert iv.orb_signal(11.5, 11, 8, atr=1.0, min_rr=0)["signal"] == "long"
    assert iv.orb_signal(7.5, 11, 8, atr=1.0, min_rr=0)["signal"] == "short"
    assert iv.orb_signal(9.0, 11, 8, atr=1.0, min_rr=0)["signal"] == "none"


def test_orb_rejects_bad_geometry():
    """R/R считался и раньше, но никто на него не смотрел. Замер 30.07 в 14:04:
    из 36 сетапов ORB у 35 R/R был ниже 1.0, медиана около 0.27 — стоп на границе
    утреннего диапазона стоял в 1-5% от цены, а цель 1.5*ATR была в разы меньше.
    Худшие: MGNT 0.12, SMLT 0.22 при риске 5.1%."""
    r = iv.orb_signal(11.5, 11, 8, atr=1.0)          # порог по умолчанию 1.5
    assert r["signal"] == "none"
    assert "R/R" in r["reason"] and r["risk_reward"] == 0.43


def test_orb_accepts_good_geometry():
    """Узкий диапазон и достаточная цель — вход выдаётся."""
    r = iv.orb_signal(10.1, 10.0, 9.9, atr=1.0)
    assert r["signal"] == "long" and r["risk_reward"] >= 1.5


def test_news_plan_keeps_no_floor_by_deliberate_choice():
    """У новостного плана стоп стоит за ВСЕЙ свечой выноса (около 2.5*ATR), а цель
    1.5*ATR, поэтому единый порог обнулил бы новостную ветку целиком — не потому,
    что она плоха, а потому что у неё неверно задана цель. Это отдельная задача."""
    p = iv.news_whipsaw_plan(event_high=12, event_low=8, price=12.3,
                             vwap_last=11, atr=1.0)
    assert p["signal"] == "long" and p["risk_reward"] < 1.5


def test_volatility_state_detects_expansion():
    hh = [10] * 30 + [10, 10.5, 11, 12, 13]
    ll = [9.9] * 30 + [9.5, 9, 8.5, 8, 7]
    cc = [9.95] * 30 + [10, 10, 10.5, 11, 12]
    st = iv.volatility_state(hh, ll, cc, period=5, lookback=20)
    assert st["state"] == "expansion"


def test_volatility_state_detects_squeeze():
    # сначала волатильно (диапазон ~2), потом затухание (диапазон ~0.04)
    # -> текущий ATR у минимумов lookback -> squeeze
    hh = [11] * 32 + [10.02] * 8
    ll = [9] * 32 + [9.98] * 8
    cc = [10] * 32 + [10.0] * 8
    st = iv.volatility_state(hh, ll, cc, period=5, lookback=40)
    assert st["state"] == "squeeze"
    assert st["atr_rank"] <= 0.25


def test_detect_spike_and_reversal_candle():
    o = [10, 10, 10, 10, 10]
    h = [10.2, 10.2, 10.2, 10.2, 12]
    l = [9.8, 9.8, 9.8, 9.8, 8]
    c = [10, 10, 10, 10, 10.1]
    d = iv.detect_spike(o, h, l, c, k=2.0)
    assert d["spike"] is True and d["reversal"] is True
    assert d["range_ratio"] >= 2.0


def test_classify_event_requires_spike_plus_confirmation():
    assert iv.classify_event(True, msg_zscore=3.1)["event"] is True
    assert iv.classify_event(True, has_fresh_news=True)["event"] is True
    assert iv.classify_event(True, msg_zscore=0.5)["event"] is False   # спайк без подтверждения
    assert iv.classify_event(False, has_fresh_news=True)["event"] is False


def test_news_whipsaw_resolution_long_and_wait():
    long_plan = iv.news_whipsaw_plan(event_high=12, event_low=8, price=12.3,
                                     vwap_last=11, atr=1.0)
    assert long_plan["signal"] == "long"
    assert long_plan["stop_loss"] == 8 and long_plan["take_profit"] == 16.3

    wait_plan = iv.news_whipsaw_plan(event_high=12, event_low=8, price=11,
                                     vwap_last=11.5, atr=1.0)
    assert wait_plan["signal"] == "wait"


def test_session_phase_and_last_minutes():
    hm = lambda h, m: h * 60 + m
    assert iv.session_phase(hm(9, 55)) == "pre"
    assert iv.session_phase(hm(10, 30)) == "main"
    assert iv.session_phase(hm(18, 55)) == "break"
    assert iv.session_phase(hm(19, 30)) == "evening"
    assert iv.session_phase(hm(2, 0)) == "closed"
    assert iv.is_last_minutes(hm(23, 45)) is True
    assert iv.is_last_minutes(hm(10, 30)) is False


def test_orderbook_sentiment():
    # покупатели доминируют (bid/ask 2, поток 70% buy) -> позитив
    r = iv.orderbook_sentiment(2.0, 70.0)
    assert r["signal"] > 0.15 and r["label"] == "positive"
    # продавцы доминируют
    r = iv.orderbook_sentiment(0.5, 30.0)
    assert r["signal"] < -0.15 and r["label"] == "negative"
    # баланс
    r = iv.orderbook_sentiment(1.0, 50.0)
    assert r["label"] == "neutral"
    # нет данных
    assert iv.orderbook_sentiment(None, None)["signal"] == 0.0


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"✅ {name}")
    print(f"\n{passed} тестов пройдено.")
