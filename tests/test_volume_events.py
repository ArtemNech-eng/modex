"""
Сканер объёма: пришли ли деньги, и в какой момент они пошли.

Артём поставил задачу так: «обычно за минуту проходит 5 млн ₽, сейчас 18 млн ₽»
и отдельно — момент, когда объём НАЧАЛ расти: 4 → 6 → 11 → 19. Второе
интереснее: «сегодня большой объём» это состояние, а «объём пошёл четыре минуты
назад» — событие со временем.

Что защищают эти тесты:

    рубли             лот у SBER 1, у UGLD 1000, у GAZP 10. Список по лотам
                      сравнивал бы несравнимое

    частичный бар     незакрытый отбрасывается: пятиминутка на первой минуте
                      набрала пятую часть и всегда выглядит тихой

    норма без нулей   минуты без сделок утянули бы медиану вниз и объявили
                      всплеском любую обычную минуту

    две нормы         скользящая ПОЛЗЁТ вместе с растущим объёмом; норма по
                      времени суток этого не делает, но её пока нет

    мало дней         норма по времени НЕ строится на двух выходных. 02.08 в
                      базе было ровно столько, и обе — дилерские котировки

    ускорение         подряд растущие бары И значимость. Ряд 1 → 1.4 → 2 → 2.8
                      формально ускоряется втрое, но это ничто

    порядок           по КРАТНОСТИ, а не по рублям: миллиард у SBER это обычный
                      день, сто миллионов у DATA — событие

    пол по обороту    минута тише позиции Артёма это минута, где он был бы всей
                      ликвидностью. 02.08 на экране: EUTR ×99.7 при 39 469 ₽

    без вердиктов     31.07 одиночный RVOL измерялся ПЛОСКИМ на всех порогах

Объёмы в фикстурах — ПРАВДОПОДОБНЫЕ ДЕНЬГИ, а не круглые сотни: минута в
10 тыс ₽ описывает рынок, которого для него нет. Замер по 50 бумагам
наблюдения: средняя минута p10 182 тыс ₽, медиана 914 тыс ₽.
"""
import pathlib

import pytest

from src.analysis.volume_events import (detect, detect_step, scan, rates,
                                        day_profile, DEFAULTS, MIN_DAYS, NEED)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def bar(i, vol, close=100.0, day="2026-08-03"):
    return {"ts": f"{day}T10:{i:02d}", "open": close, "high": close,
            "low": close, "close": close, "volume": vol}


def steady(n=10, vol=100, close=100.0):
    return [bar(i, vol, close) for i in range(n)]


def kinds(evs):
    return sorted(e["kind"] for e in evs)


# ─── рубли, а не лоты ─────────────────────────────────────────────────────────

def test_turnover_is_in_rubles_with_lot_size():
    """
    Лот у SBER 1, у UGLD 1000. Сто лотов у одной и у другой — разные деньги, и
    список по лотам сравнивал бы несравнимое.
    """
    rows = steady(10, vol=1000) + [bar(10, 10000), bar(11, 10000)]
    small = detect_step(rows, 1, lot=1)
    big = detect_step(rows, 1, lot=1000)
    assert small and big
    assert big[0]["rub"] == small[0]["rub"] * 1000


def test_price_enters_the_turnover():
    """Тысяча лотов по 100 ₽ и по 5000 ₽ — разные деньги."""
    rows = steady(10, vol=1000, close=100.0) + [bar(10, 10000, 100.0),
                                                bar(11, 10000, 100.0)]
    cheap = detect_step(rows, 1, lot=1)[0]["rub"]
    rows2 = steady(10, vol=1000, close=5000.0) + [bar(10, 10000, 5000.0),
                                                  bar(11, 10000, 5000.0)]
    rich = detect_step(rows2, 1, lot=1)[0]["rub"]
    assert rich == cheap * 50


# ─── только закрытые бары ─────────────────────────────────────────────────────

def test_forming_bar_is_ignored():
    """
    У незакрытого бара объём ЧАСТИЧНЫЙ. Сравнение с нормой всегда даёт «низкий»
    просто потому, что бар не дожил.
    """
    rows = steady(10, vol=100)
    quiet = detect_step(rows, 1, lot=1)
    rows.append(bar(10, 99999))                 # выброс в ТЕКУЩЕМ баре
    assert detect_step(rows, 1, lot=1) == quiet


def test_too_little_history():
    assert detect_step(steady(NEED - 1), 1, lot=1) == []


# ─── норма ────────────────────────────────────────────────────────────────────

