"""
ISS Trades — ground truth дельты по BUYSELL

19.08 ЗАФИКСИРОВАНО: flow.buy_pct/delta из Tinkoff инвертированы.
Примеры:
  CBOM -7.76% при bid/ask 0.43 но buy 93.8%
  ASTR бриф +6238 vs ISS -7586 (знак противоположный, модуль близок)
  NVTK/SBER аналогично

Ground truth:
  ISS trades.json поле BUYSELL:
    B = buyer aggressor = покупка по ask (снимает ликвидность с ask)
    S = seller aggressor = продажа по bid
  Это соответствует определению aggressor (investopedia):
    aggressor removes liquidity, buying at ask / selling at bid

Эндпоинты:
  trades: https://iss.moex.com/iss/engines/stock/markets/shares/securities/{TICKER}/trades.json
    params: iss.meta=off, iss.only=trades, limit=100, start=NUMTRADES-100
    limit !=100 молча возвращает 10 (замерено)
    reverse=true игнорируется
    TRADINGSESSION 0 утро 1 основная 2 вечер vs flow session morning/main

  marketdata: https://iss.moex.com/iss/engines/stock/markets/shares/securities/{TICKER}.json
    iss.only=marketdata, columns: BIDDEPTHT, OFFERDEPTHT, WAPRICE, NUMTRADES, LAST, etc
    valid: ASTR 1.29 vs 1.19 (пример)

  candles index: https://iss.moex.com/iss/engines/stock/markets/index/securities/{INDEX}/candles.json?interval=10
    INDEX = IMOEX, IMOEX2 — 07:00-23:50, в основную совпадает до копейки

Этот модуль — единственный источник дельты для торговли.
Tinkoff flow deprecated, использовать только для отладки с пометкой UNRELIABLE.
"""

import os
import json
import logging
import time
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

ISS_TRADES_URL = "https://iss.moex.com/iss/engines/stock/markets/shares/securities/{ticker}/trades.json"
ISS_MARKETDATA_URL = "https://iss.moex.com/iss/engines/stock/markets/shares/securities/{ticker}.json"
ISS_CANDLES_URL = "https://iss.moex.com/iss/engines/stock/markets/{market}/securities/{sec}/candles.json"

# Для индекса
ISS_INDEX_CANDLES_URL = "https://iss.moex.com/iss/engines/stock/markets/index/securities/{sec}/candles.json"


