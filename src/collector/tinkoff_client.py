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

TINKOFF_BASE = "https://invest-public-api.tinkoff.ru/rest"

# FIGI основных тикеров MOEX (Financial Instrument Global Identifier)
TICKER_TO_FIGI: dict[str, str] = {
    "SBER":  "BBG004730N88",
    "GAZP":  "BBG004730RP0",
    "LKOH":  "BBG004731032",
    "GMKN":  "BBG004731489",
    "NVTK":  "BBG00475KKY8",
    "ROSN":  "BBG004731354",
    "YNDX":  "BBG006L8G4H1",
    "TATN":  "BBG004RVFFC0",
    "MTSS":  "BBG004S681B4",
    "MGNT":  "BBG004ZVJECK",
    "ALRS":  "BBG004S68B31",
    "PLZL":  "BBG000R607Y3",
    "VTBR":  "BBG004730ZJ9",
    "AFLT":  "BBG004S683W7",
    "MAGN":  "BBG004S686W0",
    "NLMK":  "BBG004S681M2",
    "CHMF":  "BBG005D0XH17",
    "PHOR":  "BBG004S689R0",
    "IRAO":  "BBG004S68473",
    "RUAL":  "BBG008F2T3T2",
    "SNGS":  "BBG004S686N0",
    "HYDR":  "BBG00475JZZ6",
    "FIVE":  "BBG00JXPFBN0",
    "OZON":  "BBG00Y91R9T3",
    "MOEX":  "BBG004730JJ5",
    "TCSG":  "BBG00QPYJ5H0",
    "SIBN":  "BBG004731SV2",
    "FLOT":  "BBG000LNHHJ9",
    "SMLT":  "BBG00F6NKQX3",
    "VKCO":  "BBG00178PGX3",
    "POSI":  "BBG0134B4V73",
    "ASTR":  "BBG016RTGJG2",
    "HEAD":  "BBG00KHGQ0H4",
    "WUSH":  "BBG00Y6FBGG4",
    "AFKS":  "BBG004S68614",
    "PIKK":  "BBG004S68BH6",
    "CBOM":  "BBG009GSYN76",
    "BSPB":  "BBG000QJW156",
    "MTLR":  "BBG004S68598",
    "LSRG":  "BBG0030S3MX5",
    "UPRO":  "BBG004S68829",
    "MSNG":  "BBG004S689B4",
    "TRNFP": "BBG00475KXX1",
    "DIAS":  "BBG0165B2XH5",
    "NKNC":  "BBG004S681N1",
    "EUTR":  "BBG004S6873G",
}


class TinkoffClient:
    """Клиент Tinkoff Invest REST API."""

    def __init__(self, token: str = None):
        self.token = token or os.getenv("TINKOFF_TOKEN", "")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _ok(self) -> bool:
        return bool(self.token)

    async def _post(self, endpoint: str, body: dict) -> Optional[dict]:
        if not self._ok():
            return None
        url = f"{TINKOFF_BASE}/{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=self.headers, json=body)
                if resp.status_code != 200:
                    logger.warning(f"Tinkoff API {endpoint}: {resp.status_code} {resp.text[:200]}")
                    return None
                return resp.json()
        except Exception as e:
            logger.warning(f"Tinkoff API error {endpoint}: {e}")
            return None

    async def get_figi(self, ticker: str) -> Optional[str]:
        """Получить FIGI по тикеру (сначала из кэша, потом из API)."""
        ticker = ticker.upper()
        if ticker in TICKER_TO_FIGI:
            return TICKER_TO_FIGI[ticker]
        # Поиск через API
        data = await self._post(
            "tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument",
            {"query": ticker, "instrumentKind": "INSTRUMENT_TYPE_SHARE", "apiTradeAvailableFlag": True},
        )
        if not data:
            return None
        instruments = data.get("instruments", [])
        for inst in instruments:
            if inst.get("ticker", "").upper() == ticker:
                figi = inst.get("figi")
                TICKER_TO_FIGI[ticker] = figi   # кэшируем
                return figi
        return None

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

        return {
            "best_bid":      bids[0]["price"] if bids else None,
            "best_ask":      asks[0]["price"] if asks else None,
            "spread_pct":    spread_pct,
            "bid_ask_ratio": bid_ask_ratio,
            "pressure":      pressure,
            "total_bid_qty": total_bid_qty,
            "total_ask_qty": total_ask_qty,
            "top_bids":      bids[:5],
            "top_asks":      asks[:5],
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
        from_ = (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")

        data = await self._post(
            "tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastTrades",
            {"figi": figi, "from": from_, "to": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
        if not data or "trades" not in data:
            return None

        def _price(p): return float(p.get("units", 0)) + float(p.get("nano", 0)) / 1e9

        trades = data["trades"][-limit:]
        buys  = [t for t in trades if t.get("direction") == "TRADE_DIRECTION_BUY"]
        sells = [t for t in trades if t.get("direction") == "TRADE_DIRECTION_SELL"]

        buy_vol  = sum(int(t.get("quantity", 0)) for t in buys)
        sell_vol = sum(int(t.get("quantity", 0)) for t in sells)
        total    = buy_vol + sell_vol

        buy_pct  = round(buy_vol  / total * 100, 1) if total else 50
        sell_pct = round(sell_vol / total * 100, 1) if total else 50

        if buy_pct >= 60:
            flow = "агрессивные покупки 🟢"
        elif sell_pct >= 60:
            flow = "агрессивные продажи 🔴"
        else:
            flow = "смешанный поток ⚪"

        avg_price = round(
            sum(_price(t.get("price", {})) * int(t.get("quantity", 0)) for t in trades) / total, 2
        ) if total else None

        return {
            "total_trades": len(trades),
            "buy_pct":      buy_pct,
            "sell_pct":     sell_pct,
            "buy_volume":   buy_vol,
            "sell_volume":  sell_vol,
            "order_flow":   flow,
            "avg_price":    avg_price,
        }

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
