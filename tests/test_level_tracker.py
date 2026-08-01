"""
Жизнь ценового уровня: то, чего не было в book_minute.

Там хранится РАЗМЕР крупнейшей заявки, но не её ЦЕНА. Поэтому на вопрос «уровень
100.50 держали или сняли» ответить было нельзя: видно, что плита исчезла, а на
какой цене она стояла — нет. Из-за этого пять строк из примера, который просил
Артём, были недостижимы: цена уровня, сколько на нём стояло, сколько исполнено,
сколько раз восстановился, пробит ли.

Что проверяется здесь:

    восстановление     возврат к сопоставимому размеру ПОСЛЕ ухода, а не любое
                       дрожание объёма
    исполнение         сделки приписываются уровню по точной цене
    пробой             цена ушла за уровень — факт, а не прогноз
    рубли              лотность обязательна: у UGLD лот 1000, у SBER 1
    память             уровни, которых давно нет, вычищаются
"""
import pytest

from src.analysis.level_tracker import LevelTracker, GONE_SHARE


def book(bids, asks):
    return list(bids), list(asks)


# ─── появление и размер ───────────────────────────────────────────────────────

def test_level_is_remembered_with_its_price():
    """
    Главное, чего не хватало. Уровень опознаётся ЦЕНОЙ, а не только размером.
    """
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00",
              [(276.50, 100), (276.40, 50)], [(276.60, 900)])
    lv = [x for x in t.notable("SBER", lot=1, top=1) if x["side"] == "ask"][0]
    assert lv["price"] == 276.60
    assert lv["peak_lots"] == 900


def test_peak_remembers_the_largest_size_ever_seen():
    """
    Максимум, а не текущий размер: важно, что плита БЫЛА. Текущий размер скажет
    лишь то, что её уже нет.
    """
    t = LevelTracker()
    for q in (100, 900, 200):
        t.on_book("SBER", "2026-08-03T10:00", [(276.50, q)], [(276.60, 10)])
    lv = [x for x in t.notable("SBER") if x["side"] == "bid"][0]
    assert lv["peak_lots"] == 900 and lv["now_lots"] == 200


# ─── рубли и лотность ─────────────────────────────────────────────────────────

def test_rubles_need_the_lot_size():
    """
    У UGLD лот 1000, у SBER 1. Тысяча лотов у них — это разные деньги на три
    порядка, и в лотах их сравнивать бессмысленно.
    """
    t = LevelTracker()
    t.on_book("UGLD", "2026-08-03T10:00", [(10.0, 500)], [(10.1, 10)])
    small = [x for x in t.notable("UGLD", lot=1) if x["side"] == "bid"][0]
    big = [x for x in t.notable("UGLD", lot=1000) if x["side"] == "bid"][0]
    assert small["peak_rub"] == 5000            # 500 x 10.0 x 1
    assert big["peak_rub"] == 5_000_000         # 500 x 10.0 x 1000


def test_lot_defaults_to_one_and_never_zero():
    t = LevelTracker()
    t.on_book("X", "2026-08-03T10:00", [(2.0, 10)], [(2.1, 1)])
    assert [x for x in t.notable("X", lot=0) if x["side"] == "bid"][0]["peak_rub"] == 20


# ─── уход, восстановление, исполнение ─────────────────────────────────────────

def test_level_that_left_the_book_is_marked_gone():
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 900)], [(276.60, 10)])
    t.on_book("SBER", "2026-08-03T10:01", [(276.40, 50)], [(276.60, 10)])
    lv = [x for x in t.notable("SBER") if x["side"] == "bid"][0]
    assert lv["gone_count"] == 1 and lv["now_lots"] == 0
    assert lv["peak_lots"] == 900, "но что она БЫЛА — помним"


def test_restoration_counts_only_after_a_real_departure():
    """
    Иначе каждое дрожание объёма записывалось бы как восстановление, и счётчик
    «восстановился 3 раза» перестал бы что-либо означать.
    """
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 900)], [(276.60, 10)])
    t.on_book("SBER", "2026-08-03T10:01", [(276.50, 850)], [(276.60, 10)])
    t.on_book("SBER", "2026-08-03T10:02", [(276.50, 880)], [(276.60, 10)])
    lv = [x for x in t.notable("SBER") if x["side"] == "bid"][0]
    assert lv["restored_count"] == 0, "объём колебался, но уровень не уходил"


def test_level_restored_three_times():
    """Ровно то, что в примере: «уровень восстановился 3 раза»."""
    t = LevelTracker()
    m = 0
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 1000)], [(276.60, 10)])
    for _ in range(3):
        m += 1
        t.on_book("SBER", f"2026-08-03T10:{m:02d}", [(276.40, 5)], [(276.60, 10)])
        m += 1
        t.on_book("SBER", f"2026-08-03T10:{m:02d}", [(276.50, 950)], [(276.60, 10)])
    lv = [x for x in t.notable("SBER") if x["side"] == "bid"][0]
    assert lv["restored_count"] == 3
    assert lv["gone_count"] == 3


