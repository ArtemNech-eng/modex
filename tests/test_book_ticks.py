"""
Секундный ряд стакана: то, что терялось между пакетом и минутой.

Стрим получает стакан до десяти раз в секунду, но всё складывалось в МИНУТНУЮ
корзину, а последовательность внутри минуты выбрасывалась — от неё оставались
минимум и максимум перекоса. По размаху нельзя сказать, что было десять секунд
назад.

Из-за этого не работали три вещи: изменение перекоса за 10 и 30 секунд, скорость
появления и исчезновения ликвидности, исполнение возле лучших цен. Данные
ПРИХОДИЛИ, но не сохранялись — та же болезнь, что была с полем источника сделки.

Что проверяется здесь:

    свежесть      слот перезаписывается последним пакетом секунды, а не средним
    отсчёт назад  ближайший слот НЕ НОВЕЕ нужного, а не точное совпадение
    отдельно      добавленное и снятое считаются раздельно, не одной разностью
    источник      дилерский и биржевой ряды не смешиваются
    накопление    счётчики исполнения обнуляются после свёртки минуты
"""
import pytest

from src.analysis.book_ticks import TickRing, SECONDS, NEAR_TICKS

T0 = 1785000000          # произвольная секунда эпохи


def ring(**kw):
    return TickRing(**kw)


def feed(r, sec, bid, ask, tk="SBER", src="exchange", **kw):
    r.on_book(tk, src, sec, bid, ask, kw.get("bid5", bid / 2),
              kw.get("ask5", ask / 2), kw.get("bb", 100.0),
              kw.get("ba", 100.1), kw.get("bid_top", 0), kw.get("ask_top", 0))


# ─── перекос и его изменение ──────────────────────────────────────────────────

def test_imbalance_now():
    r = ring()
    feed(r, T0, 700, 300)
    assert r.deltas("SBER", "exchange", T0)["imb"] == pytest.approx(0.7)


def test_change_over_ten_and_thirty_seconds():
    """Главное, чего не было: перекос сменился за 10 секунд, а не за минуту."""
    r = ring()
    feed(r, T0 - 40, 200, 800)          # 0.2
    feed(r, T0 - 20, 500, 500)          # 0.5
    feed(r, T0 - 8, 600, 400)           # 0.6
    feed(r, T0, 800, 200)               # 0.8
    d = r.deltas("SBER", "exchange", T0)
    assert d["imb"] == pytest.approx(0.8)
    # За 10 секунд берётся слот НЕ НОВЕЕ, чем T0-10, то есть T0-20 с его 0.5.
    # Слот T0-8 новее отсечки и не годится — иначе «за 10 секунд» означало бы
    # «за сколько получилось».
    assert d["imb_d10s"] == pytest.approx(0.3)
    # За 30 секунд ближайший не новее — T0-40 с его 0.2.
    assert d["imb_d30s"] == pytest.approx(0.6)


def test_lookback_takes_nearest_not_newer():
    """
    Именно «не новее», а не «ровно тогда». В тихую секунду пакета может не быть
    вовсе, и требование точного совпадения давало бы пустоту на ровном рынке.
    """
    r = ring()
    feed(r, T0 - 47, 100, 900)          # 0.1, ровно 47 секунд назад
    feed(r, T0, 900, 100)               # 0.9
    d = r.deltas("SBER", "exchange", T0)
    assert d["imb_d30s"] == pytest.approx(0.8), "взят слот 47с назад, ближайший не новее"


def test_no_lookback_slot_gives_no_delta():
    """Если истории на нужную глубину нет, поля просто не появляется."""
    r = ring()
    feed(r, T0, 500, 500)
    d = r.deltas("SBER", "exchange", T0)
    assert "imb_d30s" not in d and "imb_d60s" not in d


def test_last_packet_of_the_second_wins():
    """
    Нужно состояние на КОНЕЦ секунды, а не среднее по ней: среднее сгладило бы
    ровно те резкие смены, которые ищем.
    """
    r = ring()
    for bid in (100, 500, 900):
        feed(r, T0, bid, 100)
    assert r.deltas("SBER", "exchange", T0)["imb"] == pytest.approx(0.9)


