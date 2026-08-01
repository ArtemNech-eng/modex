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
    flow, _, _ = a.drain()
    assert [r["ts"] for r in flow["exchange"]["SBER"]] == ["2026-08-03T10:00", "2026-08-03T10:01"]


def test_buy_and_sell_are_split_by_exchange_flag():
    """
    Направление берётся у биржи, а не достраивается правилом тика: 31.07 по
    SIBN флаг дал 79.2% покупок, а правило тика 39.9% — расхождение вдвое.
    """
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, 7)     # BUY
    a.add_trade("SBER", _utc(7, 0, 20), 2, 100.0, 3)     # SELL
    r = a.drain()[0]["exchange"]["SBER"][0]
    assert r["buy_volume"] == 7 and r["sell_volume"] == 3


def test_unknown_direction_counts_in_trades_but_not_in_sides():
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 0, 100.0, 9)     # UNSPECIFIED
    r = a.drain()[0]["exchange"]["SBER"][0]
    assert r["trade_count"] == 1
    assert r["buy_volume"] == 0 and r["sell_volume"] == 0


def test_biggest_trade_is_kept():
    """Размер крупнейшей сделки: 1000 одной и десять по 100 — разные события."""
    a = Aggregator()
    for q in (100, 900, 50):
        a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, q)
    assert a.drain()[0]["exchange"]["SBER"][0]["max_trade"] == 900


def test_vwap_numerator_lets_price_be_recovered():
    """Сумма цена*объём, а не готовый VWAP: средние усреднять нельзя."""
    a = Aggregator()
    a.add_trade("X", _utc(7, 0, 10), 1, 100.0, 1)
    a.add_trade("X", _utc(7, 0, 20), 1, 200.0, 3)
    r = a.drain()[0]["exchange"]["X"][0]
    assert r["vwap_num"] == pytest.approx(700.0)
    assert r["vwap_num"] / (r["buy_volume"] + r["sell_volume"]) == pytest.approx(175.0)


def test_zero_quantity_and_empty_ticker_are_ignored():
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, 0)
    a.add_trade("", _utc(7, 0, 10), 1, 100.0, 5)
    assert a.drain() == ({}, {}, {})


# ─── стакан ───────────────────────────────────────────────────────────────────

def test_book_keeps_sums_not_averages():
    """
    Складываются суммы: среднее нельзя усреднить повторно при склейке минут
    в пятиминутки, а сумму сложить можно.
    """
    a = Aggregator()
    a.add_book("SBER", _utc(7, 0, 10), 600, 400, 271.5, 271.6)
    a.add_book("SBER", _utc(7, 0, 40), 200, 800, 271.4, 271.5)
    r = a.drain()[1]["exchange"]["SBER"][0]
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
    r = a.drain()[1]["exchange"]["SBER"][0]
    assert r["imb_max"] == pytest.approx(0.8)
    assert r["imb_min"] == pytest.approx(0.2)


def test_last_best_prices_win_inside_the_minute():
    a = Aggregator()
    a.add_book("X", _utc(7, 0, 10), 10, 10, 100.0, 100.5)
    a.add_book("X", _utc(7, 0, 50), 10, 10, 101.0, 101.5)
    r = a.drain()[1]["exchange"]["X"][0]
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
    assert a.drain() == ({}, {}, {})


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
# Quotation живёт в common.proto, а не в marketdata.proto.
common = pytest.importorskip("src.collector.tinkoff_pb.common_pb2")


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
    assert len(reqs) == 3, "три подписки: сделки, стаканы, свечи"

    kinds = [r.WhichOneof("payload") for r in reqs]
    assert kinds == ["subscribe_trades_request", "subscribe_order_book_request",
                     "subscribe_candles_request"]

    tr = reqs[0].subscribe_trades_request
    assert len(tr.instruments) == 2
    assert tr.subscription_action == pb.SUBSCRIPTION_ACTION_SUBSCRIBE
    assert tr.trade_source == pb.TRADE_SOURCE_UNSPECIFIED, \
        "источник не задаём: явный EXCHANGE совпал с нулём сделок, фильтр перенесён к нам"

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
    assert len(reqs) == 3
    assert len(reqs[0].subscribe_trades_request.instruments) == 80