def test_trades_are_attributed_to_the_level_by_exact_price():
    """
    «исполнено ~1.6 млн ₽». Биржевые цены стоят на сетке шага, поэтому
    совпадение точное и допуски не нужны.
    """
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 1000)], [(276.60, 500)])
    t.on_trade("SBER", 276.60, 300)
    t.on_trade("SBER", 276.60, 100)
    t.on_trade("SBER", 276.55, 999)          # мимо уровней
    lv = [x for x in t.notable("SBER") if x["side"] == "ask"][0]
    assert lv["traded_lots"] == 400


def test_execution_counter_resets_on_restoration():
    """
    «после последнего восстановления: 0 ₽» — отдельный счётчик, иначе нельзя
    отличить свежевыставленную заявку от уже проторгованной.
    """
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 1000)], [(276.60, 10)])
    t.on_trade("SBER", 276.50, 400)
    t.on_book("SBER", "2026-08-03T10:01", [(276.40, 5)], [(276.60, 10)])
    t.on_book("SBER", "2026-08-03T10:02", [(276.50, 950)], [(276.60, 10)])
    lv = [x for x in t.notable("SBER") if x["side"] == "bid"][0]
    assert lv["traded_lots"] == 400, "всего исполнено — помним"
    assert lv["traded_since_restore_rub"] == 0, "а после возврата пока ничего"
    assert lv["restored_count"] == 1


# ─── пробой ───────────────────────────────────────────────────────────────────

def test_bid_level_is_broken_when_price_goes_below():
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 1000)], [(276.60, 10)])
    t.on_book("SBER", "2026-08-03T10:01", [(276.00, 10)], [(276.10, 10)])
    lv = [x for x in t.notable("SBER") if x["side"] == "bid"][0]
    assert lv["broken"] is True


def test_ask_level_is_broken_when_price_goes_above():
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 10)], [(276.60, 1000)])
    t.on_book("SBER", "2026-08-03T10:01", [(277.00, 10)], [(277.10, 10)])
    lv = [x for x in t.notable("SBER") if x["side"] == "ask"][0]
    assert lv["broken"] is True


def test_level_still_inside_the_book_is_not_broken():
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 1000)], [(276.60, 10)])
    t.on_book("SBER", "2026-08-03T10:01", [(276.55, 20), (276.50, 1000)],
              [(276.60, 10)])
    lv = [x for x in t.notable("SBER") if x["side"] == "bid"][0]
    assert lv["broken"] is False


# ─── границы и память ─────────────────────────────────────────────────────────

def test_gone_share_is_not_zero_and_says_why():
    """
    Биржа отдаёт 20 уровней. Заявка может выпасть из окна, не исчезнув, и это
    неотличимо от снятия. Порог не нулевой именно поэтому, и причина записана
    рядом с ним.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/analysis/level_tracker.py").read_text()
    assert 0 < GONE_SHARE < 0.5
    i = src.index("GONE_SHARE = 0")
    assert "20 уровней" in src[max(0, i - 400):i], "причина пишется ПЕРЕД порогом"


def test_old_levels_are_pruned():
    """
    Без чистки карта растёт весь день: цена ходит, и уровней за сессию набегают
    тысячи на бумагу.
    """
    t = LevelTracker(keep_minutes=5)
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 900)], [(276.60, 10)])
    t.on_book("SBER", "2026-08-03T10:01", [(276.00, 10)], [(276.10, 10)])
    before = t.stats()["levels_tracked"]
    removed = t.prune("2026-08-03T10:30")
    assert removed > 0 and t.stats()["levels_tracked"] < before


def test_живой_уровень_не_вычищается():
    t = LevelTracker(keep_minutes=5)
    t.on_book("SBER", "2026-08-03T10:30", [(276.50, 900)], [(276.60, 10)])
    assert t.prune("2026-08-03T10:30") == 0
    assert t.stats()["levels_tracked"] == 2


def test_empty_and_broken_input_do_not_crash():
    t = LevelTracker()
    t.on_book("", "2026-08-03T10:00", [], [])
    t.on_book("SBER", "2026-08-03T10:00", [(0, 100), (276.5, 0)], [])
    t.on_trade("SBER", 0, 100)
    t.on_trade("", 276.5, 100)
    t.prune("")
    assert t.notable("SBER") == []


def test_sides_are_independent_at_the_same_price():
    """
    Одна цена бывает и заявкой на покупку, и на продажу в разные моменты. Если
    сторону не различать, счётчики двух разных заявок сложатся в одну.
    """
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 900)], [(276.60, 10)])
    t.on_book("SBER", "2026-08-03T10:01", [(276.40, 10)], [(276.50, 700)])
    got = {x["side"]: x for x in t.notable("SBER")}
    assert got["bid"]["price"] == 276.50 and got["bid"]["peak_lots"] == 900
    assert got["ask"]["price"] == 276.50 and got["ask"]["peak_lots"] == 700


def test_no_direction_or_recommendation_in_output():
    """
    «Уровень пробит» — факт. «Значит цена пойдёт дальше» — утверждение, которого
    здесь нет и не будет.
    """
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 900)], [(276.60, 10)])
    for lv in t.notable("SBER"):
        for bad in ("signal", "direction", "recommend", "target", "entry"):
            assert bad not in lv


# ─── подключение к стриму и экрану ────────────────────────────────────────────

def test_tracker_is_fed_with_the_source_label():
    """
    Дилерский стакан — котировки брокера, и жизнь уровня в нём означает не то же
    самое. Но отбрасывать его нельзя: первая версия так и делала, и на закрытой
    бирже трекер оставался пустым — механику нельзя было проверить до
    понедельника. Пишем оба, помечая источник.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    src = (root / "src/collector/stream.py").read_text()
    i = src.index("self.levels.on_book")
    assert "source=src" in src[i:i + 400], "источник передаётся в трекер"
    j = src.index("self.levels.on_trade")
    assert "source=src" in src[j:j + 200]
    assert 'if src == "exchange":\n                self.levels' not in src, \
        "дилерский больше не отбрасывается"


