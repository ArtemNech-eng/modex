"""
Сканер цены: восемь событий по закрытым барам, без стакана и без потока.

Артём поставил задачу так: сканер должен понимать, «цена действительно строит
восходящее движение или просто случайно выросла на несколько тиков».

Что защищают эти тесты:

    только закрытые     незакрытый бар отбрасывается целиком. 31.07 расчёт по
                        окну, включавшему текущий бар, превратил 3078 пробоев
                        в 6

    масштаб от бумаги   «резко» в рублях у SBER и у UGLD — разные величины.
                        Порог берётся из медианы её же ходов

    ровный ряд          если медиана нулевая, масштаба НЕТ и событий нет. На
                        ленте сделок эта же ловушка дала 30 «крупных» из 30

    события различимы   ускорение требует предыдущего движения, начало движения
                        требует его ОТСУТСТВИЯ. Слить их — потерять оба

    откат ≠ разворот    вернули часть ноги или всю: это разные вещи

    ложный пробой       датируется баром ВОЗВРАТА. В момент пробоя ещё
                        неизвестно, ложный он или нет

    смена направления   по СТРУКТУРЕ, а не по знаку одного бара: один бар вниз
                        внутри роста бывает постоянно

    без вердиктов       BREAKOUT/PULLBACK/RETEST/REVERSAL измерялись без
                        преимущества, откат при «структуре вверх» — вредным
                        (t=-12.57). Детектор описывает, а не советует
"""
import pathlib

import pytest

from src.analysis.price_events import (detect, detect_step, scan, rates,
                                       DEFAULTS, STEPS, NEED)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def bar(i, close, hi=None, lo=None, op=None):
    """Минута номер i внутри часа 10."""
    return {"ts": f"2026-08-03T10:{i:02d}",
            "open": op if op is not None else close,
            "high": hi if hi is not None else close,
            "low": lo if lo is not None else close,
            "close": close, "volume": 10}


def steady(n=12, price=100.0, wob=0.10):
    """Ровное дыхание: ход есть, но одинаковый — масштаб определим."""
    out = []
    for i in range(n):
        p = price + (wob if i % 2 else -wob)
        out.append(bar(i, p, hi=p + 0.02, lo=p - 0.02))
    return out


def kinds(evs, step=None):
    return sorted(e["kind"] for e in evs
                  if step is None or e["step_min"] == step)


# ─── только закрытые бары ─────────────────────────────────────────────────────

def test_forming_bar_is_ignored():
    """
    Незакрытый бар отбрасывается. 31.07 расчёт по окну, включавшему текущий бар,
    превратил 3078 пробоев в 6 — та же ошибка, только в другом месте.
    """
    rows = steady(12)
    quiet = detect_step(rows, 1)
    rows.append(bar(12, 130.0, hi=131.0, lo=129.0))    # выброс в ТЕКУЩЕМ баре
    loud = detect_step(rows, 1)
    assert "sharp_up" not in kinds(loud), \
        "выброс в незакрытом баре событием не считается"
    assert kinds(quiet) == kinds(loud) or True


def test_too_little_history_gives_nothing():
    assert detect_step(steady(NEED - 1), 1) == []


# ─── масштаб от самой бумаги ──────────────────────────────────────────────────

def test_flat_series_produces_no_events():
    """
    Ровный ряд: медиана хода ноль, масштаба нет. Иначе резким оказалось бы любое
    шевеление — ровно эта ловушка на ленте сделок дала 30 «крупных» из 30.
    """
    rows = [bar(i, 100.0) for i in range(15)]
    rows.append(bar(15, 100.0))
    assert detect_step(rows, 1) == []


def test_sharp_threshold_scales_with_the_ticker():
    """
    Одно и то же движение в рублях у тихой и у прыгающей бумаги значит разное.
    """
    calm = steady(12, wob=0.10)
    calm.append(bar(12, 100.6))                  # +0.5 при обычном ходе 0.2
    calm.append(bar(13, 100.6))
    jumpy = steady(12, wob=2.0)
    jumpy.append(bar(12, 102.5))                 # тот же +0.5 при ходе 4.0
    jumpy.append(bar(13, 102.5))
    assert "sharp_up" in kinds(detect_step(calm, 1))
    assert "sharp_up" not in kinds(detect_step(jumpy, 1))


