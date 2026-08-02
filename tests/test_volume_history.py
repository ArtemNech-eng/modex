"""
История объёма по таймфреймам и её связка с ценой.

Артём объяснил, зачем: «цена +0.4% → объём ×1.2» не особо интересно, а «цена
+0.4% → объём ×4.8 → новые максимумы» уже другое дело. Различает их не объём и не
цена по отдельности, а их СВЯЗКА.

Отдельно от `test_volume.py`: там старый RVOL (`technical.volume_stats`), который
сравнивает день с днями. Здесь минутные бары внутри дня.

Что защищают эти тесты:

    частичный бар   ГЛАВНОЕ. Пятиминутка на первой минуте набрала пятую часть
                    объёма. Сравнение её СУММЫ с суммами закрытых бар всегда
                    даёт «объём низкий» — просто потому, что бар не дожил. У
                    текущего бара считается ТЕМП на минуту, и сравнивается он с
                    темпом, а не с суммой.

    причинность     норма только по ПРОШЛЫМ закрытым барам, последний в неё не
                    входит. 31.07 сравнение с окном, включавшим текущий бар,
                    дало 6 событий вместо 3078

    медиана         одна минута выноса не должна поднимать норму так, что
                    следующая такая же перестанет быть заметной

    два разделения  volume_buy/sell от биржи это АГРЕССОР сделки; объём на
                    росте/падении это направление ЦЕНЫ. Разные вещи

    без советов     31.07 RVOL как фильтр измерялся ПЛОСКИМ на всех порогах.
                    Значит слов «сигнал» и «начало движения» тут быть не может
"""
import pathlib

import pytest

from src.analysis.volume_history import (volume_frame, profile, STEPS,
                                         BASE_NEED)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def bar(i, close, vol, hi=None, lo=None, op=None):
    """Минута номер i внутри часа 10 с заданным объёмом."""
    return {"ts": f"2026-08-03T10:{i:02d}",
            "high": hi if hi is not None else close * 1.001,
            "low": lo if lo is not None else close * 0.999,
            "close": close, "open": op if op is not None else close,
            "volume": vol}


def steady(n=31, price=100.0, vol=100):
    """Ровный ряд: цена и объём одинаковые."""
    return [bar(i, price, vol) for i in range(n)]


# ─── частичный объём текущего бара ────────────────────────────────────────────

def test_forming_bar_is_measured_by_pace_not_by_sum():
    """
    ГЛАВНЫЙ тест файла. Закрытые пятиминутки по 500 лотов, то есть 100 в минуту.
    Текущий бар живёт одну минуту и набрал 400 — это ЧЕТЫРЁХКРАТНЫЙ темп.

    Сравнение сумм сказало бы 400/500 = ×0.8, «объём низкий». Ровно наоборот.
    """
    rows = [bar(i, 100.0, 100) for i in range(30)]        # 6 бар по 500
    rows.append(bar(30, 100.0, 400))                      # текущий: 1 минута
    v = volume_frame(rows, 5)
    fm = v["forming"]
    assert fm["minutes"] == 1
    assert fm["volume_so_far"] == 400
    assert fm["pace_per_min"] == pytest.approx(400.0)
    assert fm["pace_mult"] == pytest.approx(4.0), \
        "четырёхкратный темп, а не ×0.8 по сумме"


def test_forming_sum_never_enters_the_multiple():
    """
    Кратность `volume_mult` относится к последнему ЗАКРЫТОМУ бару. Незакрытый в
    неё не попадает ни при каких условиях — иначе каждый бар в начале своей жизни
    выглядел бы тихим.
    """
    rows = [bar(i, 100.0, 100) for i in range(30)]
    rows.append(bar(30, 100.0, 1))                        # почти пустая минута
    v = volume_frame(rows, 5)
    assert v["volume_mult"] == pytest.approx(1.0), \
        "последний ЗАКРЫТЫЙ бар нормальный, тихая текущая минута его не портит"


def test_forming_pace_needs_history():
    rows = [bar(i, 100.0, 100) for i in range(7)]
    v = volume_frame(rows, 5)
    assert "pace_per_min" in v["forming"]
    assert "pace_mult" not in v["forming"], "нормы ещё нет — кратности нет"


# ─── норма и кратность ────────────────────────────────────────────────────────

