"""
Тесты чистой функции триажа interest_score (src/agent/screen.py).
Запуск: `python tests/test_screen.py` или через pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent.screen import interest_score


def test_empty_is_zero():
    r = interest_score(None, None, None)
    assert r["interest"] == 0.0 and r["direction"] == "flat"


def test_orb_long_plus_technical_is_interesting_long():
    intraday = {"setup": "orb", "signal": "long", "observe": False,
                "volatility_state": "expansion", "event": {"event": False}}
    technical = {"trade_plan": {"entry_status": "enter", "risk_reward": 2.5},
                 "score": 0.6, "range_position": 0.9}
    r = interest_score(None, technical, intraday)
    assert r["interest"] >= 0.6
    assert r["direction"] == "long"


def test_observe_reduces_interest():
    base = {"setup": "orb", "signal": "long", "observe": False, "event": {"event": False}}
    obs = {**base, "observe": True}
    hi = interest_score(None, None, base)["interest"]
    lo = interest_score(None, None, obs)["interest"]
    assert lo < hi


def test_news_resolution_scores_high():
    intraday = {"setup": "news_resolution", "signal": "short", "observe": False,
                "event": {"event": True}}
    r = interest_score(None, None, intraday)
    assert r["interest"] >= 0.6 and r["direction"] == "short"


def test_sentiment_anomaly_bearish():
    sentiment = {"is_anomaly": True, "volume_zscore": 2.5, "sentiment_index": 25}
    r = interest_score(sentiment, None, None)
    assert r["interest"] >= 0.4
    assert r["direction"] == "short"


def test_score_capped_at_one():
    intraday = {"setup": "news_resolution", "signal": "long", "observe": False,
                "event": {"event": True}, "volatility_state": "expansion"}
    technical = {"trade_plan": {"entry_status": "enter", "risk_reward": 3}, "score": 0.9,
                 "range_position": 0.95}
    sentiment = {"is_anomaly": True, "volume_zscore": 3, "sentiment_index": 80}
    r = interest_score(sentiment, technical, intraday)
    assert r["interest"] == 1.0
    assert r["direction"] == "long"


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"✅ {name}")
    print(f"\n{passed} тестов пройдено.")