# ─── разбор ответа на подписку ────────────────────────────────────────────────
#
# Живой прогон 01.08: за семь минут пришло 4991 сообщение стакана и НОЛЬ сделок,
# при том что опрос REST в те же минуты видел сделки по FEES, TRMK, LSRG.
# Причин было две, и обе прятали сами себя.

def test_subscription_field_names_match_the_contract():
    """
    Список статусов называется по-разному у каждого типа подписки. Я читал
    общее «subscriptions», которого нет ни у одного: обработчик падал на
    AttributeError, а падение уходило в debug-лог.
    """
    from src.collector.stream import SUB_FIELD
    for resp_name, field in SUB_FIELD.items():
        cls = "".join(p.capitalize() for p in resp_name.split("_"))
        cls = cls.replace("Orderbook", "OrderBook").replace("Lastprice", "LastPrice")
        msg = getattr(pb, cls, None)
        assert msg is not None, f"нет сообщения {cls}"
        assert field in [f.name for f in msg.DESCRIPTOR.fields], \
            f"{cls} не имеет поля {field}"


def test_status_is_compared_as_enum_not_as_string():
    """
    Вторая ошибка: «SUCCESS» in str(enum). Enum это число, str(1) даёт «1»,
    проверка всегда ложна — стрим сообщал «подписано 0» при живом стакане.
    """
    assert "SUCCESS" not in str(pb.SUBSCRIPTION_STATUS_SUCCESS), \
        "именно поэтому строковое сравнение и не работало"
    src = (ROOT / "src/collector/stream.py").read_text()
    assert 'SubscriptionStatus.Name' in src, "статус берём именем, а не строкой"


def test_subscription_statuses_are_counted_by_name():
    from src.collector.stream import MarketStream
    s = MarketStream("x", {"SBER": "F1", "MVID": "F2", "DATA": "F3"})
    resp = pb.MarketDataResponse(
        subscribe_trades_response=pb.SubscribeTradesResponse(
            trade_subscriptions=[
                pb.TradeSubscription(figi="F1",
                                     subscription_status=pb.SUBSCRIPTION_STATUS_SUCCESS),
                pb.TradeSubscription(figi="F2",
                                     subscription_status=pb.SUBSCRIPTION_STATUS_SUCCESS),
                pb.TradeSubscription(figi="F3",
                                     subscription_status=pb.SUBSCRIPTION_STATUS_SOURCE_IS_INVALID),
            ]))
    s._handle(resp)
    got = s.stats["subscriptions"]["subscribe_trades_response"]
    assert got == {"SUBSCRIPTION_STATUS_SUCCESS": 2,
                   "SUBSCRIPTION_STATUS_SOURCE_IS_INVALID": 1}
    assert s.stats["subscribed"] == 2
    assert s.stats["handler_errors"] == 0, "разбор не должен падать"


def test_rejection_is_visible_in_health_without_reading_logs():
    """Отказ должен быть виден показателем, а не строчкой в debug-логе."""
    from src.collector.stream import MarketStream
    s = MarketStream("x", {"SBER": "F1"})
    s._handle(pb.MarketDataResponse(
        subscribe_trades_response=pb.SubscribeTradesResponse(
            trade_subscriptions=[pb.TradeSubscription(
                figi="F1",
                subscription_status=pb.SUBSCRIPTION_STATUS_SOURCE_IS_INVALID)])))
    h = s.health()
    assert h["subscribed"] == 0
    assert "SUBSCRIPTION_STATUS_SOURCE_IS_INVALID" in str(h["subscriptions"])


def test_trade_source_is_not_set_in_the_request():
    """
    Регрессия. Явный TRADE_SOURCE_EXCHANGE в подписке совпал с нулём сделок за
    семь минут. По контракту умолчание — TRADE_SOURCE_ALL; дилерские отсекаем
    у себя, где это видно и измеримо.
    """
    from src.collector.stream import MarketStream
    req = list(MarketStream("x", {"SBER": "F1"})._subscribe_requests())[0]
    assert req.subscribe_trades_request.trade_source == pb.TRADE_SOURCE_UNSPECIFIED