def test_baseline_skips_empty_minutes():
    """
    Минуты без сделок утянули бы медиану вниз, и любая обычная минута стала бы
    всплеском — та же ловушка, что дала 30 «крупных» сделок из 30.
    """
    rows = [bar(i, 0) for i in range(6)] + [bar(i, 1000) for i in range(6, 14)]
    rows.append(bar(14, 3000))
    rows.append(bar(15, 3000))
    got = detect_step(rows, 1, lot=1)
    assert got and got[0]["base_rub"] == round(1000 * 100.0), "норма по ненулевым"


def test_baseline_excludes_the_measured_bar():
    """Бар, который меряем, в свою же норму входить не может."""
    # НОРМА ЗДЕСЬ ДОЛЖНА БЫТЬ НАСТОЯЩЕЙ. Раньше было 100 лотов × 100 ₽ =
    # 10 000 ₽ — ниже порога шума (20 000 ₽), то есть тест проверял своё
    # утверждение на бумаге, которой почти не торгуют. Проверяется
    # исключение измеряемого бара из своей же нормы, а не поведение
    # на шуме — для шума есть отдельные тесты.
    rows = steady(12, vol=1000) + [bar(12, 100000), bar(13, 100000)]
    got = detect_step(rows, 1, lot=1)
    assert got and got[0]["base_rub"] == round(1000 * 100.0)
    assert got[0]["base_thin"] is False, "норма 100 тыс ₽ — это не шум"
    assert got[0]["times"] >= 50


def test_no_baseline_no_events():
    rows = [bar(i, 0) for i in range(12)]
    assert detect_step(rows, 1, lot=1) == []


# ─── всплеск ──────────────────────────────────────────────────────────────────

def test_surge_is_the_case_from_the_request():
    """
    «Обычно 5 млн, сейчас 18 млн» — это ×3.6, выше порога.
    """
    rows = steady(12, vol=50000, close=100.0)      # 5 млн ₽ за минуту
    rows += [bar(12, 180000), bar(13, 180000)]     # 18 млн ₽
    got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
    assert got
    assert got[0]["times"] == pytest.approx(3.6, abs=0.05)
    assert got[0]["base_source"] == "скользящая"


def test_ordinary_volume_is_not_a_surge():
    rows = steady(12, vol=100) + [bar(12, 150), bar(13, 150)]
    assert "volume_surge" not in kinds(detect_step(rows, 1, lot=1))


# ─── ускорение: момент, когда объём пошёл ─────────────────────────────────────

def test_acceleration_matches_the_example():
    """
    Ровно ряд Артёма: 4 → 6 → 11 → 19. Шаги ×1.5, ×1.8, ×1.7 — три подряд.
    """
    rows = steady(10, vol=4000, close=1000.0)          # 4 млн ₽
    rows += [bar(10, 6000, 1000.0), bar(11, 11000, 1000.0),
             bar(12, 19000, 1000.0)]
    rows.append(bar(13, 19000, 1000.0))
    got = [e for e in detect_step(rows, 1, lot=1)
           if e["kind"] == "volume_accelerating"]
    assert got
    assert got[0]["bars_growing"] == DEFAULTS["grow_bars"]
    assert len(got[0]["series"]) == DEFAULTS["grow_bars"] + 1


def test_growth_from_nothing_is_not_acceleration():
    """
    Ряд может расти втрое и всё равно оставаться НИЖЕ обычного: бумага просто
    просыпается после пустых минут. Это не «деньги пошли», а возврат к норме.

    Здесь норма 1000, а ряд 200 → 300 → 450 → 675 — три роста подряд, и в конце
    всё ещё две трети от обычного.
    """
    rows = steady(10, vol=1000)
    rows += [bar(10, 200), bar(11, 300), bar(12, 450), bar(13, 675)]
    rows.append(bar(14, 675))
    got = kinds(detect_step(rows, 1, lot=1))
    assert "volume_accelerating" not in got, "рост есть, значимости нет"


def test_one_spike_is_not_acceleration():
    """
    Один выброс — это всплеск, а не разгон. Момент, когда объём ПОШЁЛ, требует
    последовательности.
    """
    rows = steady(12, vol=100) + [bar(12, 10000), bar(13, 10000)]
    got = kinds(detect_step(rows, 1, lot=1))
    assert "volume_surge" in got
    assert "volume_accelerating" not in got


def test_growth_must_be_uninterrupted():
    rows = steady(10, vol=1000)
    rows += [bar(10, 3000), bar(11, 2000), bar(12, 9000)]   # провал в середине
    rows.append(bar(13, 9000))
    assert "volume_accelerating" not in kinds(detect_step(rows, 1, lot=1))


# ─── норма по времени суток ───────────────────────────────────────────────────

def weekdays(n, start="2026-08-03"):
    """
    N БУДНЕЙ подряд, а не N календарных дат.

    Первая версия этих тестов брала range(10) подряд идущих чисел месяца и
    падала: внутри две субботы с воскресеньями, будней остаётся семь. Код был
    прав, тест считал дни неправильно — четвёртый раз за проект.
    """
    import datetime as dt
    d = dt.datetime.strptime(start, "%Y-%m-%d")
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += dt.timedelta(days=1)
    return out