def test_volume_multiple_of_the_last_closed_bar():
    """Ровные 100, потом бар на 480 в минуту — это ×4.8 из примера Артёма."""
    rows = [bar(i, 100.0, 100) for i in range(25)]         # 5 закрытых по 500
    rows += [bar(i, 100.0, 480) for i in range(25, 30)]    # шестой: 2400
    rows.append(bar(30, 100.0, 10))                        # текущий
    v = volume_frame(rows, 5)
    assert v["volume_baseline"] == 500
    assert v["volume_mult"] == pytest.approx(4.8)


def test_baseline_excludes_the_bar_being_measured():
    """
    Норма считается по ПРОШЛЫМ барам. Включить измеряемый бар в его же норму —
    та самая ошибка, из-за которой 31.07 вместо 3078 пробоев нашлось 6.
    """
    rows = [bar(i, 100.0, 100) for i in range(25)]
    rows += [bar(i, 100.0, 10000) for i in range(25, 30)]  # огромный последний
    rows.append(bar(30, 100.0, 10))
    v = volume_frame(rows, 5)
    assert v["volume_baseline"] == 500, "выброс в норму не попал"
    assert v["volume_mult"] > 50


def test_baseline_is_median_not_mean():
    """
    Одна минута выноса не должна поднять норму так, что следующая такая же
    перестанет быть заметной. Среднее по [500,500,500,500,10000] это 2400,
    медиана 500.
    """
    rows = [bar(i, 100.0, 100) for i in range(20)]         # 4 бара по 500
    rows += [bar(i, 100.0, 2000) for i in range(20, 25)]   # пятый: 10000
    rows += [bar(i, 100.0, 500) for i in range(25, 30)]    # шестой: 2500
    rows.append(bar(30, 100.0, 10))
    v = volume_frame(rows, 5)
    assert v["volume_baseline"] == 500, "медиана, а не среднее 2400"
    assert v["volume_mult"] == pytest.approx(5.0)


def test_no_baseline_without_enough_bars():
    rows = [bar(i, 100.0, 100) for i in range(BASE_NEED * 5 - 1)]
    v = volume_frame(rows, 5)
    assert "volume_mult" not in v
    assert "volume_baseline" not in v


def test_baseline_ignores_empty_bars():
    """
    Тихие минуты без сделок бывают. Нули в медиане утянули бы норму вниз, и любой
    обычный бар стал бы «в десять раз выше нормы».
    """
    rows = [bar(i, 100.0, 0) for i in range(10)]           # 2 пустых бара
    rows += [bar(i, 100.0, 100) for i in range(10, 35)]    # 5 по 500
    rows.append(bar(35, 100.0, 10))
    v = volume_frame(rows, 5)
    assert v["volume_baseline"] == 500
    assert v["base_bars_used"] == 4, "пустые бары в норму не вошли"


# ─── пропущенные минуты внутри ЗАКРЫТОГО бара ─────────────────────────────────

def test_closed_bar_with_missing_minutes_is_flagged():
    """
    Найдено на РЕАЛЬНЫХ данных 01.08: у MTLR закрытая пятиминутка 17:20 содержала
    ОДНУ минуту из пяти — 15 лотов против нормы 476, то есть ×0.03.

    Сумма по интервалу верна. Но пропуск минуты означает либо «сделок не было»,
    либо «сбор данных лежал», и это РАЗНЫЕ вещи. Staleness до 6 минут измерялся.
    Без пометки одно читается как другое.
    """
    rows = [bar(i, 100.0, 100) for i in range(25)]          # 5 полных бар
    rows.append(bar(27, 100.0, 15))                         # бар 25-29: 1 минута
    rows.append(bar(31, 100.0, 10))                         # текущий
    v = volume_frame(rows, 5)
    assert v["last_bar_minutes"] == 1
    assert v["last_bar_partial"] is True
    assert v["volume_mult"] == pytest.approx(0.03), "кратность честная, но с пометкой"


def test_full_closed_bar_is_not_flagged_partial():
    rows = [bar(i, 100.0, 100) for i in range(30)]
    rows.append(bar(30, 100.0, 10))
    v = volume_frame(rows, 5)
    assert v["last_bar_minutes"] == 5
    assert "last_bar_partial" not in v


# ─── концентрация объёма в одной минуте ───────────────────────────────────────

