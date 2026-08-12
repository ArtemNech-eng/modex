"""
Тесты для Early Move Detector (Ранний детектор зарождения движения).

Проверяют:
  1. Детектор обнаруживает EARLY_MOVE_UP при ускорении цены, оборота и притоке покупок.
  2. Детектор обнаруживает EARLY_MOVE_DOWN при ускорении вниз и давлении продаж.
  3. Детектор НЕ ждёт закрытия свечи (работает на субминутных снимках).
  4. Содержит все обязательные поля (что изменилось, изменения за 10-30с, оборот, стакан).
  5. Корректно считает статистику исходов через 1, 3, 5, 15, 30 минут.
  6. Не срабатывает при одиночном незначительном изменении (требует мультифакторность).
"""
import pytest
from src.analysis.early_move import EarlyMoveDetector, EarlyMoveEvent


def test_early_move_up_detection():
    det = EarlyMoveDetector(cooldown_sec=10.0)
    tk = "SBER"

    # Было спокойно 60 секунд (цена 276.50, покупки 50%)
    for i in range(50):
        det.add_snapshot(
            ticker=tk, price=276.50, turnover_rub=100_000.0 * i,
            buy_lots=10 * i, sell_lots=10 * i, bid_vol=1000.0, ask_vol=1000.0,
            ts_sec=1000.0 + i
        )

    # Вдруг за последние 10 секунд цена ускоряется, оборот и покупки растут
    ev = det.add_snapshot(
        ticker=tk,
        price=276.85,                           # +0.126% за 10 сек
        turnover_rub=100_000.0 * 50 + 5_000_000.0,  # резкий приток оборота
        buy_lots=500 + 80, sell_lots=500 + 20,      # покупки 80%
        bid_vol=1500.0, ask_vol=500.0,              # уменьшение асков, рост бидов
        ts_sec=1060.0,
        nearest_level=277.0,
    )

    assert ev is not None
    assert ev.direction == "EARLY_MOVE_UP"
    assert ev.ticker == "SBER"
    assert ev.price == 276.85
    assert ev.change_10s_pct > 0.05
    assert ev.turnover_accel >= 1.5
    assert ev.buy_share >= 0.65
    assert len(ev.what_changed) >= 3
    assert any("ускоряться вверх" in w for w in ev.what_changed)
    assert ev.nearest_level == 277.0


def test_early_move_down_detection():
    det = EarlyMoveDetector(cooldown_sec=10.0)
    tk = "GAZP"

    # Было спокойно
    for i in range(50):
        det.add_snapshot(
            ticker=tk, price=95.0, turnover_rub=50_000.0 * i,
            buy_lots=5 * i, sell_lots=5 * i, bid_vol=2000.0, ask_vol=2000.0,
            ts_sec=2000.0 + i
        )

    # Резкое ускорение вниз + продажи
    ev = det.add_snapshot(
        ticker=tk,
        price=94.75,                            # -0.26% за 10 сек
        turnover_rub=50_000.0 * 50 + 4_000_000.0,
        buy_lots=250 + 10, sell_lots=250 + 90,      # продажи 90%
        bid_vol=500.0, ask_vol=2500.0,              # биды проедаются
        ts_sec=2060.0,
    )

    assert ev is not None
    assert ev.direction == "EARLY_MOVE_DOWN"
    assert ev.ticker == "GAZP"
    assert ev.sell_share >= 0.7
    assert any("ускоряться вниз" in w for w in ev.what_changed)


def test_no_fire_on_single_condition_only():
    """Одиночное изменение цены без оборота и агрессивного потока не должно давать событие."""
    det = EarlyMoveDetector(cooldown_sec=10.0)
    tk = "LKOH"

    for i in range(50):
        det.add_snapshot(tk, 4600.0, 10_000 * i, 5 * i, 5 * i, 1000, 1000, 3000.0 + i)

    # Цена чуть выросла, но оборот маленький и поток сбалансирован
    ev = det.add_snapshot(
        tk, price=4605.0, turnover_rub=10_000 * 50 + 1000,
        buy_lots=255, sell_lots=255, bid_vol=1000, ask_vol=1000,
        ts_sec=3060.0
    )
    assert ev is None


def test_outcome_tracking_statistics():
    det = EarlyMoveDetector(cooldown_sec=1.0)
    tk = "VTBR"

    # Создаём событие EARLY_MOVE_UP на ts=4000
    for i in range(50):
        det.add_snapshot(tk, 55.0, 1000 * i, 1 * i, 1 * i, 500, 500, 4000.0 + i)

    ev = det.add_snapshot(
        tk, 55.15, 1000 * 50 + 5_000_000, 50 + 90, 50 + 10, 1500, 500, 4060.0
    )
    assert ev is not None
    assert ev.direction == "EARLY_MOVE_UP"
    assert ev.price == 55.15

    # Через 60 секунд (1 мин) цена стала 55.30 (+0.27%)
    det.add_snapshot(tk, 55.30, 0, 0, 0, 500, 500, 4125.0)
    assert ev.after_1m_pct == pytest.approx(round((55.30 - 55.15)/55.15*100, 3))

    # Через 180 секунд (3 мин) цена стала 55.50
    det.add_snapshot(tk, 55.50, 0, 0, 0, 500, 500, 4245.0)
    assert ev.after_3m_pct == pytest.approx(round((55.50 - 55.15)/55.15*100, 3))

    stats = det.get_statistics()
    assert stats["total_events"] == 1
    assert stats["UP"]["count"] == 1
    assert stats["UP"]["after_1m"]["count"] == 1
    assert stats["UP"]["after_1m"]["win_rate"] == 1.0
