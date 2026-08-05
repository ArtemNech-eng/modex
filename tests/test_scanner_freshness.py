"""
Сканер объёма не должен отдавать событий, которых сейчас нет.

Два разных вида неправды проверяются здесь.

СТАРЫЙ БАР. Сканер сравнивал последний ЗАКРЫТЫЙ бар с нормой, не спрашивая,
когда тот закрылся. Поток встал в 14:10 — в 16:00 на экране всё ещё «оборот в
10 раз выше нормы». Метку времени рядом читает человек; поля берут дашборд,
сортировка и агент.

РАЗОРВАННЫЙ РЯД. «Три бара подряд растут» бралось срезом списка, а срез ничего
не знает о времени: между элементами может лежать пропущенная тихая минута или
ночь между вечерней и утренней сессией.

Тесты держат «сейчас» в руках (параметр `at`), иначе они зависели бы от часов
машины и зеленели бы через раз.
"""
import datetime as dt

from src.analysis import volume_events as VE

BASE = "2026-08-05T14:00"          # среда, основная сессия
QUIET = 600                        # лотов в минуту: 600 × 100 ₽ = 60 000 ₽
PRICE = 100.0


def _minute(base: str, i: int) -> str:
    t = dt.datetime.strptime(base, "%Y-%m-%dT%H:%M") + dt.timedelta(minutes=i)
    return t.strftime("%Y-%m-%dT%H:%M")


def _rows(vols, base: str = BASE):
    return [{"ts": _minute(base, i), "open": PRICE, "high": PRICE,
             "low": PRICE, "close": PRICE, "volume": v}
            for i, v in enumerate(vols)]


#  Восемь тихих минут, всплеск, и ещё одна минута хвостом: последний бар ряда
#  считается незакрытым и в расчёт не идёт, поэтому всплеск должен быть
#  предпоследним.
SURGE_VOLS = [QUIET] * 8 + [QUIET * 10, QUIET]
SURGE_ROWS = _rows(SURGE_VOLS)
FRESH = _minute(BASE, 9)           # «сейчас» — минута после всплеска


def test_fresh_bar_still_gives_an_event():
    """Защита не должна глушить нормальный случай."""
    evs = VE.detect_step(SURGE_ROWS, 1, lot=1, at=FRESH)
    assert [e["kind"] for e in evs] == ["volume_surge"]


def test_a_two_hour_old_bar_is_not_an_event():
    """Тот же ряд, но смотрим на него через два часа."""
    late = _minute(BASE, 9 + 120)
    assert VE.detect_step(SURGE_ROWS, 1, lot=1, at=late) == []


def test_the_age_is_the_reason_and_not_something_else():
    """
    Если поднять порог возраста, событие возвращается.

    Иначе тест выше проходил бы и по любой другой причине — например, если бы
    ряд перестал давать событие вовсе.
    """
    late = _minute(BASE, 9 + 120)
    evs = VE.detect_step(SURGE_ROWS, 1, lot=1, at=late, p={"max_age": 10000})
    assert [e["kind"] for e in evs] == ["volume_surge"]


def test_the_event_carries_its_age():
    evs = VE.detect_step(SURGE_ROWS, 1, lot=1, at=FRESH)
    assert evs[0]["age_min"] == 1.0


def test_a_bar_marked_one_minute_ahead_is_not_dropped():
    """
    Разметка бара минутой вперёд встречается в проде и будущим не является.

    05.08 в 22:29 живой скан отдавал событие с меткой 22:30. Если считать такой
    бар негодным, сканер замолчит на ровном месте.
    """
    early = _minute(BASE, 7)
    evs = VE.detect_step(SURGE_ROWS, 1, lot=1, at=early)
    assert [e["kind"] for e in evs] == ["volume_surge"]


#  Разгон: шесть тихих минут, затем три растущих бара подряд.
GROW_VOLS = [QUIET] * 6 + [1500, 2500, 4200, QUIET]


def test_a_real_run_up_is_still_caught():
    rows = _rows(GROW_VOLS)
    kinds = {e["kind"] for e in VE.detect_step(rows, 1, lot=1,
                                               at=_minute(BASE, 9))}
    assert "volume_accelerating" in kinds


def test_a_run_up_with_a_hole_in_it_is_not_a_run_up():
    """
    Та же тройка баров, но одна минута между ними пропущена.

    Всплеск при этом обязан остаться: пропуск минуты не отменяет того, что
    оборот высок — он отменяет только слово «подряд».
    """
    rows = [r for r in _rows(GROW_VOLS) if not r["ts"].endswith(":07")]
    kinds = {e["kind"] for e in VE.detect_step(rows, 1, lot=1,
                                               at=_minute(BASE, 9))}
    assert "volume_accelerating" not in kinds
    assert "volume_surge" in kinds


def test_evening_and_next_morning_are_not_consecutive():
    """Ряд 23:47 → 23:49 → 06:52 как срез выглядит непрерывным."""
    night = [{"ts": "2026-08-04T23:47"}, {"ts": "2026-08-04T23:49"},
             {"ts": "2026-08-05T06:52"}]
    assert VE._contiguous(night, 1) is False


def test_minute_by_minute_is_contiguous():
    row = [{"ts": _minute(BASE, i)} for i in range(4)]
    assert VE._contiguous(row, 1) is True


def test_silence_from_staleness_is_counted():
    """
    Пустая таблица обязана отличаться от таблицы с устаревшими данными.

    Тот же урок, что дали below_floor и warming_up.
    """
    minutes = {"SBER": SURGE_ROWS, "GAZP": SURGE_ROWS}
    assert VE.stale(minutes, at=FRESH) == 0
    assert VE.stale(minutes, at=_minute(BASE, 200)) == 2


def test_scan_passes_the_clock_down():
    """Иначе защита стояла бы в ядре и не работала бы через API."""
    minutes = {"SBER": SURGE_ROWS}
    assert VE.scan(minutes, at=FRESH)
    assert VE.scan(minutes, at=_minute(BASE, 200)) == []


def test_without_a_clock_it_uses_moscow_time():
    """
    Контейнер живёт по UTC, а минутные ключи московские.

    Если взять UTC-часы, каждый бар состарится ровно на три часа и сканер
    замолчит целиком — молча и повсеместно.
    """
    now = VE._now_msk()
    assert (now - dt.datetime.utcnow()).total_seconds() > 3 * 3600 - 5
