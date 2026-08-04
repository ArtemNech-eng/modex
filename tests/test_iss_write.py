"""
Что именно дозаливка отдаёт в базу.

Стрим пишет бары через db.merge_candle_minutes(tk, rows). Как именно та функция
разбирает строку — внутреннее дело src/db.py, и ставить на него дозаливку
значит обещать то, чего никто не проверял. Поэтому договор узкий:
в базу уходит ровно та же шестёрка ключей, что у стрима.

Служебные поля сверки не «на всякий случай пусть полежат» — они работают
ДО записи и там же выбрасываются.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collector.iss_minutes import (  # noqa: E402
    STREAM_KEYS, bars_of, stream_rows, turnover_error,
)
from src.analysis.volume_events import _rub  # noqa: E402

COLS = ["begin", "end", "open", "close", "high", "low", "value", "volume"]


def row(day, hh, mm, shares, close, value):
    return [f"{day} {hh:02d}:{mm:02d}:00", f"{day} {hh:02d}:{mm:02d}:59",
            close, close, close, close, value, shares]


def payload(rows):
    return {"candles": {"columns": COLS, "data": rows}}


def day(shares=1000, close=100.0, lot=10, minutes=5):
    """Ответ ISS в штуках и рублях, как он и приходит."""
    rows = [row("2026-07-20", 10, 5 + i, shares, close, shares * close)
            for i in range(minutes)]
    return bars_of(payload(rows), lot=lot)


def test_only_the_keys_the_stream_writes_reach_the_database():
    bars = day()
    assert bars, "бары должны разобраться, иначе тест проверяет пустоту"
    #  в самих барах служебное есть — оно нужно для сверки
    assert "shares" in bars[0] and "value_rub" in bars[0]

    rows = stream_rows(bars)
    assert len(rows) == len(bars)
    for r in rows:
        assert set(r) == set(STREAM_KEYS)
        assert "shares" not in r
        assert "value_rub" not in r
        assert "source" not in r


def test_stripping_the_service_fields_does_not_move_the_rubles():
    """Сверка должна говорить про то, что ляжет в базу, а не про черновик."""
    lot = 10
    bars = day(lot=lot)
    assert turnover_error(bars, lot=lot) < 1e-9

    before = sum(_rub(b, lot) for b in bars)
    after = sum(_rub(r, lot) for r in stream_rows(bars))
    assert after == before
    #  и это те же рубли, что пришли от ISS
    assert after == sum(float(b["value_rub"]) for b in bars)


def test_a_row_without_time_is_not_written_at_all():
    rows = stream_rows([{"ts": "", "close": 100.0, "volume": 1.0},
                        {"close": 100.0, "volume": 1.0},
                        None,
                        {"ts": "2026-07-20T10:05:00", "close": 100.0, "volume": 1.0}])
    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-07-20T10:05:00"


def test_nothing_in_nothing_out():
    assert stream_rows([]) == []
    assert stream_rows(None) == []
