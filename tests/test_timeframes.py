"""
Пять таймфреймов рядом и их согласие.

Артём объяснил, зачем: «1м разворот вверх, 5м ещё вниз, 15м вниз» — это
совершенно другая ситуация, чем «1м ↑ / 5м ↑ / 15м ↑». Одна цифра изменения их
не различает, а расхождение таймфреймов есть ФАКТ о данных.

Главное, что защищают эти тесты:

    незакрытый бар   30-минутный бар на второй минуте и завершённый — не одно и
                     то же. Структура считается ТОЛЬКО по закрытым, текущий
                     отдаётся отдельно с пометкой. На минутах ошибка незаметна,
                     на тридцатиминутках она переворачивает картину.

    боковик          не отсутствие ответа, а отдельный ответ

    ускорение        замедление при том же направлении и ускорение — разные
                     состояния, одна цифра изменения их не различает

    без советов      структура это описание. 31.07 требование «структура вверх»
                     для покупки на откате измерялось ВРЕДНЫМ: t=-12.57
"""
import pytest

from src.analysis.timeframes import (bars, structure, frame, profile,
                                     STEPS, FLAT_PCT)


def bar(i, hi, lo, close=None, op=None):
    """Минута номер i внутри часа 10."""
    return {"ts": f"2026-08-03T10:{i:02d}", "high": hi, "low": lo,
            "close": close if close is not None else (hi + lo) / 2,
            "open": op if op is not None else (hi + lo) / 2, "volume": 10}


def rising(n=30, start=100.0, step_pct=0.05):
    out = []
    p = start
    for i in range(n):
        p *= (1 + step_pct / 100)
        out.append(bar(i, p * 1.001, p * 0.999, close=p))
    return out


def falling(n=30, start=100.0, step_pct=0.05):
    return [bar(i, r["high"], r["low"], close=r["close"])
            for i, r in enumerate(reversed(rising(n, start, step_pct)))]


# ─── сборка бар ───────────────────────────────────────────────────────────────

def test_minutes_group_into_bars_of_the_step():
    rows = [bar(i, 100 + i, 99 + i, close=100 + i) for i in range(15)]
    b5 = bars(rows, 5)
    assert len(b5) == 3
    assert b5[0]["minutes"] == 5
    assert b5[0]["high"] == 104 and b5[0]["low"] == 99


def test_bar_takes_extremes_and_last_close():
    rows = [bar(0, 101, 99, close=100), bar(1, 105, 98, close=104),
            bar(2, 103, 97, close=99)]
    b = bars(rows, 3)[0]
    assert b["high"] == 105 and b["low"] == 97
    assert b["close"] == 99, "закрытие последней минуты"


def test_current_bar_is_marked_forming():
    """
    Главная тонкость. Тридцатиминутный бар на второй минуте и завершённый — не
    одно и то же: его границы и закрытие ещё изменятся.
    """
    rows = [bar(i, 100, 99, close=99.5) for i in range(7)]
    b5 = bars(rows, 5)
    assert b5[0]["complete"] is True, "минуты 0-4 закрыты"
    assert b5[-1]["complete"] is False, "минуты 5-6 ещё набираются"


def test_full_bar_containing_the_last_minute_is_still_forming():
    """
    Признак — не «мало минут», а «содержит последнюю минуту ряда». Бар может быть
    полным по счёту минут и всё равно быть текущим, если тихие минуты пропущены.
    """
    rows = [bar(i, 100, 99, close=99.5) for i in range(10)]
    b5 = bars(rows, 5)
    assert b5[-1]["minutes"] == 5
    assert b5[-1]["complete"] is False


def test_empty_and_broken_input():
    assert bars([], 5) == []
    assert bars([{"ts": "bad"}, {"ts": "2026-08-03T10:00"}], 5) == []


# ─── структура HH/HL и LH/LL ─────────────────────────────────────────────────

def test_higher_high_and_higher_low_is_up():
    closed = [{"high": 100, "low": 99}, {"high": 101, "low": 100}]
    s = structure(closed)
    assert s["hh"] and s["hl"] and s["structure"] == "up"


def test_lower_high_and_lower_low_is_down():
    closed = [{"high": 101, "low": 100}, {"high": 100, "low": 99}]
    s = structure(closed)
    assert s["lh"] and s["ll"] and s["structure"] == "down"


def test_outside_bar_is_expanding_not_up():
    """
    Максимум выше и минимум ниже — бар шире предыдущего. Это не тренд вверх, и
    называть его «up» значило бы соврать.
    """
    closed = [{"high": 100, "low": 99}, {"high": 102, "low": 98}]
    assert structure(closed)["structure"] == "expanding"


def test_inside_bar_is_compression():
    """Максимум ниже, минимум выше — сжатие внутри предыдущего бара."""
    closed = [{"high": 102, "low": 98}, {"high": 100, "low": 99}]
    assert structure(closed)["structure"] == "inside"


def test_structure_needs_two_bars():
    assert structure([{"high": 100, "low": 99}]) == {}


# ─── направление и боковик ───────────────────────────────────────────────────

def test_direction_up_on_rising_series():
    f = frame(rising(30), 5)
    assert f["direction"] == "up" and f["change_pct"] > 0


def test_direction_down_on_falling_series():
    f = frame(falling(30), 5)
    assert f["direction"] == "down" and f["change_pct"] < 0


def test_flat_is_an_answer_not_a_gap():
    """
    Боковик — отдельный ответ, а не отсутствие ответа. Иначе ровный рынок
    выглядел бы как отсутствие данных.
    """
    rows = [bar(i, 100.001, 99.999, close=100.0) for i in range(30)]
    f = frame(rows, 5)
    assert f["direction"] == "flat"
    assert abs(f["change_pct"]) < FLAT_PCT