def test_stale_slots_are_ignored():
    """Кольцо переиспользует слоты; без проверки секунды всплыло бы прошлое."""
    r = ring(seconds=60)
    feed(r, T0 - 500, 900, 100)
    feed(r, T0, 500, 500)
    d = r.deltas("SBER", "exchange", T0)
    assert d["samples"] == 1, "слот пятисотсекундной давности не считается"


# ─── скорость ликвидности ─────────────────────────────────────────────────────

def test_added_and_removed_are_counted_separately():
    """
    Разность скрывает главное: стакан, где добавили и сняли по миллиону, и
    стакан, где не было ничего, дают одинаковый ноль.
    """
    r = ring()
    feed(r, T0 - 20, 1000, 1000)
    feed(r, T0 - 10, 2000, 1000)        # +1000 на биде
    feed(r, T0, 1000, 1000)             # -1000 на биде
    s = r.speed("SBER", "exchange", T0, window=30)
    assert s["bid_added_per_sec"] > 0
    assert s["bid_removed_per_sec"] > 0, "снятое видно отдельно, а не гасится добавленным"


def test_quiet_book_has_no_speed():
    r = ring()
    for back in (20, 10, 0):
        feed(r, T0 - back, 1000, 1000)
    s = r.speed("SBER", "exchange", T0, window=30)
    assert s["bid_added_per_sec"] == 0 and s["bid_removed_per_sec"] == 0


def test_peak_rate_is_per_second_not_per_window():
    """
    Пиковая скорость — за секунду. Вспышка в одну секунду и ровный приток за
    тридцать это разные события, а среднее по окну их уравнивает.
    """
    r = ring()
    feed(r, T0 - 2, 1000, 1000)
    feed(r, T0 - 1, 6000, 1000)        # +5000 за одну секунду
    feed(r, T0, 6100, 1000)
    s = r.speed("SBER", "exchange", T0, window=30)
    assert s["bid_peak_add_per_sec"] == pytest.approx(5000.0)


def test_one_sample_gives_no_speed():
    r = ring()
    feed(r, T0, 1000, 1000)
    assert r.speed("SBER", "exchange", T0) == {}


# ─── исполнение возле лучших цен ──────────────────────────────────────────────

def test_execution_at_best_near_and_deep():
    """
    Различие содержательное: сделка по лучшей цене снимает верхнюю заявку, а
    сделка глубоко означает, что верх уже пробили и агрессор идёт дальше.
    """
    r = ring()
    r.on_trade("SBER", "exchange", 100.10, 500, 100.00, 100.10, tick=0.01)
    r.on_trade("SBER", "exchange", 100.12, 300, 100.00, 100.10, tick=0.01)
    r.on_trade("SBER", "exchange", 100.90, 200, 100.00, 100.10, tick=0.01)
    n = r.near_best("SBER", "exchange")
    assert n["at_best_lots"] == 500
    assert n["near_lots"] == 300, "в двух шагах от лучшей"
    assert n["deep_lots"] == 200
    assert n["at_best_share"] == pytest.approx(0.5)


def test_unknown_tick_degrades_to_exact_price_only():
    """
    Шаг цены у бумаг разный, подставлять единый нельзя. Без шага «возле»
    вырождается в «точно по цене»: лучше недосчитать, чем посчитать неверно.
    """
    r = ring()
    r.on_trade("SBER", "exchange", 100.05, 100, 100.00, 100.10, tick=0)
    n = r.near_best("SBER", "exchange")
    assert n["near_lots"] == 0 and n["deep_lots"] == 100


def test_near_ticks_is_documented():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/analysis/book_ticks.py").read_text()
    assert NEAR_TICKS >= 1
    i = src.index("NEAR_TICKS = ")
    assert "шаг" in src[i:i + 120].lower()


