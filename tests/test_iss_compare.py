"""
Сверка минуток ISS с тем, что стрим записал сам.

Зачем отдельная сверка, если есть turnover_error: потому что turnover_error
НЕ ЛОВИТ ЛОТНОСТЬ и НЕ ЛОВИТ КЛЮЧ МИНУТЫ. Первый тест доказывает это
числом, чтобы никто (в том числе я) больше не называл её защитой
от ошибки в единицах.

Последний тест — тот самый случай с прода 05.08: обе стороны полны,
общих минут ноль.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.collector.iss_minutes import (  # noqa: E402
    MIN_COMMON,
    bars_of,
    compare_to_db,
    turnover_error,
)

COLS = ["begin", "end", "open", "close", "high", "low", "value", "volume"]
DAY = "2026-07-31"


def row(hh, mm, shares, close=100.0):
    """Строка ISS: value считается от ШТУК, как у биржи."""
    t = f"{DAY} {hh:02d}:{mm:02d}:00"
    return [t, t, close, close, close, close, shares * close, shares]


def payload(rows):
    return {"candles": {"columns": COLS, "data": rows}}


def iss_day(lot, minutes=60, shares=1000):
    rows = [row(10, i, shares) for i in range(minutes)]
    return bars_of(payload(rows), lot=lot)


def db_day(volume, minutes=60):
    """Строки стрима: ключ минуты БЕЗ СЕКУНД, как у msk_minute."""
    return [{"ts": f"{DAY}T10:{i:02d}", "volume": volume, "close": 100.0}
            for i in range(minutes)]


def test_turnover_check_is_blind_to_the_lot_size():
    """Главное признание: лот сокращается, сверка молчит при любом."""
    for lot in (1, 10, 1000):
        bars = iss_day(lot=lot)
        err = turnover_error(bars, lot=lot)
        assert err is not None and err < 1e-9, lot


def test_the_same_units_give_ratio_one():
    bars = iss_day(lot=10)          # 1000 штук / 10 = 100 лотов
    got = compare_to_db(bars, db_day(volume=100.0))
    assert got["ok"] is True
    assert abs(got["median_ratio"] - 1.0) < 1e-9
    assert "совпадают" in got["verdict"]


def test_shares_against_lots_show_up_as_the_lot_size():
    """Стрим в лотах, а мы бы записали штуки (лот взят за 1)."""
    bars = iss_day(lot=1)           # 1000 «лотов» — на самом деле штуки
    got = compare_to_db(bars, db_day(volume=100.0))
    assert got["ok"] is False
    assert abs(got["median_ratio"] - 10.0) < 1e-9
    assert "лотность" in got["verdict"]


def test_lots_against_shares_show_up_as_the_inverse():
    bars = iss_day(lot=1000)        # поделили на 1000, а стрим писал по 100
    got = compare_to_db(bars, db_day(volume=100.0))
    assert got["ok"] is False
    assert got["median_ratio"] < 0.05


def test_a_few_percent_apart_is_still_accepted():
    """Стрим мог пропустить часть сделок — это не ошибка единиц."""
    bars = iss_day(lot=10)
    got = compare_to_db(bars, db_day(volume=103.0))
    assert got["ok"] is True


def test_too_few_common_minutes_is_not_a_verdict():
    """Мало точек — ответ не «всё хорошо», а «не знаю»."""
    bars = iss_day(lot=10, minutes=MIN_COMMON - 1)
    got = compare_to_db(bars, db_day(volume=100.0, minutes=MIN_COMMON - 1))
    assert got["ok"] is None
    assert "мало общих минут" in got["verdict"]


def test_an_empty_database_day_is_not_a_pass():
    bars = iss_day(lot=10)
    got = compare_to_db(bars, [])
    assert got["ok"] is None
    assert got["common"] == 0
    assert got["db_minutes"] == 0


def test_one_wild_minute_does_not_decide_the_verdict():
    """Медиана, а не среднее: один аукционный выброс не ломает ответ."""
    bars = iss_day(lot=10)
    rows = db_day(volume=100.0)
    rows[0]["volume"] = 0.01       # стрим поднялся среди минуты
    got = compare_to_db(bars, rows)
    assert got["ok"] is True


def test_full_data_but_zero_overlap_names_the_key_mismatch():
    """
    Ровно то, что случилось на проде 05.08: 500 минут у ISS, 777 в базе,
    общих ноль. Ответ «мало точек» был бы формально верен и совершенно
    бесполезен — причина в формате ключа, и её надо назвать.
    """
    bars = iss_day(lot=10)
    stale = [{"ts": f"{DAY}T10:{i:02d}:00", "volume": 100.0} for i in range(60)]
    got = compare_to_db(bars, stale)
    assert got["ok"] is False
    assert got["iss_minutes"] == 60 and got["db_minutes"] == 60
    assert got["common"] == 0
    assert "РАЗНЫЙ КЛЮЧ" in got["verdict"]
