"""
Сканер цены: возраст события и непрерывность ряда.

Часы здесь ПЕРЕДАЮТСЯ параметром `at`, а не берутся из системы. Правило,
зависящее от времени, нельзя проверять по времени запуска: такой тест зелен
утром и красен вечером, и его краснота ничего не сообщает.

Три проверяемых утверждения:

    событие не переживает бар, который описывает
    «подряд» — это про время, а не про соседство в списке
    вчерашний бар остаётся вчерашним и не исчезает из середины истории
"""
from datetime import datetime, timedelta

from src.analysis.price_events import (DEFAULTS, _age_min, _last_run,
                                       _now_msk, detect_step, stale)
from src.analysis.timeframes import bars

DAY = "2026-08-06"
PREV = "2026-08-05"


def _rows(closes, day=DAY, start_min=600):
    """Минутные бары от start_min (минута суток) по возрастанию времени."""
    out = []
    for i, c in enumerate(closes):
        mm = start_min + i
        out.append({"ts": f"{day}T{mm // 60:02d}:{mm % 60:02d}",
                    "open": c, "high": c + 0.01, "low": c - 0.01,
                    "close": c, "volume": 100})
    return out


#  Восемь спокойных ходов по 0.1 и один в 1.4 — обычный ход бумаги 0.1, значит
#  последний в четырнадцать раз больше. Последняя строка — формирующийся бар:
#  он отбрасывается, и последним закрытым остаётся резкий.
SHARP = [100, 100.1, 100.0, 100.1, 100.0, 100.1, 100.0, 100.1, 101.5, 101.5]
SHARP_LAST_CLOSED_END = datetime(2026, 8, 6, 10, 9)   # 10:08 + одна минута


def _kinds(evs):
    return {e["kind"] for e in evs}


def test_fresh_bar_still_gives_an_event():
    evs = detect_step(_rows(SHARP), 1, at=SHARP_LAST_CLOSED_END)
    assert "sharp_up" in _kinds(evs), "свежий резкий ход обязан находиться"


def test_a_two_hour_old_bar_is_not_an_event():
    evs = detect_step(_rows(SHARP), 1,
                      at=SHARP_LAST_CLOSED_END + timedelta(hours=2))
    assert evs == [], "двухчасовой бар — это не «сейчас»"


def test_the_event_carries_its_age():
    evs = detect_step(_rows(SHARP), 1,
                      at=SHARP_LAST_CLOSED_END + timedelta(minutes=1))
    assert evs, "минутный возраст в пределах порога"
    for e in evs:
        assert "age_min" in e, "событие без возраста — непроверяемое заявление"
    assert any(abs(e["age_min"] - 1.0) < 0.01 for e in evs)


def test_a_bar_marked_one_minute_ahead_is_not_dropped():
    """Метки биржи бывают впереди часов контейнера. Это не повод молчать."""
    evs = detect_step(_rows(SHARP), 1,
                      at=SHARP_LAST_CLOSED_END - timedelta(minutes=1))
    assert "sharp_up" in _kinds(evs)


def test_the_age_is_measured_from_the_close_of_the_bar():
    """
    У бара метка ПЕРВОЙ минуты. Тридцатиминутный бар, закрывшийся минуту назад,
    по метке выглядел бы получасовой стариной.
    """
    at = datetime(2026, 8, 6, 10, 31)
    assert _age_min("2026-08-06T10:00", 30, at) == 1.0
    assert _age_min("2026-08-06T10:00", 1, at) == 30.0


def test_the_night_gap_is_not_a_price_move():
    """
    Вечер вчера по 100, утро сегодня по 110. Разрыв в списке — соседний, во
    времени — семь часов. Прежний код читал его как ход в сто обычных.
    """
    rows = (_rows([100, 100.01, 100.0, 100.01, 100.0, 100.01, 100.0, 100.01],
                  day=PREV, start_min=1420)
            + _rows([110, 110.01, 110.0, 110.01, 110.0, 110.01, 110.0,
                     110.01, 110.0, 110.01], day=DAY, start_min=600))
    evs = detect_step(rows, 1, at=datetime(2026, 8, 6, 10, 9))
    assert "sharp_up" not in _kinds(evs), "ночь — не резкое ускорение"
    assert "sharp_down" not in _kinds(evs)


def test_yesterdays_bar_is_not_called_forming():
    """
    Ключ считался из минуты суток без даты: вчерашние 10:00 и сегодняшние 10:00
    были одним ключом, и вчерашний бар выбрасывался из середины истории.
    """
    rows = (_rows([100, 100, 100, 100, 100, 100], day=PREV, start_min=600)
            + _rows([110, 110, 110, 110, 110, 110], day=DAY, start_min=600))
    bs = bars(rows, 30)
    assert len(bs) == 2, "два дня — два бара, а не один"
    assert bs[0]["complete"] is True, "вчерашний бар давно закрыт"
    assert bs[-1]["complete"] is False, "сегодняшний ещё набирается"


def test_two_days_do_not_melt_into_one_bar():
    rows = (_rows([100, 101], day=PREV, start_min=600)
            + _rows([200, 201], day=DAY, start_min=600))
    bs = bars(rows, 30)
    assert [b["close"] for b in bs] == [101, 201]


def test_a_long_hole_breaks_the_run():
    closed = [{"key": k, "ts": "x"} for k in (1, 2, 3, 30, 31)]
    assert _last_run(closed, 1, 15) == closed[-2:]


def test_quiet_minutes_do_not_break_the_run():
    """Тихие минуты неликвида — не разрыв сессии. Иначе история исчезнет."""
    closed = [{"key": k, "ts": "x"} for k in (1, 2, 3, 9, 10)]
    assert _last_run(closed, 1, 15) == closed


def test_silence_from_staleness_is_counted():
    minutes = {"OLD": _rows([100, 100, 100], start_min=600),
               "NEW": _rows([100, 100, 100], start_min=720)}
    assert stale(minutes, at=datetime(2026, 8, 6, 12, 3)) == 1


def test_without_a_clock_it_uses_moscow_time():
    """Ключи баров московские, контейнер живёт по UTC. Разница ровно три часа."""
    from datetime import timezone
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((_now_msk() - utc).total_seconds() - 3 * 3600) < 5


def test_the_gate_is_a_number_and_not_a_guess_in_the_code():
    assert DEFAULTS["max_age"] >= 1
    assert DEFAULTS["max_hole"] >= 1
