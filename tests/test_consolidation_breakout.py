"""Тесты сетапа «пробой внутридневной консолидации».

Запуск: python3 tests/test_consolidation_breakout.py

ЗАЧЕМ СЕТАП. Единственной техникой входа был пробой диапазона открытия, а он живёт
первые 90 минут. 30.07 Мечел прошёл 7.1% от минимума дня (37.09 -> 39.72), и в
момент правильного входа ни один сетап не срабатывал: диапазон открытия просрочен,
новостей по бумаге за весь день НОЛЬ, а дневная техника рекомендовала ШОРТ у верхней
границы коридора. За день система выдала 36 сетапов ORB, из которых 35 имели R/R
ниже единицы, и ни одного настоящего.

ПОЧЕМУ ЦЕЛЬ ЗАДАНА В РИСКЕ. С целью в ATR планируемый R/R структурно занижался: у
победившего входа по Мечелу выходило 1.05 при фактическом ходе 4.03R, то есть мой
же порог R/R>=1.5 отклонил бы лучшую сделку дня. Цель entry + 2R делает R/R равным
двум по построению, и остаётся один содержательный вопрос — достигается ли она.

ПРОВЕРКА НА ИСТОРИИ. 12 ликвидных бумаг, 10-мин свечи, 18 торговых дней июля 2026,
издержки 0.05% на круг. Ожидание положительно во всех конфигурациях, но устойчиво
только при узком сжатии:
    ширина <=2.0 ATR: 365 входов, +0.065R; по половинам -0.098R и +0.176R — знак
                      МЕНЯЕТСЯ, ненадёжно;
    ширина <=1.5 ATR: 149 входов, +0.077R; первая половина после издержек в минусе;
    ширина <=1.2 ATR:  55 входов, +0.264R; по половинам +0.381R и +0.220R, после
                      издержек +0.291R и +0.149R.
Поэтому порог 1.2. Выборка 55 входов и один режим рынка — это измеряемая гипотеза,
а не установленное преимущество.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.analysis.intraday as iv          # noqa: E402


def _bars(c_low, c_high, breakout_close, base_vol=100, break_vol=400, n=8):
    """Консолидация из n-1 баров плюс пробойный бар."""
    highs = [c_high] * (n - 1) + [breakout_close + 0.02]
    lows = [c_low] * (n - 1) + [c_high - 0.01]
    closes = [(c_low + c_high) / 2] * (n - 1) + [breakout_close]
    vols = [base_vol] * (n - 1) + [break_vol]
    return highs, lows, closes, vols


# ─────────────────── настоящий случай: Мечел, 13:25 ──────────────────────────

def test_fires_on_real_mechel_setup():
    """ГЛАВНОЕ. Сжатие 38.01-38.29 (1.17xATR), пробой на объёме 3.97x, цена выше
    VWAP. Это вход, который система пропустила, а он дал MFE 4.03R при MAE 0.03R."""
    h, l, c, v = _bars(38.01, 38.29, 38.35, base_vol=100, break_vol=400)
    r = iv.consolidation_breakout(h, l, c, v, atr=0.24, vwap=37.9)
    assert r["signal"] == "long", r.get("reason")
    assert r["stop_loss"] == 38.01, "стоп обязан стоять под минимумом сжатия"
    assert r["risk_reward"] == 2.0, "цель задана в риске -> R/R равен 2 по построению"
    assert r["take_profit"] == 39.03, r["take_profit"]
    assert r["width_atr"] <= 1.2


def test_target_is_two_r_exactly():
    h, l, c, v = _bars(100.0, 100.5, 100.6)
    r = iv.consolidation_breakout(h, l, c, v, atr=0.5, vwap=99.0)
    risk = r["entry"] - r["stop_loss"]
    assert abs((r["take_profit"] - r["entry"]) - 2 * risk) < 1e-6


# ─────────────────── отказы, и каждый с причиной ─────────────────────────────

def test_refuses_wide_range():
    """Диапазон шире порога — это коридор, а не сжатие. Настоящий случай: после
    выноса Мечела на 40.17 шесть баров растянулись на 2.76xATR."""
    h, l, c, v = _bars(38.0, 39.0, 39.1)
    r = iv.consolidation_breakout(h, l, c, v, atr=0.3, vwap=37.5)
    assert r["signal"] == "none"
    assert "не сжатие" in r["reason"] and r["width_atr"] > 1.2


def test_refuses_without_volume():
    """Выход без участия: объём пробоя не подтверждает."""
    h, l, c, v = _bars(38.01, 38.29, 38.35, base_vol=100, break_vol=110)
    r = iv.consolidation_breakout(h, l, c, v, atr=0.24, vwap=37.9)
    assert r["signal"] == "none" and "объём" in r["reason"]


def test_refuses_below_vwap():
    """Пробой вверх при цене ниже средней дня — отскок внутри снижения."""
    h, l, c, v = _bars(38.01, 38.29, 38.35)
    r = iv.consolidation_breakout(h, l, c, v, atr=0.24, vwap=39.5)
    assert r["signal"] == "none" and "VWAP" in r["reason"]


def test_refuses_without_breakout():
    h, l, c, v = _bars(38.01, 38.29, 38.20)
    r = iv.consolidation_breakout(h, l, c, v, atr=0.24, vwap=37.9)
    assert r["signal"] == "none" and "не вышла" in r["reason"]


def test_refuses_excessive_risk():
    """Риск выше предела: сжатие узкое в ATR, но сам ATR огромен."""
    h, l, c, v = _bars(90.0, 96.0, 96.5)
    r = iv.consolidation_breakout(h, l, c, v, atr=6.0, vwap=80.0, max_risk_pct=3.0)
    assert r["signal"] == "none" and "риск" in r["reason"]


def test_refuses_without_atr():
    h, l, c, v = _bars(38.01, 38.29, 38.35)
    assert iv.consolidation_breakout(h, l, c, v, atr=None, vwap=37.9)["signal"] == "none"
    assert iv.consolidation_breakout(h, l, c, v, atr=0, vwap=37.9)["signal"] == "none"


def test_every_refusal_states_reason():
    """Молчаливый отказ не отличить от «нечего торговать»."""
    cases = [
        _bars(38.0, 39.0, 39.1) + (0.3, 37.5),          # широкий
        _bars(38.01, 38.29, 38.35, 100, 110) + (0.24, 37.9),   # объём
        _bars(38.01, 38.29, 38.35) + (0.24, 39.5),      # VWAP
        _bars(38.01, 38.29, 38.20) + (0.24, 37.9),      # нет пробоя
    ]
    for h, l, c, v, atr, vwap in cases:
        r = iv.consolidation_breakout(h, l, c, v, atr=atr, vwap=vwap)
        assert r["signal"] == "none"
        assert r.get("reason") and len(r["reason"]) > 10, r


# ─────────────────── связь с контекстом ──────────────────────────────────────

def test_wired_into_context():
    """Сетап обязан ДОЙТИ до контекста. Прежняя версия теста проверяла «сетап ИЛИ
    причина отказа» и проходила через отказ, потому что фикстура давала ширину
    1.27xATR при пороге 1.2 — то есть сетап НИКОГДА не срабатывал, и слабый тест это
    скрыл. Здесь геометрия подобрана расчётом: сжатие 1.07xATR, объём 4x."""
    import src.agent.intraday_analyst as ia
    n = 30
    dates = [f"2026-07-30T{13 + i // 12:02d}:{(i * 5) % 60:02d}:00+03:00" for i in range(n)]
    c = {"open": [37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.26, 38.34], "high": [37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 38.2, 38.2, 38.2, 38.2, 38.2, 38.2, 38.2, 38.2, 38.3, 38.36], "low": [37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 38.05, 38.05, 38.05, 38.05, 38.05, 38.05, 38.05, 38.05, 38.18, 38.24],
         "close": [37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.26, 38.34], "volume": [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 400], "dates": dates}
    ctx = ia.compute_intraday_context(c, 14 * 60)
    obs = ctx.get("breakout_observation")
    assert obs is not None or ctx["setup"] == "consolidation_breakout", \
        (ctx["setup"], ctx.get("breakout_blocked"))
    plan = obs or ctx.get("plan")
    assert plan["risk_reward"] == 2.0 and plan["width_atr"] <= 1.2


def test_context_records_breakout_refusal():
    import src.agent.intraday_analyst as ia
    n = 30
    dates = [f"2026-07-30T{13 + i // 12:02d}:{(i * 5) % 60:02d}:00+03:00" for i in range(n)]
    c = {"open": [38.0] * n, "high": [39.0] * n, "low": [37.0] * n,
         "close": [38.5] * n, "volume": [100] * n, "dates": dates}
    ctx = ia.compute_intraday_context(c, 14 * 60)
    assert "breakout_blocked" in ctx


def test_thresholds_come_from_settings():
    from config.settings import (BREAKOUT_MAX_WIDTH_ATR, BREAKOUT_TARGET_R,
                                 BREAKOUT_VOL_MULT, BREAKOUT_ENABLED)
    assert BREAKOUT_MAX_WIDTH_ATR == 1.2, "порог подтверждён разбиением выборки"
    assert BREAKOUT_TARGET_R == 2.0
    assert BREAKOUT_VOL_MULT == 1.5
    assert BREAKOUT_ENABLED is True



# ───── режим наблюдения ───────────────────────────────────────────────────────

def test_observe_mode_is_default():
    """Проверка на 181 торговом дне показала, что лонговая сторона убыточна во ВСЕХ
    конфигурациях после издержек (лучшая -0.113R). Те +0.264R на 18 днях июля были
    эффектом режима. Поэтому сетап по умолчанию НЕ сигнал.

    Выключить целиком нельзя: тогда не будет наблюдений и решать через месяц будет
    нечем. Считать сигналом нельзя: это торговля без доказанного преимущества."""
    from config.settings import BREAKOUT_MODE
    assert BREAKOUT_MODE == "observe"


def test_observation_is_not_called_a_setup():
    """Никакой потребитель не должен принять наблюдение за сетап."""
    import src.agent.intraday_analyst as ia
    n = 30
    dates = [f"2026-07-30T{13 + i // 12:02d}:{(i * 5) % 60:02d}:00+03:00" for i in range(n)]
    c = {"open": [37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.26, 38.34], "high": [37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 38.2, 38.2, 38.2, 38.2, 38.2, 38.2, 38.2, 38.2, 38.3, 38.36], "low": [37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 38.05, 38.05, 38.05, 38.05, 38.05, 38.05, 38.05, 38.05, 38.18, 38.24],
         "close": [37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.26, 38.34], "volume": [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 400], "dates": dates}
    ctx = ia.compute_intraday_context(c, 14 * 60)
    assert ctx["setup"] != "consolidation_breakout", "в режиме observe это не сетап"
    obs = ctx["breakout_observation"]
    assert obs and obs["validated"] is False and obs["mode"] == "observe"


def test_observation_carries_full_plan():
    """Наблюдение без уровней бесполезно: исход не посчитать."""
    import src.agent.intraday_analyst as ia
    n = 30
    dates = [f"2026-07-30T{13 + i // 12:02d}:{(i * 5) % 60:02d}:00+03:00" for i in range(n)]
    c = {"open": [37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.26, 38.34], "high": [37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 37.5, 38.2, 38.2, 38.2, 38.2, 38.2, 38.2, 38.2, 38.2, 38.3, 38.36], "low": [37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 37.22, 38.05, 38.05, 38.05, 38.05, 38.05, 38.05, 38.05, 38.05, 38.18, 38.24],
         "close": [37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 37.4, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.12, 38.26, 38.34], "volume": [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 400], "dates": dates}
    obs = ia.compute_intraday_context(c, 14 * 60)["breakout_observation"]
    for k in ("entry", "stop_loss", "take_profit", "risk_reward", "width_atr", "vol_x"):
        assert obs.get(k) is not None, k

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