# ─── источники не смешиваются ─────────────────────────────────────────────────

def test_sources_are_separate_series():
    """
    Дилерский стакан это котировки брокера, и скорость его изменения означает не
    то же самое. Смешивание сделало бы бесполезными обе половины.
    """
    r = ring()
    feed(r, T0, 900, 100, src="exchange")
    feed(r, T0, 100, 900, src="dealer")
    assert r.deltas("SBER", "exchange", T0)["imb"] == pytest.approx(0.9)
    assert r.deltas("SBER", "dealer", T0)["imb"] == pytest.approx(0.1)
    assert r.stats()["series"] == 2


# ─── свёртка в минуту для базы ────────────────────────────────────────────────

def test_minute_summary_keeps_the_swing_not_the_series():
    """
    Секундный ряд в базу не пишется: 4.9 млн строк в день и 35 ГБ за 90 дней при
    20 ГБ свободных. Сохраняются СВОЙСТВА ряда — насколько сильно и быстро
    менялся стакан.
    """
    r = ring()
    # Ряд длиннее минуты: иначе за 30 секунд наберётся ОДНА разность, и её
    # максимум совпадёт с минимумом — размаха не увидеть.
    feed(r, T0 - 90, 200, 800)          # 0.2
    feed(r, T0 - 55, 900, 100)          # 0.9  -> против 0.2 это +0.7
    feed(r, T0 - 20, 100, 900)          # 0.1  -> против 0.9 это -0.8
    feed(r, T0, 700, 300)               # 0.7
    m = r.minute_summary("SBER", "exchange", T0)
    assert m["samples"] == 4
    assert m["imb_d30_max"] > 0 and m["imb_d30_min"] < 0, \
        "размах в обе стороны, а не одно среднее"
    assert "bid_added" in m and "bid_removed" in m
    assert "traded_at_best" in m


def test_minute_summary_needs_two_samples():
    r = ring()
    feed(r, T0, 500, 500)
    assert r.minute_summary("SBER", "exchange", T0) == {}


def test_trade_counters_reset_after_the_minute():
    """
    Счётчики исполнения накопительные. Без обнуления после свёртки минутные
    значения превратились бы в суммы с начала дня — и выглядели бы
    правдоподобно, как уже было с накопленной дельтой 31.07.
    """
    r = ring()
    r.on_trade("SBER", "exchange", 100.10, 500, 100.00, 100.10, tick=0.01)
    assert r.near_best("SBER", "exchange")["traded_lots"] == 500
    r.reset_trades("SBER", "exchange")
    assert r.near_best("SBER", "exchange") == {}


def test_reset_without_arguments_clears_everything():
    r = ring()
    r.on_trade("A", "exchange", 1.0, 10, 1.0, 1.1)
    r.on_trade("B", "exchange", 1.0, 10, 1.0, 1.1)
    r.reset_trades()
    assert r.near_best("A", "exchange") == {} and r.near_best("B", "exchange") == {}


# ─── устойчивость и границы ───────────────────────────────────────────────────

def test_broken_input_does_not_crash():
    r = ring()
    feed(r, 0, 100, 100)                    # нулевая секунда
    feed(r, T0, 0, 0)                       # пустой стакан
    r.on_book("", "exchange", T0, 1, 1, 1, 1, 1, 1)
    r.on_trade("", "exchange", 1.0, 1, 1.0, 1.1)
    r.on_trade("SBER", "exchange", 0, 1, 1.0, 1.1)
    assert r.deltas("SBER", "exchange", T0) == {}


def test_ring_depth_is_bounded():
    """
    Память ограничена по построению: слотов ровно столько, сколько секунд
    глубины, и они переиспользуются. Иначе за день набежал бы ряд на 60 тысяч
    точек на бумагу.
    """
    r = ring(seconds=30)
    for i in range(500):
        feed(r, T0 + i, 1000 + i, 1000)
    buf = r.ring[("SBER", "exchange")]
    assert len(buf) == 30, "кольцо не растёт"
    assert r.stats()["depth_sec"] == 30