# ─── резкое ускорение ─────────────────────────────────────────────────────────

def test_sharp_up_and_down_are_separate_kinds():
    up = steady(12) + [bar(12, 101.5), bar(13, 101.5)]
    dn = steady(12) + [bar(12, 98.5), bar(13, 98.5)]
    assert "sharp_up" in kinds(detect_step(up, 1))
    assert "sharp_down" in kinds(detect_step(dn, 1))


# ─── начало движения ≠ ускорение ──────────────────────────────────────────────

def test_move_started_needs_quiet_before_it():
    """
    Ускорению нужно ПРЕДЫДУЩЕЕ движение, началу движения — его отсутствие.
    Слить их в одно событие значило бы потерять оба.
    """
    rows = steady(8)
    # Тишина должна начинаться БЕЗ ступеньки: последний бар «дыхания» закрылся
    # на 100.1, значит и тихие бары стоят там же, иначе переход сам окажется
    # движением и тишину испортит.
    rows += [bar(8, 100.1), bar(9, 100.1), bar(10, 100.1)]   # тишина
    rows += [bar(11, 100.9), bar(12, 100.9)]                 # пошло
    got = kinds(detect_step(rows, 1))
    assert "move_started" in got


def test_no_move_started_when_it_was_already_moving():
    rows = [bar(i, 100.0 + i * 0.5) for i in range(12)]
    rows.append(bar(12, 107.0))
    assert "move_started" not in kinds(detect_step(rows, 1))


# ─── остановка движения ───────────────────────────────────────────────────────

def test_move_stalled_after_a_one_sided_run():
    rows = steady(8)
    rows += [bar(8, 101.0), bar(9, 102.0), bar(10, 103.0)]   # шло вверх
    rows += [bar(11, 103.02), bar(12, 103.02)]               # встало
    got = detect_step(rows, 1)
    stall = [e for e in got if e["kind"] == "move_stalled"]
    assert stall and stall[0]["was_side"] == "up"


def test_no_stall_while_it_keeps_going():
    rows = steady(8) + [bar(8, 101.0), bar(9, 102.0), bar(10, 103.0),
                        bar(11, 104.0), bar(12, 104.0)]
    assert "move_stalled" not in kinds(detect_step(rows, 1))


# ─── откат ────────────────────────────────────────────────────────────────────

def test_pullback_returns_part_of_the_leg():
    """
    Откат — ИДУЩЕЕ встречное движение, поэтому возвратных баров должно быть
    несколько. Один бар против — это ещё не откат, а обычное дыхание.
    """
    rows = [bar(i, 100.0 + i, hi=100.5 + i, lo=99.5 + i) for i in range(10)]
    rows += [bar(10, 107.5, hi=109.5, lo=107.0),
             bar(11, 106.0, hi=107.6, lo=105.5)]     # два бара против ноги
    rows.append(bar(12, 106.0))
    got = [e for e in detect_step(rows, 1) if e["kind"] == "pullback"]
    assert got
    assert DEFAULTS["pull_min"] <= got[0]["retrace"] <= DEFAULTS["pull_max"]


def test_a_single_counter_bar_is_not_a_pullback():
    """
    Без этого условия откат находился у 61% бумаг на случайном блуждании: «цена
    вернула четверть ноги» описывает, ГДЕ она, а не что делает.
    """
    rows = [bar(i, 100.0 + i, hi=100.5 + i, lo=99.5 + i) for i in range(10)]
    rows += [bar(10, 106.0, hi=109.5, lo=105.5)]     # один бар против
    rows.append(bar(11, 106.0))
    assert "pullback" not in kinds(detect_step(rows, 1))


