"""
Контекст рынка: индекс, широта, сектор и сила бумаги относительно них.

Артём сформулировал точно: «IMOEX −0.4%, а SBER +0.8% — это гораздо интереснее,
чем просто SBER +0.8%». Одно и то же движение бумаги означает разное в
зависимости от того, куда идёт всё остальное.

Что защищают эти тесты:

    два эталона        медиана НАШЕЙ корзины (та же секунда, задержки нет) и
                       IMOEX (настоящий, взвешенный, но опрошенный с
                       задержкой). Подставлять одно вместо другого нельзя

    возраст индекса    на быстром движении задержка переворачивает знак
                       разницы, поэтому возраст обязан быть в выдаче

    широта             то, что индекс СКРЫВАЕТ: он растёт на двух тяжёлых
                       бумагах, пока падают шестьдесят

    разные стороны     «обгоняет» и «идёт против рынка» — не одно и то же:
                       обогнать можно и падая медленнее

    нет данных ≠ ноль  бумага без истории в расчёт не попадает, а не считается
                       неизменившейся

    маленький сектор   «два из трёх растут» весит не так, как «сорок из
                       шестидесяти», и это помечается

    без вердиктов      «сильная бумага» — утверждение о будущем; связь
                       опережения рынка с последующим движением не измерялась
"""
import pathlib

import pytest

from src.analysis.market_context import (changes, breadth, rank, relative,
                                         sector_view, context, FLAT_PCT,
                                         MIN_SECTOR)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def series(*closes, start=0):
    """Минутный ряд закрытий с метками времени."""
    return [(f"2026-08-03T10:{start + i:02d}", c) for i, c in enumerate(closes)]


# ─── изменения по бумагам ─────────────────────────────────────────────────────

def test_change_over_n_minutes():
    m = {"SBER": series(100.0, 101.0, 102.0)}
    assert changes(m, back=1)["SBER"] == pytest.approx(0.9901, abs=1e-3)
    assert changes(m, back=2)["SBER"] == pytest.approx(2.0)


def test_ticker_without_enough_history_is_absent_not_zero():
    """
    «Не изменилась» и «не знаем» — разные вещи. Подставить ноль значило бы
    записать бумагу без истории в боковик и испортить широту.
    """
    m = {"SBER": series(100.0, 101.0), "GAZP": series(100.0)}
    c = changes(m, back=1)
    assert "SBER" in c and "GAZP" not in c


def test_broken_values_are_skipped():
    m = {"A": series(100.0, "мусор"), "B": series(0.0, 100.0),
         "C": series(100.0, 102.0)}
    c = changes(m, back=1)
    assert set(c) == {"C"}


# ─── широта рынка ─────────────────────────────────────────────────────────────

def test_breadth_counts_up_down_flat():
    """
    То, что индекс СКРЫВАЕТ: он может расти на двух тяжёлых бумагах, пока падают
    шестьдесят.
    """
    chg = {"A": 1.0, "B": 0.8, "C": -0.5, "D": -0.7, "E": 0.0}
    b = breadth(chg)
    assert b["up"] == 2 and b["down"] == 2 and b["flat"] == 1
    assert b["total"] == 5
    assert b["best_pct"] == 1.0 and b["worst_pct"] == -0.7


def test_flat_is_an_answer():
    chg = {"A": 0.01, "B": -0.02, "C": 0.0}
    b = breadth(chg)
    assert b["flat"] == 3 and b["up"] == 0 and b["down"] == 0
    assert abs(b["median_pct"]) < FLAT_PCT


def test_breadth_empty_on_no_data():
    assert breadth({}) == {}


# ─── место бумаги ─────────────────────────────────────────────────────────────