def test_dealer_trades_go_to_their_own_bucket():
    """
    Дилерские сделки не смешиваются с биржевыми, но и НЕ выбрасываются:
    01.08 в них оказались перекосы до 98% по 57 бумагам, и проверить, значит ли
    это что-нибудь, можно только на накопленных данных.
    """
    from src.collector.stream import MarketStream, TRADE_SOURCE_DEALER
    assert TRADE_SOURCE_DEALER == pb.TRADE_SOURCE_DEALER, "число разошлось с контрактом"

    s = MarketStream("x", {"SBER": "F1"})
    for source, qty in ((pb.TRADE_SOURCE_EXCHANGE, 10), (pb.TRADE_SOURCE_DEALER, 999)):
        t = pb.Trade(ticker="SBER", direction=pb.TRADE_DIRECTION_BUY,
                     price=common.Quotation(units=100, nano=0), quantity=qty,
                     trade_source=source)
        t.time.FromSeconds(1785000000)
        s._handle(pb.MarketDataResponse(trade=t))
    assert s.stats["trades"] == 1 and s.stats["trades_dealer"] == 1
    flow = s.agg.drain()[0]
    assert flow["exchange"]["SBER"][0]["buy_volume"] == 10
    assert flow["dealer"]["SBER"][0]["buy_volume"] == 999, "дилерская сохранена отдельно"


def test_dealer_orderbook_is_separated_too():
    """
    Тот же изъян, что у сделок, только в стакане: умолчание подписки —
    ORDERBOOK_TYPE_ALL, «биржевой И дилера вместе». Дилерский стакан это
    котировки брокера, а не место, где формируется цена.
    """
    from src.collector.stream import MarketStream, ORDERBOOK_TYPE_DEALER
    assert ORDERBOOK_TYPE_DEALER == pb.ORDERBOOK_TYPE_DEALER

    s = MarketStream("x", {"SBER": "F1"})
    for typ, qty in ((pb.ORDERBOOK_TYPE_EXCHANGE, 10), (pb.ORDERBOOK_TYPE_DEALER, 999)):
        ob = pb.OrderBook(ticker="SBER", depth=20, is_consistent=True,
                          order_book_type=typ,
                          bids=[pb.Order(price=common.Quotation(units=100, nano=0),
                                         quantity=qty)],
                          asks=[pb.Order(price=common.Quotation(units=101, nano=0),
                                         quantity=qty)])
        ob.time.FromSeconds(1785000000)
        s._handle(pb.MarketDataResponse(orderbook=ob))
    assert s.stats["books"] == 1 and s.stats["books_dealer"] == 1
    book = s.agg.drain()[1]
    assert book["exchange"]["SBER"][0]["bid_vol_sum"] == 10
    assert book["dealer"]["SBER"][0]["bid_vol_sum"] == 999, "дилерский отдельно"


def test_incoming_source_mix_is_visible():
    """
    Состав входящего видно, а не предполагается. Я дважды ошибся на догадках:
    что явный EXCHANGE в подписке безопасен и что стакан приходит только
    биржевой. Суббота 01.08 показала 102 сделки, все дилерские, при закрытой
    бирже — такое должно быть видно показателем.
    """
    from src.collector.stream import MarketStream
    s = MarketStream("x", {"SBER": "F1"})
    for src in (pb.TRADE_SOURCE_DEALER, pb.TRADE_SOURCE_DEALER,
                pb.TRADE_SOURCE_EXCHANGE):
        t = pb.Trade(ticker="SBER", direction=pb.TRADE_DIRECTION_BUY, quantity=1,
                     price=common.Quotation(units=1, nano=0), trade_source=src)
        t.time.FromSeconds(1785000000)
        s._handle(pb.MarketDataResponse(trade=t))
    assert s.health()["sources"]["trade_source"] == {
        "TRADE_SOURCE_DEALER": 2, "TRADE_SOURCE_EXCHANGE": 1}


# ─── колонка источника: хранить оба, по умолчанию отдавать биржевой ───────────