def day_rows(day, vol, minutes=540):
    """
    Правдоподобный торговый день, а не двадцать баров. Основная сессия это
    примерно 540 минут; день из двадцати баров не описывает форму суток, и
    считать его днём наравне с полным нельзя.
    """
    out = []
    for k in range(minutes):
        h, m = 10 + k // 60, k % 60
        out.append({"ts": f"{day}T{h:02d}:{m:02d}", "open": 100.0, "high": 100.0,
                    "low": 100.0, "close": 100.0, "volume": vol})
    return out


def test_time_of_day_profile_needs_enough_days():
    """
    ГЛАВНОЕ ПРО ЧЕСТНОСТЬ. 02.08 в базе было два дня, оба выходные, оба с
    дилерскими котировками. «Обычный объём 14:30» по ним — выдумка.
    """
    few = {d: day_rows(d, 100) for d in weekdays(MIN_DAYS - 1)}
    assert day_profile(few, lot=1) == {}
    enough = {d: day_rows(d, 100) for d in weekdays(MIN_DAYS)}
    assert day_profile(enough, lot=1), "дней хватило — норма построена"


def test_time_of_day_profile_is_used_when_present():
    prof = {600 + i: 100000.0 for i in range(20)}    # 10:00-10:19 по 100 тыс ₽
    rows = steady(12, vol=1000, close=100.0)         # скользящая норма 100 тыс
    rows += [bar(12, 5000, 100.0), bar(13, 5000, 100.0)]
    got = detect_step(rows, 1, lot=1, profile=prof)
    assert got and got[0]["base_source"] == "время суток"


def test_source_of_the_baseline_is_always_reported():
    """
    Читатель обязан знать, с чем сравнили. Две нормы отвечают на разные вопросы,
    и молча подставлять одну вместо другой нельзя.
    """
    rows = steady(12, vol=100) + [bar(12, 1000), bar(13, 1000)]
    for e in detect_step(rows, 1, lot=1):
        assert e["base_source"] in ("скользящая", "время суток")


def test_five_minute_step_sums_the_minutes_of_the_profile():
    """
    У пятиминутки норма — сумма её минут, а не норма одной. Иначе она всегда
    выглядела бы впятеро выше нормы.
    """
    prof = {m: 1000.0 for m in range(600, 640)}
    rows = [bar(i, 10, 100.0) for i in range(40)]
    rows.append(bar(40, 10, 100.0))
    got = detect_step(rows, 5, lot=1, profile=prof)
    assert not got, "обычный объём не должен стать всплеском из-за шага"


# ─── сканер по всем бумагам ───────────────────────────────────────────────────

def test_scan_orders_by_multiple_not_by_rubles():
    """
    Миллиард у SBER — обычный день, сто миллионов у DATA — событие. Сортировка
    по рублям превратила бы список в рейтинг ликвидности, который и так известен.
    """
    big = steady(12, vol=100000, close=300.0) + [bar(12, 400000, 300.0),
                                                 bar(13, 400000, 300.0)]
    # У SMALL теперь норма 90 тыс ₽, а не 9 тыс: проверяется порядок
    # сортировки, а не поведение на шуме — иначе тест держал бы сразу
    # две разные вещи и сломался бы от любой из них.
    small = steady(12, vol=1000, close=90.0) + [bar(12, 10000, 90.0),
                                                bar(13, 10000, 90.0)]
    got = scan({"BIG": big, "SMALL": small}, lots={"BIG": 1, "SMALL": 1})
    assert [x["ticker"] for x in got][0] == "SMALL", "у SMALL кратность выше"
    assert got[0]["max_times"] > got[1]["max_times"]


def test_scan_skips_quiet_tickers():
    quiet = steady(14, vol=1000)
    loud = steady(12, vol=1000) + [bar(12, 10000), bar(13, 10000)]
    got = scan({"QUIET": quiet, "LOUD": loud}, lots={"QUIET": 1, "LOUD": 1})
    assert [x["ticker"] for x in got] == ["LOUD"]


def test_rates_report_base_frequency():
    loud = steady(12, vol=1000) + [bar(12, 10000), bar(13, 10000)]
    got = scan({"A": loud, "B": steady(14, vol=1000)}, lots={"A": 1, "B": 1})
    r = rates(got, 2)
    assert r["volume_surge"]["tickers"] == 1
    assert r["volume_surge"]["share"] == pytest.approx(0.5)


def test_scan_survives_junk():
    assert scan({}) == []
    assert scan({"X": []}) == []
    assert scan({"X": [{"ts": "плохо"}]}) == []


# ─── описание, а не совет ─────────────────────────────────────────────────────

