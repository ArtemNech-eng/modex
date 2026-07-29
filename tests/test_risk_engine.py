"""Тесты Risk Engine.

Запуск: python3 tests/test_risk_engine.py (или через pytest).
Проверяется КАЖДОЕ ограничение: движок без тестов на лимиты — это не защита,
а надежда на защиту.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.risk.engine import (  # noqa: E402
    RiskConfig, RiskState, size_position, check_limits, evaluate_trade,
)

CFG = RiskConfig(account_rub=200_000.0, risk_per_trade_pct=0.25,
                 max_position_pct=25.0, max_total_exposure_pct=50.0,
                 daily_loss_limit_r=3.0, weekly_loss_limit_r=6.0,
                 max_trades_per_day=3, max_open_positions=2,
                 max_per_sector=1, kill_switch_dd_pct=5.0)


# ─────────────────────────── размер позиции ──────────────────────────────────

def test_risk_binds_when_stop_is_wide():
    """Широкий стоп: связывает риск, а не экспозиция."""
    # риск 500₽, стоп 10₽ от входа -> 50 акций, экспозиция 5000₽ (мало)
    d = size_position(100.0, 90.0, "up", CFG)
    assert d.approved
    assert d.shares == 50
    assert d.binding_constraint == "risk"
    assert abs(d.risk_rub - 500.0) < 1e-6


def test_exposure_binds_on_tight_intraday_stop():
    """РЕАЛЬНЫЙ случай: узкий стоп -> связывает экспозиция, риск выходит НИЖЕ цели.

    Вход 315.20, стоп 313.90 (0.41% от цены). По риску вышло бы 384 акции и
    121 037₽ = 60% счёта. Лимит экспозиции 25% срезает до 158 акций.
    """
    d = size_position(315.20, 313.90, "up", CFG)
    assert d.approved
    assert d.binding_constraint == "exposure"
    assert d.shares == 158
    assert d.notional_rub <= CFG.max_position_rub + 1e-6
    # фактический риск НИЖЕ целевого — так и должно быть
    assert d.risk_rub < CFG.risk_rub
    assert abs(d.risk_pct_of_account - 0.10) < 0.01


def test_risk_never_exceeds_target():
    """Ни при каких входных данных риск не превышает целевой."""
    for entry, stop in ((100, 99), (315.2, 313.9), (1000, 995), (50, 49.5),
                        (4715, 4700), (12.5, 12.4)):
        d = size_position(entry, stop, "up", CFG)
        if d.approved:
            assert d.risk_rub <= CFG.risk_rub + 1e-6, (entry, stop, d.risk_rub)


def test_lot_rounding_is_always_down():
    """Округление лотов только вниз: превысить риск нельзя."""
    # по риску влезло бы 50 акций, лот 30 -> берём 30, не 60
    d = size_position(100.0, 90.0, "up", CFG, lot_size=30)
    assert d.approved and d.shares == 30
    assert d.risk_rub <= CFG.risk_rub


def test_zero_size_is_rejected():
    """Если не набирается ни одного лота — отказ, а не размер 0."""
    d = size_position(100.0, 90.0, "up", CFG, lot_size=1000)
    assert not d.approved and d.reason == "risk_zero_size"


def test_missing_levels_rejected():
    for entry, stop in ((None, 90.0), (100.0, None), (0, 90.0), (100.0, 0)):
        d = size_position(entry, stop, "up", CFG)
        assert not d.approved and d.reason == "risk_no_levels"


def test_stop_on_wrong_side_rejected():
    assert size_position(100.0, 101.0, "up", CFG).reason == "risk_stop_wrong_side"
    assert size_position(100.0, 99.0, "down", CFG).reason == "risk_stop_wrong_side"
    assert size_position(100.0, 100.0, "up", CFG).reason == "risk_stop_wrong_side"


def test_short_position_sizes_correctly():
    d = size_position(100.0, 110.0, "down", CFG)
    assert d.approved and d.shares == 50 and d.binding_constraint == "risk"


def test_total_exposure_room_limits_size():
    """Остаток суммарной экспозиции ограничивает сильнее лимита позиции."""
    d = size_position(315.20, 313.90, "up", CFG, available_exposure_rub=10_000.0)
    assert d.approved
    assert d.binding_constraint == "total_exposure"
    assert d.notional_rub <= 10_000.0 + 1e-6


def test_no_exposure_room_rejected():
    d = size_position(100.0, 90.0, "up", CFG, available_exposure_rub=0.0)
    assert not d.approved and d.reason == "risk_exposure_full"


# ──────────────────────────── жёсткие лимиты ─────────────────────────────────

def test_daily_loss_limit_blocks():
    st = RiskState(realized_r_today=-3.0)
    assert check_limits(st, CFG).reason == "risk_daily_loss"
    assert check_limits(RiskState(realized_r_today=-2.99), CFG).approved


def test_weekly_loss_limit_blocks():
    assert check_limits(RiskState(realized_r_week=-6.5), CFG).reason == "risk_weekly_loss"


def test_max_trades_blocks():
    assert check_limits(RiskState(trades_today=3), CFG).reason == "risk_max_trades"
    assert check_limits(RiskState(trades_today=2), CFG).approved


def test_max_positions_blocks():
    assert check_limits(RiskState(open_positions=2), CFG).reason == "risk_max_positions"


def test_kill_switch_blocks_on_drawdown():
    st = RiskState(equity_peak_rub=200_000.0, equity_now_rub=190_000.0)  # −5%
    assert check_limits(st, CFG).reason == "risk_kill_switch"
    st_ok = RiskState(equity_peak_rub=200_000.0, equity_now_rub=192_000.0)  # −4%
    assert check_limits(st_ok, CFG).approved


def test_kill_switch_inactive_without_equity_data():
    """Без данных о капитале kill switch не срабатывает ложно."""
    assert check_limits(RiskState(), CFG).approved


def test_sector_limit_only_with_sector_map():
    st = RiskState(open_sectors=["oil"])
    # без справочника — правило неактивно и об этом сообщается
    d = check_limits(st, CFG, sector="oil", sector_map_available=False)
    assert d.approved and d.sector_limit_active is False
    # со справочником — блокирует
    d2 = check_limits(st, CFG, sector="oil", sector_map_available=True)
    assert not d2.approved and d2.reason == "risk_sector_limit"


def test_limits_are_checked_before_sizing():
    """Запрет важнее размера: при сработавшем лимите размер не считается."""
    st = RiskState(realized_r_today=-5.0)
    d = evaluate_trade(315.20, 313.90, "up", st, CFG)
    assert not d.approved and d.reason == "risk_daily_loss" and d.shares == 0


def test_open_exposure_reduces_room():
    st = RiskState(open_exposure_rub=95_000.0)   # из 100 000 лимита осталось 5 000
    d = evaluate_trade(315.20, 313.90, "up", st, CFG)
    assert d.approved and d.notional_rub <= 5_000.0 + 1e-6


def test_kill_switch_priority_over_other_limits():
    """При просадке kill switch важнее лимита числа сделок."""
    st = RiskState(equity_peak_rub=200_000.0, equity_now_rub=185_000.0,
                   trades_today=99)
    assert check_limits(st, CFG).reason == "risk_kill_switch"



# ─────────────────────── ликвидность (тонкий стакан) ─────────────────────────

def test_liquidity_inactive_without_orderbook_data():
    """Без данных стакана проверка не выполняется И об этом сообщается."""
    d = size_position(100.0, 90.0, "up", CFG)
    assert d.approved and d.liquidity_active is False
    assert d.spread_pct is None and d.depth_near_mid is None


def test_stop_narrower_than_spread_is_rejected():
    """Стоп 0.4% при спреде 0.3% выбьет спредом — отказ при любом размере."""
    d = size_position(315.20, 313.90, "up", CFG, spread_pct=0.30)
    assert not d.approved and d.reason == "risk_spread_too_wide"
    assert d.liquidity_active is True and d.stop_to_spread is not None
    assert d.stop_to_spread < CFG.min_stop_to_spread


def test_wide_enough_stop_passes_spread_check():
    """Стоп 10% при спреде 0.1% — проходит с запасом."""
    d = size_position(100.0, 90.0, "up", CFG, spread_pct=0.10)
    assert d.approved and d.liquidity_active is True
    assert d.stop_to_spread == 100.0


def test_thin_book_reduces_size_not_rejects():
    """Тонкий стакан УМЕНЬШАЕТ размер: бумага остаётся доступной."""
    # по риску влезло бы 50 акций; глубина 200 лотов × 10% = 20
    d = size_position(100.0, 90.0, "up", CFG, depth_near_mid=200)
    assert d.approved
    assert d.shares == 20 and d.binding_constraint == "depth"
    assert d.risk_rub < CFG.risk_rub          # риск вышел ниже целевого


def test_deep_book_does_not_bind():
    """Глубокий стакан не ограничивает: связывает риск, как и без него."""
    d = size_position(100.0, 90.0, "up", CFG, depth_near_mid=100_000)
    assert d.approved and d.shares == 50 and d.binding_constraint == "risk"


def test_too_thin_book_rejected_with_own_code():
    """Если даже одного лота не набирается — отдельный код, не risk_zero_size."""
    d = size_position(100.0, 90.0, "up", CFG, depth_near_mid=5)
    assert not d.approved and d.reason == "risk_book_too_thin"
    assert d.binding_constraint == "depth"


def test_liquidity_never_increases_size():
    """Ликвидность может только уменьшить размер, никогда не увеличить."""
    base = size_position(100.0, 90.0, "up", CFG).shares
    for depth in (10, 100, 1000, 10_000):
        d = size_position(100.0, 90.0, "up", CFG, depth_near_mid=depth)
        if d.approved:
            assert d.shares <= base, (depth, d.shares, base)


def test_evaluate_trade_passes_liquidity_through():
    d = evaluate_trade(100.0, 90.0, "up", RiskState(), CFG, depth_near_mid=200)
    assert d.approved and d.binding_constraint == "depth" and d.shares == 20


def test_liquidity_from_orderbook_parsing():
    from src.risk.engine import liquidity_from_orderbook as lfo
    assert lfo({"spread_pct": 0.12, "depth_near_mid": 340}) == (0.12, 340)
    assert lfo(None) == (None, None)
    assert lfo({}) == (None, None)
    # спред 0 — признак неполного снимка, а не идеальной ликвидности
    assert lfo({"spread_pct": 0, "depth_near_mid": 10}) == (None, 10)
    assert lfo({"spread_pct": "мусор", "depth_near_mid": "мусор"}) == (None, None)

# ────────────────────────────── запуск ───────────────────────────────────────

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