def test_key_includes_source_so_two_sources_never_collide():
    """
    Ключ строки — минута, тикер И источник. Без источника в ключе биржевая и
    дилерская минуты писались бы в ОДНУ строку и складывались: ровно то
    смешивание, ради устранения которого всё и делается.
    """
    src = (ROOT / "src/db.py").read_text()
    assert src.count('{ticker.upper()}:{source}') == 2, "ключ в обеих таблицах"


def test_historical_rows_are_labelled_mixed_not_exchange():
    """
    Строки до 01.08 собраны без различения источника — в них биржевые и
    дилерские сделки в НЕИЗВЕСТНОЙ пропорции. Пометить их 'exchange' значило бы
    соврать: анализ по умолчанию взял бы загрязнённое как чистое.
    """
    src = (ROOT / "src/db.py").read_text()
    for table in ("flow_minute", "book_minute"):
        i = src.index(f'"{table}": {{"source"')
        assert "'mixed'" in src[i:i + 90], f"{table}: историю метим mixed"


def test_migration_covers_the_new_tables():
    """
    create_all создаёт таблицы, но НЕ добавляет колонки в существующие. Обе
    таблицы уже живут в базе, значит колонка добавляется миграцией — иначе
    приложение упадёт на первом же запросе.
    """
    src = (ROOT / "src/db.py").read_text()
    i = src.index("\n_ADDED_COLUMNS = {")     # \n, иначе совпадёт _PREDICTION_ADDED_COLUMNS
    block = src[i:i + 400]
    assert '"flow_minute"' in block and '"book_minute"' in block
    assert "for table, cols in _ADDED_COLUMNS.items()" in src, "миграция по всем таблицам"


def test_reads_default_to_exchange():
    """Забыть указать источник должно быть безопасно: молча вернётся биржевой."""
    src = (ROOT / "src/db.py").read_text()
    assert 'source: str = "dealer"' not in src, "умолчание нигде не дилерское"
    assert src.count('source: str = "exchange"') >= 4, \
        "записи и чтения — везде умолчание биржевое"
    api = (ROOT / "src/api/main.py").read_text()
    assert 'source: str = "dealer"' not in api
    assert api.count('source: str = "exchange"') >= 2


def test_endpoints_reject_unknown_source():
    """Опечатка в источнике не должна молча отдавать пустой список."""
    api = (ROOT / "src/api/main.py").read_text()
    assert 'FLOW_SOURCES = ("exchange", "dealer", "mixed", "all")' in api
    assert api.count("if source not in FLOW_SOURCES:") >= 2, \
        "каждый маршрут с параметром source обязан его проверять"


def test_dealer_data_is_stored_not_dropped():
    """
    Регрессия на мою же правку. Первая версия дилерские данные считала и
    ВЫБРАСЫВАЛА. Проверить, значит ли что-нибудь выходное позиционирование
    розницы, можно только на накопленных данных — выброшенное не накопится.
    """
    stream = (ROOT / "src/collector/stream.py").read_text()
    assert 'source=src' in stream, "источник передаётся в накопитель"
    assert 'self.stats["trades_dealer"] += 1\n                return' not in stream, \
        "дилерские больше не отбрасываются"
    main = (ROOT / "main.py").read_text()
    assert "merge_flow_minutes(tk, rows, None, source=src," in main
    assert "merge_book_minutes(tk, rows, source=src," in main


# ─── структура уровней: плита против ровной раскладки ─────────────────────────
#
# Сумма по 20 уровням не отличает одну большую заявку от размазанного объёма, а
# для вопроса «кто давит цену» это разные картины. Уровни целиком не храним —
# 20 цен по десять раз в секунду на 80 бумаг это уже не минутная таблица.

def test_wall_and_spread_out_book_look_different():
    """Одинаковый общий объём, разная структура — и это должно быть видно."""
    a = Aggregator()
    # плита: 1000 в одной заявке, ещё 1000 по мелочи
    a.add_book("WALL", _utc(7, 0, 10), 2000, 2000, 1.0, 1.1,
               bid5=1200, ask5=400, bid_top=1000, ask_top=120)
    # ровно: тот же объём, ни одной крупной
    a.add_book("FLAT", _utc(7, 0, 10), 2000, 2000, 1.0, 1.1,
               bid5=500, ask5=400, bid_top=120, ask_top=120)
    book = a.drain()[1]["exchange"]
    assert book["WALL"][0]["bid_top_max"] == 1000
    assert book["FLAT"][0]["bid_top_max"] == 120
    assert book["WALL"][0]["bid5_sum"] > book["FLAT"][0]["bid5_sum"]