def test_no_verdict_fields():
    rows = steady(12, vol=100) + [bar(12, 10000), bar(13, 10000)]
    blob = str(detect(rows, lot=1)).lower()
    for bad in ("signal", "buy", "sell", "entry", "деньги пришли",
                "начало движения", "покупать", "сильн"):
        assert bad not in blob, bad


def test_measured_flatness_of_rvol_is_written_next_to_the_code():
    src = (ROOT / "src/analysis/volume_events.py").read_text()
    assert "ПЛОСКИМ" in src, "измеренный результат RVOL записан рядом"
    assert "две выходных" in src or "оба выходные" in src, \
        "почему нормы по времени пока нет — записано"


def test_thresholds_are_marked_as_guesses():
    src = (ROOT / "src/analysis/volume_events.py").read_text()
    i = src.index("SURGE = ")
    assert "Догадка" in src[max(0, i - 300):i]


def test_rubles_are_documented_as_an_approximation():
    src = (ROOT / "src/analysis/volume_events.py").read_text()
    assert "ПРИБЛИЖЕНИЕ" in src, "оборот по закрытию, а не по цене сделок"


# ─── подключение ──────────────────────────────────────────────────────────────

def test_scanner_endpoint_exists():
    api = (ROOT / "src/api/main.py").read_text()
    assert '@app.get("/api/volume-scan"' in api
    i = api.index("async def get_volume_scan")
    # До СЛЕДУЮЩЕГО маршрута, а не первые 3000 символов. Счётчик символов
    # ломается от любой добавленной строки: тело выросло, и тест объявил
    # поломкой собственное окно, а не код.
    j = api.find("\n@app.", i)
    body = api[i:j if j > 0 else len(api)]
    assert "profiles_ready" in body, "видно, готова ли норма по времени"
    assert "vol_profiles" in body


def test_profile_is_built_in_background_from_past_days_only():
    """
    Сегодняшний день в норму входить не может: минута сравнивалась бы сама с
    собой. И считается это раз в час, а не на каждый запрос — 80 бумаг × 30 дней
    из базы на каждое обращение к сканеру было бы разорительно.
    """
    m = (ROOT / "main.py").read_text()
    assert "_volume_profiles" in m
    i = m.index("async def _volume_profiles")
    body = m[i:i + 2500]
    assert "range(1, 31)" in body, "дни берутся со вчерашнего"
    assert "if d == today" in body, "сегодня исключён явно"
    assert "3600" in body, "раз в час"


def test_page_has_a_second_table_and_names_the_baseline():
    page = (ROOT / "dashboard/price-scan.html").read_text()
    assert "Объём — пришли ли деньги" in page
    assert "всплеск оборота" in page and "оборот разгоняется" in page
    assert "норма скользящая" in page, "источник нормы назван на экране"
    assert "выдумать норму" in page, "почему нормы по времени нет — на экране"


# ─── пол по обороту ───────────────────────────────────────────────────────────

def test_huge_multiple_on_tiny_turnover_is_not_an_event():
    """
    НАЙДЕНО НА ЖИВОМ ЭКРАНЕ 02.08, не тестом.

    EUTR стоял первым на доске с «оборот в 99.7 раза выше нормы». За этой
    строкой было 39 469 ₽ при норме 396 ₽. Относительный тест отработал
    безупречно и выдал бессмыслицу: у Артёма позиция 150–250 тыс ₽, он был бы
    всей этой минутой целиком.

    Та же яма, что дала 30 «крупных» сделок из 30. `_baseline` выбрасывает бары
    с НУЛЁМ и ничего не может против бара в 396 ₽.
    """
    rows = [bar(i, 4, close=100.0) for i in range(12)]      # норма 400 ₽
    rows += [bar(12, 395), bar(13, 395)]                    # 39 500 ₽, ×98.75
    got = detect_step(rows, 1, lot=1)
    assert got == [], "кратность огромна, денег нет"


def test_the_exact_acceleration_series_from_the_screen():
    """Разгон 44 → 352 → 3388 → 39777 ₽ начинался с сорока четырёх рублей."""
    rows = [bar(i, 4, close=100.0) for i in range(10)]
    for i, v in enumerate((0.44, 3.52, 33.88, 397.77)):
        rows.append(bar(10 + i, v))
    rows.append(bar(14, 397.77))
    assert "volume_accelerating" not in kinds(detect_step(rows, 1, lot=1))


