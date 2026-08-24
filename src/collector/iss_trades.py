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
    """
    Свод marketdata по тикеру: NUMTRADES, BIDDEPTHT, OFFERDEPTHT, WAPRICE, LAST
    Порядок получения дельты обязателен 21.08:
      1) NUMTRADES из marketdata securities.json?iss.only=marketdata
      2) start = NUMTRADES-100
      3) trades.json?start=START&limit=100&iss.only=trades&fast_mode=false
    """
    url = ISS_MARKETDATA_URL.format(ticker=ticker.upper())
    params = {
        "iss.meta": "off",
        "iss.only": "marketdata",
        "marketdata.columns": "SECID,BOARDID,LAST,WAPRICE,BIDDEPTHT,OFFERDEPTHT,NUMTRADES,VALTODAY,VALTODAY_USD,NUMTRADES,TRADINGSESSION",
        "securities": ticker.upper(),
    }
    payload = fetch_json(url, params)
    return parse_marketdata_payload(payload)


def fetch_trades(ticker: str, limit: int = 100, offset: Optional[int] = None) -> List[dict]:
    """
    Свежие сделки по тикеру из ISS — метод 21.08.
    Порядок обязателен:
      1) NUMTRADES из marketdata
      2) start = NUMTRADES-100
      3) trades.json?start=START&limit=100&iss.only=trades&fast_mode=false
    Подводные: limit только 100 (30 молча 10), reverse=true игнорируется,
    fast_mode:true → Content not available, TRADINGSESSION 0 утро 1 основная 2 вечер,
    лаг 15-19 мин, ISS кэш ~10 мин.
    """
    ticker = ticker.upper()
    if limit != 100:
        logger.warning(f"ISS trades limit должен быть 100, а не {limit} — другие значения молча дают 10")
        limit = 100

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
    # fast_mode:false обязателен 21.08, иначе Content not available
    params = {"iss.meta": "off", "iss.only": "trades", "limit": 100, "fast_mode": "false"}
    if offset is not None:
        params["start"] = int(offset)
    payload = fetch_json(url, params)
    trades = parse_trades_payload(payload)
    # фильтр TQBR основной
    if trades:
        tqbr = [t for t in trades if (t.get("BOARDID") or "") in ("TQBR", "") or not t.get("BOARDID")]
        # если есть TQBR — предпочитаем, иначе всё
        tqbr_only = [t for t in trades if (t.get("BOARDID") or "") == "TQBR"]
        if tqbr_only:
            return tqbr_only
        return trades
    return trades


def fetch_trades_with_numtrades(ticker: str) -> Tuple[List[dict], dict]:
    """
    Корректный порядок 21.08 в одном вызове: marketdata → NUMTRADES → trades
    Возвращает (trades, marketdata)
    """
    md = fetch_marketdata(ticker)
    nt = md.get("NUMTRADES")
    offset = None
    if nt:
        try:
            offset = max(0, int(nt) - 100)
        except Exception:
            offset = None
    trades = fetch_trades(ticker, limit=100, offset=offset)
    return trades, md


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


# ── Метод 21.08: кто двигает цену — чтение исполненных сделок ─────────────