def test_biggest_order_of_the_minute_is_the_max_not_the_average():
    """
    Важно, что плита БЫЛА. Усреднение размажет её по минуте и спрячет: три
    пакета по 100 и один на 900 дадут среднее 300, то есть ничего особенного.
    """
    a = Aggregator()
    for top in (100, 900, 100, 100):
        a.add_book("X", _utc(7, 0, 10), 1000, 1000, 1.0, 1.1, bid_top=top)
    assert a.drain()[1]["exchange"]["X"][0]["bid_top_max"] == 900


def test_levels_are_sorted_before_taking_the_best_five():
    """
    Регрессия на молчаливое допущение. bids[0] как лучшая цена сходится на
    живых данных, но «пять лучших» опирается на порядок ВСЕГО списка. Такие
    допущения тут уже выходили боком — сортируем явно.
    """
    from src.collector.stream import MarketStream
    s = MarketStream("x", {"SBER": "F1"})
    # уровни НАМЕРЕННО перемешаны, лучшая цена в середине
    ob = pb.OrderBook(
        ticker="SBER", depth=20, is_consistent=True,
        order_book_type=pb.ORDERBOOK_TYPE_EXCHANGE,
        bids=[pb.Order(price=common.Quotation(units=99, nano=0), quantity=7),
              pb.Order(price=common.Quotation(units=101, nano=0), quantity=500),
              pb.Order(price=common.Quotation(units=100, nano=0), quantity=3)],
        asks=[pb.Order(price=common.Quotation(units=105, nano=0), quantity=9),
              pb.Order(price=common.Quotation(units=102, nano=0), quantity=400)])
    ob.time.FromSeconds(1785000000)
    s._handle(pb.MarketDataResponse(orderbook=ob))
    r = s.agg.drain()[1]["exchange"]["SBER"][0]
    assert r["best_bid"] == 101.0, "лучший бид — САМАЯ ВЫСОКАЯ цена покупки"
    assert r["best_ask"] == 102.0, "лучший аск — САМАЯ НИЗКАЯ цена продажи"
    assert r["bid_top_max"] == 500 and r["ask_top_max"] == 400


def test_near_share_shows_where_the_volume_sits():
    """
    Доля объёма в пяти лучших уровнях. Близко к 1 — заявки прижаты к цене,
    близко к 0 — размазаны вглубь и цену не держат.
    """
    rows = [{"ts": "2026-08-03T10:00", "session": "main", "updates": 2,
             "bid_vol_sum": 1000.0, "ask_vol_sum": 1000.0, "spread_sum": 0.2,
             "best_bid": 100.0, "best_ask": 100.1, "imb_min": 0.5,
             "imb_max": 0.5, "bid5_sum": 900.0, "ask5_sum": 100.0,
             "bid_top_max": 400, "ask_top_max": 50}]
    r = aggregate_book(rows, 1, "X")[0]
    assert r["bid_near_share"] == pytest.approx(0.9), "покупатели у самой цены"
    assert r["ask_near_share"] == pytest.approx(0.1), "продавцы размазаны вглубь"
    assert r["bid_top"] == 400 and r["ask_top"] == 50


def test_glue_sums_five_levels_and_maxes_the_wall():
    """При склейке минут пятёрка складывается, а плита берётся максимумом."""
    base = {"ts": "2026-08-03T10:00", "session": "main", "updates": 1,
            "bid_vol_sum": 100.0, "ask_vol_sum": 100.0, "spread_sum": 0.1,
            "best_bid": 1.0, "best_ask": 1.1, "imb_min": 0.5, "imb_max": 0.5,
            "bid5_sum": 40.0, "ask5_sum": 40.0, "bid_top_max": 30,
            "ask_top_max": 10}
    second = {**base, "ts": "2026-08-03T10:01", "bid_top_max": 90}
    r = aggregate_book([base, second], 5, "X")[0]
    assert r["bid_near_share"] == pytest.approx(80.0 / 200.0)
    assert r["bid_top"] == 90, "плита из второй минуты не должна потеряться"


