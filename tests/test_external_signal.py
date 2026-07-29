"""Тесты приёма сценария от внешнего аналитика.

Запуск: python3 tests/test_external_signal.py

Проверяется валидация. Внешний путь обязан быть НЕ мягче внутреннего: если сюда
пролезет сценарий без цели, без обоснования или с нулевой уверенностью, он
попадёт в журнал и молча выпадет из измерения — и сравнение аналитиков окажется
подделкой.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.agent.external_signal import validate_scenario  # noqa: E402

OK = {
    "ticker": "SBER", "direction": "up",
    "entry": 315.20, "stop": 313.90, "target": 317.80,
    "confidence": 0.65, "analyst": "hyperagent",
    "reason": "цена выше VWAP, расширение объёма, поток покупок",
    "invalidation": "закрепление ниже VWAP",
}


def _err(**over):
    p = dict(OK); p.update(over)
    return validate_scenario(p)[0]


def test_valid_scenario_passes():
    errors, norm = validate_scenario(OK)
    assert errors == [], errors
    assert norm["direction"] == "up" and norm["ticker"] == "SBER"


def test_rr_is_computed():
    _, norm = validate_scenario(OK)
    # |317.80-315.20| / |315.20-313.90| = 2.60 / 1.30 = 2.0
    assert norm["rr"] == 2.0


def test_direction_synonyms_normalised():
    for src, dst in (("long", "up"), ("buy", "up"), ("short", "down"), ("sell", "down")):
        p = dict(OK); p["direction"] = src
        if dst == "down":
            p.update(stop=317.0, target=310.0)
        errors, norm = validate_scenario(p)
        assert errors == [], (src, errors)
        assert norm["direction"] == dst


def test_missing_target_rejected():
    """Без цели intraday_outcome не оценит сделку, realized_r не посчитается,
    и сигнал выпадет из метрики в R и из виртуального счёта."""
    assert any("target" in e for e in _err(target=None))


def test_missing_stop_or_entry_rejected():
    assert any("stop" in e for e in _err(stop=None))
    assert any("entry" in e for e in _err(entry=None))


def test_negative_or_garbage_levels_rejected():
    assert any("entry" in e for e in _err(entry=-5))
    assert any("stop" in e for e in _err(stop="мусор"))
    assert any("target" in e for e in _err(target=float("nan")))


def test_stop_on_wrong_side_rejected():
    assert any("стоп" in e for e in _err(stop=316.0))                    # long
    p = dict(OK); p.update(direction="down", stop=313.0, target=310.0)
    assert any("стоп" in e for e in validate_scenario(p)[0])


def test_target_on_wrong_side_rejected():
    """Risk Engine сторону ЦЕЛИ не проверяет — значит проверяем здесь,
    иначе бессмысленная сделка дойдёт до расчёта размера."""
    assert any("цель" in e for e in _err(target=314.0))                  # long
    p = dict(OK); p.update(direction="down", stop=317.0, target=320.0)
    assert any("цель" in e for e in validate_scenario(p)[0])


def test_zero_confidence_rejected():
    """При confidence=0 запись не считается направленным прогнозом
    в accuracy_stats и молча выпадет из измерения."""
    assert any("confidence" in e for e in _err(confidence=0))
    assert any("confidence" in e for e in _err(confidence=None))
    assert any("confidence" in e for e in _err(confidence=1.5))


def test_reason_and_invalidation_required():
    assert any("reason" in e for e in _err(reason="коротко"))
    assert any("invalidation" in e for e in _err(invalidation=""))


def test_unknown_analyst_rejected():
    assert any("analyst" in e for e in _err(analyst="кто-то"))
    for a in ("hyperagent", "claude-api", "human"):
        assert _err(analyst=a) == []


def test_unknown_ticker_rejected():
    """Неотслеживаемый тикер: оценка исхода будет невозможна."""
    errors = _err(ticker="НЕТТАКОГО")
    assert any("не отслеживается" in e for e in errors)


def test_empty_payload_collects_all_errors():
    errors, _ = validate_scenario({})
    assert len(errors) >= 5
    assert any("ticker" in e for e in errors)
    assert any("direction" in e for e in errors)


def test_short_scenario_valid():
    p = dict(OK); p.update(direction="down", entry=100.0, stop=102.0, target=95.0)
    errors, norm = validate_scenario(p)
    assert errors == [] and norm["rr"] == 2.5


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