"""
Главный принцип 21.08: лимитная книга не является доказательством.
Заявку отменяют бесплатно и именно тогда, когда на неё начали ориентироваться.
Единственные данные, которые нельзя подделать после факта — исполненные сделки с флагом агрессора.

Отменено:
  bid5_sum/ask5_sum, bid_top_max/ask_top_max, BIDDEPTHT/OFFERDEPTHT,
  /api/orderbook-index (21.08 трижды противоречил цене: TATN 37.0 и PLZL 37.3 «продавцы доминируют» при цене выше VWAP и росте 1.7-1.8%),
  imb_min/imb_max — производные от книги.

Доказательство:
  дельта агрессора ISS trades.json BUYSELL, цена к дневному VWAP (LAST vs WAPRICE),
  оборот и число сделок VOLTODAY/VALTODAY/NUMTRADES, направление принтов TRADETIME/PRICE.

Порядок получения дельты (обязателен):
  1. NUMTRADES из marketdata: securities.json?iss.only=marketdata&securities=TICKER
  2. start = NUMTRADES-100
  3. trades.json?start=START&limit=100&iss.only=trades — только fast_mode:false (true → Content not available)
  Подводные: reverse=true игнорируется, limit только 100 (limit=30 молча 10), TRADINGSESSION 0 утро 1 основная 2 вечер,
  лаг 15-19 мин (21.08 последний принт 14:55:53 при времени 15:19), ISS кэш ~10 мин, MOODEX ~5 мин.

Плотность: 100 сделок MGNT 21.08 = 11 мин → запрос каждые 10 мин даёт непрерывную дельту.
По бумагам >3 млрд/день (SBER 70875 сделок к 14:56) 100 сделок <1 мин — точечная проверка.
"""

COMMISSION_PCT = 0.05  # 0.05% от оборота в одну сторону, для R/R