def test_level_columns_are_migrated():
    """Таблица уже существует в базе, значит колонки добавляются миграцией."""
    src = (ROOT / "src/db.py").read_text()
    i = src.index('"book_minute": {')
    block = src[i:i + 420]
    for col in ("bid5_sum", "ask5_sum", "bid_top_max", "ask_top_max"):
        assert col in block, f"{col} не мигрируется"


# ─── свечи: объём накопительный, его нельзя складывать ────────────────────────
#
# Без свечей минутного бара у нас не было вовсе: поток сделок даёт VWAP и объём,
# но не open/high/low/close. За OHLC система ходила в REST, а ISS отдаёт минутки
# с задержкой около 15 минут — для интрадея бесполезно.

def test_candle_volume_is_replaced_not_summed():
    """
    САМАЯ ОПАСНАЯ ЛОВУШКА. Биржа присылает свечу многократно по ходу минуты, и в
    каждой версии volume уже включает всё предыдущее. Сложение версий завысило бы
    объём в разы — и выглядело бы правдоподобно, как уже было с накопленной
    дельтой 31.07.
    """
    a = Aggregator()
    for vol in (100, 250, 400):            # одна и та же минута, объём растёт
        a.add_candle("SBER", _utc(7, 0, 10), 100.0, 101.0, 99.0, 100.5,
                     volume=vol, vol_buy=vol // 2, vol_sell=vol // 2)
    r = a.drain()[2]["SBER"][0]
    assert r["volume"] == 400, "последняя версия, а не 100+250+400"
    assert r["updates"] == 3, "число версий при этом считается"


def test_candle_borders_expand_and_close_is_last():
    """
    high и low берутся крайними: пакеты могут прийти не по порядку, а
    «корректирующая» свеча приходит уже после закрытия интервала.
    """
    a = Aggregator()
    a.add_candle("X", _utc(7, 0, 10), 100.0, 101.0, 99.5, 100.5, 10, 5, 5)
    a.add_candle("X", _utc(7, 0, 40), 100.0, 103.0, 98.0, 102.0, 20, 12, 8)
    a.add_candle("X", _utc(7, 0, 50), 100.0, 102.0, 99.0, 101.5, 25, 15, 10)
    r = a.drain()[2]["X"][0]
    assert r["open"] == 100.0, "открытие не меняется"
    assert r["high"] == 103.0 and r["low"] == 98.0, "границы крайние"
    assert r["close"] == 101.5, "закрытие — последнее"


def test_candle_carries_exchange_side_split():
    """
    volume_buy и volume_sell приходят от биржи В САМОЙ СВЕЧЕ. Это независимая
    сверка нашего разбора направлений: заметное расхождение означает ошибку в
    одном из двух расчётов.
    """
    a = Aggregator()
    a.add_candle("X", _utc(7, 0, 10), 1.0, 1.0, 1.0, 1.0, 100, 70, 30)
    r = a.drain()[2]["X"][0]
    assert r["volume_buy"] == 70 and r["volume_sell"] == 30


from src.db import aggregate_candles                              # noqa: E402

CANDLES = [
    {"ts": "2026-08-03T10:00", "session": "main", "open": 100.0, "high": 101.0,
     "low": 99.0, "close": 100.5, "volume": 500, "volume_buy": 300,
     "volume_sell": 200},
    {"ts": "2026-08-03T10:01", "session": "main", "open": 100.5, "high": 103.0,
     "low": 100.0, "close": 102.0, "volume": 700, "volume_buy": 500,
     "volume_sell": 200},
]


def test_glued_bar_takes_first_open_and_last_close():
    r = aggregate_candles(CANDLES, 5, "X")
    assert len(r) == 1
    assert r[0]["open"] == 100.0, "открытие ПЕРВОЙ минуты"
    assert r[0]["close"] == 102.0, "закрытие ПОСЛЕДНЕЙ"
    assert r[0]["high"] == 103.0 and r[0]["low"] == 99.0


def test_volume_across_minutes_is_summed():
    """
    Внутри одной минуты объём накопительный, а РАЗНЫЕ минуты не пересекаются —
    там складывать и нужно. Легко перепутать одно с другим.
    """
    r = aggregate_candles(CANDLES, 5, "X")[0]
    assert r["volume"] == 1200
    assert r["volume_buy"] == 800 and r["volume_sell"] == 400
    assert r["buy_ratio"] == pytest.approx(800 / 1200, abs=1e-4)   # округляем до 4 знаков


def test_range_and_change_are_derived_on_read():
    r = aggregate_candles(CANDLES, 5, "X")[0]
    assert r["range"] == pytest.approx(4.0)     # 103 - 99
    assert r["change"] == pytest.approx(2.0)    # 102 - 100


# ─── живое состояние: минуя базу ──────────────────────────────────────────────

def test_snapshot_does_not_empty_the_buffer():
    """
    Сброс в базу идёт раз в 20 секунд, и до него минута существует только в
    памяти. Живой запрос обязан читать, НЕ забирая: иначе он украл бы данные у
    записи, и минута не попала бы в базу вовсе.
    """
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, 5)
    a.add_book("SBER", _utc(7, 0, 10), 600, 400, 100.0, 100.1)
    a.add_candle("SBER", _utc(7, 0, 10), 100.0, 101.0, 99.0, 100.5, 50, 30, 20)

    snap = a.snapshot("SBER")
    assert snap["flow"]["exchange"]["buy_volume"] == 5
    assert snap["book"]["exchange"]["bid_vol_sum"] == 600
    assert snap["candle"]["close"] == 100.5

    flow, book, candle = a.drain()
    assert flow and book and candle, "после снимка данные ОСТАЛИСЬ для записи"