def test_one_big_minute_is_distinguishable_from_steady_interest():
    """
    Найдено на РЕАЛЬНЫХ данных: у SBER бар 17:20 это [102, 86, 2023, 17, 68] —
    «×5.66 за пять минут» на деле ОДНА минута.

    Ровный повышенный интерес и один блок дают одну и ту же кратность. Различает
    их только доля крупнейшей минуты.
    """
    rows = [bar(i, 100.0, 100) for i in range(25)]
    rows += [bar(25, 100.0, 102), bar(26, 100.0, 86), bar(27, 100.0, 2023),
             bar(28, 100.0, 17), bar(29, 100.0, 68)]
    rows.append(bar(30, 100.0, 10))
    v = volume_frame(rows, 5)
    assert v["top_minute_share"] > 0.85, "одна минута забрала почти всё"

    even = [bar(i, 100.0, 100) for i in range(25)]
    even += [bar(i, 100.0, 460) for i in range(25, 30)]
    even.append(bar(30, 100.0, 10))
    w = volume_frame(even, 5)
    assert w["volume_mult"] == pytest.approx(4.6)
    assert w["top_minute_share"] == pytest.approx(0.2), "ровно по минутам"


# ─── ускорение объёма ─────────────────────────────────────────────────────────

def test_volume_acceleration_against_previous_bar():
    """
    Кратность к норме и кратность к предыдущему бару — разные вещи. Объём может
    быть выше нормы и при этом уже затухать, и одно число это скрывает.
    """
    rows = [bar(i, 100.0, 100) for i in range(20)]
    rows += [bar(i, 100.0, 400) for i in range(20, 25)]    # 2000
    rows += [bar(i, 100.0, 200) for i in range(25, 30)]    # 1000, вдвое меньше
    rows.append(bar(30, 100.0, 10))
    v = volume_frame(rows, 5)
    assert v["volume_prev"] == 2000
    assert v["volume_accel"] == pytest.approx(0.5), "выше нормы, но уже затухает"
    assert v["volume_mult"] > 1, "и при этом всё ещё выше нормы"


def test_no_acceleration_with_one_closed_bar():
    rows = [bar(i, 100.0, 100) for i in range(7)]
    assert "volume_accel" not in volume_frame(rows, 5)


# ─── объём на росте и на падении ──────────────────────────────────────────────

def test_volume_split_by_price_direction():
    """
    Куда шла ЦЕНА в барах с этим объёмом. Растущие минуты по 300 лотов,
    падающие по 100 — рост забирает три четверти объёма.
    """
    rows = []
    for i in range(30):
        if i % 2 == 0:
            rows.append(bar(i, 101.0, 300, op=100.0))      # закрылась выше
        else:
            rows.append(bar(i, 100.0, 100, op=101.0))      # закрылась ниже
    rows.append(bar(30, 100.0, 10))
    v = volume_frame(rows, 1)
    assert v["vol_up"] > v["vol_down"]
    assert v["vol_up_share"] == pytest.approx(0.75, abs=0.02)


def test_flat_bars_go_to_neither_side():
    """Бар, закрывшийся ровно там же, не приписывается ни росту, ни падению."""
    rows = [bar(i, 100.0, 100, op=100.0) for i in range(30)]
    rows.append(bar(30, 100.0, 10))
    v = volume_frame(rows, 1)
    assert v["vol_up"] == 0 and v["vol_down"] == 0
    assert v["vol_up_share"] == 0


def test_bar_direction_is_not_the_exchange_aggressor_split():
    """
    Два РАЗНЫХ разделения, и путать их нельзя. volume_buy/volume_sell приходит от
    биржи и означает агрессора сделки. Наш расчёт — направление бара. Минута
    может быть на 90% покупок по агрессору и закрыться ниже, если продавцы
    держали лимитами.

    Здесь проверяется, что мы не притворяемся вторым: имён биржевого разделения в
    нашей выдаче нет, а разница описана рядом с кодом.
    """
    rows = [bar(i, 101.0, 100, op=100.0) for i in range(30)]
    rows.append(bar(30, 101.0, 10))
    v = volume_frame(rows, 1)
    assert "volume_buy" not in v and "volume_sell" not in v
    src = (ROOT / "src/analysis/volume_history.py").read_text()
    assert "АГРЕССОР" in src, "разница между двумя разделениями записана в коде"


# ─── новые максимумы ──────────────────────────────────────────────────────────

def test_new_high_is_flagged():
    """
    «Новые максимумы» из примера. Считается по ЗАКРЫТЫМ барам окна, НЕ включая
    измеряемый: иначе он всегда был бы максимумом самого себя.
    """
    rows = [bar(i, 100.0, 100, hi=100.1, lo=99.9) for i in range(25)]
    rows += [bar(i, 105.0, 100, hi=105.1, lo=104.9) for i in range(25, 30)]
    rows.append(bar(30, 105.0, 10))
    v = volume_frame(rows, 5)
    assert v["at_new_high"] is True
    assert v["at_new_low"] is False


