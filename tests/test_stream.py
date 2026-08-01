"""
Постоянное соединение: проверяется то, что можно проверить без биржи.

Токена в песочнице нет и быть не должно — он живёт в окружении приложения.
Значит сетевую часть до деплоя проверить нельзя. Тем важнее, что считающая
часть отделена от сети: Aggregator и aggregate_book не знают ни про gRPC, ни
про базу, и проверяются здесь полностью.

Что именно защищают эти тесты:

    двойной счёт   опрос и стрим пишут в одну таблицу flow_minute. Если опрос
                   не отключить при включённом стриме, объём удвоится. Это уже
                   случалось в другой форме: 31.07 накопленная дельта, собранная
                   сложением перекрывающихся снимков, оказалась завышена в разы.

    метки сессий   границы утро/основная/вечер должны совпадать с minute_buckets.
                   Разойдутся — одна минута получит разные метки в двух таблицах.

    размах перекоса  среднее за минуту скрывает разворот. Минута «сначала 80%
                   на покупку, потом 20%» и ровные 50% дают одно среднее.

    битый стакан   биржа помечает пакет is_consistent=false, когда дошли не все
                   заявки. Считать такой пакет настоящим перекосом нельзя.
"""
from datetime import datetime, timezone

import pytest

from src.collector.stream import Aggregator, msk_minute, session_of, quotation


def _utc(h, m, s=0):
    return datetime(2026, 8, 3, h, m, s, tzinfo=timezone.utc)


# ─── время и цена ─────────────────────────────────────────────────────────────

def test_time_is_converted_to_moscow():
    """Биржа отдаёт UTC, вся система живёт по МСК. Две зоны — источник ошибок."""
    assert msk_minute(_utc(6, 59, 30)) == "2026-08-03T09:59"


def test_session_borders_match_minute_buckets():
    """
    Те же границы, что в minute_buckets. Если разойдутся, одна и та же минута
    получит разные метки в flow_minute и book_minute.
    """
    assert session_of("2026-08-03T07:00") == "morning"
    assert session_of("2026-08-03T09:49") == "morning"
    assert session_of("2026-08-03T09:50") == "main"
    assert session_of("2026-08-03T18:59") == "main"
    assert session_of("2026-08-03T19:00") == "evening"


def test_price_is_assembled_from_units_and_nano():
    class Q:
        units, nano = 271, 570000000
    assert quotation(Q()) == pytest.approx(271.57)
    assert quotation(None) == 0.0


# ─── поток сделок ─────────────────────────────────────────────────────────────

def test_trades_land_in_their_own_minutes():
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, 5)
    a.add_trade("SBER", _utc(7, 1, 5), 1, 101.0, 7)
    flow, _ = a.drain()
    assert [r["ts"] for r in flow["SBER"]] == ["2026-08-03T10:00", "2026-08-03T10:01"]


def test_buy_and_sell_are_split_by_exchange_flag():
    """
    Направление берётся у биржи, а не достраивается правилом тика: 31.07 по
    SIBN флаг дал 79.2% покупок, а правило тика 39.9% — расхождение вдвое.
    """
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, 7)     # BUY
    a.add_trade("SBER", _utc(7, 0, 20), 2, 100.0, 3)     # SELL
    r = a.drain()[0]["SBER"][0]
    assert r["buy_volume"] == 7 and r["sell_volume"] == 3


def test_unknown_direction_counts_in_trades_but_not_in_sides():
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 0, 100.0, 9)     # UNSPECIFIED
    r = a.drain()[0]["SBER"][0]
    assert r["trade_count"] == 1
    assert r["buy_volume"] == 0 and r["sell_volume"] == 0


def test_biggest_trade_is_kept():
    """Размер крупнейшей сделки: 1000 одной и десять по 100 — разные события."""
    a = Aggregator()
    for q in (100, 900, 50):
        a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, q)
    assert a.drain()[0]["SBER"][0]["max_trade"] == 900


