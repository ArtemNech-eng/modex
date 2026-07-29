"""Тесты относительного объёма.

Запуск: python3 tests/test_volume.py

Объём — базовая проверка любого сетапа: пробой на интересе и пробой на пустоте
это разные события. Данные приходили от MOEX в каждом ответе, но выбрасывались
при разборе, поэтому проверка была недоступна. Здесь проверяется арифметика и,
главное, что функция не выдумывает значение при нехватке данных.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.analysis.technical import volume_stats  # noqa: E402


def test_spike_detected():
    """Последний бар вдвое выше среднего — всплеск."""
    v = [100] * 20 + [250]
    r = volume_stats(v)
    assert r["rel_volume"] == 2.5
    assert r["volume_label"] == "всплеск объёма"


def test_above_average():
    r = volume_stats([100] * 20 + [140])
    assert r["rel_volume"] == 1.4
    assert r["volume_label"] == "выше среднего"


def test_normal_and_below():
    assert volume_stats([100] * 20 + [100])["volume_label"] == "норма"
    assert volume_stats([100] * 20 + [50])["volume_label"] == "ниже среднего"


def test_turnover_takes_priority_over_shares():
    """Оборот в рублях сравним между бумагами разной цены, штуки — нет.
    Поэтому метка считается по обороту, если он есть."""
    r = volume_stats([100] * 20 + [100],        # штуки: норма
                     [100] * 20 + [300])        # оборот: всплеск
    assert r["rel_volume"] == 1.0
    assert r["rel_turnover"] == 3.0
    assert r["volume_label"] == "всплеск объёма"   # решает оборот


def test_no_data_returns_none_not_zero():
    """Ноль читался бы как «объёма нет», хотя данных просто не было."""
    for bad in (None, [], [None, None], [100]):
        r = volume_stats(bad)
        assert r["rel_volume"] is None, bad
        assert r["volume_label"] is None, bad


def test_zeros_and_nones_ignored():
    """Нулевые и отсутствующие бары (праздники, остановка торгов) не портят среднее."""
    v = [None, 0, 100, 100, 100, 0, None, 200]
    r = volume_stats(v)
    assert r["rel_volume"] == 2.0          # 200 / среднее(100,100,100)
    assert r["last_volume"] == 200


def test_lookback_limits_window():
    """Среднее берётся по последним lookback барам, а не по всей истории."""
    v = [1000] * 50 + [100] * 10 + [200]
    r = volume_stats(v, lookback=10)
    assert r["rel_volume"] == 2.0          # против 100, а не против 1000


def test_partial_bar_warning_always_present():
    """Последний бар может быть незавершённым. Предупреждение обязательно,
    иначе низкий rel_volume в середине дня прочитают как отсутствие интереса."""
    r = volume_stats([100] * 20 + [50])
    assert r["last_bar_may_be_partial"] is True


def test_real_ozon_shape():
    """Форма реальных данных: рост +4.34% прошёл на обороте 1.35x среднего —
    выше среднего, но НЕ всплеск. Именно это отличает убедительный пробой от
    умеренного участия."""
    values = [3.5e9] * 20 + [4.98e9]
    r = volume_stats(None, values)
    assert r["rel_turnover"] == 1.42
    assert r["volume_label"] == "выше среднего"
    assert r["volume_label"] != "всплеск объёма"



# ─────────── происхождение данных: реалтайм или задержка ─────────────────────

from src.agent.intraday_analyst import compute_intraday_context  # noqa: E402


def _candles(n=30, price=100.0):
    return {"dates": [f"2026-07-29 1{i//60}:{i%60:02d}:00" for i in range(n)],
            "open": [price] * n, "high": [price * 1.01] * n,
            "low": [price * 0.99] * n, "close": [price] * n,
            "volume": [1000] * n}


def test_source_recorded_in_context():
    """Имя источника обязано доезжать до контекста: без него нельзя понять,
    ПОЧЕМУ данные запоздали и по каким бумагам это систематически."""
    ctx = compute_intraday_context(_candles(), minute_of_day=12 * 60,
                                   delayed=True, source="moex_iss")
    assert ctx is not None
    assert ctx["source"] == "moex_iss"
    assert ctx["delayed"] is True


def test_realtime_source_recorded():
    ctx = compute_intraday_context(_candles(), minute_of_day=12 * 60,
                                   delayed=False, source="tinkoff")
    assert ctx["source"] == "tinkoff" and ctx["delayed"] is False


def test_source_absent_is_none_not_guess():
    """Если источник не передан — None, а не догадка про Tinkoff."""
    ctx = compute_intraday_context(_candles(), minute_of_day=12 * 60)
    assert ctx["source"] is None
    assert ctx["delayed"] is False


def test_batch_line_and_legend_stay_in_sync():
    """Строка batch-скрина и легенда должны меняться вместе: модель читает
    сжатый формат, и рассинхрон превращает данные в шум."""
    src = open(os.path.join(ROOT, "src", "agent", "claude_agent.py"),
               encoding="utf-8").read()
    assert "rv{_s(b.get('rvol'))}" in src, "объём не попал в строку batch"
    assert "' RT' if b.get('rt') else ' DLY'" in src, "метка реалтайма не в строке"
    assert "rv<объём к среднему" in src, "объём не описан в легенде"
    assert "RT=реалтайм/DLY=" in src, "метка реалтайма не описана в легенде"

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