def test_no_new_high_inside_a_range():
    v = volume_frame(steady(31), 5)
    assert v["at_new_high"] is False and v["at_new_low"] is False


def test_new_low_is_flagged_too():
    rows = [bar(i, 100.0, 100, hi=100.1, lo=99.9) for i in range(25)]
    rows += [bar(i, 95.0, 100, hi=95.1, lo=94.9) for i in range(25, 30)]
    rows.append(bar(30, 95.0, 10))
    assert volume_frame(rows, 5)["at_new_low"] is True


# ─── связка цены и объёма ─────────────────────────────────────────────────────

def rising_on_volume():
    """Двадцать пять ровных минут, потом рост на четырёхкратном объёме."""
    rows = [bar(i, 100.0, 100, hi=100.1, lo=99.9) for i in range(25)]
    rows += [bar(25 + i, 100.4 + i * 0.01, 480,
                 hi=100.5 + i * 0.01, lo=100.3 + i * 0.01) for i in range(5)]
    rows.append(bar(30, 100.45, 10))
    return rows


def test_price_and_volume_are_reported_together():
    """
    То, ради чего всё. «+0.4% при ×1.2» и «+0.4% при ×4.8 с новым максимумом» —
    разные картины, и различает их только связка в одном месте.
    """
    f = profile(rising_on_volume(), steps=(5,))["frames"]["5m"]
    assert f["volume_mult"] == pytest.approx(4.8)
    assert f["price_change_pct"] is not None
    assert f["price_direction"] in ("up", "down", "flat")


def test_readable_line_carries_both_numbers():
    line = profile(rising_on_volume())["line_5m"]
    assert "объём ×" in line
    assert "цена" in line
    assert "новый максимум" in line


def test_boring_case_reads_as_boring():
    """
    Ровный объём даёт ×1 — ровно тот случай, который Артём назвал «не особо
    интересно». Он должен выглядеть скучно, а не отсутствовать.
    """
    assert profile(steady(31))["frames"]["5m"]["volume_mult"] == pytest.approx(1.0)


def test_all_steps_present():
    p = profile(steady(80))
    assert set(p["frames"]) == {f"{s}m" for s in STEPS}
    assert STEPS == (1, 3, 5, 15)


def test_empty_and_broken_input():
    assert profile([])["frames"]["5m"]["bars"] == 0
    profile([{"ts": "мусор"},
             {"ts": "2026-08-03T10:00", "volume": "нечисло",
              "high": 100, "low": 99, "close": 99.5}])


def test_non_numeric_volume_counts_as_zero():
    rows = [bar(i, 100.0, 100) for i in range(30)]
    rows[3]["volume"] = None
    rows[4]["volume"] = "х"
    rows.append(bar(30, 100.0, 10))
    assert volume_frame(rows, 5)["volume_baseline"] > 0


# ─── описание, а не совет ─────────────────────────────────────────────────────

def test_no_signal_fields():
    """
    31.07 RVOL как самостоятельный фильтр измерялся ПЛОСКИМ на всех порогах.
    Связку цены с объёмом никто не мерил. Значит утверждать нечего.
    """
    rows = [bar(i, 100.0 + i * 0.1, 100 * (1 + i)) for i in range(40)]
    blob = str(profile(rows)).lower()
    for bad in ("signal", "recommend", "buy", "sell", "entry", "target",
                "strong", "strength", "начало движения"):
        assert bad not in blob, bad


def test_measured_flatness_of_rvol_is_written_next_to_the_code():
    src = (ROOT / "src/analysis/volume_history.py").read_text()
    assert "ПЛОСКИМ" in src, "измеренный результат RVOL записан рядом с кодом"
    assert "ЧАСТИЧНЫЙ ОБЪЁМ" in src, "ловушка незакрытого бара описана"


# ─── подключение к карточке и экрану ──────────────────────────────────────────

def test_volume_profile_is_wired_into_the_card():
    api = (ROOT / "src/api/main.py").read_text()
    assert "vol_profile(rows)" in api


def test_page_shows_the_pairing():
    page = (ROOT / "dashboard/market-watch.html").read_text()
    assert "объём" in page.lower()
    assert "volume_mult" in page