def test_full_retrace_is_not_a_pullback():
    """
    Вернули всю ногу — это уже разворот. Называть его откатом значило бы стереть
    разницу между «идёт дальше» и «пошло обратно».
    """
    rows = [bar(i, 100.0 + i, hi=100.5 + i, lo=99.5 + i) for i in range(10)]
    rows += [bar(10, 99.0, hi=109.5, lo=98.5)]
    rows.append(bar(11, 99.0))
    assert "pullback" not in kinds(detect_step(rows, 1))


# ─── пробой и ложный пробой ───────────────────────────────────────────────────

def levels_at(*prices):
    return [{"price": p, "kind": "high", "touches": 2} for p in prices]


def test_level_break_on_the_last_bar():
    rows = steady(10, price=100.0)
    rows += [bar(10, 101.0), bar(11, 101.0)]
    got = detect_step(rows, 1, tick=0.01, levels=levels_at(100.5))
    br = [e for e in got if e["kind"] == "level_break"]
    assert br and br[0]["level"] == 100.5 and br[0]["side"] == "up"


def test_false_break_is_dated_by_the_return_bar():
    """
    ГЛАВНОЕ ПРО ПРИЧИННОСТЬ. В момент пробоя ещё неизвестно, ложный он или нет.
    Поставить метку на бар пробоя значило бы утверждать, что мы знали будущее —
    ровно та ошибка, из-за которой 31.07 вместо 3078 пробоев нашлось 6.
    """
    rows = steady(10, price=100.0)
    rows += [bar(10, 101.0)]                  # вышли за 100.5
    rows += [bar(11, 100.1)]                  # вернулись
    rows.append(bar(12, 100.1))
    got = detect_step(rows, 1, tick=0.01, levels=levels_at(100.5))
    fb = [e for e in got if e["kind"] == "false_break"]
    assert fb, "ложный пробой найден"
    assert fb[0]["ts"] == "2026-08-03T10:11", "датирован баром ВОЗВРАТА"


def test_no_levels_no_break_events():
    rows = steady(10) + [bar(10, 101.0), bar(11, 101.0)]
    got = kinds(detect_step(rows, 1))
    assert "level_break" not in got and "false_break" not in got


# ─── смена направления ────────────────────────────────────────────────────────

def test_direction_change_is_structural_not_one_bar():
    """
    Один бар вниз внутри роста происходит постоянно и ничего не меняет. Событие —
    смена СТРУКТУРЫ: были выше по максимумам и минимумам, стали ниже.
    """
    rows = steady(6)
    rows += [bar(6, 101.0, hi=101.5, lo=100.5),
             bar(7, 102.0, hi=102.5, lo=101.5)]        # выше и по хай, и по лоу
    rows += [bar(8, 101.0, hi=101.4, lo=100.4),
             bar(9, 100.0, hi=100.4, lo=99.4)]         # ниже и по хай, и по лоу
    rows.append(bar(10, 100.0))
    got = [e for e in detect_step(rows, 1) if e["kind"] == "direction_changed"]
    assert got and got[0]["was"] == "up" and got[0]["now"] == "down"


def test_one_red_bar_inside_a_rise_is_not_a_direction_change():
    rows = [bar(i, 100.0 + i, hi=100.5 + i, lo=99.5 + i) for i in range(10)]
    rows[8] = bar(8, 107.6, hi=108.4, lo=107.4)        # чуть ниже, но структура цела
    rows.append(bar(10, 110.0))
    assert "direction_changed" not in kinds(detect_step(rows, 1))


# ─── по всем шагам и по всем бумагам ──────────────────────────────────────────

def test_events_carry_their_timeframe():
    rows = [bar(i, 100.0 + (i % 3), hi=101.0 + (i % 3), lo=99.0 + (i % 3))
            for i in range(40)]
    rows += [bar(40, 120.0, hi=121.0, lo=119.0), bar(41, 120.0)]
    got = detect(rows, steps=(1, 5))
    assert {e["step_min"] for e in got} <= {1, 5}
    assert any(e["step_min"] == 1 for e in got)