def test_snapshot_is_a_copy_not_a_reference():
    """Правка ответа не должна менять накопитель."""
    a = Aggregator()
    a.add_trade("X", _utc(7, 0, 10), 1, 100.0, 5)
    snap = a.snapshot("X")
    snap["flow"]["exchange"]["buy_volume"] = 999999
    assert a.drain()[0]["exchange"]["X"][0]["buy_volume"] == 5


def test_snapshot_filters_by_ticker():
    a = Aggregator()
    a.add_trade("SBER", _utc(7, 0, 10), 1, 100.0, 5)
    a.add_trade("GAZP", _utc(7, 0, 10), 1, 100.0, 7)
    assert a.snapshot("SBER")["flow"]["exchange"]["buy_volume"] == 5
    assert a.snapshot("MVID") == {"flow": {}, "book": {}, "candle": None}


def test_candle_and_live_endpoints_exist():
    src = (ROOT / "src/api/main.py").read_text()
    assert "/api/candles/{ticker}" in src
    assert "/api/live/{ticker}" in src
    main = (ROOT / "main.py").read_text()
    assert "merge_candle_minutes" in main, "свечи должны писаться при сбросе"
    assert "prune_candle_minute" in main, "и чиститься по сроку"


# ─── задвоение при перекате деплоя ────────────────────────────────────────────

from src.db import pick_fullest                                   # noqa: E402


def test_two_containers_are_not_summed():
    """
    01.08 при перекате Coolify держал два контейнера, у каждого свой стрим, и оба
    писали в ОДНУ строку со складывающим слиянием. По SBER за 14:03 сумма дала
    736 лотов против настоящих 468 — завышение в полтора раза и с виду
    совершенно правдоподобное.
    """
    rows = [
        {"ts": "2026-08-01T14:03", "trade_count": 40, "buy_volume": 248},
        {"ts": "2026-08-01T14:03", "trade_count": 40, "buy_volume": 248},
    ]
    out = pick_fullest(rows, "trade_count")
    assert len(out) == 1, "одна минута — одно наблюдение"
    assert out[0]["buy_volume"] == 248, "НЕ 496"