def test_vwap_numerator_lets_price_be_recovered():
    """Сумма цена*объём, а не готовый VWAP: средние усреднять нельзя."""
    a = Aggregator()
    a.add_trade("X", _utc(7, 0, 10), 1, 100.0, 1)
    a.add_trade("X", _utc(7, 0, 20), 1, 200.0, 3)
    r = a.drain()[0]["X"][0]
    assert r["vwap_num"] == pytest.approx(700.0)
    assert r["vwap_num"] / (r["buy_volume"] + r["sell_volume"]) == pytest.approx(175.0)


def test_zero_quantity_and_empty_ticker_are_ignored():
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, 0)
    a.add_trade("", _utc(7, 0, 10), 1, 100.0, 5)
    assert a.drain() == ({}, {})


# ─── стакан ───────────────────────────────────────────────────────────────────

def test_book_keeps_sums_not_averages():
    """
    Складываются суммы: среднее нельзя усреднить повторно при склейке минут
    в пятиминутки, а сумму сложить можно.
    """
    a = Aggregator()
    a.add_book("SBER", _utc(7, 0, 10), 600, 400, 271.5, 271.6)
    a.add_book("SBER", _utc(7, 0, 40), 200, 800, 271.4, 271.5)
    r = a.drain()[1]["SBER"][0]
    assert r["updates"] == 2
    assert r["bid_vol_sum"] == 800 and r["ask_vol_sum"] == 1200
    assert r["bid_vol_sum"] / (r["bid_vol_sum"] + r["ask_vol_sum"]) == pytest.approx(0.4)


def test_book_records_the_swing_inside_the_minute():
    """
    Главное, чего нет в одном среднем. Минута, где стакан был сначала 80% на
    покупку, а потом 20%, даёт то же среднее, что ровные 50%.
    """
    a = Aggregator()
    a.add_book("SBER", _utc(7, 0, 10), 800, 200, 1.0, 1.1)   # 0.8
    a.add_book("SBER", _utc(7, 0, 50), 200, 800, 1.0, 1.1)   # 0.2
    r = a.drain()[1]["SBER"][0]
    assert r["imb_max"] == pytest.approx(0.8)
    assert r["imb_min"] == pytest.approx(0.2)


def test_last_best_prices_win_inside_the_minute():
    a = Aggregator()
    a.add_book("X", _utc(7, 0, 10), 10, 10, 100.0, 100.5)
    a.add_book("X", _utc(7, 0, 50), 10, 10, 101.0, 101.5)
    r = a.drain()[1]["X"][0]
    assert r["best_bid"] == 101.0 and r["best_ask"] == 101.5


def test_empty_book_is_ignored():
    a = Aggregator()
    a.add_book("X", _utc(7, 0, 10), 0, 0, 0.0, 0.0)
    assert a.drain()[1] == {}


def test_drain_empties_the_buffer():
    """
    Обнуление при выгрузке — то, что делает дедуп ненужным. Поток отдаёт каждый
    пакет один раз, поэтому опасность здесь не задвоение, а дыра при обрыве.
    """
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, 5)
    assert a.drain()[0]
    assert a.drain() == ({}, {})


# ─── склейка стакана в 5m/session ─────────────────────────────────────────────

from src.db import aggregate_book                                # noqa: E402

BOOK_ROWS = [
    {"ts": "2026-08-03T10:00", "session": "main", "updates": 10,
     "bid_vol_sum": 600.0, "ask_vol_sum": 400.0, "spread_sum": 1.0,
     "best_bid": 100.0, "best_ask": 100.1, "imb_min": 0.55, "imb_max": 0.65},
    {"ts": "2026-08-03T10:01", "session": "main", "updates": 10,
     "bid_vol_sum": 400.0, "ask_vol_sum": 600.0, "spread_sum": 1.0,
     "best_bid": 100.2, "best_ask": 100.3, "imb_min": 0.35, "imb_max": 0.45},
]


def test_book_share_is_computed_from_sums_on_read():
    r = aggregate_book(BOOK_ROWS, 1, "SBER")
    assert r[0]["bid_share"] == pytest.approx(0.6)
    assert r[1]["bid_share"] == pytest.approx(0.4)