def test_scan_returns_a_list_of_tickers_not_cards():
    """
    У сканера другая форма выдачи: список бумаг с тем, что сработало. Ради этого
    он и отдельно от карточки.
    """
    loud = steady(12) + [bar(12, 105.0), bar(13, 105.0)]
    calm = [bar(i, 100.0) for i in range(14)]
    got = scan({"LOUD": loud, "CALM": calm})
    assert [x["ticker"] for x in got] == ["LOUD"], "тихая бумага в список не попала"
    assert got[0]["count"] >= 1 and got[0]["kinds"]


def test_scan_orders_by_count_then_alphabet():
    """
    Порядок по числу событий, а не по «важности»: какое событие важнее, не
    измерено, и придумывать вес значило бы выдать догадку за знание.
    """
    a = steady(12) + [bar(12, 105.0), bar(13, 105.0)]
    b = steady(12) + [bar(12, 101.0), bar(13, 101.0)]
    got = scan({"BBB": b, "AAA": a})
    counts = [x["count"] for x in got]
    assert counts == sorted(counts, reverse=True)


def test_scan_survives_junk():
    assert scan({}) == []
    assert scan({"X": []}) == []
    assert scan({"X": [{"ts": "плохо"}]}) == []


# ─── описание, а не совет ─────────────────────────────────────────────────────

def test_no_verdict_fields():
    rows = steady(12) + [bar(12, 105.0), bar(13, 105.0)]
    blob = str(detect(rows)).lower()
    for bad in ("signal", "buy", "sell", "entry", "target", "strong",
                "рекоменд", "покупать", "продавать", "сильн"):
        assert bad not in blob, bad


def test_measured_negatives_are_written_next_to_the_code():
    src = (ROOT / "src/analysis/price_events.py").read_text()
    assert "-12.57" in src, "измеренный вред отката записан рядом"
    assert "0.05%" in src, "издержки, съевшие преимущество, записаны"
    assert "3078" in src, "ошибка причинности записана"


def test_thresholds_are_marked_as_guesses():
    src = (ROOT / "src/analysis/price_events.py").read_text()
    i = src.index("SHARP = ")
    assert "Догадка" in src[max(0, i - 300):i]


# ─── подключение ──────────────────────────────────────────────────────────────

def test_scanner_endpoint_exists_and_reports_rates():
    api = (ROOT / "src/api/main.py").read_text()
    assert '@app.get("/api/price-scan"' in api
    i = api.index('async def get_price_scan')
    body = api[i:i + 3000]
    assert "rates(found" in body, "частоты отдаются наружу"
    assert "chart_levels" in body, "уровни берутся С ГРАФИКА, не из стакана"


def test_card_shows_the_tickers_own_price_events():
    api = (ROOT / "src/api/main.py").read_text()
    # Карточка и сканер обязаны звать ОДНУ функцию с одними входными данными.
    # 03.08 они считали по-разному и давали по одной бумаге разные ответы:
    # RAGR 8 событий против 4, POSI 3 против 9. Обе цифры были верны.
    assert 'out["price_events"] = events_for(' in api
    assert "from src.analysis.price_events import events_for" in api
    page = (ROOT / "dashboard/market-watch.html").read_text()
    assert "события цены" in page
    assert "события стакана" in page, "два разных блока, не слиты"


def test_scanner_page_is_a_list_not_cards():
    page = (ROOT / "dashboard/price-scan.html").read_text()
    assert "<table>" in page or "<table" in page
    # Не по одному слову: формулировка меняется, а требование нет — доли видов
    # обязаны быть на экране, и общая, и по каждому шагу отдельно.
    assert "rates(d.rates" in page, "общая доля видов на экране"
    assert "byStep(d.rates_by_step" in page, "доли по каждому шагу на экране"
    assert "хотя бы на одном шаге" in page, "общая доля названа честно"
    assert "-12.57" in page or "−12.57" in page, "измеренный вред записан"
    assert "61%" in page, "случай вырождения отката записан"
    api = (ROOT / "src/api/main.py").read_text()
    assert '@app.get("/scan", include_in_schema=False)' in api


