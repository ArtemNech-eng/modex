"""
MOODEX — Fundamentals
Фундаментальные данные по тикеру из Tinkoff Invest API.
P/E, дивиденды, сектор, капитализация — контекст для стоимостной оценки.
"""
import logging
import os
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

TINKOFF_BASE = "https://invest-public-api.tinkoff.ru/rest"

# Известные мультипликаторы (статичные данные как fallback)
FUNDAMENTALS_CACHE: dict[str, dict] = {
    "SBER":  {"sector": "Финансы",       "div_yield": 10.8, "pe": 3.8},
    "GAZP":  {"sector": "Нефть и газ",   "div_yield": 0.0,  "pe": 2.1},
    "LKOH":  {"sector": "Нефть и газ",   "div_yield": 13.2, "pe": 5.2},
    "GMKN":  {"sector": "Металлы",       "div_yield": 8.1,  "pe": 7.3},
    "NVTK":  {"sector": "Нефть и газ",   "div_yield": 3.2,  "pe": 9.1},
    "YNDX":  {"sector": "Технологии",    "div_yield": 0.0,  "pe": 22.4},
    "TCSG":  {"sector": "Финансы",       "div_yield": 4.1,  "pe": 6.8},
    "VTBR":  {"sector": "Финансы",       "div_yield": 15.2, "pe": 2.1},
    "ROSN":  {"sector": "Нефть и газ",   "div_yield": 6.8,  "pe": 3.9},
    "TATN":  {"sector": "Нефть и газ",   "div_yield": 14.1, "pe": 4.8},
    "PLZL":  {"sector": "Металлы",       "div_yield": 2.3,  "pe": 8.7},
    "MGNT":  {"sector": "Ритейл",        "div_yield": 11.2, "pe": 7.1},
    "NLMK":  {"sector": "Металлы",       "div_yield": 12.3, "pe": 4.2},
    "CHMF":  {"sector": "Металлы",       "div_yield": 13.8, "pe": 4.6},
    "MAGN":  {"sector": "Металлы",       "div_yield": 9.4,  "pe": 3.8},
    "POSI":  {"sector": "Технологии",    "div_yield": 5.2,  "pe": 18.3},
    "OZON":  {"sector": "Технологии",    "div_yield": 0.0,  "pe": None},
    "ALRS":  {"sector": "Металлы",       "div_yield": 4.1,  "pe": 6.2},
    "HYDR":  {"sector": "Электроэнергия","div_yield": 7.8,  "pe": 4.1},
    "AFLT":  {"sector": "Транспорт",     "div_yield": 0.0,  "pe": 5.3},
    "FIVE":  {"sector": "Ритейл",        "div_yield": 12.1, "pe": 8.4},
    "MOEX":  {"sector": "Финансы",       "div_yield": 9.8,  "pe": 7.2},
    "VKCO":  {"sector": "Технологии",    "div_yield": 0.0,  "pe": None},
    "FLOT":  {"sector": "Транспорт",     "div_yield": 11.3, "pe": 4.9},
    "SMLT":  {"sector": "Девелопмент",   "div_yield": 2.1,  "pe": 6.8},
    "PIKK":  {"sector": "Девелопмент",   "div_yield": 3.4,  "pe": 7.2},
    "HEAD":  {"sector": "Технологии",    "div_yield": 22.1, "pe": 9.8},
    "SNGS":  {"sector": "Нефть и газ",   "div_yield": 3.2,  "pe": 2.8},
    "IRAO":  {"sector": "Электроэнергия","div_yield": 8.9,  "pe": 3.1},
    "RUAL":  {"sector": "Металлы",       "div_yield": 0.0,  "pe": 4.3},
}


async def get_fundamentals(ticker: str, tinkoff_token: str = None) -> dict:
    """
    Фундаментальные данные тикера.
    Сначала пробует Tinkoff API, потом fallback на статичный кэш.
    """
    ticker = ticker.upper()
    token  = tinkoff_token or os.getenv("TINKOFF_TOKEN", "")

    result = FUNDAMENTALS_CACHE.get(ticker, {}).copy()

    # Пробуем получить актуальные данные из Tinkoff
    if token:
        try:
            from src.collector.tinkoff_client import TICKER_TO_FIGI
            figi = TICKER_TO_FIGI.get(ticker)
            if figi:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{TINKOFF_BASE}/tinkoff.public.invest.api.contract.v1"
                        f".InstrumentsService/GetShareBy",
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                        json={"idType": "INSTRUMENT_ID_TYPE_FIGI", "id": figi},
                    )
                if resp.status_code == 200:
                    data = resp.json().get("instrument", {})
                    if data.get("sector"):
                        result["sector"] = data["sector"]
                    if data.get("lot"):
                        result["lot"] = data["lot"]
                    if data.get("countryOfRisk"):
                        result["country"] = data["countryOfRisk"]
        except Exception as e:
            logger.debug(f"Tinkoff fundamentals {ticker}: {e}")

    # Форматируем для Claude
    lines = [f"📊 ФУНДАМЕНТАЛЬНЫЕ ДАННЫЕ {ticker}:"]
    if result.get("sector"):
        lines.append(f"  Сектор: {result['sector']}")
    if result.get("pe") is not None:
        pe_comment = "дёшево" if result["pe"] < 5 else "дорого" if result["pe"] > 15 else "норма"
        lines.append(f"  P/E: {result['pe']} ({pe_comment} для рынка РФ)")
    if result.get("div_yield") is not None:
        div_comment = "высокая" if result["div_yield"] > 10 else \
                      "средняя" if result["div_yield"] > 5 else \
                      "нет/низкая"
        lines.append(f"  Дивидендная доходность: {result['div_yield']}% ({div_comment})")

    result["summary"] = "\n".join(lines)
    return result
