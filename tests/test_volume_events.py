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

    без вердиктов     31.07 одиночный RVOL измерялся ПЛОСКИМ на всех порогах
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
    rows = steady(10, vol=100) + [bar(10, 1000), bar(11, 1000)]
    small = detect_step(rows, 1, lot=1)
    big = detect_step(rows, 1, lot=1000)
    assert small and big
    assert big[0]["rub"] == small[0]["rub"] * 1000


def test_price_enters_the_turnover():
    """Тысяча лотов по 100 ₽ и по 5000 ₽ — разные деньги."""
    rows = steady(10, vol=100, close=100.0) + [bar(10, 1000, 100.0),
                                               bar(11, 1000, 100.0)]
    cheap = detect_step(rows, 1, lot=1)[0]["rub"]
    rows2 = steady(10, vol=100, close=5000.0) + [bar(10, 1000, 5000.0),
                                                 bar(11, 1000, 5000.0)]
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
    rows = [bar(i, 0) for i in range(6)] + [bar(i, 100) for i in range(6, 14)]
    rows.append(bar(14, 300))
    rows.append(bar(15, 300))
    got = detect_step(rows, 1, lot=1)
    assert got and got[0]["base_rub"] == round(100 * 100.0), "норма по ненулевым"


def test_baseline_excludes_the_measured_bar():
    """Бар, который меряем, в свою же норму входить не может."""
    rows = steady(12, vol=100) + [bar(12, 10000), bar(13, 10000)]
    got = detect_step(rows, 1, lot=1)
    assert got and got[0]["base_rub"] == round(100 * 100.0)
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

def day_rows(day, vol):
    return [bar(i, vol, day=day) for i in range(20)]


def test_time_of_day_profile_needs_enough_days():
    """
    ГЛАВНОЕ ПРО ЧЕСТНОСТЬ. 02.08 в базе было два дня, оба выходные, оба с
    дилерскими котировками. «Обычный объём 14:30» по ним — выдумка.
    """
    few = {f"2026-07-{20 + i:02d}": day_rows(f"2026-07-{20 + i:02d}", 100)
           for i in range(MIN_DAYS - 1)}
    assert day_profile(few, lot=1) == {}
    enough = {f"2026-07-{10 + i:02d}": day_rows(f"2026-07-{10 + i:02d}", 100)
              for i in range(MIN_DAYS)}
    assert day_profile(enough, lot=1), "дней хватило — норма построена"


def test_time_of_day_profile_is_used_when_present():
    prof = {600 + i: 10000.0 for i in range(20)}     # 10:00-10:19 по 10 тыс ₽
    rows = steady(12, vol=100, close=100.0)          # скользящая норма 10 тыс
    rows += [bar(12, 500, 100.0), bar(13, 500, 100.0)]
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
    small = steady(12, vol=100, close=90.0) + [bar(12, 5000, 90.0),
                                               bar(13, 5000, 90.0)]
    got = scan({"BIG": big, "SMALL": small}, lots={"BIG": 1, "SMALL": 1})
    assert [x["ticker"] for x in got][0] == "SMALL", "у SMALL кратность выше"
    assert got[0]["max_times"] > got[1]["max_times"]


def test_scan_skips_quiet_tickers():
    quiet = steady(14, vol=100)
    loud = steady(12, vol=100) + [bar(12, 1000), bar(13, 1000)]
    got = scan({"QUIET": quiet, "LOUD": loud}, lots={"QUIET": 1, "LOUD": 1})
    assert [x["ticker"] for x in got] == ["LOUD"]


def test_rates_report_base_frequency():
    loud = steady(12, vol=100) + [bar(12, 1000), bar(13, 1000)]
    got = scan({"A": loud, "B": steady(14, vol=100)}, lots={"A": 1, "B": 1})
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
    body = api[i:i + 3000]
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