def test_book_glues_into_five_minute_bar():
    r = aggregate_book(BOOK_ROWS, 5, "SBER")
    assert len(r) == 1
    assert r[0]["updates"] == 20
    assert r[0]["bid_share"] == pytest.approx(0.5), "1000 на 1000"


def test_flip_across_the_middle_is_flagged():
    """Склеенный бар был и выше, и ниже половины — это разворот, а не покой."""
    r = aggregate_book(BOOK_ROWS, 5, "SBER")
    assert r[0]["flipped"] is True
    assert aggregate_book(BOOK_ROWS[:1], 5, "SBER")[0]["flipped"] is False


def test_average_spread_uses_update_count():
    r = aggregate_book(BOOK_ROWS, 5, "SBER")
    assert r[0]["avg_spread"] == pytest.approx(2.0 / 20)


def test_empty_input_gives_empty_output():
    assert aggregate_book([], 1) == []


# ─── защита от двойного счёта и от тихой поломки ──────────────────────────────

from pathlib import Path                                          # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_polling_does_not_write_flow_when_stream_is_on():
    """
    Регрессия. Опрос и стрим складывают объём в ОДНУ минуту таблицы flow_minute.
    Если оба пишут — объём удвоится, и это не будет видно: цифры останутся
    правдоподобными.
    """
    src = (ROOT / "main.py").read_text()
    assert "if not STREAM_ENABLED:" in src, "опрос обязан молчать при стриме"
    i_guard = src.index("if not STREAM_ENABLED:")
    i_write = src.index("merge_flow_minutes", i_guard)
    between = src[i_guard:i_write]
    assert between.count("\n") < 12, "запись потока должна быть ВНУТРИ проверки"


def test_stream_subscribes_only_to_exchange_trades():
    """
    Дилерские сделки не проходят через стакан и завышают поток. В REST этого
    разделения у нас не было вовсе.
    """
    src = (ROOT / "src/collector/stream.py").read_text()
    assert "TRADE_SOURCE_EXCHANGE" in src


def test_inconsistent_orderbook_is_skipped():
    """Биржа сама говорит, что дошли не все заявки."""
    src = (ROOT / "src/collector/stream.py").read_text()
    assert "is_consistent" in src


def test_ticker_comes_from_the_message_not_from_a_table():
    """
    Рукописной таблицы FIGI здесь быть не должно: 30.07 в прежней 22 записи из
    43 указывали на чужие инструменты, и подмена была тихой.
    """
    src = (ROOT / "src/collector/stream.py").read_text()
    assert "t.ticker" in src and "ob.ticker" in src


def test_grpc_versions_are_pinned_as_a_pair():
    """
    Сгенерированный код требует runtime protobuf не ниже того, которым
    сгенерирован. Разъедутся версии — приложение упадёт при импорте.
    """
    req = (ROOT / "requirements.txt").read_text()
    assert "grpcio==" in req and "protobuf==" in req
    assert "googleapis-common-protos==" in req


def test_stream_is_off_by_default():
    """
    На этих данных торгуют реальными деньгами, а сетевую часть нельзя проверить
    до деплоя. Включение — отдельное осознанное действие.
    """
    src = (ROOT / "config/settings.py").read_text()
    i = src.index("STREAM_ENABLED")
    assert '"false"' in src[i:i + 120]


def test_health_endpoint_exists():
    src = (ROOT / "src/api/main.py").read_text()
    assert "/api/stream/health" in src
    assert "/api/book/{ticker}" in src


# ─── сборка настоящих запросов, без сети ──────────────────────────────────────
#
# Токена в песочнице нет, соединение проверить нельзя. Но запрос подписки
# собирается из тех же контрактов, что уйдут на биржу, и его можно построить и
# сериализовать здесь. Это ловит ошибку в имени поля или enum до деплоя —
# иначе она вылезла бы в понедельник на открытии.

pb = pytest.importorskip("src.collector.tinkoff_pb.marketdata_pb2",
                         reason="protobuf не установлен")