def test_floor_scales_with_step_length():
    """
    У пятиминутки оборот вчетверо больше по построению. Плоский порог сделал бы
    её вчетверо снисходительнее — и события расползлись бы по длинным шагам.
    """
    small = {"floor": 1000.0}
    rows = [bar(i, 1, close=100.0) for i in range(20)]      # 100 ₽ в минуту
    rows += [bar(20 + i, 12, close=100.0) for i in range(6)]  # 1200 ₽ в минуту
    rows.append(bar(26, 12, close=100.0))
    assert detect_step(rows, 1, lot=1, p=small), "минутка проходит пол 1000"
    assert detect_step(rows, 5, lot=1, p=small) == [], "пятиминутке нужно 5000"


def test_real_money_still_passes():
    """Пол режет мёртвые минуты, а не бумаги: SBER на всплеске проходит."""
    rows = [bar(i, 40000, close=300.0) for i in range(12)]   # 12 млн ₽ в минуту
    rows += [bar(12, 160000, close=300.0), bar(13, 160000, close=300.0)]
    got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
    assert got and got[0]["times"] == pytest.approx(4.0, abs=0.05)


def test_floor_is_configurable_without_deploy():
    """Порог — догадка по размеру позиции. Артём меняет его сам, через Coolify."""
    import src.analysis.volume_events as m
    assert "VOLUME_FLOOR_RUB" in pathlib.Path(m.__file__).read_text()
    rows = [bar(i, 4, close=100.0) for i in range(12)] + [bar(12, 395), bar(13, 395)]
    assert detect_step(rows, 1, lot=1, p={"floor": 1000.0}), "с низким полом видно"


def test_suppressed_count_is_reported():
    """
    Пустая таблица должна читаться как «всё выброшено полом», а не как «на рынке
    спокойно». Молчание без причины — та же ошибка, что «0 событий» на пустой
    памяти после деплоя.
    """
    from src.analysis.volume_events import below_floor
    quiet = {"EUTR": [bar(i, 4, close=100.0) for i in range(8)]}
    loud = {"SBER": [bar(i, 40000, close=300.0) for i in range(8)]}
    assert below_floor(quiet) == 1
    assert below_floor(loud) == 0
    assert below_floor({**quiet, **loud}) == 1


# ─── какие дни считаются днями ────────────────────────────────────────────────

def test_weekend_days_do_not_count():
    """
    МОЯ СОБСТВЕННАЯ ОШИБКА, НАЙДЕННАЯ УТРОМ 03.08.

    В docstring day_profile я написал, что по двум выходным норму строить
    нельзя, — и оставил их считаться днями. Через восемь торговых дней порог
    min_days набрался бы вместе с ними, и я бы этого уже не увидел.

    У выходной сессии другие часы (02:00-23:49 против 06:59), другой объём и
    нет горбов открытия и закрытия. Медиана каждой минуты просела бы, и обычная
    минута понедельника стала бы «всплеском».
    """
    # 01 и 02 августа 2026 — суббота и воскресенье.
    week = {"2026-08-01": day_rows("2026-08-01", 100),
            "2026-08-02": day_rows("2026-08-02", 100)}
    budni = {d: day_rows(d, 100) for d in weekdays(MIN_DAYS)}
    assert day_profile(week, lot=1) == {}, "две выходных это ноль дней"
    assert day_profile({**week, **budni}, lot=1), "будней хватило"
    # И выходные не должны влиять на саму норму.
    only = day_profile(budni, lot=1)
    both = day_profile({**week, **budni}, lot=1)
    assert only == both, "выходные не попали в медиану"


def test_short_days_do_not_count():
    """
    День, в который поток стоял и легло десять баров вместо шестисот, не
    описывает форму суток — но len() считает его наравне с полным.
    """
    from src.analysis.volume_events import MIN_BARS_DAY
    full = {d: day_rows(d, 100) for d in weekdays(MIN_DAYS - 1)}
    stub = {"2026-09-01": day_rows("2026-09-01", 100, minutes=MIN_BARS_DAY - 1)}
    assert day_profile({**full, **stub}, lot=1) == {}, "огрызок это не день"


def test_unparseable_day_key_is_dropped_not_crashed():
    days = {d: day_rows(d, 100) for d in weekdays(MIN_DAYS)}
    days["сегодня"] = day_rows("2026-08-04", 100)
    assert day_profile(days, lot=1), "мусорный ключ не роняет расчёт"


# ─── тонкая норма: событие настоящее, число нет ───────────────────────────────

def test_thin_baseline_is_marked_and_not_called_a_multiple():
    """
    НАЙДЕНО НА ЖИВОМ ЭКРАНЕ 03.08 в 09:22.

        ETLN  ×255.0   оборот 893 625 ₽   норма 3 505 ₽

    Бар прошёл пол — деньги настоящие, событие настоящее. Но число бессмысленно:
    знаменатель шум. Пол задаёт, какие деньги считаются деньгами; к знаменателю
    он обязан применяться так же, как к числителю.
    """
    rows = [bar(i, 35, close=100.0) for i in range(12)]      # норма 3 500 ₽
    rows += [bar(12, 8936, close=100.0), bar(13, 8936, close=100.0)]
    got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
    assert got
    e = got[0]
    assert e["base_thin"] is True
    assert "раза выше нормы" not in e["why"], "кратность считать не по чему"
    assert "после тишины" in e["why"]
    assert "894 тыс" in e["why"], "числа в тексте читаемые"


