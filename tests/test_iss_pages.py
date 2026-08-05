"""
Постраничность ISS: ответ — это страница, а не день.

На живой базе 05.08 сверка вернула ровно 500 минут у ISS во все шесть
дней на трёх бумагах, при 541-777 минутах в базе стрима. Одинаковое
круглое число на разных бумагах и разных днях — это потолок страницы.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.collector import iss_minutes as I  # noqa: E402
from src.analysis.volume_events import MIN_BARS_DAY  # noqa: E402

COLS = ["begin", "end", "open", "close", "high", "low", "value", "volume"]
DAY = "2026-08-04"


def row(minute, shares=1000, close=100.0):
    """Минута дня по номеру от полуночи: сессия длиннее одной страницы."""
    t = f"{DAY} {minute // 60:02d}:{minute % 60:02d}:00"
    return [t, t, close, close, close, close, shares * close, shares]


def page(minutes):
    return {"candles": {"columns": COLS, "data": [row(m) for m in minutes]}}


def test_url_can_ask_for_the_next_page():
    first = I.candles_url("SBER", DAY)
    second = I.candles_url("SBER", DAY, start=I.PAGE)
    assert "start=0" in first
    assert f"start={I.PAGE}" in second
    assert "interval=1" in second and "token" not in second.lower()


def test_a_full_page_means_there_is_more():
    full = page(range(600, 600 + I.PAGE))
    assert I.page_is_full(full) is True


def test_a_short_page_is_the_last_one():
    assert I.page_is_full(page(range(600, 610))) is False
    assert I.page_is_full({}) is False
    assert I.page_is_full({"candles": {"columns": COLS, "data": []}}) is False


def test_a_page_full_of_junk_is_still_a_full_page():
    """
    Признак «есть следующая» считает СЫРЫЕ строки, а не годные бары.

    Иначе одна битая минута внутри страницы обрывала бы перебор на
    середине дня — и снова молча.
    """
    data = [row(m) for m in range(600, 600 + I.PAGE)]
    data[7][0] = "мусор"          # begin не разберётся
    payload = {"candles": {"columns": COLS, "data": data}}
    assert I.page_is_full(payload) is True
    assert len(I.bars_of(payload)) == I.PAGE - 1


def test_pages_are_stitched_into_one_day():
    """Две страницы по 500 и хвост — это 777 минут, как в базе стрима."""
    p1 = page(range(400, 900))          # 500 минут
    p2 = page(range(900, 1177))         # ещё 277
    bars = I.bars_of_pages([p1, p2])
    assert len(bars) == 777
    assert bars[0]["ts"].endswith("T06:40")
    assert bars[-1]["ts"].endswith("T19:36")
    #  Строго по времени и без дыр между страницами
    keys = [b["ts"] for b in bars]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_overlapping_pages_do_not_double_a_minute():
    """Перехлёст страниц безопасен: минута замещает себя, а не удваивается."""
    p1 = page(range(600, 700))
    p2 = page(range(650, 750))          # 50 минут повторяются
    bars = I.bars_of_pages([p1, p2])
    assert len(bars) == 150
    assert len({b["ts"] for b in bars}) == 150


def test_stitching_nothing_gives_nothing():
    assert I.bars_of_pages([]) == []
    assert I.bars_of_pages([{}, None]) == []


def test_one_page_looks_like_a_whole_day_and_passes_every_check():
    """
    ГЛАВНОЕ ЗДЕСЬ. Обрезанный день не вызывает НИ ОДНОГО возражения
    у старых проверок: баров 500 при пороге 200, сверка оборота нулевая.

    Потому что сверка оборота берёт и свой пересчёт, и чужие рубли из
    ОДНОГО и того же ответа: недостающие минуты отсутствуют в обеих
    сторонах сразу и сокращаются. Поймать недобор можно только счётом
    страниц, и именно поэтому нужен page_is_full.
    """
    cut = page(range(400, 400 + I.PAGE))
    bars = I.bars_of(cut, lot=10)

    assert len(bars) == I.PAGE
    assert len(bars) > MIN_BARS_DAY, "порог «день состоялся» пройден обрезком"
    assert I.turnover_error(bars, lot=10) < 1e-9, "сверка молчит на обрезке"

    #  И единственный честный признак беды:
    assert I.page_is_full(cut) is True, "только счёт строк выдаёт недобор"


def test_the_missing_part_is_always_one_edge_of_the_session():
    """
    Почему недобор страниц хуже просто нехватки данных: пропадают не
    случайные минуты, а всегда один и тот же конец сессии — то есть
    ровно тот перекос по времени суток, от которого дозаливка лечит.
    """
    whole = list(range(400, 1177))                  # 777 минут сессии
    only_first_page = I.bars_of(page(whole[:I.PAGE]))
    stitched = I.bars_of_pages([page(whole[:I.PAGE]), page(whole[I.PAGE:])])

    lost = {b["ts"] for b in stitched} - {b["ts"] for b in only_first_page}
    assert len(lost) == 277
    #  Всё потерянное лежит ПОЗЖЕ всего, что осталось
    assert min(lost) > max(b["ts"] for b in only_first_page)