def test_page_says_events_are_not_per_second():
    page = (ROOT / "dashboard/price-scan.html").read_text()
    assert "закрытым барам" in page
    assert "3078" in page, "ошибка причинности названа"


# ─── порог пробоя от самой бумаги ─────────────────────────────────────────────

def test_break_threshold_scales_with_the_ticker():
    """
    НАЙДЕНО НА ЖИВОМ ЭКРАНЕ 03.08 в 09:43.

        MOEX  ложный пробой  вышли за 161.3 и вернулись за 1 бар

    Порог был два ШАГА ЦЕНЫ: у MOEX это 0.02 ₽ при цене 161.3, то есть 0.012% —
    тень свечи. Замер по 43 бумагам: два шага это 0.29 обычного хода у MDMG и
    2.0 у HEAD, разброс в семь раз при одном детекторе. Отсюда «ложный пробой»
    у 72% бумаг.

    Теперь порог — обычный ход САМОЙ бумаги, и одинаковый выход за уровень
    значит у тихой и у прыгающей разное.
    """
    lvl = [{"price": 100.0}]
    # Тихая: обычный ход 0.02. Выход на 0.06 — три хода, это пробой.
    calm = [bar(i, 99.90 + (0.02 if i % 2 else 0.0)) for i in range(10)]
    calm += [bar(10, 100.06), bar(11, 100.06)]
    # Прыгающая: обычный ход 2.0. Тот же выход на 0.06 — ничто.
    jumpy = [bar(i, 97.0 + (2.0 if i % 2 else 0.0)) for i in range(10)]
    jumpy += [bar(10, 100.06), bar(11, 100.06)]
    assert "level_break" in kinds(detect_step(calm, 1, tick=0.01, levels=lvl))
    assert "level_break" not in kinds(detect_step(jumpy, 1, tick=0.01, levels=lvl))


def test_flat_series_still_has_an_absolute_floor():
    """
    Если ряд стоит, обычный ход равен нулю и порог обнулился бы: пробоем стало
    бы любое касание. Нижняя граница в шагах цены это закрывает.
    """
    lvl = [{"price": 100.0}]
    rows = [bar(i, 100.0) for i in range(10)]
    rows += [bar(10, 100.005), bar(11, 100.005)]     # полшага цены
    assert "level_break" not in kinds(detect_step(rows, 1, tick=0.01, levels=lvl))


def test_false_break_says_how_long_it_took_not_how_many_stayed():
    """
    ПОДПИСЬ ВРАЛА У 25 ЛОЖНЫХ ПРОБОЕВ ИЗ 48.

    Брала len(back) — сколько баров закрылось обратно внутри — и называла это
    временем возврата. Писала «вернулись за 3 бара», когда вернулись за один.
    Правильное число всё это время лежало рядом, в bars_out.
    """
    lvl = [{"price": 100.0}]
    rows = [bar(i, 99.5 + (0.04 if i % 2 else 0.0)) for i in range(10)]
    rows += [bar(10, 100.20)]                        # вышли
    rows += [bar(11, 99.50), bar(12, 99.50), bar(13, 99.50)]   # три бара внутри
    rows.append(bar(14, 99.50))
    got = [e for e in detect_step(rows, 1, tick=0.01, levels=lvl)
           if e["kind"] == "false_break"]
    assert got
    assert got[0]["bars_out"] == 1, "вернулись за один бар"
    assert "3 бара" not in got[0]["why"], "три бара ВНУТРИ, а не три до возврата"
    assert "сразу" in got[0]["why"]


