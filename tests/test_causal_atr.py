"""
Числа для ведения сделки считаются по данным, доступным В МОМЕНТ сделки.

Ошибка, ради которой написан файл: в realized_price_after стояло
    atr = iv.intraday_atr(highs, lows, closes)
по ВСЕЙ выборке. intraday_atr берёт последние 14 баров, а выборка тянется до
момента ОЦЕНКИ — значит трейл вчерашней сделки считался по сегодняшней
волатильности. В момент сделки этого числа не существовало.

Направление искажения не одностороннее, но следствие одно: R в журнале
невоспроизводим в реальном времени, и правило, выведенное из такого R, в живой
торговле не повторяется.

Здесь проверяется само свойство причинности: ATR, посчитанный до входа, не
должен зависеть от того, что случилось после.
"""
import pytest

from src.analysis.intraday import intraday_atr


def _series(n, base=100.0, step=0.5):
    """Ровная пила: предсказуемый ATR, легко отличить хвост от головы."""
    highs, lows, closes = [], [], []
    for i in range(n):
        mid = base + (i % 3) * step
        highs.append(mid + step)
        lows.append(mid - step)
        closes.append(mid)
    return highs, lows, closes


def test_atr_before_entry_ignores_later_bars():
    """ATR по срезу до входа не меняется, что бы ни случилось после входа."""
    h, l, c = _series(60)
    entry_i = 40
    before = intraday_atr(h[:entry_i], l[:entry_i], c[:entry_i])

    # После входа — резкий всплеск волатильности (в 20 раз шире).
    h2, l2, c2 = list(h), list(l), list(c)
    for i in range(entry_i, len(h2)):
        h2[i] = c2[i] + 10.0
        l2[i] = c2[i] - 10.0
    after_spike = intraday_atr(h2[:entry_i], l2[:entry_i], c2[:entry_i])

    assert before == after_spike, (
        "ATR до входа обязан не зависеть от будущих баров — иначе это "
        "заглядывание вперёд"
    )


def test_full_window_atr_does_leak_the_future():
    """
    Контрольный тест: старый способ (ATR по всей выборке) действительно течёт.
    Если этот тест когда-нибудь упадёт — значит intraday_atr перестал брать
    хвост, и комментарии в realized_price_after надо переписать.
    """
    h, l, c = _series(60)
    calm = intraday_atr(h, l, c)

    h2, l2, c2 = list(h), list(l), list(c)
    for i in range(40, len(h2)):
        h2[i] = c2[i] + 10.0
        l2[i] = c2[i] - 10.0
    stormy = intraday_atr(h2, l2, c2)

    assert stormy > calm * 3, (
        "по всей выборке ATR обязан подхватить поздний всплеск — именно это "
        "и попадало в трейл сделки из прошлого"
    )


def test_intraday_outcome_slices_before_entry():
    """
    В оценке исхода сделки (intraday_outcome — там живёт трейл) ATR должен
    считаться по срезу до входа.
    """
    import inspect

    from src.agent import intraday_analyst as ia

    src = inspect.getsource(ia.intraday_outcome)
    assert "_entry_i" in src and "highs[:_entry_i]" in src
    assert _code_only(src).count("intraday_atr(highs, lows, closes)") == 0, (
        "вернулся ATR по всей выборке — это заглядывание вперёд"
    )


def _code_only(src: str) -> str:
    """
    Исходник без строк-комментариев. Нужно потому, что рядом с правкой
    намеренно оставлен комментарий с ПРЕЖНИМ вызовом — иначе через полгода
    непонятно, что именно чинили. Проверять надо код, а не объяснение.
    """
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


def test_no_full_window_atr_anywhere_in_evaluation():
    """Ни одна функция оценки не должна брать ATR по всей выборке."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "src/agent/intraday_analyst.py").read_text()
    assert _code_only(src).count("intraday_atr(highs, lows, closes)") == 0


@pytest.mark.parametrize("entry_i", [20, 35, 50])
def test_causal_atr_is_stable_across_entry_points(entry_i):
    """Для одной серии ATR до входа зависит только от истории до входа."""
    h, l, c = _series(60)
    a = intraday_atr(h[:entry_i], l[:entry_i], c[:entry_i])
    # Дописываем произвольный хвост — значение обязано остаться прежним.
    b = intraday_atr((h + [999.0] * 5)[:entry_i],
                     (l + [0.1] * 5)[:entry_i],
                     (c + [500.0] * 5)[:entry_i])
    assert a == b