def test_minimum_depth_is_enforced():
    assert TickRing(seconds=1).seconds >= 10, "меньше десяти секунд бессмысленно"


def test_default_depth_covers_a_minute_lookback():
    assert SECONDS >= 60, "иначе изменение за 60 секунд посчитать нечем"


def test_no_direction_or_recommendation():
    """«Перекос сменился за 10 секунд» — факт. Что будет дальше — не здесь."""
    r = ring()
    feed(r, T0 - 20, 200, 800)
    feed(r, T0, 800, 200)
    for got in (r.deltas("SBER", "exchange", T0),
                r.speed("SBER", "exchange", T0),
                r.minute_summary("SBER", "exchange", T0)):
        for bad in ("signal", "direction", "recommend", "entry", "target"):
            assert bad not in got


# ─── подключение к стриму, базе и экрану ──────────────────────────────────────

import pathlib                                                    # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_ring_is_fed_from_the_stream():
    src = (ROOT / "src/collector/stream.py").read_text()
    assert "self.ticks = TickRing()" in src
    assert "self.ticks.on_book(" in src and "self.ticks.on_trade(" in src


def test_price_step_comes_from_iss_per_ticker():
    """
    Шаг цены разный: SBER 0.01, VTBR 0.005, MVID 0.05, UGLD 0.0001. Единый порог
    «возле лучшей цены» был бы неверен почти везде.
    """
    main = (ROOT / "main.py").read_text()
    assert "MINSTEP" in main and "stream.steps = steps" in main
    src = (ROOT / "src/collector/stream.py").read_text()
    assert "tick=float(self.steps.get(tk) or 0)" in src


def test_trade_counters_are_reset_after_each_flush():
    """
    Счётчики исполнения накопительные. Без обнуления после сброса минутные
    значения стали бы суммами с начала дня — и выглядели бы правдоподобно.
    """
    src = (ROOT / "src/collector/stream.py").read_text()
    i = src.index("minute_summary")
    assert "reset_trades" in src[i:i + 900]


def test_derived_go_to_the_database_but_not_the_raw_series():
    """
    Сырой секундный ряд не пишется: 4.9 млн строк в день, 35 ГБ за 90 дней при
    20 ГБ свободных. В базу уходят СВОЙСТВА ряда.
    """
    db = (ROOT / "src/db.py").read_text()
    assert "class MicroMinute" in db
    assert "async def merge_micro_minutes" in db
    assert "async def prune_micro_minute" in db
    i = db.index("class MicroMinute")
    assert "35 ГБ" in db[i:i + 1200], "арифметика записана рядом с таблицей"
    main = (ROOT / "main.py").read_text()
    assert "merge_micro_minutes" in main and "prune_micro_minute" in main


def test_swing_keeps_its_sign_on_read():
    """
    Наибольший сдвиг перекоса нужен ЗНАКОВЫМ. 31.07 расстояние до VWAP мерилось
    по модулю, «выше» и «ниже» слились, и вывод получился обратным правильному.
    """
    db = (ROOT / "src/db.py").read_text()
    i = db.index("imb_swing_10s")
    assert "abs(r.imb_d10_max) >= abs(r.imb_d10_min)" in db[i:i + 200]


def test_live_card_shows_the_three_new_things():
    api = (ROOT / "src/api/main.py").read_text()
    for k in ('out["imbalance"]', 'out["liquidity_speed"]', 'out["execution"]'):
        assert k in api, k
    assert "/api/micro/{ticker}" in api
    page = (ROOT / "dashboard/market-watch.html").read_text()
    assert "перекос стакана" in page
    assert "скорость ликвидности" in page
    assert "исполнение относительно лучшей" in page


def test_page_explains_why_seconds_are_not_stored():
    """Ограничение должно быть НА ЭКРАНЕ, а не только в коде."""
    page = (ROOT / "dashboard/market-watch.html").read_text()
    assert "35 ГБ" in page and "не пишется" in page