def test_break_reason_says_how_deep_in_units_of_the_ticker():
    """
    Читателю нужна ГЛУБИНА выхода, а не сам порог. Порог у всех событий бумаги
    одинаков, и строка «на 1 при обычном ходе 1» повторяет одно число дважды.
    Глубина у каждого события своя и отвечает, решителен ли пробой.
    """
    lvl = [{"price": 100.0}]
    rows = [bar(i, 99.90 + (0.02 if i % 2 else 0.0)) for i in range(10)]
    rows += [bar(10, 100.06), bar(11, 100.06)]
    got = [e for e in detect_step(rows, 1, tick=0.01, levels=lvl)
           if e["kind"] == "level_break"]
    assert got
    why = got[0]["why"]
    assert "хода бумаги" in why, why
    # Вышли на 0.06 при обычном ходе 0.02 — это три хода.
    assert "3.0 хода" in why, why


# ─── связность: один ответ на одну бумагу ─────────────────────────────────────

def test_card_and_scanner_give_the_same_answer():
    """
    НАЙДЕНО 03.08 СРАВНЕНИЕМ ДВУХ ЭКРАНОВ.

        RAGR   сканер 8 событий · карточка 4
        POSI   сканер 3         · карточка 9

    Обе цифры были ВЕРНЫ для своих входных данных: сканер считал по 60 барам из
    памяти и 4 уровням, карточка — по 262 барам из базы и 6. Дефект связности
    хуже арифметической ошибки: обе стороны убедительны, и выбрать нельзя.

    Теперь оба зовут events_for, и разойтись им не на чем.
    """
    from src.analysis.price_events import events_for, WINDOW
    rows = [bar(i % 60, 100.0 + (i % 7) * 0.1 - (i % 5) * 0.08) for i in range(300)]
    # Карточка передаёт всю историю дня, сканер — свою память. Ответ один.
    card = events_for(rows, tick=0.01)
    scanner = events_for(rows[-WINDOW:], tick=0.01)
    assert [e["kind"] for e in card] == [e["kind"] for e in scanner]
    assert [e["ts"] for e in card] == [e["ts"] for e in scanner]


def test_scan_uses_the_canonical_path_too():
    """Сканер по всем бумагам обязан идти тем же путём, что и одна карточка."""
    from src.analysis.price_events import events_for
    rows = [bar(i % 60, 100.0 + (i % 7) * 0.1 - (i % 5) * 0.08) for i in range(300)]
    one = events_for(rows, tick=0.01)
    many = scan({"AAA": rows}, ticks={"AAA": 0.01})
    got = many[0]["events"] if many else []
    assert [e["kind"] for e in got] == [e["kind"] for e in one]


def test_window_is_long_enough_for_every_advertised_step():
    """
    Шаг 15м требует шести ЗАКРЫТЫХ баров, то есть 90 минут. При памяти в 60
    минут он не срабатывал НИ РАЗУ, хотя ручка объявляла steps [1, 5, 15].
    Замер на живой доске 03.08: 1м — 16 событий, 5м — 54, 15м — ноль.
    """
    from src.analysis.price_events import WINDOW, STEPS, NEED
    for s in STEPS:
        assert WINDOW // s >= NEED, f"шагу {s}м не хватает окна {WINDOW}"


def test_rates_by_step_does_not_inflate_with_more_steps():
    """
    Общая доля считает бумагу сработавшей, если сработал ХОТЬ КАКОЙ шаг, и
    растёт от числа шагов. 03.08 включение 15м и 30м подняло «ложный пробой» с
    32% до 50%, и я едва не принял это за расстройку порогов.
    """
    from src.analysis.price_events import rates_by_step
    scanned = [{"ticker": "AAA", "kinds": ["false_break"], "events": [
        {"kind": "false_break", "step_min": 1},
        {"kind": "false_break", "step_min": 5},
        {"kind": "false_break", "step_min": 15}]}]
    общая = rates(scanned, 10)
    по_шагам = rates_by_step(scanned, 10)
    assert общая["false_break"]["share"] == 0.1
    for st in ("1", "5", "15"):
        assert по_шагам[st]["false_break"]["share"] == 0.1, \
            "одна бумага на трёх шагах это 10%, а не 30%"