def test_normal_baseline_keeps_the_multiple():
    rows = [bar(i, 4000, close=100.0) for i in range(12)]    # норма 400 тыс ₽
    rows += [bar(12, 20000, close=100.0), bar(13, 20000, close=100.0)]
    got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
    assert got and got[0]["base_thin"] is False
    assert "раза выше нормы" in got[0]["why"]


# ─── норма считается по СВОЕЙ сессии ──────────────────────────────────────────

def sbar(hh, mm, vol, close=100.0):
    return {"ts": f"2026-08-03T{hh:02d}:{mm:02d}", "open": close, "high": close,
            "low": close, "close": close, "volume": vol}


def test_morning_bars_are_not_a_baseline_for_the_main_session():
    """
    НАЙДЕНО НА ОТКРЫТИИ ОСНОВНОЙ СЕССИИ 03.08.

    Утренняя сессия и основная — разные рынки. Замер: основная тяжелее утренней
    в 2.5 раза по медиане, у VTBR в 11.8. В 10:05 последние 20 баров это почти
    целиком утро, и каждая бумага «всплескивает» просто потому, что открылась
    основная сессия.

        время   сессии смешаны   только своя   нормы ещё нет
        10:01        7                0            14
        10:05        8                0            16
        10:12        1                1             0

    Ровно те минуты, когда на доску смотрят.
    """
    # Утро: тонкое. 20 баров по 300 тыс ₽.
    rows = [sbar(9, 20 + i, 3000) for i in range(20)]
    # Основная открылась: 6 баров по 900 тыс ₽ — втрое тяжелее утра, но для
    # основной сессии это обычная минута.
    rows += [sbar(10, i, 9000) for i in range(6)]
    rows.append(sbar(10, 6, 9000))
    got = kinds(detect_step(rows, 1, lot=1))
    assert "volume_surge" not in got, "утро не норма для основной сессии"


def test_not_enough_own_session_bars_means_no_event():
    """Пока своих баров мало, честнее промолчать, чем сравнить с чужим рынком."""
    rows = [sbar(9, 20 + i, 3000) for i in range(20)]
    rows += [sbar(10, 0, 90000), sbar(10, 1, 90000)]      # открылись мощно
    assert detect_step(rows, 1, lot=1) == [], "своей сессии ещё нет"


def test_surge_inside_one_session_still_fires():
    """Отбор по сессии не должен глушить настоящий всплеск внутри сессии."""
    rows = [sbar(10, i, 3000) for i in range(12)]         # 300 тыс ₽
    rows += [sbar(10, 12, 15000), sbar(10, 13, 15000)]    # 1.5 млн ₽, ×5
    got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
    assert got and got[0]["times"] == pytest.approx(5.0, abs=0.1)


def test_warming_up_is_counted():
    """
    Пустая таблица в 10:01 обязана читаться как «сессия только открылась», а не
    как «необычного оборота нет».
    """
    from src.analysis.volume_events import warming_up
    fresh = [sbar(9, 20 + i, 3000) for i in range(20)] + [sbar(10, 0, 9000),
                                                          sbar(10, 1, 9000)]
    settled = [sbar(10, i, 3000) for i in range(14)]
    assert warming_up({"A": fresh}) == 1
    assert warming_up({"B": settled}) == 0
    assert warming_up({"A": fresh, "B": settled}) == 1


# ─── порог «норму не из чего считать» ─────────────────────────────────────────

def test_thin_threshold_is_not_the_floor():
    """
    НАЙДЕНО ПЕРЕПРОВЕРКОЙ НА БИРЖЕВЫХ ДАННЫХ, по просьбе Артёма.

    Первая версия брала для «тонкой нормы» сам пол, помноженный на длину шага, и
    смешала два разных вопроса. Замер за день по всем 80 бумагам, 558 событий с
    меткой:

        медиана «тонкой» нормы бара     109 384 ₽
        из них настоящий шум (< 10 тыс)      4%
        на шаге 5м медиана нормы        455 412 ₽

    Полмиллиона рублей — не шум. Кратность скрывалась в 96% случаев, где она
    осмысленна.

    «Норму не из чего считать» — про число сделок в знаменателе. «Норма мала для
    позиции» — про ликвидность, и это закрывает пол на ЧИСЛИТЕЛЕ.
    """
    from src.analysis.volume_events import THIN_RUB, FLOOR_RUB
    assert THIN_RUB < FLOOR_RUB, "порог шума ниже пола позиции"
    src = pathlib.Path("src/analysis/volume_events.py").read_text()
    assert "109 384" in src, "замер записан рядом с порогом"


