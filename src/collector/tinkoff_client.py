"""
MOODEX — Tinkoff Invest API Client

Даёт Claude данные которых нет на MOEX ISS:
  - Точные свечи с объёмом лотов
  - Стакан (bid/ask 20 уровней) → давление покупателей/продавцов
  - Поток сделок (последние trades) → кто агрессивнее
  - Данные по инструменту (лот, шаг цены, сектор)

Всё это агрегируется в структурированный блок для Claude.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Окно запроса сделок у Tinkoff. GetLastTrades отдаёт ВСЕ сделки за период,
# а не «последние N» — поэтому окно определяет полноту сбора.
#
# Раньше стояло 4 часа, и весь ответ, кроме последних 50 сделок, выбрасывался.
# Окно должно быть заметно БОЛЬШЕ интервала между опросами, иначе между
# снимками появятся дыры. Замер 01.08: интервал по SBER 322 секунды, поэтому
# 30 минут дают пятикратный запас и payload на порядок меньше четырёхчасового.
TRADES_WINDOW_MIN = int(os.getenv("TRADES_WINDOW_MIN", "30"))

TINKOFF_BASE = "https://invest-public-api.tinkoff.ru/rest"

# Кэш соответствия тикер -> FIGI. ЗАПОЛНЯЕТСЯ ТОЛЬКО ИЗ API, вручную не ведём.
#
# Здесь была рукописная таблица на 43 записи. 30.07 сверка с FindInstrument
# показала, что 22 из них указывали на ЧУЖИЕ инструменты, а кэш проверялся
# ДО обращения к API — то есть подмена была постоянной и тихой:
#   HYDR РусГидро    <- данные FEES ФСК Россети   (0.05 руб вместо 0.32)
#   MAGN ММК         <- данные UPRO Юнипро        (1.11 руб вместо 20.89)
#   MTSS МТС         <- данные NLMK
#   NLMK             <- данные SNGSP
#   и ещё 18 тикеров получали инструменты вне списка наблюдения
# По этим тикерам чужими были ВСЕ реалтайм-данные: свечи (а значит VWAP, ATR,
# диапазон открытия, состояние волатильности), стакан, поток заявок, цена.
# Диагностика /api/health/figi существовала и резолвила верно, но её вывод
# никогда не сверяли с таблицей.
TICKER_TO_FIGI: dict[str, str] = {}

# Единая точка сборки FindInstrument: и резолвер, и probe зовут ОДИН эндпоинт с
# ОДНИМ значением instrumentKind, чтобы фильтры не разъехались между местами.
_FIND = "tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument"
SHARE_KIND = "INSTRUMENT_TYPE_SHARE"


def _clean_token(token: Optional[str]) -> str:
    r"""
    Снять с токена мусор, который добавляет ОКРУЖЕНИЕ, а не его значение: перевод
    строки в конце (Coolify и консоль часто так и хранят), пробелы от копирования,
    обрамляющие кавычки.

    03.08 прод четыре часа стоял с live 0 из 48, владелец перевыпустил токен — не
    помогло. Ровно этот симптом даёт хвост `\n`: заголовок `Authorization: Bearer
    t.xxx\n` Tinkoff отвергает как невалидный на КАЖДОМ запросе, перевыпуск кладёт
    новое значение в то же хранилище с тем же хвостом, и 401 повторяется. Мусор
    добавляет (точнее — не снимает) наш код, поэтому снимать его обязан код, и в
    ЕДИНСТВЕННОМ месте — иначе один путь чинишь, другой нет.
    """
    t = (token or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        t = t[1:-1].strip()
    return t


def _is_share(inst: dict) -> bool:
    """
    Инструмент — именно АКЦИЯ. Страховка на случай, когда фильтр instrumentKind
    снят: без неё под тикер бумаги мог бы подвернуться фьючерс или бонд того же
    эмитента — родня той подмены, из-за которой HYDR получал данные FEES.
    """
    it = str(inst.get("instrumentType") or "").lower()
    ik = str(inst.get("instrumentKind") or "")
    return it == "share" or ik == SHARE_KIND


def _pick_share(instruments: list, ticker: str,
                require_share: bool = True) -> Optional[dict]:
    """
    Выбрать из ответа FindInstrument инструмент с ТОЧНО этим тикером.

    Точное совпадение тикера — тот же ключ, которым ловилась подмена. Когда
    instrumentKind в запросе снят (`require_share=True`), дополнительно требуем
    тип «акция»: сервер уже не отфильтровал за нас. Если совпадений несколько
    (одна бумага на разных бордах), предпочитаем торгуемую по API и московский
    борд TQ*.
    """
    tk = ticker.upper()
    cands = [i for i in (instruments or [])
             if str(i.get("ticker") or "").upper() == tk
             and (not require_share or _is_share(i))]
    if not cands:
        return None
    cands.sort(
        key=lambda i: (bool(i.get("apiTradeAvailableFlag")),
                       str(i.get("classCode") or "").upper().startswith("TQ")),
        reverse=True)
    return cands[0]


class TinkoffClient:
    """Клиент Tinkoff Invest REST API."""

    def __init__(self, token: str = None):
        # Нормализуем ЗДЕСЬ, до сборки заголовка: хвост `\n` или кавычки от
        # окружения давали Bearer, который Tinkoff отвергал на каждом запросе.
        self.token = _clean_token(token or os.getenv("TINKOFF_TOKEN", ""))
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        # ПРИЧИНА ОТКАЗА ДОСТУПНА ВЫЗЫВАЮЩЕМУ, а не только логу. 03.08 прод четыре
        # часа стоял с 48 мёртвыми тикерами, и назвать причину было НЕЛЬЗЯ: статус
        # ответа знали здесь, наружу уходил None, а лога у агента нет. Перевыпуск
        # токена ничего не дал и ничего не сообщил — версию закрыли, знание не
        # прибавилось. Эти два поля и есть разница между «не работает» и диагнозом.
        self.last_error: Optional[str] = None
        self.last_status: Optional[int] = None

    def _ok(self) -> bool:
        return bool(self.token)

    def _fail(self, reason: str, status: Optional[int] = None) -> None:
        self.last_error = reason
        self.last_status = status

    async def _post(self, endpoint: str, body: dict) -> Optional[dict]:
        if not self._ok():
            self._fail("TINKOFF_TOKEN не задан")
            return None
        url = f"{TINKOFF_BASE}/{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=self.headers, json=body)
                if resp.status_code != 200:
                    logger.warning(f"Tinkoff API {endpoint}: {resp.status_code} {resp.text[:200]}")
                    self._fail(f"HTTP {resp.status_code}: {resp.text[:160]}", resp.status_code)
                    return None
                self._fail(None)
                return resp.json()
        except Exception as e:
            logger.warning(f"Tinkoff API error {endpoint}: {e}")
            self._fail(f"{type(e).__name__}: {str(e)[:160]}")
            return None

    async def _resolve(self, ticker: str) -> Optional[dict]:
        """
        Найти акцию по тикеру, СНИМАЯ фильтры по одному, если строгий запрос
        вернул 200 без совпадения.

        03.08 прод четыре часа стоял с live 0 из 48 при рабочем перевыпущенном
        токене, и probe назвал главного подозреваемого: строгий запрос
        (apiTradeAvailableFlag + instrumentKind) отвечает 200 и НЕ отдаёт ничего.
        Прежний код на это только жаловался и сдавался. Но FIGI у бумаги один и
        тот же, доступна она по API или нет, — поэтому здесь резолв ослабляет
        фильтры и всё равно требует ИМЕННО акцию с ИМЕННО этим тикером. Диагноз
        (какой фильтр отсёк) остаётся за probe: тот НЕ восстанавливается, а мерит.

        Возвращает выбранный инструмент (dict FindInstrument) или None; причину
        отказа кладёт в last_error, как и раньше.
        """
        ticker = ticker.upper()
        attempts = (
            ({"query": ticker, "instrumentKind": SHARE_KIND,
              "apiTradeAvailableFlag": True}, True),
            ({"query": ticker, "instrumentKind": SHARE_KIND}, True),
            ({"query": ticker}, False),      # kind снят — тип проверяем сами
        )
        seen: list = []
        for body, kind_filtered in attempts:
            data = await self._post(_FIND, body)
            if data is None:
                # сеть или HTTP-отказ: причину уже записал _post, ослаблять нечего
                return None
            instruments = data.get("instruments", [])
            if instruments:
                seen = sorted({str(i.get("ticker")) for i in instruments})[:8]
            got = _pick_share(instruments, ticker,
                              require_share=not kind_filtered)
            if got:
                self._fail(None)
                return got
        # Сюда — только если все три запроса ответили 200 и ни в одном не нашлось
        # акции с точным тикером. Пустой список и «пришли чужие» лечатся по-разному,
        # поэтому причина их РАЗЛИЧАЕТ.
        if seen:
            self._fail(f"ответ 200 по всем фильтрам, акции {ticker} нет; "
                       f"пришли: {', '.join(seen)}")
        else:
            self._fail(f"ответ 200 по всем фильтрам, instruments пуст — {ticker} "
                       f"не найден ни без apiTradeAvailableFlag, ни без instrumentKind")
        return None

    async def get_figi(self, ticker: str) -> Optional[str]:
        """Получить FIGI по тикеру (сначала из кэша, потом из API)."""
        ticker = ticker.upper()
        if ticker in TICKER_TO_FIGI:
            return TICKER_TO_FIGI[ticker]
        inst = await self._resolve(ticker)
        figi = inst.get("figi") if inst else None
        if figi:
            # Кэшируем лишь подтверждённое API точное совпадение-акцию: любой
            # другой путь в кэш и был причиной подмены данных.
            TICKER_TO_FIGI[ticker] = figi
            return figi
        logger.warning(f"FIGI для {ticker} не подтверждён API — реалтайм недоступен: "
                       f"{self.last_error}")
        return None

    async def resolve_live(self, ticker: str) -> Optional[dict]:
        """
        ЖИВОЙ резолв через FindInstrument, ИГНОРИРУЯ статический кэш — чтобы честно
        проверить, торгуется ли символ СЕЙЧАС (ловит переименования/делистинги:
        напр. YNDX→YDEX). Работает при закрытом рынке. None → символ не найден.
        """
        inst = await self._resolve(ticker)
        if inst and inst.get("figi"):
            return {"ticker": inst.get("ticker"), "figi": inst.get("figi"),
                    "name": inst.get("name"),
                    "trade_available": inst.get("apiTradeAvailableFlag")}
        return None

    async def probe(self, ticker: str = "SBER") -> dict:
        """
        ОДИН вызов, который НАЗЫВАЕТ причину, а не перечисляет версии через «или».
        Прежнее сообщение прода — «Tinkoff API недоступен ИЛИ токен придушен
        лимитом» — это два разных диагноза и два разных действия, и выбрать между
        ними было нечем.
        Три запроса подряд, снимающие фильтры по одному, отвечают на вопрос
        «отказ на стороне Tinkoff или наш запрос отсеивает всё сам».
        """
        ep = _FIND
        if not self._ok():
            return {"verdict": "TINKOFF_TOKEN не задан", "token_set": False}

        async def _try(body: dict) -> dict:
            data = await self._post(ep, body)
            if data is None:
                return {"ok": False, "status": self.last_status, "error": self.last_error}
            inst = data.get("instruments", [])
            return {"ok": True, "status": 200, "count": len(inst),
                    "tickers": sorted({str(i.get("ticker")) for i in inst})[:8]}

        full = await _try({"query": ticker, "instrumentKind": "INSTRUMENT_TYPE_SHARE",
                           "apiTradeAvailableFlag": True})
        no_flag = await _try({"query": ticker, "instrumentKind": "INSTRUMENT_TYPE_SHARE"})
        bare = await _try({"query": ticker})

        st = full.get("status")
        if not full["ok"]:
            if st in (401, 403):
                verdict = "токен недействителен или отозван"
            elif st == 429:
                verdict = "лимит запросов Tinkoff"
            elif st and st >= 500:
                verdict = f"Tinkoff недоступен (HTTP {st})"
            elif st:
                verdict = f"Tinkoff ответил HTTP {st}"
            else:
                verdict = "сеть или таймаут до Tinkoff — запрос не дошёл"
        elif full.get("count"):
            verdict = "исправно: API отвечает, инструменты приходят"
        elif no_flag.get("count"):
            verdict = ("наш запрос отсеивает всё сам: без apiTradeAvailableFlag "
                       "инструменты есть, с ним пусто")
        elif bare.get("count"):
            verdict = ("наш запрос отсеивает всё сам: мешает instrumentKind "
                       "INSTRUMENT_TYPE_SHARE")
        else:
            verdict = ("API отвечает 200, но инструментов не отдаёт ни с фильтрами, "
                       "ни без них")
        return {"verdict": verdict, "token_set": True, "ticker": ticker,
                "with_filters": full, "without_trade_flag": no_flag, "query_only": bare}

    async def get_candles(self, ticker: str, days: int = 365) -> Optional[dict]:
        """Дневные свечи с точным объёмом."""
        figi = await self.get_figi(ticker)
        if not figi:
            return None

        now   = datetime.now(timezone.utc)
        from_ = (now - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
        to_   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        data = await self._post(
            "tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles",
            {
                "figi": figi,
                "from": from_,
                "to": to_,
                "interval": "CANDLE_INTERVAL_DAY",
            },
        )
        if not data or "candles" not in data:
            return None

        result = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
        for c in data["candles"]:
            def _price(p): return float(p.get("units", 0)) + float(p.get("nano", 0)) / 1e9
            result["dates"].append(c.get("time", "")[:10])
            result["open"].append(_price(c.get("open", {})))
            result["high"].append(_price(c.get("high", {})))
            result["low"].append(_price(c.get("low", {})))
            result["close"].append(_price(c.get("close", {})))
            result["volume"].append(int(c.get("volume", 0)))

        return result if result["close"] else None

    async def get_intraday_candles(self, ticker: str, tf_min: int = 5,
                                   hours: int = 8) -> Optional[dict]:
        """
        Реалтайм интрадей-свечи Tinkoff (без 15-мин задержки MOEX ISS).
        tf_min: 1 / 5 / 15 / 60. hours — сколько последних часов взять.
        Возвращает параллельные массивы dates/open/high/low/close/volume.
        """
        figi = await self.get_figi(ticker)
        if not figi:
            return None

        interval = {
            1:  "CANDLE_INTERVAL_1_MIN",
            5:  "CANDLE_INTERVAL_5_MIN",
            15: "CANDLE_INTERVAL_15_MIN",
            60: "CANDLE_INTERVAL_HOUR",
        }.get(tf_min, "CANDLE_INTERVAL_5_MIN")

        now = datetime.now(timezone.utc)
        from_ = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self._post(
            "tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles",
            {"figi": figi, "from": from_, "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "interval": interval},
        )
        if not data or "candles" not in data:
            return None

        def _price(p):
            return float(p.get("units", 0)) + float(p.get("nano", 0)) / 1e9

        out = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
        for c in data["candles"]:
            out["dates"].append(c.get("time", ""))
            out["open"].append(_price(c.get("open", {})))
            out["high"].append(_price(c.get("high", {})))
            out["low"].append(_price(c.get("low", {})))
            out["close"].append(_price(c.get("close", {})))
            out["volume"].append(int(c.get("volume", 0)))
        return out if out["close"] else None

    async def get_orderbook(self, ticker: str, depth: int = 20) -> Optional[dict]:
        """
        Стакан — 20 уровней bid/ask.
        Даёт понимание: где стоят крупные заявки, есть ли давление продавцов.
        """
        figi = await self.get_figi(ticker)
        if not figi:
            return None

        data = await self._post(
            "tinkoff.public.invest.api.contract.v1.MarketDataService/GetOrderBook",
            {"figi": figi, "depth": depth},
        )
        if not data:
            return None

        def _price(p): return float(p.get("units", 0)) + float(p.get("nano", 0)) / 1e9

        bids = [{"price": _price(b["price"]), "qty": int(b["quantity"])}
                for b in data.get("bids", [])]
        asks = [{"price": _price(a["price"]), "qty": int(a["quantity"])}
                for a in data.get("asks", [])]

        if not bids or not asks:
            return None

        total_bid_qty = sum(b["qty"] for b in bids)
        total_ask_qty = sum(a["qty"] for a in asks)
        bid_ask_ratio = round(total_bid_qty / total_ask_qty, 2) if total_ask_qty else 1.0

        spread = asks[0]["price"] - bids[0]["price"] if bids and asks else 0
        spread_pct = round(spread / bids[0]["price"] * 100, 4) if bids else 0

        # Давление: ratio > 1.5 → покупатели доминируют, < 0.7 → продавцы
        if bid_ask_ratio >= 1.5:
            pressure = "покупатели доминируют 🟢"
        elif bid_ask_ratio <= 0.7:
            pressure = "продавцы доминируют 🔴"
        else:
            pressure = "баланс ⚪"

        # Карта стакана из ПОЛНЫХ уровней (до 20): крупнейшие стены-лимитки —
        # реальные уровни поддержки/сопротивления, а не только верх стакана.
        bid_walls = sorted(bids, key=lambda x: x["qty"], reverse=True)[:3]
        ask_walls = sorted(asks, key=lambda x: x["qty"], reverse=True)[:3]

        # Ликвидность: глубина у СЕРЕДИНЫ (±0.5%) + тугой спред. depth_near — сколько
        # лотов реально стоит рядом с ценой; liquidity_score 0..100 (эвристика: тугой
        # спред = ликвидно). Помогает риск-гейту и Claude отличать тонкий стакан.
        mid = (bids[0]["price"] + asks[0]["price"]) / 2 if (bids and asks) else 0
        band = mid * 0.005
        depth_near = (sum(b["qty"] for b in bids if b["price"] >= mid - band)
                      + sum(a["qty"] for a in asks if a["price"] <= mid + band))
        liquidity_score = max(0, round(100 - spread_pct * 200, 0))  # спред 0%→100, 0.5%→0
        liquidity = ("глубокий" if liquidity_score >= 70 else
                     "нормальный" if liquidity_score >= 40 else "тонкий")

        return {
            "best_bid":      bids[0]["price"] if bids else None,
            "best_ask":      asks[0]["price"] if asks else None,
            "spread_pct":    spread_pct,
            "bid_ask_ratio": bid_ask_ratio,
            "pressure":      pressure,
            "total_bid_qty": total_bid_qty,
            "total_ask_qty": total_ask_qty,
            "depth_near_mid": depth_near,      # лотов в пределах ±0.5% от середины
            "liquidity_score": liquidity_score,  # 0..100 (тугой спред = выше)
            "liquidity":     liquidity,        # тонкий | нормальный | глубокий
            "top_bids":      bids[:5],
            "top_asks":      asks[:5],
            "bid_walls":     bid_walls,
            "ask_walls":     ask_walls,
            "levels":        len(bids) + len(asks),
        }

    async def get_last_trades(self, ticker: str, limit: int = 50) -> Optional[dict]:
        """
        Последние сделки — поток ордеров.
        Показывает кто агрессивнее: покупатели (market buy) или продавцы (market sell).
        """
        figi = await self.get_figi(ticker)
        if not figi:
            return None

        now  = datetime.now(timezone.utc)
        from_ = (now - timedelta(minutes=TRADES_WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")

        data = await self._post(
            "tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastTrades",
            {"figi": figi, "from": from_, "to": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
        if not data or "trades" not in data:
            return None

        # Хронологический порядок (нужен для tick-rule).
        all_trades = sorted(data["trades"], key=lambda t: t.get("time", ""))
        if not all_trades:
            return None
        # Классификация — по последним `limit` сделкам: это ВИТРИНА для Claude
        # и дашборда, там важна свежесть, а не полнота.
        result = _classify_flow(all_trades[-limit:])
        # А в raw кладём ВСЁ ОКНО.
        #
        # Раньше здесь стояло all_trades[-limit:], то есть в накопление уходили
        # те же 50 сделок, что и в витрину, а остальное окно выбрасывалось. При
        # опросе раз в пять минут это означало, что по ликвидной бумаге в базу
        # попадали единицы процентов сделок: у SBER за пять минут их тысячи.
        # Данные приходили из Tinkoff и терялись в коде.
        result["raw"] = all_trades
        result["window_trades"] = len(all_trades)
        result["window_min"] = TRADES_WINDOW_MIN
        return result

    async def get_full_snapshot(self, ticker: str) -> dict:
        """
        Полный срез по тикеру: свечи + стакан + поток сделок.
        Возвращает структурированный dict и готовый текст для Claude.
        """
        ticker = ticker.upper()

        candles_task = self.get_candles(ticker, days=365)
        orderbook_task = self.get_orderbook(ticker)
        trades_task = self.get_last_trades(ticker)

        import asyncio
        candles, orderbook, trades = await asyncio.gather(
            candles_task, orderbook_task, trades_task,
            return_exceptions=True,
        )

        candles   = candles   if not isinstance(candles, Exception)   else None
        orderbook = orderbook if not isinstance(orderbook, Exception) else None
        trades    = trades    if not isinstance(trades, Exception)    else None

        lines = [f"📊 ДАННЫЕ TINKOFF INVEST — {ticker}:"]

        if orderbook:
            lines += [
                "",
                "  Стакан (bid/ask):",
                f"  Лучший bid: {orderbook['best_bid']} | Лучший ask: {orderbook['best_ask']}",
                f"  Спред: {orderbook['spread_pct']}%",
                f"  Соотношение bid/ask объёмов: {orderbook['bid_ask_ratio']} → {orderbook['pressure']}",
                "  Топ заявок на покупку:",
            ]
            for b in orderbook["top_bids"][:3]:
                lines.append(f"    {b['price']:.2f} × {b['qty']} лотов")
            lines.append("  Топ заявок на продажу:")
            for a in orderbook["top_asks"][:3]:
                lines.append(f"    {a['price']:.2f} × {a['qty']} лотов")

        if trades:
            lines += [
                "",
                "  Поток сделок (последние 4 часа):",
                f"  Покупки: {trades['buy_pct']}% | Продажи: {trades['sell_pct']}%",
                f"  Объём покупок: {trades['buy_volume']} лотов | Продаж: {trades['sell_volume']} лотов",
                f"  Оценка: {trades['order_flow']}",
            ]
            if trades["avg_price"]:
                lines.append(f"  Средняя цена сделок: {trades['avg_price']:.2f} ₽")

        if candles and candles["volume"]:
            vols = candles["volume"]
            avg_vol = sum(vols[-20:]) / min(20, len(vols)) if vols else 0
            last_vol = vols[-1] if vols else 0
            vol_ratio = round(last_vol / avg_vol, 2) if avg_vol else 1.0
            vol_label = "высокий ⚡" if vol_ratio > 1.5 else "низкий" if vol_ratio < 0.5 else "нормальный"
            lines += [
                "",
                f"  Объём последней сессии: {last_vol:,} лотов ({vol_ratio}× от среднего — {vol_label})",
            ]

        return {
            "candles":   candles,
            "orderbook": orderbook,
            "trades":    trades,
            "summary":   "\n".join(lines),
        }


def _classify_flow(trades: list[dict]) -> dict:
    """
    Чистая классификация потока сделок (без сети — легко тестировать) ДВУМЯ методами:
      1) по полю `direction` Tinkoff (сторона агрессора);
      2) tick-rule (знак изменения цены) — резерв на случай, если поле direction
         вырождено (например, вернуло одну сторону на 100% — типичный артефакт).
    Первичным берём direction, если он ДВУСТОРОННИЙ; иначе — tick-rule.
    В payload кладём ОБЕ оценки и сырое распределение направлений, чтобы причина
    артефактов была видна прямо в /api/feed (диагностика без отдельного лога).
    `trades` — сделки в ХРОНОЛОГИЧЕСКОМ порядке.
    """
    def _q(t) -> int:
        try:
            return int(t.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _price(p) -> float:
        p = p or {}
        return float(p.get("units", 0) or 0) + float(p.get("nano", 0) or 0) / 1e9

    n = len(trades)

    # Сырое распределение направлений (диагностика)
    dir_counts: dict[str, int] = {}
    for t in trades:
        d = t.get("direction") or "UNSPECIFIED"
        dir_counts[d] = dir_counts.get(d, 0) + 1

    buy_dir  = sum(_q(t) for t in trades if t.get("direction") == "TRADE_DIRECTION_BUY")
    sell_dir = sum(_q(t) for t in trades if t.get("direction") == "TRADE_DIRECTION_SELL")
    dir_total = buy_dir + sell_dir

    # tick-rule: uptick → buy, downtick → sell, без изменения цены → прошлая сторона
    buy_tick = sell_tick = 0
    prev = None
    last_side = None
    for t in trades:
        pr = _price(t.get("price"))
        q = _q(t)
        if prev is None or pr == prev:
            side = last_side
        elif pr > prev:
            side = "buy"
        else:
            side = "sell"
        if side == "buy":
            buy_tick += q
        elif side == "sell":
            sell_tick += q
        prev = pr
        last_side = side or last_side
    tick_total = buy_tick + sell_tick

    def _pct(b: int, s: int) -> float:
        tot = b + s
        return round(b / tot * 100, 1) if tot else 50.0

    buy_pct_dir  = _pct(buy_dir, sell_dir)
    buy_pct_tick = _pct(buy_tick, sell_tick)

    # Выбор первичного метода: aggressor-поле, если двустороннее; иначе tick-rule
    dir_onesided = dir_total > 0 and (buy_dir == 0 or sell_dir == 0)
    if dir_total > 0 and not dir_onesided:
        buy_vol, sell_vol, method = buy_dir, sell_dir, "direction"
    elif tick_total > 0:
        buy_vol, sell_vol, method = buy_tick, sell_tick, "tick"
    else:
        buy_vol, sell_vol, method = buy_dir, sell_dir, "direction"

    total = buy_vol + sell_vol
    buy_pct  = round(buy_vol / total * 100, 1) if total else 50.0
    sell_pct = round(100.0 - buy_pct, 1) if total else 50.0

    if buy_pct >= 60:
        flow = "агрессивные покупки 🟢"
    elif buy_pct <= 40:
        flow = "агрессивные продажи 🔴"
    else:
        flow = "смешанный поток ⚪"

    # Уверенность: мало сделок или подозрительный экстремум → low
    confidence = "low" if (n < 10 or buy_pct >= 97 or buy_pct <= 3) else "high"

    den = sum(_q(t) for t in trades)
    avg_price = (round(sum(_price(t.get("price")) * _q(t) for t in trades) / den, 2)
                 if den else None)

    # Footprint: объём по РЕАЛЬНОЙ цене сделок + сплит buy/sell — «какая цена
    # впитала объём и кто был агрессором». Группируем по фактической цене (шаг
    # цены = уровень), поэтому биннинг не нужен.
    fp_map: dict[float, list] = {}
    for t in trades:
        q = _q(t)
        if q <= 0:
            continue
        price = round(_price(t.get("price")), 6)
        cell = fp_map.setdefault(price, [0, 0, 0])  # [buy, sell, total]
        cell[2] += q
        d = t.get("direction")
        if d == "TRADE_DIRECTION_BUY":
            cell[0] += q
        elif d == "TRADE_DIRECTION_SELL":
            cell[1] += q
    footprint = []
    for price, (bv, sv, tot) in sorted(fp_map.items(),
                                       key=lambda kv: kv[1][2], reverse=True)[:3]:
        classified = bv + sv
        footprint.append({
            "price": price,
            "vol": tot,
            "buy_pct": round(bv / classified * 100, 1) if classified else 50.0,
        })

    return {
        "total_trades": n,
        "buy_pct":      buy_pct,
        "sell_pct":     sell_pct,
        "buy_volume":   buy_vol,
        "sell_volume":  sell_vol,
        "delta":        buy_vol - sell_vol,   # агрессивный buy−sell (снимок): + покупатели, − продавцы
        "order_flow":   flow,
        "avg_price":    avg_price,
        # ── диагностика/прозрачность (видно в /api/feed) ──
        "flow_method":       method,        # какой метод дал итог: direction | tick
        "flow_confidence":   confidence,    # high | low
        "buy_pct_direction": buy_pct_dir,   # оценка по полю Tinkoff
        "buy_pct_tick":      buy_pct_tick,  # оценка по tick-rule
        "direction_counts":  dir_counts,    # сырое распределение поля direction
        "footprint":         footprint,     # топ цен по впитанному объёму + buy%
    }


def _footprint_increment(trades: list[dict], since_ts: Optional[str]) -> dict:
    """
    Дедуп для КУМУЛЯТИВНОГО footprint: из сырых сделок берём только НОВЫЕ
    (time > since_ts) и бакетим по РЕАЛЬНОЙ цене (buy/sell/total). Пересекающиеся
    опросы (одни и те же последние 50 сделок) не задваиваются — фильтр по времени.
    Возвращает {buckets: {price_str: [buy, sell, total]}, watermark: max_ts, new: n}.
    """
    def _q(t) -> int:
        try:
            return int(t.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _price(p) -> float:
        p = p or {}
        return float(p.get("units", 0) or 0) + float(p.get("nano", 0) or 0) / 1e9

    buckets: dict[str, list] = {}
    new_n = 0
    for t in trades:
        ts = t.get("time")
        if not ts or (since_ts is not None and ts <= since_ts):
            continue
        q = _q(t)
        if q <= 0:
            continue
        new_n += 1
        price = str(round(_price(t.get("price")), 6))
        cell = buckets.setdefault(price, [0, 0, 0])
        cell[2] += q
        d = t.get("direction")
        if d == "TRADE_DIRECTION_BUY":
            cell[0] += q
        elif d == "TRADE_DIRECTION_SELL":
            cell[1] += q
    all_ts = [t.get("time") for t in trades if t.get("time")]
    watermark = max(all_ts) if all_ts else since_ts
    return {"buckets": buckets, "watermark": watermark, "new": new_n}


def minute_buckets(trades: list[dict], since_ts: Optional[str],
                   tz_shift_h: int = 3) -> dict:
    """
    Сделки, разложенные по МИНУТАМ. Дедуп тот же, что у footprint: берутся
    только сделки новее since_ts.

    Зачем отдельно от _footprint_increment. Тот бакетит по ЦЕНЕ и теряет время:
    из его результата нельзя получить ни 1m/5m/15m, ни число сделок, ни размеры
    сделок. Здесь ключ — минута, и в ней лежит всё для производных.

    ВРЕМЯ ПЕРЕВОДИТСЯ В МСК. Tinkoff отдаёт UTC, а торговая сессия и остальные
    данные в системе московские. Две зоны в одной таблице — источник ошибок,
    которые всплывают через недели.

    Возвращает {"rows": [...], "watermark": max_ts, "new": n}, где строка это
    {ts "YYYY-MM-DDTHH:MM", session, buy_volume, sell_volume, trade_count,
     max_trade, vwap_num}.
    """
    def _q(t) -> int:
        try:
            return int(t.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _price(p) -> float:
        p = p or {}
        return float(p.get("units", 0) or 0) + float(p.get("nano", 0) or 0) / 1e9

    def _msk_minute(iso: str):
        try:
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return (dt + timedelta(hours=tz_shift_h)).strftime("%Y-%m-%dT%H:%M")

    def _session(mk: str) -> str:
        m = int(mk[11:13]) * 60 + int(mk[14:16])
        if m < 9 * 60 + 50:
            return "morning"
        if m < 19 * 60:
            return "main"
        return "evening"

    acc: dict = {}
    new_n = 0
    for t in trades:
        ts = t.get("time")
        if not ts or (since_ts is not None and ts <= since_ts):
            continue
        q = _q(t)
        if q <= 0:
            continue
        mk = _msk_minute(ts)
        if not mk:
            continue
        new_n += 1
        price = _price(t.get("price"))
        cell = acc.setdefault(mk, {"ts": mk, "session": _session(mk),
                                   "buy_volume": 0, "sell_volume": 0,
                                   "trade_count": 0, "max_trade": 0,
                                   "vwap_num": 0.0})
        cell["trade_count"] += 1
        cell["max_trade"] = max(cell["max_trade"], q)
        cell["vwap_num"] += price * q
        d = t.get("direction")
        if d == "TRADE_DIRECTION_BUY":
            cell["buy_volume"] += q
        elif d == "TRADE_DIRECTION_SELL":
            cell["sell_volume"] += q
    all_ts = [t.get("time") for t in trades if t.get("time")]
    watermark = max(all_ts) if all_ts else since_ts
    return {"rows": [acc[k] for k in sorted(acc)],
            "watermark": watermark, "new": new_n}
