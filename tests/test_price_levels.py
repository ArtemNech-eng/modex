"""
Локальные уровни с графика, крайности дня и изменение потока.

Артём сказал прямо: «уровни делай не со стаканов а с графиков, стаканы лишь
дополнение». Уровень в стакане — чья-то заявка здесь и сейчас, её могут снять за
секунду. Уровень на графике — место, где цена уже разворачивалась, и оно остаётся,
даже когда в стакане на этой цене пусто.

Что защищают эти тесты:

    причинность    разворот подтверждается только через `right` бар после себя,
                   и уровень датируется минутой ПОДТВЕРЖДЕНИЯ
    склейка        276.50 и 276.52 это одно место, а не два уровня
    шаги цены      допуск в шагах, а не в рублях: в рублях он ломается о
                   двоичную арифметику
    без оценок     нет пометок «сильный» и «слабый» — сколько касаний делает
                   уровень значимым, не измерено
    время экстремума  максимум на открытии и максимум минуту назад — разное
"""
import pytest

from src.analysis.price_levels import (swings, levels, day_extremes,
                                       flow_change, MIN_BARS)


def bar(ts, hi, lo, close=None):
    if close is None:
        close = (hi + lo) / 2 if (hi is not None and lo is not None) else None
    return {"ts": ts, "high": hi, "low": lo, "close": close}


def flat(n=20, hi=100.2, lo=99.8):
    return [bar(f"2026-08-03T10:{i:02d}", hi, lo) for i in range(n)]


# ─── развороты ────────────────────────────────────────────────────────────────

def test_swing_high_needs_lower_neighbours_on_both_sides():
    rows = flat(10)
    rows[5] = bar("2026-08-03T10:05", 101.0, 99.9)      # выше всех
    sw = [s for s in swings(rows) if s["kind"] == "high"]
    assert len(sw) == 1 and sw[0]["price"] == 101.0


def test_swing_is_dated_by_its_confirmation():
    """
    Разворот виден только через `right` бар после себя. Датировать уровень самим
    разворотом значило бы утверждать, что в 10:05 мы знали про 10:08 — ровно та
    ошибка, из-за которой 31.07 вместо 3078 пробоев нашлось 6.
    """
    rows = flat(12)
    rows[5] = bar("2026-08-03T10:05", 101.0, 99.9)
    s = [x for x in swings(rows, left=3, right=3) if x["kind"] == "high"][0]
    assert s["ts"] == "2026-08-03T10:05"
    assert s["confirmed_ts"] == "2026-08-03T10:08", "подтверждение через три бара"


def test_edge_bars_are_not_swings():
    """У крайних бар нет соседей с одной стороны — разворот не подтверждаем."""
    rows = flat(8)
    rows[0] = bar("2026-08-03T10:00", 105.0, 99.9)
    rows[-1] = bar("2026-08-03T10:07", 105.0, 99.9)
    assert not [s for s in swings(rows) if s["price"] == 105.0]


def test_flat_series_has_no_swings():
    assert swings(flat(20)) == []


# ─── склейка в уровни ─────────────────────────────────────────────────────────

def test_near_prices_become_one_level():
    """
    Цена редко разворачивается на той же копейке: 276.50 и 276.52 это одно место.
    Без склейки список превратился бы в перечисление всех колебаний.
    """
    rows = flat(24, hi=276.2, lo=275.8)
    rows[5] = bar("2026-08-03T10:05", 276.50, 275.9)
    rows[12] = bar("2026-08-03T10:12", 276.52, 275.9)
    rows[19] = bar("2026-08-03T10:19", 276.51, 275.9)
    got = levels(rows, tick=0.01, tol_ticks=3)
    highs = [g for g in got if g["kind"] in ("high", "both")]
    assert len(highs) == 1, "три разворота рядом — один уровень"
    assert highs[0]["touches"] == 3


def test_far_prices_stay_separate():
    rows = flat(24, hi=276.2, lo=275.8)
    rows[5] = bar("2026-08-03T10:05", 276.50, 275.9)
    rows[12] = bar("2026-08-03T10:12", 277.90, 275.9)
    got = [g for g in levels(rows, tick=0.01, tol_ticks=3)
           if g["kind"] in ("high", "both")]
    assert len(got) == 2


