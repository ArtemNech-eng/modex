"""
MOODEX — Macro Context
Макроэкономический фон для Claude:
  - Ключевая ставка ЦБ РФ
  - Курс USD/RUB
  - Индекс IMOEX (весь рынок)
  - Нефть Brent (фьючерс BR на MOEX)

Без макро Claude не понимает: SBER падает сам или весь рынок падает?
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

CBR_KEYRATE_URL = "https://www.cbr.ru/scripts/XML_keyrate.asp"
ISS_BASE = "https://iss.moex.com/iss"


async def _fetch_cbr_rate() -> Optional[float]:
    """Ключевая ставка ЦБ РФ из официального XML."""
    try:
        now = datetime.now(timezone.utc)
        params = {
            "date_req1": (now - timedelta(days=30)).strftime("%d/%m/%Y"),
            "date_req2": now.strftime("%d/%m/%Y"),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(CBR_KEYRATE_URL, params=params)
            resp.raise_for_status()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        rates = []
        for row in root.iter("KO"):
            date = row.get("Date", "")
            val  = row.get("Rate", "").replace(",", ".")
            if val:
                rates.append((date, float(val)))
        if rates:
            rates.sort(key=lambda x: x[0])
            return rates[-1][1]
    except Exception as e:
        logger.debug(f"CBR rate fetch failed: {e}")
    return None


async def _fetch_moex_index(ticker: str, days: int = 5) -> Optional[dict]:
    """Последние свечи индекса/инструмента с MOEX ISS."""
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        url = f"{ISS_BASE}/engines/stock/markets/index/securities/{ticker}/candles.json"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params={"interval": "24", "from": start})
            resp.raise_for_status()
        data     = resp.json().get("candles", {})
        cols     = data.get("columns", [])
        rows     = data.get("data", [])
        if not rows or "close" not in cols:
            return None
        ci = cols.index("close")
        closes = [r[ci] for r in rows if r[ci] is not None]
        if len(closes) < 2:
            return None
        change = (closes[-1] / closes[-2] - 1) * 100
        change5 = (closes[-1] / closes[0] - 1) * 100 if len(closes) >= 5 else None
        return {"price": closes[-1], "change_1d": round(change, 2),
                "change_5d": round(change5, 2) if change5 else None}
    except Exception as e:
        logger.debug(f"MOEX index {ticker} failed: {e}")
    return None


async def _fetch_currency(ticker: str = "USD000UTSTOM", days: int = 3) -> Optional[float]:
    """USD/RUB с MOEX валютного рынка."""
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        url = f"{ISS_BASE}/engines/currency/markets/selt/securities/{ticker}/candles.json"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params={"interval": "24", "from": start})
            resp.raise_for_status()
        data  = resp.json().get("candles", {})
        cols  = data.get("columns", [])
        rows  = data.get("data", [])
        if not rows or "close" not in cols:
            return None
        ci = cols.index("close")
        closes = [r[ci] for r in rows if r[ci] is not None]
        return closes[-1] if closes else None
    except Exception as e:
        logger.debug(f"USD/RUB fetch failed: {e}")
    return None


async def _fetch_brent(days: int = 5) -> Optional[dict]:
    """Нефть Brent — фьючерс BRH5/BRM5 на MOEX."""
    for ticker in ["BRH5", "BRM5", "BRU5", "BRZ5", "BRF6"]:
        try:
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            url = f"{ISS_BASE}/engines/futures/markets/forts/securities/{ticker}/candles.json"
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, params={"interval": "24", "from": start})
                resp.raise_for_status()
            data  = resp.json().get("candles", {})
            cols  = data.get("columns", [])
            rows  = data.get("data", [])
            if not rows or "close" not in cols:
                continue
            ci = cols.index("close")
            closes = [r[ci] for r in rows if r[ci] is not None]
            if len(closes) >= 2:
                change = (closes[-1] / closes[-2] - 1) * 100
                return {"price": closes[-1], "change_1d": round(change, 2), "ticker": ticker}
        except Exception:
            continue
    return None


async def get_macro_context() -> dict:
    """
    Полный макро-срез для Claude.
    Запросы идут параллельно — занимает ~1-2 сек.
    """
    import asyncio
    cbr_task   = _fetch_cbr_rate()
    imoex_task = _fetch_moex_index("IMOEX")
    usd_task   = _fetch_currency()
    brent_task = _fetch_brent()

    cbr_rate, imoex, usdrub, brent = await asyncio.gather(
        cbr_task, imoex_task, usd_task, brent_task,
        return_exceptions=True,
    )
    cbr_rate = cbr_rate if not isinstance(cbr_rate, Exception) else None
    imoex    = imoex    if not isinstance(imoex, Exception)    else None
    usdrub   = usdrub   if not isinstance(usdrub, Exception)   else None
    brent    = brent    if not isinstance(brent, Exception)    else None

    lines = ["🌍 МАКРОЭКОНОМИЧЕСКИЙ ФОН (Россия):"]

    if cbr_rate:
        comment = "высокая — давит на акции" if cbr_rate >= 16 else \
                  "умеренная" if cbr_rate >= 10 else "мягкая — поддерживает рынок"
        lines.append(f"  Ключевая ставка ЦБ РФ: {cbr_rate}% ({comment})")

    if imoex:
        arrow = "↑" if imoex["change_1d"] > 0 else "↓"
        lines.append(f"  IMOEX (весь рынок): {imoex['price']:.0f} пунктов, "
                     f"день: {arrow}{abs(imoex['change_1d']):.1f}%"
                     + (f", неделя: {imoex['change_5d']:+.1f}%" if imoex.get("change_5d") else ""))

    if usdrub:
        lines.append(f"  USD/RUB: {usdrub:.2f} ₽")

    if brent:
        arrow = "↑" if brent["change_1d"] > 0 else "↓"
        lines.append(f"  Нефть Brent ({brent['ticker']}): ${brent['price']:.1f}, "
                     f"день: {arrow}{abs(brent['change_1d']):.1f}%")

    # Общий вывод о рыночном контексте
    if imoex and imoex["change_1d"] < -1:
        lines.append("  ⚠️ Весь рынок падает — учитывай при анализе отдельной акции")
    elif imoex and imoex["change_1d"] > 1:
        lines.append("  📈 Весь рынок растёт — попутный ветер для лонгов")

    return {
        "cbr_rate": cbr_rate,
        "imoex":    imoex,
        "usdrub":   usdrub,
        "brent":    brent,
        "summary":  "\n".join(lines),
    }