def test_structure_uses_only_closed_bars():
    """
    Незакрытый бар в структуру не попадает. Иначе сравнивались бы два полных
    бара против одного полного и одного огрызка.
    """
    rows = [bar(i, 100, 99, close=99.5) for i in range(10)]
    # последняя минута резко выше — но она в НЕЗАКРЫТОМ баре
    rows.append(bar(10, 120, 119, close=119.5))
    f = frame(rows, 5)
    assert f.get("structure") != "up", "выброс в текущем баре структуру не меняет"
    assert f["forming"]["high"] == 120, "но сам текущий бар виден отдельно"


# ─── ускорение ───────────────────────────────────────────────────────────────

def test_acceleration_is_detected():
    """
    Замедление при том же направлении и ускорение — разные состояния. Одна цифра
    изменения их не различает.
    """
    rows = ([bar(0, 100, 100, close=100), bar(1, 101, 101, close=101)]
            + [bar(2, 105, 105, close=105), bar(3, 105, 105, close=105)])
    f = frame(rows, 1)
    assert f["move_prev"] == pytest.approx(1.0)
    assert f["move_last"] == pytest.approx(4.0)
    assert f["pace"] == "accelerating"


def test_deceleration_is_detected():
    rows = [bar(0, 100, 100, close=100), bar(1, 105, 105, close=105),
            bar(2, 106, 106, close=106), bar(3, 106, 106, close=106)]
    f = frame(rows, 1)
    assert f["pace"] == "decelerating"


def test_turn_of_the_last_bar_is_flagged():
    """Смена знака хода — отдельный факт, не то же самое, что замедление."""
    rows = [bar(0, 100, 100, close=100), bar(1, 98, 98, close=98),
            bar(2, 99, 99, close=99), bar(3, 99, 99, close=99)]
    f = frame(rows, 1)
    assert f["turned"] is True


def test_no_pace_without_three_closed_bars():
    rows = [bar(i, 100, 99, close=99.5) for i in range(3)]
    assert "pace" not in frame(rows, 1)


# ─── согласие таймфреймов ────────────────────────────────────────────────────

def test_all_frames_agree_on_a_clean_trend():
    p = profile(rising(90))
    ag = p["agreement"]
    assert ag["all_agree"] is True
    assert ag["up"] == ag["total"]
    assert ag["fast_vs_slow"] == "same"


def test_fast_turns_up_while_slow_still_down():
    """
    Ровно случай из запроса: «1м разворот вверх, 5м ещё вниз, 15м вниз». Это
    другая ситуация, чем все вверх, и её надо видеть.
    """
    rows = falling(60, start=110.0) + rising(20, start=100.0, step_pct=0.5)
    p = profile(rows, steps=(1, 15))
    ag = p["agreement"]
    assert ag["fastest"] == "up", "минутка развернулась"
    assert ag["slowest"] == "down", "пятнадцатиминутка ещё вниз"
    assert ag["fast_vs_slow"] == "opposite"
    assert ag["all_agree"] is False


def test_agreement_line_is_readable():
    p = profile(rising(90), steps=(1, 5, 15))
    assert p["agreement"]["line"].count("/") == 2
    assert "1m" in p["agreement"]["line"]


def test_all_five_steps_are_present():
    p = profile(rising(120))
    assert set(p["frames"]) == {f"{s}m" for s in STEPS}
    assert STEPS == (1, 3, 5, 15, 30)


def test_profile_survives_short_history():
    """
    На коротком ряду старшие таймфреймы просто не наберутся, и это не ошибка:
    поля направления у них не появится.
    """
    p = profile(rising(6))
    assert "direction" in p["frames"]["1m"]
    assert "direction" not in p["frames"]["30m"]


def test_profile_on_empty_input():
    p = profile([])
    assert "agreement" not in p
    assert all(f["bars"] == 0 for f in p["frames"].values())


# ─── описание, а не совет ────────────────────────────────────────────────────

def test_no_signal_or_recommendation_fields():
    """
    Структура — описание. 31.07 требование «структура вверх» как условие для
    покупки на откате измерялось ВРЕДНЫМ: t=-12.57, положительных дней 16%.
    """
    p = profile(rising(90))
    blob = str(p).lower()
    for bad in ("signal", "recommend", "buy", "sell", "entry", "target",
                "strong", "strength"):
        assert bad not in blob, bad


def test_caveat_is_written_next_to_the_code():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/analysis/timeframes.py").read_text()
    assert "-12.57" in src, "измеренный вред структуры записан рядом с кодом"
    assert "НЕЗАКРЫТЫЙ БАР" in src


# ─── подключение к карточке и экрану ──────────────────────────────────────────

import pathlib                                                    # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_profile_is_wired_into_the_card():
    api = (ROOT / "src/api/main.py").read_text()
    assert 'out["timeframes"] = profile(rows)' in api
    i = api.index('out["timeframes"]')
    assert "ЗАКРЫТЫМ барам" in api[max(0, i - 600):i], \
        "оговорка про незакрытый бар должна стоять рядом"


def test_page_shows_all_five_and_the_disagreement():
    page = (ROOT / "dashboard/book-live.html").read_text()
    assert "таймфреймы · согласие" in page
    for k in ("1m", "3m", "5m", "15m", "30m"):
        assert f'"{k}"' in page, k
    assert "младший против старшего" in page, "расхождение названо словами"
    assert "ЗАКРЫТЫМ барам" in page, "оговорка на экране, а не только в коде"