def _fetch_json_requests(url: str, params: dict, timeout: int = 15) -> Optional[dict]:
    """Попытка через requests с verify=False (для prod где MOEX доступен)"""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.get(url, params=params, timeout=timeout, verify=False)
        if r.status_code == 200:
            return r.json()
        logger.debug(f"ISS {url} status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.debug(f"ISS requests failed {url}: {e}")
    return None


def _fetch_json_curl(url: str, params: dict, timeout: int = 15) -> Optional[dict]:
    """Fallback через curl -k (иногда TLS в python ломается, а curl работает)"""
    try:
        import subprocess
        import urllib.parse
        qs = urllib.parse.urlencode(params)
        full = f"{url}?{qs}" if qs else url
        # curl -k -s --max-time
        res = subprocess.run(
            ["curl", "-k", "-s", "--max-time", str(timeout), full],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        if res.returncode == 0 and res.stdout:
            return json.loads(res.stdout)
    except Exception as e:
        logger.debug(f"ISS curl failed {url}: {e}")
    return None


def fetch_json(url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    params = params or {}
    # пробуем requests, затем curl
    data = _fetch_json_requests(url, params, timeout=timeout)
    if data is not None:
        return data
    data = _fetch_json_curl(url, params, timeout=timeout)
    return data


def parse_trades_payload(payload: dict) -> List[dict]:
    """Разбор trades блока ISS в список словарей по именам колонок"""
    if not payload:
        return []
    block = payload.get("trades") or payload.get("data") or {}
    # trades.json может иметь ключ 'trades'
    if isinstance(block, dict):
        cols = block.get("columns") or []
        rows = block.get("data") or []
    else:
        # иногда сразу в корне trades
        return []
    out = []
    for r in rows:
        if not isinstance(r, (list, tuple)):
            continue
        if len(r) != len(cols):
            continue
        d = dict(zip(cols, r))
        out.append(d)
    return out


def parse_marketdata_payload(payload: dict) -> Dict:
    """marketdata блок -> dict по колонкам первой строки"""
    if not payload:
        return {}
    block = payload.get("marketdata") or {}
    cols = block.get("columns") or []
    data = block.get("data") or []
    if not data:
        return {}
    row = data[0]
    if len(row) != len(cols):
        return {}
    return dict(zip(cols, row))


def compute_delta(trades: List[dict]) -> dict:
    """
    Считает дельту по BUYSELL:
      B = buy aggressor (buyer hits ask)
      S = sell aggressor (seller hits bid)

    Возвращает:
      buy_volume, sell_volume, delta, buy_pct, sell_pct,
      trade_count, vwap, total_volume, total_value
    """
    buy_vol = 0
    sell_vol = 0
    buy_val = 0.0
    sell_val = 0.0
    total_vol = 0
    total_val = 0.0
    vwap_num = 0.0
    count = 0

    for t in trades:
        try:
            qty = int(t.get("QUANTITY") or t.get("quantity") or 0)
            price = float(t.get("PRICE") or t.get("price") or 0)
            bs = (t.get("BUYSELL") or t.get("buysell") or "").strip().upper()
        except Exception:
            continue
        if qty <= 0 or price <= 0:
            continue
        count += 1
        total_vol += qty
        val = qty * price
        total_val += val
        vwap_num += val
        if bs == "B":
            buy_vol += qty
            buy_val += val
        elif bs == "S":
            sell_vol += qty
            sell_val += val
        else:
            # если BUYSELL пустой — не учитываем в buy/sell, но в total идёт
            pass

    tot = buy_vol + sell_vol
    delta = buy_vol - sell_vol
    buy_pct = round(buy_vol / tot * 100, 2) if tot else None
    sell_pct = round(sell_vol / tot * 100, 2) if tot else None
    vwap = round(vwap_num / total_vol, 6) if total_vol else None

    return {
        "buy_volume": buy_vol,
        "sell_volume": sell_vol,
        "delta": delta,
        "buy_pct": buy_pct,
        "sell_pct": sell_pct,
        "trade_count": count,
        "total_volume": total_vol,
        "total_value": total_val,
        "vwap": vwap,
        "buy_value": buy_val,
        "sell_value": sell_val,
        "imbalance": round(delta / tot, 4) if tot else None,
    }


def fetch_marketdata(ticker: str) -> dict:
    """Свод marketdata по тикеру: NUMTRADES, BIDDEPTHT, OFFERDEPTHT, WAPRICE, LAST"""
    url = ISS_MARKETDATA_URL.format(ticker=ticker.upper())
    params = {
        "iss.meta": "off",
        "iss.only": "marketdata",
        "marketdata.columns": "SECID,BOARDID,LAST,WAPRICE,BIDDEPTHT,OFFERDEPTHT,NUMTRADES,VALTODAY,VALTODAY_USD",
    }
    payload = fetch_json(url, params)
    return parse_marketdata_payload(payload)


def fetch_trades(ticker: str, limit: int = 100, offset: Optional[int] = None) -> List[dict]:
    """
    Свежие сделки по тикеру из ISS.
    limit должен быть 100 — другие значения молча дают 10.
    offset = start = NUMTRADES - limit для получения последних.
    """
    ticker = ticker.upper()
    # если offset не задан — пробуем получить NUMTRADES
    if offset is None:
        try:
            md = fetch_marketdata(ticker)
            nt = md.get("NUMTRADES")
            if nt:
                nt = int(nt)
                offset = max(0, nt - limit)
        except Exception:
            offset = None

    url = ISS_TRADES_URL.format(ticker=ticker)
    params = {"iss.meta": "off", "iss.only": "trades", "limit": 100}
    if offset is not None:
        params["start"] = int(offset)
    # TQBR board — основной
    # Иногда ISS требует board, но trades.json без board отдаёт все
    payload = fetch_json(url, params)
    trades = parse_trades_payload(payload)
    # фильтр по BOARDID если много
    # оставляем только TQBR если есть
    if trades:
        tqbr = [t for t in trades if (t.get("BOARDID") or "") == "TQBR"]
        if tqbr:
            return tqbr
    return trades


def fetch_trades_delta(ticker: str, limit: int = 100) -> dict:
    """Удобный враппер: сделки + дельта"""
    trades = fetch_trades(ticker, limit=limit)
    delta = compute_delta(trades)
    delta["ticker"] = ticker.upper()
    delta["trades_sample"] = trades[:5]  # для отладки
    delta["source"] = "ISS trades.json BUYSELL ground truth"
    return delta


def aggregate_trades_by_minute(trades: List[dict]) -> List[dict]:
    """
    Группировка сделок по минуте TRADETIME (МСК).
    TRADETIME в ISS обычно "HH:MM:SS" или "YYYY-MM-DD HH:MM:SS"
    Возвращает список баров: ts, buy, sell, delta, cumulative_delta, trade_count, vwap
    """
    from collections import defaultdict

    buckets = defaultdict(lambda: {"buy": 0, "sell": 0, "count": 0, "vwap_num": 0.0, "vol": 0})

    for t in trades:
        try:
            qty = int(t.get("QUANTITY") or 0)
            price = float(t.get("PRICE") or 0)
            bs = (t.get("BUYSELL") or "").strip().upper()
            tt = str(t.get("TRADETIME") or t.get("TRADETIME") or "")
            # парсим время: ищем HH:MM
            # форматы: "10:05:23" или "2026-08-19 10:05:23"
            if " " in tt:
                date_part, time_part = tt.split(" ", 1)
            else:
                time_part = tt
                date_part = ""
            hhmm = time_part[:5]  # "HH:MM"
            # ts как "YYYY-MM-DDTHH:MM" если дата есть, иначе только HH:MM
            if date_part and len(date_part) >= 10:
                ts = f"{date_part[:10]}T{hhmm}"
            else:
                # без даты — используем сегодня МСК
                today = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
                ts = f"{today}T{hhmm}"
        except Exception:
            continue

        b = buckets[ts]
        b["vol"] += qty
        b["vwap_num"] += qty * price
        b["count"] += 1
        if bs == "B":
            b["buy"] += qty
        elif bs == "S":
            b["sell"] += qty

    # сортировка по времени и кумулятивная дельта
    out = []
    cum = 0
    for ts in sorted(buckets.keys()):
        b = buckets[ts]
        tot = b["buy"] + b["sell"]
        delta = b["buy"] - b["sell"]
        cum += delta
        out.append({
            "ts": ts,
            "buy_volume": b["buy"],
            "sell_volume": b["sell"],
            "delta": delta,
            "cumulative_delta": cum,
            "trade_count": b["count"],
            "volume": b["vol"],
            "vwap": round(b["vwap_num"] / b["vol"], 6) if b["vol"] else None,
            "buy_pct": round(b["buy"] / tot * 100, 2) if tot else None,
        })
    return out


# ── Индексные свечи IMOEX / IMOEX2 ────────────────────────────────────────────

def parse_candles_payload(payload: dict) -> List[dict]:
    """candles блок -> список словарей"""
    if not payload:
        return []
    block = payload.get("candles") or {}
    cols = block.get("columns") or []
    rows = block.get("data") or []
    out = []
    for r in rows:
        if len(r) != len(cols):
            continue
        out.append(dict(zip(cols, r)))
    return out


def fetch_index_candles(index: str = "IMOEX2", interval: int = 10,
                        from_date: Optional[str] = None,
                        till_date: Optional[str] = None) -> List[dict]:
    """
    Свечи индекса IMOEX / IMOEX2 интервал 10м.
    IMOEX2 07:00-23:50, в основную совпадает с IMOEX до копейки (проверено).
    """
    index = index.upper()
    url = ISS_INDEX_CANDLES_URL.format(sec=index)
    if from_date is None:
        from_date = (datetime.now(timezone.utc) + timedelta(hours=3) - timedelta(days=7)).strftime("%Y-%m-%d")
    if till_date is None:
        till_date = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    params = {
        "iss.meta": "off",
        "interval": interval,
        "from": from_date,
        "till": till_date,
    }
    payload = fetch_json(url, params)
    return parse_candles_payload(payload)


def compute_index_buckets(candles: List[dict]) -> dict:
    """
    Разбор индексных 10м бакетов для протокола 19.08.
    Возвращает:
      morning_value: сумма value 07:00-09:49
      first_hour_value: сумма 09:50-10:49 (6 бакетов по 10м)
      buckets: список с open/close/value
    """
    morning_val = 0.0
    first_hour_val = 0.0
    buckets = []

    for c in candles:
        try:
            begin = str(c.get("begin") or c.get("BEGIN") or "")
            # begin "2026-08-19 07:00:00"
            if " " in begin:
                date_part, time_part = begin.split(" ", 1)
                hh = int(time_part.split(":")[0])
                mm = int(time_part.split(":")[1])
                minutes = hh * 60 + mm
            else:
                continue
            open_p = float(c.get("open") or c.get("OPEN") or 0)
            close_p = float(c.get("close") or c.get("CLOSE") or 0)
            value = float(c.get("value") or c.get("VALUE") or 0)
            # иногда value=0 для индекса, тогда пробуем volume?
            if value == 0:
                value = float(c.get("volume") or c.get("VOLUME") or 0)
        except Exception:
            continue

        buckets.append({
            "begin": begin,
            "open": open_p,
            "close": close_p,
            "value": value,
            "change": close_p - open_p if open_p else 0,
        })

        # утренняя сессия 07:00-09:49
        if 7 * 60 <= minutes <= 9 * 60 + 49:
            morning_val += value
        # первый час основной 09:50-10:49
        if 9 * 60 + 50 <= minutes <= 10 * 60 + 49:
            first_hour_val += value

    return {
        "morning_value": morning_val,
        "first_hour_value": first_hour_val,
        "buckets": buckets,
    }


# ── Эффективность потока ──────────────────────────────────────────────────────

def flow_efficiency(price_move_pct: float, delta_pct_of_day: float) -> Optional[float]:
    """
    Эффективность потока = % хода цены на 1% дневного объёма в чистой дельте
    Старая метрика "пунктов на 100k лотов" непригодна кросс-бумажно (разные цены лотов).
    Новая: нормированная на % дневного объёма.

    Пример: TATN утро 17.4%/76%/6.3% etc.
    """
    if delta_pct_of_day is None or delta_pct_of_day == 0:
        return None
    try:
        return round(price_move_pct / delta_pct_of_day, 4)
    except Exception:
        return None
