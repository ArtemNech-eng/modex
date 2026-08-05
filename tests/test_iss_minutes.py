"""
Минутки с ISS. Главное здесь — ЕДИНИЦЫ и КЛЮЧ МИНУТЫ.

Ошибка на лотность не падает и не логируется: она тихо завышает норму в
лотность раз, и всякая живая минута начинает выглядеть тишью.

Ошибка в ключе минуты ещё тише: запись проходит успешно, просто в базе
появляется вторая сетка минут рядом со стримовой. На живой базе 05.08:
500 минут у ISS, 777 в базе, общих ноль.
"""
from datetime import datetime, timezone

from src.collector import iss_minutes as I
from src.collector.stream import msk_minute
from src.analysis.volume_events import (_rub, _minute_of_day, day_profile,
                                        profile_gap, MIN_DAYS, MIN_BARS_DAY)

COLS = ["begin", "end", "open", "close", "high", "low", "value", "volume"]

#  Ровно десять будних дней подряд: 20–24 и 27–31 июля 2026.
WEEKDAYS = ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23",
            "2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29",
            "2026-07-30", "2026-07-31"]


def row(day="2026-08-03", hh=10, mm=0, shares=1000, close=100.0, value=None):
    v = shares * close if value is None else value
    return [f"{day} {hh:02d}:{mm:02d}:00", f"{day} {hh:02d}:{mm:02d}:59",
            close, close, close, close, v, shares]


def payload(rows):
    return {"candles": {"columns": COLS, "data": rows}}