def test_tolerance_is_in_ticks_not_in_rubles():
    """
    Шаг разный: SBER 0.01, UGLD 0.0001. Единый допуск в рублях склеил бы у одной
    бумаги полдиапазона, а у другой не склеил ничего.
    """
    rows = flat(24, hi=10.02, lo=9.98)
    rows[5] = bar("2026-08-03T10:05", 10.0500, 9.99)
    rows[12] = bar("2026-08-03T10:12", 10.0502, 9.99)
    # шаг 0.0001: разница 2 шага, склеиваются
    one = [g for g in levels(rows, tick=0.0001, tol_ticks=3)
           if g["kind"] in ("high", "both")]
    assert len(one) == 1
    # шаг 0.00001: разница 20 шагов, не склеиваются
    two = [g for g in levels(rows, tick=0.00001, tol_ticks=3)
           if g["kind"] in ("high", "both")]
    assert len(two) == 2


def test_level_can_be_both_resistance_and_support():
    """
    Цена отталкивалась от уровня и сверху, и снизу. Это факт, а не противоречие.

    Ряд строится в два этапа: сначала цена ходит НИЖЕ уровня и один раз достаёт
    до него максимумом, потом уходит ВЫШЕ и один раз опускается до него
    минимумом. В одном ровном коридоре такого не бывает — соседи не дадут одной
    цене быть и максимумом, и минимумом.
    """
    rows = ([bar(f"2026-08-03T10:{i:02d}", 100.20, 99.80) for i in range(10)]
            + [bar(f"2026-08-03T10:{i:02d}", 101.20, 100.80) for i in range(10, 20)])
    rows[5] = bar("2026-08-03T10:05", 100.50, 99.90)     # максимум на 100.50
    rows[15] = bar("2026-08-03T10:15", 101.10, 100.50)   # минимум там же
    got = [g for g in levels(rows, tick=0.01, tol_ticks=3) if g["touches"] > 1]
    assert got and got[0]["kind"] == "both"


def test_side_and_distance_relative_to_current_price():
    rows = flat(24, hi=100.2, lo=99.8)
    rows[5] = bar("2026-08-03T10:05", 102.0, 99.9)
    rows[12] = bar("2026-08-03T10:12", 100.1, 98.0)
    got = {g["kind"]: g for g in levels(rows, tick=0.01, price_now=100.0)}
    assert got["high"]["side"] == "above" and got["high"]["dist_pct"] > 0
    assert got["low"]["side"] == "below" and got["low"]["dist_pct"] < 0


def test_sorted_by_distance_not_by_touches():
    """
    Порядок по БЛИЗОСТИ. Сколько касаний делает уровень значимым — не измерено, и
    ставить это в основу порядка значило бы выдать догадку за знание.
    """
    rows = flat(40, hi=100.2, lo=99.8)
    rows[5] = bar("2026-08-03T10:05", 105.0, 99.9)       # далеко, одно касание
    rows[15] = bar("2026-08-03T10:15", 100.5, 99.9)      # близко
    rows[25] = bar("2026-08-03T10:25", 100.52, 99.9)     # близко, второе касание
    got = levels(rows, tick=0.01, price_now=100.0)
    assert abs(got[0]["dist_pct"]) <= abs(got[-1]["dist_pct"])


def test_no_strength_labels():
    """
    Никаких «сильный» и «слабый». Именно так неделю назад появились семь шагов с
    придуманными формулами, не давшие ничего.
    """
    rows = flat(24, hi=100.2, lo=99.8)
    rows[5] = bar("2026-08-03T10:05", 102.0, 99.9)
    for g in levels(rows, tick=0.01, price_now=100.0):
        for bad in ("strength", "strong", "weak", "score", "signal", "direction"):
            assert bad not in g


def test_too_little_history_gives_nothing():
    """
    Пока баров мало, уровней нет. Иначе первые минуты дня всегда выглядели бы
    полными уровней — просто потому, что сравнивать не с чем.
    """
    assert levels(flat(MIN_BARS - 1), tick=0.01) == []


def test_broken_bars_do_not_crash():
    rows = flat(20)
    rows[7] = {"ts": "2026-08-03T10:07"}                 # пустой бар
    rows[9] = bar("2026-08-03T10:09", None, None)
    levels(rows, tick=0.01)
    swings(rows)


# ─── крайности дня ────────────────────────────────────────────────────────────

def test_day_extremes_carry_their_time():
    """
    Время важно. 30.07 по FLOT «максимум дня» стоял в 10:00, а сигнал на его
    пробой выдали в 23:30 — рынок прошёл и отверг этот уровень тринадцатью
    часами ранее.
    """
    rows = flat(20, hi=100.2, lo=99.8)
    rows[3] = bar("2026-08-03T10:03", 103.0, 99.9)
    rows[17] = bar("2026-08-03T10:17", 100.1, 97.0)
    d = day_extremes(rows)
    assert d["high"] == 103.0 and d["high_ts"] == "2026-08-03T10:03"
    assert d["low"] == 97.0 and d["low_ts"] == "2026-08-03T10:17"