def test_thin_does_not_scale_with_step():
    """
    Кратность считается бар к бару, значит шум мерится в знаменателе БАРА.
    Пятиминутка вбирает впятеро больше сделок и от шума ДАЛЬШЕ, а не ближе, —
    умножать порог на шаг было бы наоборот.
    """
    # Норма 100 тыс ₽ на баре: для минутки и для пятиминутки одинаково не шум.
    rows1 = [bar(i, 1000, close=100.0) for i in range(12)]
    rows1 += [bar(12, 20000, close=100.0), bar(13, 20000, close=100.0)]
    got1 = detect_step(rows1, 1, lot=1)
    assert got1 and got1[0]["base_thin"] is False, "100 тыс на минутном баре — не шум"
    rows5 = [sbar(10, i, 1000, close=100.0) for i in range(60)]
    for i in range(60, 70):
        rows5.append(sbar(11, i - 60, 20000, close=100.0))
    got5 = [e for e in detect_step(rows5, 5, lot=1) if e["kind"] == "volume_surge"]
    if got5:
        assert got5[0]["base_thin"] is False, "и на пятиминутке тоже"


def test_absurd_multiple_is_still_caught():
    """
    Ради чего метка и существует. Замер за день: помеченные события имеют
    медианную кратность ×42.7 и доходят до ×1510 при норме 856 ₽; непомеченные —
    медиану ×5.6. Порог отделяет абсурд, а не малые числа.
    """
    # Норма 800 ₽ на бар, оборот 1.3 млн — ровно случай VSEH ×1510.
    rows = [bar(i, 8, close=100.0) for i in range(12)]
    rows += [bar(12, 13000, close=100.0), bar(13, 13000, close=100.0)]
    got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
    assert got, "оборот прошёл пол"
    assert got[0]["base_thin"] is True
    assert "times" not in got[0], "кратности к шуму не существует"
    assert got[0]["times_vs_thin_base"] > 100, "но абсурд виден под другим именем"
    assert "раза выше нормы" not in got[0]["why"]


def test_real_awakening_keeps_its_multiple():
    """
    LENT ×236.2 при норме 33 828 ₽ и обороте 7.99 млн ₽ — настоящее пробуждение
    бумаги, и кратность здесь информативна. Прежний порог её скрывал.
    """
    rows = [bar(i, 340, close=100.0) for i in range(12)]        # норма 34 тыс ₽
    rows += [bar(12, 80000, close=100.0), bar(13, 80000, close=100.0)]
    got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
    assert got and got[0]["base_thin"] is False, "34 тыс — не шум"
    assert "раза выше нормы" in got[0]["why"]


def test_thin_base_has_no_multiple_field_at_all():
    """
    04.08 на живом экране: ASTR times:30.0 при base_rub:11223, RASP
    times:28.77 при base_rub:8945. Текст честно говорил «кратность считать
    не по чему», а поле рядом говорило обратное. Побеждает поле: его
    читают программы.
    """
    rows = [bar(i, 35, close=100.0) for i in range(12)]       # норма 3 500 ₽
    rows += [bar(12, 8936, close=100.0), bar(13, 8936, close=100.0)]
    got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
    assert got
    e = got[0]
    assert e["base_thin"] is True
    assert "times" not in e, "поля кратности нет вовсе"
    assert e["times_vs_thin_base"] > 200, "число сохранилось под другим именем"
    assert e["rub"] > 0 and e["base_rub"] > 0, "сами деньги на месте"


def test_accelerating_on_thin_base_also_loses_the_multiple():
    """Разгон из ничего — такая же неправда, что и всплеск из ничего."""
    rows = [bar(i, 35, close=100.0) for i in range(10)]       # норма 3 500 ₽
    rows += [bar(10, 3000, 100.0), bar(11, 5000, 100.0), bar(12, 9000, 100.0)]
    rows.append(bar(13, 9000, 100.0))
    got = [e for e in detect_step(rows, 1, lot=1)
           if e["kind"] == "volume_accelerating"]
    if got:
        assert "times" not in got[0]
        assert got[0]["times_vs_thin_base"] > 0