def day_rows_iss(day, minutes=240, shares=1000, close=100.0):
    out = []
    for i in range(minutes):
        out.append(row(day=day, hh=10 + (i // 60), mm=i % 60,
                       shares=shares, close=close))
    return out


def test_url_asks_for_minutes_and_carries_no_token():
    u = I.candles_url("SBER", "2026-08-03")
    assert "interval=1" in u, "минутный шаг, а не дневной"
    assert "iss.meta=off" in u
    assert "from=2026-08-03" in u and "till=2026-08-03" in u
    assert "TQBR/securities/SBER/candles.json" in u
    assert "token" not in u.lower(), "ISS отдаёт историю без ключей"


def test_broken_answers_give_nothing_instead_of_crashing():
    assert I.rows_of({}) == [] and I.rows_of(None) == []
    assert I.bars_of({"candles": {"columns": COLS, "data": None}}) == []
    short = {"candles": {"columns": COLS, "data": [[1, 2, 3]]}}
    assert I.bars_of(short) == [], "строка не той длины — не угадываем"
    assert I.bar_of({"begin": "мусор"}) is None
    assert I.bar_of(None) is None


def test_timestamp_is_exactly_what_the_stream_itself_produces():
    """
    Сверка с ФУНКЦИЕЙ стрима, а не с литералом рядом.

    Предыдущая версия этого теста требовала "2026-08-03T10:05:00" и была
    зелёной всё то время, пока формат расходился со стримом. Тест, сверяющий
    код с копией его же ответа, не проверяет ничего.
    """
    b = I.bar_of(dict(zip(COLS, row(hh=10, mm=5))))
    #  ISS отдаёт МОСКОВСКОЕ время, стрим сдвигает UTC на три часа
    same_moment = datetime(2026, 8, 3, 7, 5, tzinfo=timezone.utc)
    assert b["ts"] == msk_minute(same_moment)
    assert b["ts"] == "2026-08-03T10:05", "без секунд, как в базе"
    assert len(b["ts"]) == I.TS_LEN
    assert _minute_of_day(b["ts"]) == 605, "профиль читает минуту из этого ключа"


def test_every_backfilled_bar_matches_the_stream_key_format():
    """Не одна минута, а весь день: ключ должен совпадать везде."""
    bars = I.bars_of(payload(day_rows_iss("2026-07-30", minutes=120)))
    assert len(bars) == 120
    for b in bars:
        assert len(b["ts"]) == I.TS_LEN and b["ts"][10] == "T"
        assert b["ts"].count(":") == 1, "секунды в ключе — это вторая сетка минут"


def test_shares_become_lots_and_rubles_match_the_source():
    """ГЛАВНОЕ: после перевода _rub даёт те же рубли, что и ISS."""
    lot = 10
    src = row(shares=1000, close=100.0)          # 1000 штук × 100 ₽ = 100 000 ₽
    b = I.bar_of(dict(zip(COLS, src)), lot=lot)
    assert b["volume"] == 100.0, "1000 штук при лоте 10 это 100 лотов"
    assert b["shares"] == 1000.0 and b["value_rub"] == 100000.0
    assert _rub(b, lot) == 100000.0
    assert I.turnover_error([b], lot) < 1e-9


def test_raw_shares_would_overstate_turnover_by_the_lot_size():
    """
    Та самая ловушка, написанная числом: если положить штуки туда,
    где ждут лоты, оборот вырастет в лотность раз.

    Но заметить это можно ТОЛЬКО так: когда при пересчёте и при сверке
    взяты РАЗНЫЕ лоты. Если лот один и тот же — он сокращается, и сверка
    молчит при любой лотности (см. tests/test_iss_compare.py).
    """
    lot = 10
    raw = {"ts": "2026-08-03T10:00", "close": 100.0,
           "volume": 1000.0,                     # ШТУКИ вместо лотов
           "value_rub": 100000.0}
    assert _rub(raw, lot) == 1000000.0
    err = I.turnover_error([raw], lot)
    assert round(err, 6) == float(lot - 1), "расхождение ровно в лот минус один"


def test_lot_one_changes_nothing():
    b = I.bar_of(dict(zip(COLS, row(shares=777, close=12.5))), lot=1)
    assert b["volume"] == 777.0
    assert I.turnover_error([b], 1) < 1e-9


def test_duplicate_minutes_are_dropped_and_order_is_by_time():
    rows = [row(hh=10, mm=2), row(hh=10, mm=0), row(hh=10, mm=2),
            row(hh=10, mm=1)]
    bars = I.bars_of(payload(rows))
    assert [b["ts"][11:16] for b in bars] == ["10:00", "10:01", "10:02"], (
        "склеенные страницы ISS не должны удваивать минуту")


def test_by_day_groups_by_calendar_day():
    bars = I.bars_of(payload(day_rows_iss("2026-07-30", minutes=3)
                             + day_rows_iss("2026-07-31", minutes=2)))
    got = I.by_day(bars)
    assert sorted(got) == ["2026-07-30", "2026-07-31"]
    assert len(got["2026-07-30"]) == 3 and len(got["2026-07-31"]) == 2


def test_nothing_to_check_against_says_so_instead_of_zero():
    b = I.bar_of(dict(zip(COLS, row(value=0))))
    assert I.turnover_error([b], 1) is None, "нет value — нет сверки, а не ноль"


def test_ten_backfilled_days_close_the_gap_and_build_the_profile():
    """Сквозная проверка: заваленные дни действительно годятся в дело."""
    lot = 10
    rows = []
    for d in WEEKDAYS:
        rows += day_rows_iss(d, minutes=240, shares=1000, close=100.0)
    per_day = I.by_day(I.bars_of(payload(rows), lot=lot))

    assert len(per_day) == MIN_DAYS
    assert all(len(v) >= MIN_BARS_DAY for v in per_day.values())

    g = profile_gap(per_day)
    assert g["usable_days"] == MIN_DAYS and g["ready"] is True
    assert g["weekend_days"] == 0 and g["short_days"] == 0

    prof = day_profile(per_day, lot=lot)
    assert prof, "профиль обязан построиться на десяти днях"
    assert prof[600] == 100000.0, "норма в РУБЛЯХ и без лотного перекоса"
    assert max(prof) == 600 + 239


def test_backfilling_weekends_does_not_help():
    """Завалить выходные легче всего — и бесполезно: у них другой рынок."""
    rows = []
    for d in ["2026-08-01", "2026-08-02"]:
        rows += day_rows_iss(d, minutes=240)
    per_day = I.by_day(I.bars_of(payload(rows)))
    g = profile_gap(per_day)
    assert g["days_in_db"] == 2 and g["weekend_days"] == 2
    assert g["usable_days"] == 0 and g["ready"] is False
    assert not day_profile(per_day, lot=1)