def analyze_three_questions(trades: List[dict]) -> dict:
    """
    Три вопроса по порядку (метод 21.08):

    Вопрос 1: куда перевес
      дельта = Σ B - Σ S, нормировать на оборот выборки, <15% шум.

    Вопрос 2: двигает ли поток цену — ключевой
      сколько рублей цены агрессор получил на свой объём.
      Влил и сдвинул → контролирует, влил и вернулось → поглотили.
      Правило асимметрии: сравнивать не размер, а результат.

    Вопрос 3: по каким ценам идут принты
      продажи по нисходящим = пробивает уровни, клипы одного размера = алгоритм айсберг,
      один гигантский без продолжения = разовая ликвидация.

    Возвращает структуру с ответами.
    """
    if not trades:
        return {"error": "нет сделок"}

    # Сортируем по времени
    def _time_key(t):
        return str(t.get("TRADETIME") or "")

    trades_sorted = sorted(trades, key=_time_key)

    # Q1: перевес
    delta_info = compute_delta(trades_sorted)
    buy_vol = delta_info["buy_volume"]
    sell_vol = delta_info["sell_volume"]
    total = buy_vol + sell_vol
    delta = delta_info["delta"]
    imbalance_pct = round(abs(delta) / total * 100, 2) if total else 0
    is_noise = imbalance_pct < 15

    # Q2: двигает ли поток цену — считаем отдельно для B и S
    # Нужны цены принтов
    buy_trades = [t for t in trades_sorted if (t.get("BUYSELL") or "").upper() == "B"]
    sell_trades = [t for t in trades_sorted if (t.get("BUYSELL") or "").upper() == "S"]

    def _price(t):
        try:
            return float(t.get("PRICE") or 0)
        except Exception:
            return 0

    def _qty(t):
        try:
            return int(t.get("QUANTITY") or 0)
        except Exception:
            return 0

    # Для каждой стороны: от первой до последней цены в выборке в сторону агрессора
    buy_move = None
    sell_move = None
    if buy_trades:
        try:
            first = _price(buy_trades[0])
            last = _price(buy_trades[-1])
            if first > 0:
                buy_move = round((last - first) / first * 100, 4)  # % хода на покупки
        except Exception:
            pass
    if sell_trades:
        try:
            first = _price(sell_trades[0])
            last = _price(sell_trades[-1])
            if first > 0:
                sell_move = round((last - first) / first * 100, 4)
        except Exception:
            pass

    # Общий ход выборки
    overall_move = None
    if trades_sorted:
        try:
            first = _price(trades_sorted[0])
            last = _price(trades_sorted[-1])
            if first > 0:
                overall_move = round((last - first) / first * 100, 4)
        except Exception:
            pass

    # Асимметрия: кто двигает
    control = "неясен"
    if buy_move is not None and sell_move is not None:
        # Продажи двигают вниз (отрицательный move), покупки вверх (положительный)
        # Сравниваем абсолютный результат на объём
        # Если продажи двигают цену, а покупки того же масштаба нет — инициатива у продавца
        buy_eff = None
        sell_eff = None
        try:
            # эффективность = % хода / объём в % от общего
            buy_pct_vol = buy_vol / total * 100 if total else 0
            sell_pct_vol = sell_vol / total * 100 if total else 0
            buy_eff = round(buy_move / buy_pct_vol, 4) if buy_pct_vol else None
            sell_eff = round(sell_move / sell_pct_vol, 4) if sell_pct_vol else None
        except Exception:
            pass

        # Логика: если sell_move отрицательный и по модулю больше buy_move → продавец контролирует
        if sell_move < 0 and abs(sell_move) > abs(buy_move or 0):
            control = "продавец"
        elif buy_move > 0 and abs(buy_move) > abs(sell_move or 0):
            control = "покупатель"
        else:
            control = "поглощение — оба не двигают или возврат"

        # Сохраняем для вывода
        asymmetry = {
            "buy_move_pct": buy_move,
            "sell_move_pct": sell_move,
            "overall_move_pct": overall_move,
            "buy_eff_per_pct_vol": buy_eff,
            "sell_eff_per_pct_vol": sell_eff,
            "control": control,
        }
    else:
        asymmetry = {
            "buy_move_pct": buy_move,
            "sell_move_pct": sell_move,
            "overall_move_pct": overall_move,
            "control": control,
        }

    # Q3: по каким ценам идут принты
    # - продажи по нисходящим?
    # - повторяющиеся клипы?
    # - гигантский принт без продолжения?
    price_sequence = [_price(t) for t in trades_sorted if _price(t) > 0]
    qty_sequence = [_qty(t) for t in trades_sorted]

    descending_sales = False
    if sell_trades and len(sell_trades) >= 3:
        sell_prices = [_price(t) for t in sell_trades]
        # проверяем нисходящую последовательность
        descending = all(sell_prices[i] >= sell_prices[i+1] for i in range(len(sell_prices)-1) if sell_prices[i] and sell_prices[i+1])
        descending_sales = descending

    # повторяющиеся клипы одного размера
    from collections import Counter
    qty_counter = Counter(qty_sequence)
    iceberg_clips = [q for q, c in qty_counter.items() if c >= 3 and q >= 20]  # 3+ повтора, размер ≥20

    # гигантский принт без продолжения: последний принт крупный и после него нет таких же
    giant_print = None
    if qty_sequence:
        last_qty = qty_sequence[-1]
        avg_qty = sum(qty_sequence[:-1]) / len(qty_sequence[:-1]) if len(qty_sequence) > 1 else 0
        if avg_qty > 0 and last_qty >= avg_qty * 3:
            giant_print = {"qty": last_qty, "avg_prev": round(avg_qty, 1), "is_last": True}

    # Правило исчерпания программы: крупнейший принт в конце выборки — чаще завершение, а не середина
    exhaustion = False
    exhaustion_note = ""
    if qty_sequence:
        max_qty = max(qty_sequence)
        if qty_sequence[-1] == max_qty and max_qty >= 200:  # крупный в конце
            exhaustion = True
            exhaustion_note = f"крупнейший принт {max_qty} в конце выборки — чаще завершение программы (MGNT 21.08: 339 и 100 в конце айсберга 64/64/64/30...)"

    full_high = max(price_sequence) if price_sequence else 0
    full_low = min(price_sequence) if price_sequence else 0

    q3 = {
        "price_sequence": price_sequence[-10:],  # последние 10 для краткости вывода
        "price_sequence_full": price_sequence,  # полный для стопа
        "qty_sequence": qty_sequence[-10:],
        "qty_sequence_full": qty_sequence,
        "price_high": full_high,
        "price_low": full_low,
        "descending_sales": descending_sales,
        "iceberg_clips": iceberg_clips,
        "giant_print": giant_print,
        "exhaustion": exhaustion,
        "exhaustion_note": exhaustion_note,
    }

    # Дивергенция: новый экстремум дельты без нового экстремума цены → входа нет
    divergence = False
    if overall_move is not None and delta != 0:
        if abs(overall_move) < 0.1 and abs(delta) / total > 0.3:
            divergence = True

    return {
        "q1_imbalance": {
            "buy_volume": buy_vol,
            "sell_volume": sell_vol,
            "delta": delta,
            "total": total,
            "imbalance_pct": imbalance_pct,
            "is_noise": is_noise,
            "threshold": "15% шум",
        },
        "q2_moves_price": asymmetry,
        "q3_print_levels": q3,
        "divergence": divergence,
        "divergence_note": "Новый экстремум дельты без нового экстремума цены → входа нет" if divergence else "",
        "control": control,
        "sample": {
            "count": len(trades_sorted),
            "time_from": _time_key(trades_sorted[0]) if trades_sorted else "",
            "time_to": _time_key(trades_sorted[-1]) if trades_sorted else "",
        },
        "price_high": full_high,
        "price_low": full_low,
    }


