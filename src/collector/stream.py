"""
Постоянное соединение с биржей вместо опроса по кругу.

ЗАЧЕМ. Замер 01.08 показал, что даёт опрос REST: 32 бумаги ядра получают снимок
раз в ~5 минут, 48 бумаг хвоста — раз в ~43 минуты (6 штук за цикл при цикле в
322 секунды). В хвосте лежали MVID, DATA, SGZH, UGLD — то есть те, что ходят
сильнее всего. И дело не только в редкости: один снимок раз в 43 минуты это
ТОЧКА, а по точке нельзя увидеть, что выкупать начали три минуты назад.

Лимиты стрима (проверено по документации 01.08):
    подписок на одно соединение     300   свечи + стаканы + сделки суммарно
    нам нужно                       160   80 бумаг × 2 типа
    запросов на подписку в минуту   100   мы шлём 2
    доставка сделок                 без ограничения частоты
    доставка стакана                не чаще раза в 100 мс

ЧТО ДАЁТ ПОТОК, ЧЕГО НЕ БЫЛО В REST:

    ticker прямо в сообщении. Раньше тикер приходилось сопоставлять с FIGI
    самим, и 30.07 выяснилось, что 22 записи рукописной таблицы указывали на
    ЧУЖИЕ инструменты: HYDR получал данные FEES, MAGN — данные UPRO. Теперь
    биржа называет бумагу в каждом пакете, и сверять нечего.

    trade_source. Сделки бывают биржевые и дилерские (внутренние сделки
    брокера). Подписываемся ТОЛЬКО на биржевые: дилерские не проходят через
    стакан и завышают поток. В REST-выдаче этого разделения у нас не было
    вообще, то есть накопленный поток мог быть замусорен.

    is_consistent у стакана. Биржа сама помечает пакет, в котором дошли не все
    заявки. Такие пропускаем, а не считаем как настоящий перекос.

ЕДИНИЦЫ. quantity в сделках и объёмы в стакане приходят в ЛОТАХ, не в штуках.
Так же было в REST, поэтому старые и новые записи сопоставимы. Пересчёт в штуки
сознательно не делаем: лотность меняется во времени, и хранить пересчитанное
значит потерять исходное.

ДЕДУП НЕ НУЖЕН. В отличие от REST, где окна запросов перекрывались и спасал
watermark по времени последней сделки, поток отдаёт каждую сделку один раз.
Обратная сторона: при обрыве соединения появляется не задвоение, а ДЫРА.
Поэтому здесь есть сторож, который переподключается при тишине, и счётчик
обрывов в диагностике — дыру видно.

УСТРОЙСТВО. Сеть отделена от арифметики. Aggregator — чистый класс без сети и
базы, он тестируется. MarketStream — только соединение, подписки и обрывы.
Токена в песочнице нет, поэтому сетевую часть проверить локально нельзя, и тем
важнее, что считающая часть проверяема.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

ENDPOINT = os.getenv("TINKOFF_GRPC", "invest-public-api.tinkoff.ru:443")
MSK_SHIFT_H = 3

# Тишина, после которой соединение считается мёртвым. Днём на 80 бумагах пакеты
# идут непрерывно; минута молчания в торговое время означает обрыв, который
# gRPC не заметил. Ночью тишина законна, поэтому сторож смотрит на расписание.
SILENCE_SEC = int(os.getenv("STREAM_SILENCE_SEC", "60"))


# Живой стрим процесса. Нужен эндпоинту диагностики: без него «работает ли
# сбор» можно узнать только по косвенным признакам, а на прошлой неделе
# полдня ушло на то, чтобы понять, что сбор молча стоял.
CURRENT: Optional["MarketStream"] = None


def msk_minute(dt: datetime) -> str:
    """Метка минуты в МСК. Биржа отдаёт UTC, вся система живёт по Москве."""
    return (dt + timedelta(hours=MSK_SHIFT_H)).strftime("%Y-%m-%dT%H:%M")


def session_of(mk: str) -> str:
    """
    Утро / основная / вечер. Границы те же, что в minute_buckets — если они
    разойдутся, одна и та же минута получит разные метки в двух таблицах.
    """
    m = int(mk[11:13]) * 60 + int(mk[14:16])
    if m < 9 * 60 + 50:
        return "morning"
    if m < 19 * 60:
        return "main"
    return "evening"


def quotation(q) -> float:
    """Цена Tinkoff: целая часть плюс миллиардные доли."""
    if q is None:
        return 0.0
    return float(getattr(q, "units", 0) or 0) + float(getattr(q, "nano", 0) or 0) / 1e9


class Aggregator:
    """
    Накопление пакетов в память до записи в базу.

    Почему в память, а не строкой на пакет. На 80 бумагах стакан приходит до
    десяти раз в секунду — это до 800 записей в секунду. Такой поток не нужен
    ни для какого решения: важно, КАКИМ был перекос в течение минуты и менялся
    ли он. Поэтому минута — единица хранения, а внутри минуты копятся суммы.

    Хранятся СУММЫ, а не средние. Среднее нельзя усреднить повторно при склейке
    минут в пятиминутки, а сумму сложить можно.

    Хранится также размах перекоса за минуту (imb_min/imb_max). Одно среднее
    значение скрывает разворот: минута, где стакан был сначала 80% на покупку,
    а потом 20%, даёт то же среднее, что и ровные 50%.
    """

    def __init__(self):
        self.flow: dict = {}
        self.book: dict = {}
        self.trades_seen = 0
        self.books_seen = 0
        self.books_skipped = 0

    def add_trade(self, ticker: str, when: datetime, direction: int,
                  price: float, qty: int) -> None:
        if not ticker or qty <= 0:
            return
        mk = msk_minute(when)
        k = (ticker.upper(), mk)
        r = self.flow.get(k)
        if r is None:
            r = {"ts": mk, "session": session_of(mk), "buy_volume": 0,
                 "sell_volume": 0, "trade_count": 0, "max_trade": 0,
                 "vwap_num": 0.0}
            self.flow[k] = r
        # Направление берётся у биржи. Достраивать его правилом тика нельзя:
        # 31.07 по SIBN флаг дал 79.2% покупок, а правило тика 39.9%.
        if direction == 1:                                   # TRADE_DIRECTION_BUY
            r["buy_volume"] += qty
        elif direction == 2:                                 # TRADE_DIRECTION_SELL
            r["sell_volume"] += qty
        r["trade_count"] += 1
        r["max_trade"] = max(r["max_trade"], qty)
        r["vwap_num"] += price * qty
        self.trades_seen += 1

    def add_book(self, ticker: str, when: datetime, bid_vol: int, ask_vol: int,
                 best_bid: float, best_ask: float) -> None:
        if not ticker:
            return
        total = bid_vol + ask_vol
        if total <= 0:
            return
        mk = msk_minute(when)
        k = (ticker.upper(), mk)
        share = bid_vol / total
        r = self.book.get(k)
        if r is None:
            r = {"ts": mk, "session": session_of(mk), "updates": 0,
                 "bid_vol_sum": 0.0, "ask_vol_sum": 0.0, "spread_sum": 0.0,
                 "best_bid": 0.0, "best_ask": 0.0,
                 "imb_min": share, "imb_max": share}
            self.book[k] = r
        r["updates"] += 1
        r["bid_vol_sum"] += bid_vol
        r["ask_vol_sum"] += ask_vol
        if best_bid > 0 and best_ask > 0:
            r["spread_sum"] += best_ask - best_bid
            r["best_bid"] = best_bid
            r["best_ask"] = best_ask
        r["imb_min"] = min(r["imb_min"], share)
        r["imb_max"] = max(r["imb_max"], share)
        self.books_seen += 1

    def drain(self) -> tuple[dict, dict]:
        """
        Отдать накопленное и обнулить. Возвращает две карты тикер -> строки.

        Обнуление здесь, а не после успешной записи, сознательно: при сбое базы
        потеряется минута данных, но накопитель не будет расти без предела и не
        задвоит уже записанное. Потерю видно в диагностике по разрыву минут.
        """
        f: dict = {}
        for (tk, _), r in self.flow.items():
            f.setdefault(tk, []).append(r)
        b: dict = {}
        for (tk, _), r in self.book.items():
            b.setdefault(tk, []).append(r)
        self.flow, self.book = {}, {}
        return f, b


class MarketStream:
    """
    Одно соединение: подписки, чтение, переподключение.

    figis — карта тикер -> FIGI, резолвится СНАРУЖИ через FindInstrument.
    Своей таблицы соответствий здесь нет и быть не должно.
    """

    def __init__(self, token: str, figis: dict, depth: int = 20,
                 flush_sec: int = 20, on_flush=None):
        self.token = token
        self.figis = {t.upper(): f for t, f in (figis or {}).items() if f}
        self.depth = depth
        self.flush_sec = flush_sec
        self.on_flush = on_flush
        self.agg = Aggregator()
        self.last_msg: dict = {}          # тикер -> время последнего пакета
        self.stats = {"connected_at": None, "reconnects": 0, "messages": 0,
                      "trades": 0, "books": 0, "books_skipped": 0,
                      "last_error": None, "subscribed": 0}
        # Простой флаг, а НЕ asyncio.Event. Event в Python 3.9 привязывается
        # к циклу событий прямо в конструкторе, и объект нельзя создать до
        # запуска цикла. Семантика Event здесь не нужна: ждать на нём мы не
        # ждём, только проверяем и ставим.
        self._stopped = False

    # ─── подписки ────────────────────────────────────────────────────────────

    def _subscribe_requests(self):
        """
        Два запроса на всё: сделки и стаканы. Лимит — 100 запросов на подписку
        в минуту, поэтому инструменты передаются списком, а не по одному.
        """
        from .tinkoff_pb import marketdata_pb2 as md
        ids = list(self.figis.values())
        yield md.MarketDataRequest(
            subscribe_trades_request=md.SubscribeTradesRequest(
                subscription_action=md.SUBSCRIPTION_ACTION_SUBSCRIBE,
                instruments=[md.TradeInstrument(instrument_id=f) for f in ids],
                # Только биржевые. Дилерские сделки не проходят через стакан.
                trade_source=md.TRADE_SOURCE_EXCHANGE,
            ))
        yield md.MarketDataRequest(
            subscribe_order_book_request=md.SubscribeOrderBookRequest(
                subscription_action=md.SUBSCRIPTION_ACTION_SUBSCRIBE,
                instruments=[md.OrderBookInstrument(instrument_id=f,
                                                    depth=self.depth)
                             for f in ids],
            ))

    async def _request_iter(self, queue: asyncio.Queue):
        """Исходящая половина двунаправленного стрима."""
        for r in self._subscribe_requests():
            yield r
        while not self._stopped:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                continue
            if item is None:
                return
            yield item

    # ─── чтение ──────────────────────────────────────────────────────────────

    def _handle(self, resp) -> None:
        name = resp.WhichOneof("payload")
        if name == "trade":
            t = resp.trade
            tk = (t.ticker or "").upper()
            when = t.time.ToDatetime().replace(tzinfo=timezone.utc)
            self.agg.add_trade(tk, when, int(t.direction),
                               quotation(t.price), int(t.quantity))
            self.last_msg[tk] = datetime.now(timezone.utc)
            self.stats["trades"] += 1
        elif name == "orderbook":
            ob = resp.orderbook
            tk = (ob.ticker or "").upper()
            if not ob.is_consistent:
                # Биржа сама говорит, что дошли не все заявки. Считать такой
                # пакет настоящим перекосом нельзя.
                self.stats["books_skipped"] += 1
                self.agg.books_skipped += 1
                return
            bid = sum(int(o.quantity) for o in ob.bids)
            ask = sum(int(o.quantity) for o in ob.asks)
            bb = quotation(ob.bids[0].price) if ob.bids else 0.0
            ba = quotation(ob.asks[0].price) if ob.asks else 0.0
            when = ob.time.ToDatetime().replace(tzinfo=timezone.utc)
            self.agg.add_book(tk, when, bid, ask, bb, ba)
            self.last_msg[tk] = datetime.now(timezone.utc)
            self.stats["books"] += 1
        elif name in ("subscribe_trades_response", "subscribe_order_book_response"):
            payload = getattr(resp, name)
            ok = sum(1 for s in payload.subscriptions
                     if "SUCCESS" in str(s.subscription_status))
            bad = [s for s in payload.subscriptions
                   if "SUCCESS" not in str(s.subscription_status)]
            self.stats["subscribed"] += ok
            if bad:
                logger.warning("подписка отклонена по %d инструментам: %s",
                               len(bad), str(bad[0].subscription_status))
            logger.info("подписка %s: принято %d, отклонено %d", name, ok, len(bad))
        self.stats["messages"] += 1

    # ─── жизненный цикл ──────────────────────────────────────────────────────

    async def _session(self) -> None:
        import grpc
        from .tinkoff_pb import marketdata_pb2_grpc as mdg

        creds = grpc.ssl_channel_credentials()
        # keepalive нужен, потому что молчащее TCP-соединение может умереть на
        # промежуточном узле так, что обе стороны считают его живым.
        opts = [("grpc.keepalive_time_ms", 30000),
                ("grpc.keepalive_timeout_ms", 10000),
                ("grpc.keepalive_permit_without_calls", 1),
                ("grpc.max_receive_message_length", 16 * 1024 * 1024)]
        async with grpc.aio.secure_channel(ENDPOINT, creds, options=opts) as ch:
            stub = mdg.MarketDataStreamServiceStub(ch)
            queue: asyncio.Queue = asyncio.Queue()
            meta = (("authorization", f"Bearer {self.token}"),
                    ("x-app-name", "modex.stream"))
            call = stub.MarketDataStream(self._request_iter(queue), metadata=meta)
            self.stats["connected_at"] = datetime.now(timezone.utc).isoformat()
            logger.info("стрим открыт: %d бумаг, глубина %d",
                        len(self.figis), self.depth)
            async for resp in call:
                if self._stopped:
                    break
                try:
                    self._handle(resp)
                except Exception as e:                       # noqa: BLE001
                    logger.debug("пакет не разобран: %s", e)

    async def _flusher(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self.flush_sec)
            flow, book = self.agg.drain()
            if not flow and not book:
                continue
            if self.on_flush:
                try:
                    await self.on_flush(flow, book)
                except Exception as e:                       # noqa: BLE001
                    logger.warning("запись потока не удалась: %s", e)

    async def _watchdog(self) -> None:
        """
        Сторож тишины. gRPC не всегда замечает обрыв: соединение висит открытым,
        а данные не идут. Проверяется ТОЛЬКО в торговое время, иначе ночная
        тишина вызывала бы бесконечные переподключения.
        """
        while not self._stopped:
            await asyncio.sleep(20)
            if not self.last_msg:
                continue
            now = datetime.now(timezone.utc)
            msk = now + timedelta(hours=MSK_SHIFT_H)
            if msk.weekday() >= 5 or not (7 <= msk.hour < 24):
                continue
            quiet = (now - max(self.last_msg.values())).total_seconds()
            if quiet > SILENCE_SEC:
                logger.warning("тишина %.0f с в торговое время — переподключаюсь",
                               quiet)
                raise RuntimeError(f"silence {quiet:.0f}s")

    async def _run_once(self) -> None:
        """
        Три задачи как ОДНО целое: упала любая — гасим остальные.

        Здесь был дефект. Раньше стояло asyncio.gather(...) без обработки, и он
        отдаёт первое исключение НЕМЕДЛЕННО, но соседние задачи при этом
        продолжают жить. То есть при срабатывании сторожа тишины внешний цикл
        открывал бы ВТОРОЕ соединение поверх первого, и с каждым обрывом их
        становилось бы больше — до упора в лимит соединений. Причём в данных
        это выглядело бы как задвоение объёма, а не как ошибка.
        """
        tasks = [asyncio.ensure_future(c) for c in
                 (self._session(), self._flusher(), self._watchdog())]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for t in done:
            if t.exception():
                raise t.exception()

    async def run(self) -> None:
        """Вечный цикл: соединение, а при обрыве — новое, с ростом паузы."""
        global CURRENT
        CURRENT = self
        backoff = 1
        while not self._stopped:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:                           # noqa: BLE001
                self.stats["reconnects"] += 1
                self.stats["last_error"] = f"{type(e).__name__}: {e}"[:200]
                logger.warning("стрим оборван (%s), пауза %d с",
                               self.stats["last_error"], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                backoff = 1

    def stop(self) -> None:
        self._stopped = True

    def health(self) -> dict:
        """Что показывать в диагностике: возраст данных по каждой бумаге."""
        now = datetime.now(timezone.utc)
        ages = {t: round((now - ts).total_seconds(), 1)
                for t, ts in self.last_msg.items()}
        fresh = sum(1 for a in ages.values() if a <= 60)
        return {
            **self.stats,
            "tickers_subscribed": len(self.figis),
            "tickers_with_data": len(ages),
            "tickers_fresh_60s": fresh,
            "oldest_sec": max(ages.values()) if ages else None,
            "ages_sec": dict(sorted(ages.items(), key=lambda kv: -kv[1])),
        }