def test_thin_ticker_is_not_ranked_above_real_money():
    """
    ГЛАВНОЕ ПОСЛЕДСТВИЕ. Сортировка шла по times, и верх доски занимал
    шум: ASTR ×30 на 11 тыс ₽ стоял выше всего настоящего.
    """
    real = steady(12, vol=1000, close=100.0)                 # норма 100 тыс ₽
    real += [bar(12, 5000, 100.0), bar(13, 5000, 100.0)]     # 500 тыс ₽, ×5
    thin = [bar(i, 35, close=100.0) for i in range(12)]      # норма 3 500 ₽
    thin += [bar(12, 8936, close=100.0), bar(13, 8936, close=100.0)]
    got = scan({"REAL": real, "THIN": thin}, lots={"REAL": 1, "THIN": 1})
    assert [x["ticker"] for x in got][0] == "REAL", "шум не возглавляет доску"
    row = [x for x in got if x["ticker"] == "THIN"][0]
    assert row["max_times"] is None, "кратности нет и у строки целиком"
    assert row["no_multiple"] is True, "и это названо явно, а не нулём"
    assert row["events"], "само событие осталось: деньги пришли"


def test_normal_money_keeps_the_multiple_field():
    real = steady(12, vol=1000, close=100.0)
    real += [bar(12, 5000, 100.0), bar(13, 5000, 100.0)]
    got = scan({"REAL": real}, lots={"REAL": 1})
    assert got[0]["max_times"] >= 3
    assert got[0]["no_multiple"] is False


from src.analysis.volume_events import (profile_gap, profile_note,  # noqa: E402
                                        MIN_BARS_DAY)


def test_profile_gap_agrees_with_the_real_filter_at_the_boundary():
    """
    ГЛАВНОЕ. Диагностика, расходящаяся с настоящим фильтром, хуже
    её отсутствия: она скажет «готово» при пустом профиле.
    Проверяется совпадение РОВНО на границе.
    """
    days = weekdays(MIN_DAYS)
    rows = {d: day_rows(d, 100) for d in days}
    g = profile_gap(rows)
    assert g["usable_days"] == MIN_DAYS
    assert g["ready"] is True and g["missing_days"] == 0
    assert day_profile(rows, lot=1), "профиль строится ровно тогда же"

    fewer = {d: day_rows(d, 100) for d in days[:-1]}
    g2 = profile_gap(fewer)
    assert g2["ready"] is False and g2["missing_days"] == 1
    assert not day_profile(fewer, lot=1), "и не строится ровно тогда же"


def test_profile_gap_names_weekends_and_short_days():
    """Именно это случилось в проде: стрим поднялся в субботу."""
    rows = {"2026-08-03": day_rows("2026-08-03", 100),            # понедельник
            "2026-08-04": day_rows("2026-08-04", 100),            # вторник
            "2026-08-05": day_rows("2026-08-05", 100),            # среда
            "2026-08-01": day_rows("2026-08-01", 100),            # суббота
            "2026-08-02": day_rows("2026-08-02", 100),            # воскресенье
            "2026-07-31": day_rows("2026-07-31", 100, minutes=50)}   # короткий
    g = profile_gap(rows)
    assert g["days_in_db"] == 6
    assert g["weekend_days"] == 2, "выходные названы отдельно"
    assert g["short_days"] == 1, "короткий день назван отдельно"
    assert g["usable_days"] == 3
    assert g["missing_days"] == MIN_DAYS - 3
    assert g["min_bars_day"] == MIN_BARS_DAY, "порог виден тому, кто читает"
    assert not day_profile(rows, lot=1)


def test_profile_gap_says_when_there_is_nothing_at_all():
    g = profile_gap({})
    assert g["days_in_db"] == 0 and g["usable_days"] == 0
    assert g["ready"] is False and g["missing_days"] == MIN_DAYS


def test_unparseable_day_is_not_counted_as_a_trading_day():
    g = profile_gap({"не-дата": day_rows("2026-08-03", 100)})
    assert g["usable_days"] == 0 and g["empty_days"] == 1


def test_the_note_carries_the_numbers_not_just_words():
    """«Дней пока мало» звучит одинаково и при поломке строителя."""
    rows = {"2026-08-03": day_rows("2026-08-03", 100),
            "2026-08-01": day_rows("2026-08-01", 100)}
    note = profile_note([profile_gap(rows)])
    assert "1 торговых дней из %d" % MIN_DAYS in note
    assert "выходных 1" in note
    assert str(MIN_BARS_DAY) in note, "порог дня назван"


def test_the_note_says_when_there_is_nothing_at_all():
    assert "нет вовсе" in profile_note([])


def test_the_builder_asks_for_the_reason_and_stays_short():
    """
    Соседний тест читает РОВНО 2500 символов после начала функции.
    04.08 два прогона упали именно потому, что добавленные строки
    вытолкнули sleep(3600) за это окно и тест ОСЛЕП, а не нашёл баг.
    """
    m = (ROOT / "main.py").read_text(encoding="utf-8")
    i = m.index("async def _volume_profiles")
    body = m[i:i + 2500]
    assert "profile_gap" in body, "строитель обязан спрашивать причину"
    assert "profile_note" in body, "и класть её в лог числами"
    assert "3600" in body, "и оставаться коротким: раз в час видно в окне"
