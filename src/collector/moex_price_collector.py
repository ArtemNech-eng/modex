"""
MOODEX — MOEX Price Collector
Загружает исторические и текущие цены акций через официальный
бесплатный API Московской биржи (ISS API).

Документация: https://iss.moex.com/iss/reference/
Не требует авторизации.
"""
import asyncio
import logging
from datetime import datetime, date, timedelta, timezone
from dataclasses import dataclass
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

MOEX_ISS = "https://iss.moex.com/iss"


@dataclass
class Candle:
    ticker: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime

    @property
    def change_pct(self) -> float:
        """Изменение в % от открытия"""
        if self.open == 0:
            return 0.0
        return (self.close - self.open) / self.open * 100


@dataclass
class CorrelationResult:
    ticker: str
    company_name: str
    correlation: float          # [-1, +1]
    lead_minutes: int           # на сколько минут настроение опережает цену
    sample_size: int            # кол-во точек данных
    signal_accuracy: float      # % правильных предсказаний направления


class MOEXPriceCollector:
    """
    Загружает данные о ценах с Московской биржи.
    """

    def __init__(self):
        self._cache: dict[str, list[Candle]] = {}
        self._last_update: dict[str, datetime] = {}

    async def get_candles(
        self,
        ticker: str,
        interval: int = 1,          # 1 = 1 минута, 10 = 10 минут, 60 = 1 час
        from_date: Optional[date] = None,
        till_date: Optional[date] = None,
    ) -> list[Candle]:
        """
        Загрузить свечи по тикеру.

        interval: 1, 10, 60 (минуты), 24 (день), 7 (неделя), 31 (месяц)
        """
        from_date = from_date or (date.today() - timedelta(days=7))
        till_date = till_date or date.today()

        url = (
            f"{MOEX_ISS}/engines/stock/markets/shares/boards/TQBR"
            f"/securities/{ticker}/candles.json"
        )
        params = {
            "interval": interval,
            "from": from_date.isoformat(),
            "till": till_date.isoformat(),
            "start": 0,
        }

        candles = []
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, params=params, timeout=15)
                if resp.status_code != 200:
                    logger.warning(f"MOEX API вернул {resp.status_code} для {ticker}")
                    return []

                data = resp.json()
                columns = data["candles"]["columns"]
                rows = data["candles"]["data"]

                col_map = {col: i for i, col in enumerate(columns)}

                for row in rows:
                    ts_str = row[col_map["begin"]]
                    try:
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        ts = ts.replace(tzinfo=timezone.utc)
                    except Exception:
                        continue

                    candles.append(Candle(
                        ticker=ticker,
                        open=float(row[col_map["open"]] or 0),
                        high=float(row[col_map["high"]] or 0),
                        low=float(row[col_map["low"]] or 0),
                        close=float(row[col_map["close"]] or 0),
                        volume=float(row[col_map["volume"]] or 0),
                        timestamp=ts,
                    ))

                logger.info(f"📈 MOEX: загружено {len(candles)} свечей для {ticker}")

            except Exception as e:
                logger.error(f"Ошибка загрузки цен {ticker}: {e}")

        return candles

    async def get_current_price(self, ticker: str) -> Optional[float]:
        """Получить текущую цену тикера"""
        url = f"{MOEX_ISS}/engines/stock/markets/shares/boards/TQBR/securities/{ticker}.json"
        params = {"iss.meta": "off", "iss.only": "marketdata"}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, timeout=10)
                data = resp.json()
                cols = data["marketdata"]["columns"]
                rows = data["marketdata"]["data"]
                if not rows:
                    return None
                col_map = {c: i for i, c in enumerate(cols)}
                last = rows[0][col_map.get("LAST", 0)]
                return float(last) if last else None
        except Exception:
            return None

    async def get_multiple_prices(self, tickers: list[str]) -> dict[str, float]:
        """Загрузить текущие цены для нескольких тикеров одним запросом"""
        url = f"{MOEX_ISS}/engines/stock/markets/shares/boards/TQBR/securities.json"
        params = {"iss.meta": "off", "iss.only": "marketdata", "securities": ",".join(tickers)}
        prices = {}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, timeout=15)
                data = resp.json()
                cols = data["marketdata"]["columns"]
                rows = data["marketdata"]["data"]
                col_map = {c: i for i, c in enumerate(cols)}
                for row in rows:
                    ticker = row[col_map.get("SECID", 0)]
                    last = row[col_map.get("LAST", 1)]
                    if ticker and last:
                        prices[ticker] = float(last)
        except Exception as e:
            logger.error(f"Ошибка загрузки цен: {e}")
        return prices