def test_rank_is_a_share_not_a_number():
    """
    «Двенадцатая из восьмидесяти» и «двенадцатая из пятнадцати» — разные вещи, а
    номер их не различает.
    """
    chg = {"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.0, "E": -1.0}
    r = rank(chg, "B")
    assert r["beats"] == 3 and r["of"] == 4
    assert r["percentile"] == pytest.approx(0.75)


def test_rank_absent_for_unknown_ticker():
    assert rank({"A": 1.0, "B": 2.0}, "ZZZZ") is None


# ─── сила относительно эталона ────────────────────────────────────────────────

def test_the_case_from_the_request():
    """
    Ровно пример Артёма: индекс −0.4%, бумага +0.8%. Это НЕ просто «обгоняет» —
    они идут в РАЗНЫЕ стороны, и это отдельный факт.
    """
    r = relative(0.8, -0.4)
    assert r["diff_pp"] == pytest.approx(1.2)
    assert r["vs_bench"] == "обгоняет"
    assert r["opposite"] is True


def test_outperforming_while_both_fall_is_not_opposite():
    """
    Обогнать можно и падая медленнее. Смешивать это с «идёт против рынка»
    значило бы стереть разницу между силой и просто меньшим падением.
    """
    r = relative(-0.2, -0.9)
    assert r["vs_bench"] == "обгоняет"
    assert "opposite" not in r


def test_small_divergence_is_together():
    r = relative(0.30, 0.28)
    assert r["vs_bench"] == "вместе"


def test_relative_needs_both_numbers():
    assert relative(0.5, None) == {}
    assert relative(None, 0.5) == {}


# ─── сектор ───────────────────────────────────────────────────────────────────

def test_sector_breadth_uses_only_peers():
    chg = {"SBER": 1.0, "VTBR": 0.8, "SVCB": -0.3, "GAZP": -2.0}
    sectors = {"SBER": "финансы", "VTBR": "финансы", "SVCB": "финансы",
               "GAZP": "нефть и газ"}
    sv = sector_view(chg, sectors, "SBER")
    assert sv["sector"] == "финансы" and sv["peers"] == 3
    assert sv["up"] == 2 and sv["down"] == 1
    assert "vs_sector" in sv


def test_tiny_sector_is_flagged():
    """
    «Два из трёх растут» звучит так же весомо, как «сорок из шестидесяти», а
    весит совсем иначе.
    """
    chg = {"A": 1.0, "B": -1.0}
    sectors = {"A": "химия", "B": "химия"}
    sv = sector_view(chg, sectors, "A")
    assert sv["peers"] < MIN_SECTOR and sv["too_few"] is True


def test_unknown_sector_gives_nothing():
    assert sector_view({"A": 1.0}, {"B": "химия"}, "A") == {}


# ─── всё вместе ───────────────────────────────────────────────────────────────

def basket():
    return {
        "SBER": series(100.0, 100.2, 100.4, 100.6, 100.8, 101.0),
        "GAZP": series(100.0, 99.9, 99.8, 99.7, 99.6, 99.5),
        "LKOH": series(100.0, 99.95, 99.9, 99.85, 99.8, 99.75),
        "VTBR": series(100.0, 100.0, 100.0, 100.0, 100.0, 100.0),
    }


def test_context_gives_both_benchmarks():
    """
    Два эталона рядом, и подставлять один вместо другого нельзя: у медианы
    корзины нет задержки, но она равновзвешена по нашим бумагам; у индекса
    настоящий вес, но он опрошен.
    """
    idx = {"name": "IMOEX", "value": 2200.0, "change_pct": -0.4,
           "changes": {1: -0.05, 5: -0.4}, "age_sec": 12.0}
    c = context(basket(), "SBER", index=idx, steps=(1, 5))
    f = c["frames"]["5m"]
    assert "vs_basket" in f and "vs_index" in f
    assert f["vs_index"]["opposite"] is True, "бумага вверх, индекс вниз"
    assert c["index"]["age_sec"] == 12.0, "возраст индекса обязателен"


def test_context_survives_without_the_index():
    """
    ISS может не ответить. Корзина считается всё равно: она из нашего потока и
    от внешнего источника не зависит.
    """
    c = context(basket(), "SBER", steps=(1, 5))
    assert "vs_basket" in c["frames"]["5m"]
    assert "vs_index" not in c["frames"]["5m"]
    assert "index" not in c


def test_context_breadth_matches_the_basket():
    c = context(basket(), "SBER", steps=(5,))
    b = c["frames"]["5m"]["breadth"]
    assert b["total"] == 4 and b["up"] == 1 and b["down"] == 2
    assert b["flat"] == 1


def test_context_empty_input():
    assert context({}, "SBER") == {"frames": {}}


# ─── описание, а не совет ─────────────────────────────────────────────────────

def test_no_verdicts_about_the_future():
    """
    «Сильная бумага» означает «дальше пойдёт вверх». Связь опережения рынка с
    последующим движением я не мерил, а похожие метки 31.07 измерялись
    бесполезными, одна вредной: t=-12.57.
    """
    idx = {"name": "IMOEX", "change_pct": -0.4, "changes": {1: -0.05, 5: -0.4}}
    blob = str(context(basket(), "SBER", index=idx)).lower()
    for bad in ("сильн", "слаб", "strong", "weak", "signal", "buy", "sell",
                "recommend", "лидер", "аутсайдер"):
        assert bad not in blob, bad


def test_the_two_benchmark_caveat_is_written_next_to_the_code():
    src = (ROOT / "src/analysis/market_context.py").read_text()
    assert "ДВА ЭТАЛОНА" in src
    assert "перевернуть знак" in src, "риск задержки индекса описан"


# ─── подключение ──────────────────────────────────────────────────────────────

def test_wired_into_the_card():
    api = (ROOT / "src/api/main.py").read_text()
    assert 'out["market"] = context(' in api
    i = api.index('out["market"] = context(')
    assert "age_sec" in api[max(0, i - 700):i], "возраст индекса считается рядом"


def test_sectors_come_from_moex_indices_not_from_a_guess():
    m = (ROOT / "main.py").read_text()
    assert "MOEXOG" in m and "MOEXFN" in m
    assert "analytics" in m, "состав берётся из аналитики отраслевых индексов"


def test_page_shows_market_around():
    page = (ROOT / "dashboard/book-live.html").read_text()
    assert "рынок вокруг" in page
    assert "против корзины" in page and "против IMOEX" in page
    assert "в разные стороны" in page


def test_repeated_minute_overwrites_instead_of_duplicating():
    """
    Сброс идёт чаще, чем раз в минуту, поэтому одна и та же минута приходит
    несколько раз. Без перезаписи «за 5 минут» означало бы «за пять последних
    записей», то есть за полторы минуты — и вся широта рынка врала бы.
    """
    from src.collector.stream import MarketStream
    s = MarketStream.__new__(MarketStream)      # без сети и токена
    s.minutes = {}
    s.note_minute("SBER", "2026-08-03T10:00", 100.0)
    s.note_minute("SBER", "2026-08-03T10:00", 100.5)   # та же минута
    s.note_minute("SBER", "2026-08-03T10:01", 101.0)
    assert list(s.minutes["SBER"]) == [("2026-08-03T10:00", 100.5),
                                       ("2026-08-03T10:01", 101.0)]


def test_minute_history_is_bounded_and_skips_junk():
    from src.collector.stream import MarketStream
    s = MarketStream.__new__(MarketStream)
    s.minutes = {}
    for i in range(100):
        s.note_minute("SBER", f"2026-08-03T{10 + i // 60:02d}:{i % 60:02d}", 100.0 + i)
    assert len(s.minutes["SBER"]) <= 40
    s.note_minute("", "2026-08-03T10:00", 100.0)
    s.note_minute("GAZP", None, 100.0)
    s.note_minute("GAZP", "2026-08-03T10:00", None)
    assert "GAZP" not in s.minutes and "" not in s.minutes
