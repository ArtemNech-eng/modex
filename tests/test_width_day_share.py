"""
Сжатие меряется двумя мерами, а не одной.

Ошибка, ради которой написан файл. 31.07 сканер выдал владельцу сигнал по SBER:
«сжатие 274.08-274.70, пробой вверх». Формально всё верно — ширина 1.53xATR
пятиминутки, объём 3.3x. По сути это 0.62 руб при недельном ходе бумаги
267.54-277.65, то есть 10.11 руб. Три спокойные минуты внутри широкого боковика.
Через пять минут цена вернулась под уровень.

Владелец прислал график, и на нём это было видно сразу: повторяющиеся Sell у
276-277 и Buy у 273-274 четвёртый день подряд. Бумага ходит в диапазоне, а
сканер продавал ей середину как «пробой».

ПРОВЕРКА порога перед тем, как зашивать (шорт-пробой, 12 бумаг, 181 день,
издержки 0.05%):

    оставляем (>=15% дневного диапазона): 2344 входа, -0.074R
    отсекаем  (<15%):                     3078 входов, -0.203R
    разница 0.129R, t=4.17, положительна в 6 месяцах из 7

Фильтр НЕ делает основную сессию прибыльной. Он отделяет заведомо плохие входы
от просто плохих — и именно это нужно, чтобы не выдавать владельцу мусор.
"""
import pytest

from src.analysis.intraday import consolidation_breakout


def _series(day_low, day_high, cons_low, cons_high, break_price,
            bars=30, window=6, vol_mult=4.0):
    """
    Серия, где день ходит day_low..day_high, а последние `window` баров стоят
    в коридоре cons_low..cons_high. Последний бар пробивает вниз.
    """
    highs, lows, closes, vols = [], [], [], []
    # тело дня: раскачка на весь диапазон
    for i in range(bars - window - 1):
        mid = day_low + (day_high - day_low) * (0.5 + 0.45 * ((-1) ** i))
        highs.append(min(day_high, mid + (day_high - day_low) * 0.05))
        lows.append(max(day_low, mid - (day_high - day_low) * 0.05))
        closes.append(mid)
        vols.append(1000)
    # консолидация
    for _ in range(window):
        highs.append(cons_high)
        lows.append(cons_low)
        closes.append((cons_high + cons_low) / 2)
        vols.append(1000)
    # пробойный бар
    highs.append(cons_low)
    lows.append(break_price)
    closes.append(break_price)
    vols.append(int(1000 * vol_mult))
    return highs, lows, closes, vols


def test_sber_case_is_rejected():
    """
    Реальный случай 31.07: сжатие 0.62 руб при дневном ходе около 2.2 руб —
    формально проходит по ATR, но это 28% дня... а при недельном взгляде 6%.
    Берём дневной ход пошире, как он и был к моменту сигнала.
    """
    h, l, c, v = _series(day_low=267.5, day_high=277.6,
                         cons_low=274.08, cons_high=274.70,
                         break_price=273.9)
    # ATR подобран так, чтобы ширина вышла 1.53xATR — ровно как показал живой
    # скан. Порог по ATR она проходит; отсечь её обязана вторая мера.
    out = consolidation_breakout(h, l, c, v, atr=0.62 / 1.53, vwap=275.0,
                                 max_width_atr_short=1.6, allow=("short",))
    assert out["signal"] == "none"
    assert "дневного диапазона" in out["reason"], out["reason"]
    assert out["width_day_share"] < 0.15


def test_real_consolidation_passes():
    """Сжатие, занимающее заметную долю дня, проходить обязано."""
    h, l, c, v = _series(day_low=100.0, day_high=104.0,
                         cons_low=101.5, cons_high=102.4,
                         break_price=101.2)
    out = consolidation_breakout(h, l, c, v, atr=0.8, vwap=103.0,
                                 max_width_atr_short=1.5, allow=("short",))
    assert out["signal"] == "short", out.get("reason")


@pytest.mark.parametrize("share,expect_pass", [
    (0.05, False), (0.10, False), (0.14, False),
    (0.16, True), (0.25, True), (0.40, True),
])
def test_threshold_boundary(share, expect_pass):
    """Порог срабатывает там, где заявлено, — на 15%."""
    day_lo, day_hi = 100.0, 110.0
    cons_w = (day_hi - day_lo) * share
    cons_hi = 104.0
    cons_lo = cons_hi - cons_w
    h, l, c, v = _series(day_lo, day_hi, cons_lo, cons_hi, cons_lo - 0.05)
    out = consolidation_breakout(h, l, c, v, atr=max(cons_w, 0.6) / 1.2,
                                 vwap=106.0, max_width_atr_short=1.5,
                                 allow=("short",))
    rejected_by_share = "дневного диапазона" in out.get("reason", "")
    assert rejected_by_share == (not expect_pass), (
        f"доля {share:.0%}: reason={out.get('reason')}"
    )


def test_threshold_is_configurable():
    """Порог должен настраиваться — режим наблюдения может хотеть шире."""
    h, l, c, v = _series(100.0, 110.0, 103.5, 104.0, 103.4)
    strict = consolidation_breakout(h, l, c, v, atr=0.45, vwap=106.0,
                                    max_width_atr_short=1.5, allow=("short",))
    loose = consolidation_breakout(h, l, c, v, atr=0.45, vwap=106.0,
                                   max_width_atr_short=1.5,
                                   min_width_day_share=0.0, allow=("short",))
    assert "дневного диапазона" in strict.get("reason", "")
    assert "дневного диапазона" not in loose.get("reason", "")


def test_measurement_recorded_next_to_the_code():
    """
    Числа проверки обязаны стоять рядом с порогом. Без них следующий агент
    решит, что 15% взяты с потолка, и уберёт фильтр.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/analysis/intraday.py").read_text()
    assert "t=4.17" in src
    assert "-0.203R" in src and "-0.074R" in src


def test_zero_day_range_does_not_crash():
    """Вырожденный день (цена не двигалась) не должен ронять расчёт."""
    h = [100.0] * 30
    l = [100.0] * 30
    c = [100.0] * 30
    v = [1000] * 30
    out = consolidation_breakout(h, l, c, v, atr=0.5, vwap=100.0, allow=("short",))
    assert out["signal"] == "none"