def test_hardcoded_direction_numbers_match_the_contract():
    """
    В Aggregator направление сравнивается с числами 1 и 2, а не с именами:
    иначе пришлось бы тащить protobuf в чистый класс. Значит числа надо
    сторожить — молчаливое расхождение перепутает покупки с продажами.
    """
    assert pb.TRADE_DIRECTION_BUY == 1
    assert pb.TRADE_DIRECTION_SELL == 2


def test_subscription_requests_are_built_and_serialize():
    from src.collector.stream import MarketStream
    s = MarketStream("нет-токена",
                     {"SBER": "BBG004730N88", "MVID": "BBG004S68CP5"}, depth=20)
    reqs = list(s._subscribe_requests())
    assert len(reqs) == 2, "две подписки: сделки и стаканы"

    kinds = [r.WhichOneof("payload") for r in reqs]
    assert kinds == ["subscribe_trades_request", "subscribe_order_book_request"]

    tr = reqs[0].subscribe_trades_request
    assert len(tr.instruments) == 2
    assert tr.subscription_action == pb.SUBSCRIPTION_ACTION_SUBSCRIBE
    assert tr.trade_source == pb.TRADE_SOURCE_EXCHANGE, "дилерские сделки не берём"

    ob = reqs[1].subscribe_order_book_request
    assert all(i.depth == 20 for i in ob.instruments)

    for r in reqs:
        assert r.SerializeToString(), "запрос должен сериализоваться"


def test_all_instruments_go_in_one_request_per_type():
    """
    Лимит — 100 запросов на подписку в минуту. Инструменты передаются списком,
    а не по одному: 80 бумаг по отдельности сожгли бы лимит на первой минуте.
    """
    from src.collector.stream import MarketStream
    figis = {f"T{i:02d}": f"FIGI{i:08d}" for i in range(80)}
    reqs = list(MarketStream("x", figis)._subscribe_requests())
    assert len(reqs) == 2
    assert len(reqs[0].subscribe_trades_request.instruments) == 80


def test_broken_task_takes_the_others_down_with_it():
    """
    Регрессия на мой собственный дефект. Первая версия вызывала asyncio.gather
    и полагалась на то, что он погасит соседей. Он их НЕ гасит: отдаёт первое
    исключение сразу, а остальные задачи продолжают жить. При срабатывании
    сторожа тишины внешний цикл открыл бы ВТОРОЕ соединение поверх живого
    первого, и с каждым обрывом их становилось бы больше.

    В данных это выглядело бы как задвоение объёма — то есть как правдоподобные
    цифры, а не как ошибка.
    """
    import asyncio as aio
    from src.collector.stream import MarketStream

    s = MarketStream("x", {"SBER": "FIGI"})
    cancelled = []

    async def boom():
        await aio.sleep(0.01)
        raise RuntimeError("обрыв")

    def survivor(name):
        async def _c():
            try:
                await aio.sleep(10)
            except aio.CancelledError:
                cancelled.append(name)
                raise
        return _c

    s._session, s._flusher, s._watchdog = boom, survivor("flush"), survivor("watch")

    # Цикл создаётся и возвращается на место ЯВНО. Первая версия делала
    # new_event_loop().run_until_complete(...) и бросала цикл открытым — от
    # этого падали четыре теста в других файлах. Ровно та же болезнь, что и в
    # прошлый раз с importlib.reload(src.db): удобство в тесте, поломка у соседа.
    try:
        prev = aio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        prev = None
    loop = aio.new_event_loop()
    aio.set_event_loop(loop)
    try:
        with pytest.raises(RuntimeError, match="обрыв"):
            loop.run_until_complete(s._run_once())
    finally:
        loop.close()
        aio.set_event_loop(prev)
    assert sorted(cancelled) == ["flush", "watch"], "соседи должны быть погашены"


def test_subscription_count_fits_one_connection():
    """160 подписок на 80 бумаг против лимита в 300 на соединение."""
    from src.collector.stream import MarketStream
    figis = {f"T{i:02d}": f"FIGI{i:08d}" for i in range(80)}
    reqs = list(MarketStream("x", figis)._subscribe_requests())
    total = sum(len(getattr(r, r.WhichOneof("payload")).instruments) for r in reqs)
    assert total == 160 and total <= 300