def test_position_inside_the_day_range():
    """0 у минимума дня, 1 у максимума. Где цена внутри дня — это факт."""
    rows = [bar("2026-08-03T10:00", 110.0, 90.0, close=100.0)]
    d = day_extremes(rows)
    assert d["range"] == pytest.approx(20.0)
    assert d["position"] == pytest.approx(0.5)


def test_day_extremes_on_empty_input():
    assert day_extremes([])["high"] is None


# ─── изменение потока ─────────────────────────────────────────────────────────

def test_flow_change_compares_two_windows():
    """
    Дельта −500 говорит, что продают. Но −500 после −2000 означает, что давление
    СЛАБЕЕТ, а −500 после +300 — что оно только началось. Одно число, разные
    ситуации.
    """
    rows = ([{"buy_volume": 0, "sell_volume": 400} for _ in range(5)]
            + [{"buy_volume": 0, "sell_volume": 100} for _ in range(5)])
    c = flow_change(rows, window=5)
    assert c["delta_prev"] == -2000
    assert c["delta_now"] == -500
    assert c["delta_change"] == 1500, "давление ослабло, хотя дельта отрицательна"


def test_flow_flip_is_flagged():
    """Смена знака — отдельный факт: поток развернулся, а не просто ослаб."""
    rows = ([{"buy_volume": 0, "sell_volume": 300} for _ in range(5)]
            + [{"buy_volume": 300, "sell_volume": 0} for _ in range(5)])
    c = flow_change(rows, window=5)
    assert c["flipped"] is True


def test_no_flip_when_pressure_only_weakens():
    rows = ([{"buy_volume": 0, "sell_volume": 400} for _ in range(5)]
            + [{"buy_volume": 0, "sell_volume": 100} for _ in range(5)])
    assert flow_change(rows, window=5)["flipped"] is False


def test_flow_change_needs_two_full_windows():
    rows = [{"buy_volume": 10, "sell_volume": 5} for _ in range(7)]
    assert flow_change(rows, window=5) == {}


# ─── подключение к карточке и экрану ──────────────────────────────────────────

import pathlib                                                    # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_all_five_gaps_are_closed_in_the_card():
    """
    Пять пробелов из проверки 01.08: изменение за 1 минуту, максимум и минимум
    дня, изменение потока, дневной ATR, локальные уровни.
    """
    api = (ROOT / "src/api/main.py").read_text()
    assert "for n in (1, 3, 5, 15):" in api, "минутное изменение цены"
    for k in ('out["day"]', 'out["flow_change"]', 'out["price_levels"]',
              'out["atr_day"]'):
        assert k in api, k


def test_daily_atr_is_daily_and_says_so():
    """
    До этого на карточке был средний диапазон за 14 МИНУТ под именем ATR. Для
    стопа он бесполезен: риск считается от дневного хода.
    """
    main = (ROOT / "main.py").read_text()
    assert "interval=24" in main, "дневные свечи, а не минутные"
    assert "volatility_state" in main, "переиспользуем уже проверенный расчёт"
    i = main.index("ДНЕВНОЙ ATR")
    assert "14 МИНУТ" in main[i:i + 700], "разница записана рядом"
    api = (ROOT / "src/api/main.py").read_text()
    assert "vwap_dist_atr_day" in api and "vwap_dist_atr14m" in api, \
        "оба поля живут отдельно и не путаются"


def test_atr_fetch_does_not_block_the_stream():
    """
    Восемьдесят запросов к ISS с паузами — это минуты. Стрим не должен их ждать:
    без ATR карточка работает, просто без одного поля.
    """
    main = (ROOT / "main.py").read_text()
    assert "asyncio.create_task(_atr_background())" in main


def test_page_shows_levels_and_says_they_are_not_rated():
    """
    Рамка должна быть НА ЭКРАНЕ: смотреть будут на экран, а не в код.
    """
    page = (ROOT / "dashboard/market-watch.html").read_text()
    assert "уровни с графика" in page
    assert "не измерено" in page
    assert "ATR дня" in page
    assert "было / стало" in page, "изменение потока"
    assert "где цена в диапазоне" in page


def test_levels_come_from_chart_not_from_the_book():
    """
    Артём сказал прямо: «уровни делай не со стаканов а с графиков, стаканы лишь
    дополнение». Модуль обязан работать по свечам, а не по стакану.
    """
    src = (ROOT / "src/analysis/price_levels.py").read_text()
    assert "с ГРАФИКА" in src
    for book_field in ("bid_vol_sum", "ask_vol_sum", "bid_share"):
        assert book_field not in src, f"{book_field} — это стакан, не график"