def test_lot_size_comes_from_iss_not_from_a_hand_table():
    """
    Лотность у бумаг разная: SBER 1, GAZP 10, UGLD 1000. Рукописной таблицы тут
    быть не должно — 30.07 такая таблица для FIGI дала 22 подмены из 43.
    """
    import pathlib
    main = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "LOTSIZE" in main and "iss.moex.com" in main
    assert "stream.lots = lots" in main


def test_live_page_and_light_mode_exist():
    """
    Экран обновляется раз в секунду. Полная карточка ходит в базу на три
    таблицы; восемь бумаг в секунду дали бы 24 запроса к SQLite. Поэтому есть
    лёгкий режим, читающий только память.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    api = (root / "src/api/main.py").read_text()
    assert "/api/book-live/{ticker}" in api
    assert "light: bool = False" in api
    assert "/book-live" in api
    page = (root / "dashboard/book-live.html").read_text()
    assert "light=true" in page, "страница обязана пользоваться лёгким режимом"
    assert "setInterval(tickLight, 1000)" in page


def test_page_says_it_describes_and_does_not_advise():
    """
    Рамка должна быть НА ЭКРАНЕ, а не только в коде: смотреть будут на экран.
    """
    import pathlib
    page = (pathlib.Path(__file__).resolve().parents[1]
            / "dashboard/book-live.html").read_text()
    assert "описывает состояние" in page
    assert "не измерено" in page
    assert "вредным" in page, "и про измеренный вред структуры тоже"


def test_sources_are_tracked_separately():
    """
    Первая версия кормила трекер ТОЛЬКО биржевым стаканом, и на закрытой бирже он
    оставался пустым — проверить механику до понедельника было нельзя. Теперь
    отслеживаются оба, но раздельно: дилерский это котировки брокера, там нет
    чужих заявок, которые можно съесть.
    """
    t = LevelTracker()
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 900)], [(276.60, 10)],
              source="exchange")
    t.on_book("SBER", "2026-08-03T10:00", [(276.50, 33)], [(276.60, 10)],
              source="dealer")
    ex = [x for x in t.notable("SBER", source="exchange") if x["side"] == "bid"][0]
    de = [x for x in t.notable("SBER", source="dealer") if x["side"] == "bid"][0]
    assert ex["peak_lots"] == 900 and de["peak_lots"] == 33
    assert t.stats()["by_source"] == {"exchange": 2, "dealer": 2}
    assert t.stats()["tickers"] == 1, "тикер один, источников два"


def test_fallback_checks_for_flow_not_just_for_rows():
    """
    Регрессия. Свечи источником не фильтруются, поэтому при закрытой бирже
    minute_rows возвращал строки из одних свечей: проверка «если строк нет» не
    срабатывала, и наружу шла картина без потока и без пометки об этом.
    """
    import pathlib
    api = (pathlib.Path(__file__).resolve().parents[1]
           / "src/api/main.py").read_text()
    assert "def _has_flow(rs)" in api
    assert "if not _has_flow(rows):" in api
    assert 'out["source"] = src' in api, "источник должен быть виден в ответе"