def test_the_fuller_observation_wins():
    """
    Контейнер, поднявшийся посреди минуты, видел меньше. Берём того, у кого
    событий больше — он прожил минуту целиком.
    """
    rows = [
        {"ts": "2026-08-01T14:03", "trade_count": 12, "buy_volume": 100},
        {"ts": "2026-08-01T14:03", "trade_count": 40, "buy_volume": 248},
    ]
    assert pick_fullest(rows, "trade_count")[0]["buy_volume"] == 248


def test_different_minutes_all_survive():
    """Отсечка работает ВНУТРИ минуты, а не между минутами."""
    rows = [{"ts": f"2026-08-01T14:0{i}", "trade_count": i} for i in range(1, 5)]
    assert len(pick_fullest(rows, "trade_count")) == 4


def test_result_is_ordered_by_time():
    """Дальше идёт склейка в бары, ей нужен порядок."""
    rows = [{"ts": "2026-08-01T14:05", "updates": 1},
            {"ts": "2026-08-01T14:03", "updates": 1},
            {"ts": "2026-08-01T14:04", "updates": 1}]
    assert [r["ts"][11:] for r in pick_fullest(rows, "updates")] == \
        ["14:03", "14:04", "14:05"]


def test_instance_goes_into_the_key():
    """
    Без метки экземпляра в ключе два контейнера снова попадут в одну строку.
    """
    src = (ROOT / "src/db.py").read_text()
    assert src.count('key += f":{instance}"') == 2, "поток и стакан"
    stream = (ROOT / "src/collector/stream.py").read_text()
    assert "self.instance = uuid.uuid4().hex[:6]" in stream
    main = (ROOT / "main.py").read_text()
    assert main.count("instance=stream.instance") == 2


def test_reads_pick_fullest_not_sum():
    src = (ROOT / "src/db.py").read_text()
    assert 'pick_fullest(plain, "trade_count")' in src, "поток"
    assert 'pick_fullest(plain, "updates")' in src, "стакан"


def test_candle_cross_check_exists():
    """
    Сверка со свечой — то, чем задвоение и нашлось. Пусть остаётся постоянной
    проверкой, а не разовым скриптом.
    """
    src = (ROOT / "src/db.py").read_text()
    assert "async def flow_candle_check" in src
    i = src.index("async def flow_candle_check")
    body = src[i:i + 2600]
    assert 'source == "exchange"' in body, \
        "дилерские сделки в биржевую свечу не входят, сверять их бессмысленно"
    assert "ours > theirs" in body, "подозрительно ПРЕВЫШЕНИЕ, а не недосчёт"
    api = (ROOT / "src/api/main.py").read_text()
    assert "/api/flow/{ticker}/check" in api


def test_handler_failure_is_not_swallowed():
    """
    Проглоченное исключение и скрыло всю историю. Ошибка разбора обязана
    попадать в показатели, а не только в лог.
    """
    from src.collector.stream import MarketStream
    s = MarketStream("x", {"SBER": "F1"})
    s._note_error("проверка")
    assert s.health()["handler_errors"] == 1
    assert s.health()["last_handler_error"] == "проверка"
    src = (ROOT / "src/collector/stream.py").read_text()
    assert "logger.debug(\"пакет не разобран" not in src


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
    """240 подписок на 80 бумаг против лимита в 300 на соединение."""
    from src.collector.stream import MarketStream
    figis = {f"T{i:02d}": f"FIGI{i:08d}" for i in range(80)}
    reqs = list(MarketStream("x", figis)._subscribe_requests())
    total = sum(len(getattr(r, r.WhichOneof("payload")).instruments) for r in reqs)
    assert total == 240, "80 бумаг на три типа данных"
    assert total <= 300, "лимит на одно соединение"


def test_candles_are_one_minute():
    """Интервал минутный: на нём и строится вся интрадей-картина."""
    from src.collector.stream import MarketStream
    reqs = list(MarketStream("x", {"SBER": "F1"})._subscribe_requests())
    cd = reqs[2].subscribe_candles_request
    assert all(i.interval == pb.SUBSCRIPTION_INTERVAL_ONE_MINUTE
               for i in cd.instruments)
    assert cd.waiting_close is False, \
        "нужна свеча, обновляемая ПО ХОДУ минуты, а не только на закрытии"