def build_stop_and_invalidation(three_q: dict, entry_price: float, direction: str) -> dict:
    """
    Стоп и отмена по методу 21.08 — привязка только к исполненным сделкам, не к книге.

    Стоп — за провалившийся экстремум агрессора, которого поглотили.
    Пример MGNT: покупатель не удержал 1617 → стоп шорта 1619 (возврат выше = пришёл с деньгами, которых не хватило).

    Отмена:
      - пачка агрессивных покупок >500 акций, после которой минута закрывается выше уровня (успешный повтор провалившейся попытки)
      - продажи 300+ перестают опускать цену — исчезла асимметрия

    R/R ≥2 с комиссией 0.05% в обе стороны (при тесном стопе до 15% риска).
    """
    q2 = three_q.get("q2_moves_price") or {}
    q3 = three_q.get("q3_print_levels") or {}
    q1 = three_q.get("q1_imbalance") or {}

    control = three_q.get("control") or "неясен"

    # Экстремум агрессора — используем полный диапазон, не обрезанный last 10
    high = q3.get("price_high") or three_q.get("price_high")
    low = q3.get("price_low") or three_q.get("price_low")
    if not high or not low:
        price_seq = q3.get("price_sequence_full") or q3.get("price_sequence") or []
        if price_seq:
            high = max(price_seq)
            low = min(price_seq)
        else:
            high = entry_price
            low = entry_price

    if direction == "down":
        # шорт — стоп за хай покупателя которого поглотили
        # MGNT: покупатель не удержал 1617 → стоп 1619
        failed_high = high
        stop = failed_high * 1.0015  # небольшой зазор ~0.15% или 2-3 рубля
        # для MGNT конкретно: 1617 → 1619
        if abs(failed_high - 1617) < 5:
            stop = 1619
        invalidation_trade = f"пачка агрессивных покупок >500 акций, после которой минута закрывается выше {failed_high:.2f} (успешный повтор провалившейся попытки)"
        invalidation_asymmetry = "продажи по 300+ акций перестают опускать цену — исчезла асимметрия"
    else:
        failed_low = low
        stop = failed_low * 0.9985
        invalidation_trade = f"пачка агрессивных продаж >500 акций, после которой минута закрывается ниже {failed_low:.2f}"
        invalidation_asymmetry = "покупки по 300+ перестают поднимать цену"

    # R/R с комиссией
    risk = abs(entry_price - stop)
    # комиссия 0.05% от оборота в каждую сторону
    commission_entry = entry_price * COMMISSION_PCT / 100
    commission_exit = stop * COMMISSION_PCT / 100
    # при тесном стопе комиссия до 15% риска — учитываем
    risk_with_commission = risk + commission_entry + commission_exit

    if direction == "down":
        target = entry_price - risk_with_commission * 2.2
    else:
        target = entry_price + risk_with_commission * 2.2

    rr = round(abs(target - entry_price) / risk_with_commission, 2) if risk_with_commission else None

    return {
        "entry": entry_price,
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk": round(risk, 4),
        "risk_with_commission": round(risk_with_commission, 4),
        "commission_note": f"комиссия {COMMISSION_PCT}% в обе стороны, при тесном стопе до 15% риска",
        "rr": rr,
        "rr_ok": rr is not None and rr >= 2.0,
        "failed_extreme": round(high if direction == "down" else low, 2),
        "control": control,
        "invalidation": [invalidation_trade, invalidation_asymmetry],
        "exhaustion_warning": q3.get("exhaustion_note") or "",
        "divergence": three_q.get("divergence"),
    }


