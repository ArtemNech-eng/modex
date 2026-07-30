"""Тесты единой классификации состояния рынка.

Запуск: python3 tests/test_market_state.py

ЗАЧЕМ. Все куски существовали по отдельности: regime в дневной технике,
volatility_state в интрадее, classify_event в детекторе новостей, ликвидность внутри
риск-контура. Ни одна функция не сводила их в ОДНО состояние, и потребитель — человек
или модель — должен был держать четыре поля в голове и сам понимать их взаимодействие.

ПОЧЕМУ НЕ ПРОСТО regime. Прежний детектор пометил SMLT «боковиком» при цене на 23%
выше SMA20 и 11% выше SMA50: он смотрел на ADX и расхождение средних, но НЕ на
положение цены относительно них. Здесь тренд требует согласия нескольких независимых
признаков, а при разногласии честно возвращается RANGE с указанием причины.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.analysis.market_state import (classify_market_state as cl,  # noqa: E402
                                       STATES, ADX_TREND)


# ───────────────────── ВЕТО идёт первым ──────────────────────────────────────

def test_mismatch_is_veto():
    """Чужая серия перекрывает всё остальное: 30.07 таблица FIGI указывала 22
    бумаги из 43 на другие инструменты."""
    r = cl(price=100, sma20=90, sma50=80, adx=40, vwap=95, mismatch=True)
    assert r["state"] == "ILLIQUID" and r["tradeable"] is False


def test_stale_is_veto():
    """Устаревшие свечи: 30.07 по пятнадцати бумагам приходила серия за ВЧЕРА."""
    r = cl(price=100, sma20=90, sma50=80, adx=40, stale=True, age_min=972)
    assert r["state"] == "ILLIQUID" and "972" in " ".join(r["why"])


def test_wide_spread_is_veto():
    r = cl(price=100, sma20=90, sma50=80, adx=40, spread_pct=1.7)
    assert r["state"] == "ILLIQUID" and "спред" in " ".join(r["why"])


def test_normal_spread_not_veto():
    """Реальные спреды ликвидных бумаг 0.004-0.10%, их вето не касается."""
    r = cl(price=100, sma20=90, sma50=80, adx=40, vwap=95, spread_pct=0.03)
    assert r["state"] != "ILLIQUID"


def test_veto_beats_news_and_trend():
    r = cl(price=100, sma20=90, sma50=80, adx=45, vwap=95,
           news_event=True, mismatch=True)
    assert r["state"] == "ILLIQUID"


# ───────────────────── новостное событие ─────────────────────────────────────

def test_news_event_ranks_above_trend():
    """Поведение цены на новостном выносе другое — состояние обязано это называть."""
    r = cl(price=100, sma20=90, sma50=80, adx=45, vwap=95,
           intraday_structure="вверх", news_event=True, news_lag_min=9.7)
    assert r["state"] == "NEWS_EVENT"
    assert "новость" in " ".join(r["why"]) and "до выноса" in " ".join(r["why"])
    assert r["signals"]["news_lag_min"] == 9.7


# ───────────────────── тренд по согласию признаков ───────────────────────────

def test_clear_uptrend():
    r = cl(price=110, sma20=100, sma50=95, adx=35, vwap=108,
           intraday_structure="вверх")
    assert r["state"] == "TREND_UP" and r["confidence"] >= 0.8


def test_clear_downtrend():
    """Настоящий случай AFLT 30.07: ADX 37.3, цена ниже обеих средних и VWAP."""
    r = cl(price=32.35, sma20=32.51, sma50=37.4, adx=37.3, vwap=32.77,
           intraday_structure="вниз")
    assert r["state"] == "TREND_DOWN" and r["confidence"] >= 0.8


def test_weak_adx_is_range_even_if_signals_agree():
    """ADX ниже порога — направление средних ещё ничего не значит."""
    r = cl(price=110, sma20=100, sma50=95, adx=12, vwap=108,
           intraday_structure="вверх")
    assert r["state"] == "RANGE" and "ADX" in " ".join(r["why"])


def test_conflicting_signals_give_range_with_reason():
    """НАСТОЯЩИЙ СЛУЧАЙ SMLT 30.07. Цена на 23.7% выше SMA20 и 11.4% выше SMA50, но
    SMA20 ПОД SMA50 и цена ниже VWAP. Прежний детектор говорил «боковик», не глядя
    на положение цены; здесь тот же ответ, но с названной причиной."""
    r = cl(price=349.6, sma20=282.56, sma50=313.9, adx=23.8, vwap=358.0,
           intraday_structure="боковик")
    assert r["state"] == "RANGE"
    assert "расходятся" in " ".join(r["why"]) or "ADX" in " ".join(r["why"])
    assert r["signals"]["price_vs_sma20_pct"] > 20, "положение цены обязано учитываться"


def test_range_never_pretends_to_be_trend():
    """При разногласии тренд объявлять нельзя — именно на этом ошибался regime."""
    r = cl(price=100, sma20=95, sma50=105, adx=40, vwap=101,
           intraday_structure="боковик")
    assert r["state"] == "RANGE"


# ───────────────────── волатильность как отдельная ось ───────────────────────

def test_volatility_is_separate_axis():
    """«Тренд вверх при сжатии» и «тренд вверх при расширении» — разные вещи, и
    схлопывать их в один ярлык значит терять информацию."""
    up_hi = cl(price=110, sma20=100, sma50=95, adx=35, vwap=108,
               intraday_structure="вверх", volatility_state="expansion")
    up_lo = cl(price=110, sma20=100, sma50=95, adx=35, vwap=108,
               intraday_structure="вверх", volatility_state="squeeze")
    assert up_hi["state"] == up_lo["state"] == "TREND_UP"
    assert up_hi["volatility"] == "HIGH" and up_lo["volatility"] == "LOW"


def test_all_seven_states_reachable():
    """Список состояний должен покрывать то, что просил владелец."""
    got = set()
    got.add(cl(price=1, mismatch=True)["state"])
    got.add(cl(price=100, sma20=90, sma50=80, adx=45, vwap=99, news_event=True)["state"])
    got.add(cl(price=110, sma20=100, sma50=95, adx=35, vwap=108,
               intraday_structure="вверх")["state"])
    got.add(cl(price=90, sma20=100, sma50=105, adx=35, vwap=92,
               intraday_structure="вниз")["state"])
    got.add(cl(price=100, sma20=100, sma50=100, adx=10, vwap=100)["state"])
    assert got == {"ILLIQUID", "NEWS_EVENT", "TREND_UP", "TREND_DOWN", "RANGE"}
    vols = {cl(price=100, sma20=100, sma50=100, adx=10, volatility_state=v)["volatility"]
            for v in ("expansion", "squeeze", "normal")}
    assert vols == {"HIGH", "LOW", "NORMAL"}


def test_reason_always_present():
    """Состояние без обоснования нельзя проверить."""
    for kw in ({"mismatch": True}, {"news_event": True}, {"adx": 40},
               {"adx": 10}, {}):
        r = cl(price=100, sma20=99, sma50=98, vwap=100, **kw)
        assert r.get("why") and r.get("note"), kw


def test_wired_into_brief():
    import pathlib
    src = pathlib.Path(ROOT, "src", "agent", "screen.py").read_text(encoding="utf-8")
    for f in ('"state"', '"state_conf"', '"state_vol"', '"tradeable"'):
        assert f in src, f


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