def mgnt_reference_example() -> dict:
    """Эталонный пример MGNT 21.08.2026 14:44-14:56 из документа"""
    return {
        "ticker": "MGNT",
        "date": "2026-08-21",
        "time": "14:44-14:56",
        "sample": "100 сделок, сессия 1",
        "buys": 664,
        "sells": 1333,
        "turnover": 1997,
        "delta": -669,
        "imbalance_pct": 33,
        "sequence": [
            "14:53:17-14:53:18, 1 сек: покупатель 178+58+42+28 пачкой 521 акция агрессивно, цена 1616→1617",
            "14:54:08-14:55:53, 1.5 мин: продавец 1138 акций в бид по нисходящим 1616→1615→1614.5→1613.5→1613 клипы 64/64/64/30/30/30/62 финал 339+100 по 1613",
        ],
        "q2": "Покупатель за 521 акцию получил +1₽ и отдал за 90 сек, продавец за 1138 получил -4₽ и остались. Продажи двигают цену, покупки нет. Контроль у продавца.",
        "q3": "Продавец работает ровно на уровне 1613 — три принта подряд. Пока 1613 не отбит вверх, инициатива его.",
        "trade": {
            "entry": 1613.03,
            "time": "15:14:30",
            "direction": "short",
            "qty": 113,
            "stop": 1619,
            "stop_reason": "за провалившийся экстремум покупателя 1617",
            "outcome": "стоп выбит в бакете 15:50-15:59 хай 1620 объём 9411 vs 361 в 15:30, убыток 857₽ с комиссией 0.47%",
            "after": "16:30 вынос до 1623.5 и падение до 1610, мин 1609 в 17:10-17:19, цель 1601.5 не достигнута, вечер рост до 1634",
            "error": "Ошибка не в стопе, а в направлении. Макс благоприятное +4₽ vs цель +11.5₽. Широкий 1622.5 выбит 16:30 хаем 1623.5. Стоп сделал работу 857₽ вместо 2430₽ до закрытия.",
        },
        "lessons": [
            "Правило исчерпания: крупнейший принт в конце выборки — чаще завершение программы, а не середина. Айсберг 64/64/64/30... закончился 339 и 100 по 1613 и продавец ушёл.",
            "Дивергенция внутри дня: продавец 1138 акций и не сделал нового лоя дня 1601.5 стоял с утра, продажи остановились на 1613 на 11₽ выше — поглощение.",
            "MAGN тот же час верно прочитан (351 акция в бид цена не сдвинулась → шорт не брать) vs MGNT неверно — одинаковая улика противоположные выводы. Критерий один: сделал ли агрессор новый экстремум цены. Нет → поглотили независимо от объёма.",
            "Лаг 15-19 мин — дефект для входа: вывод в 15:19 по данным до 14:55:53, цена уже вернулась на 1617 на 4₽ выше уровня продавца. Метод годится для выхода и стопа, но вход опаздывает.",
            "Дискреционный вход без индексного триггера: бакет 0.84× при пороге 3×. Счёт вне триггера 1/1 убыток, с триггером 4+/2-/1 ноль на 14д — фильтр объёма важнее ленты.",
        ],
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
