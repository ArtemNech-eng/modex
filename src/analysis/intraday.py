"""
MOODEX — Внутридневная (интрадей) аналитика и торговля волатильностью.

Чистые функции без сети и внешних зависимостей — их легко тестировать.
Рассчитаны на минутные / 5-минутные свечи ОДНОЙ торговой сессии MOEX.

Здесь собрано ядро интрадей-логики:
  • vwap / intraday_atr / candle_anatomy — базовые интрадей-метрики;
  • opening_range + orb_signal — диапазон открытия и пробой (ORB);
  • volatility_state — сжатие → расширение (squeeze breakout);
  • detect_spike + news_whipsaw_plan — новостной вынос и вход «на разрешении»;
  • session_phase / is_last_minutes — фазы сессии MOEX (основная/вечерняя).

Все функции возвращают простые словари/числа, чтобы их удобно было отдавать в
контекст Claude и сохранять в журнал. Не является инвестиционной рекомендацией.
"""
from typing import Optional

# ─── Базовые интрадей-метрики ─────────────────────────────────────────────────

def typical_prices(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """Типичная цена (H+L+C)/3 по каждой свече."""
    n = min(len(highs), len(lows), len(closes))
    return [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]


def vwap(highs: list[float], lows: list[float], closes: list[float],
         volumes: list[float]) -> list[float]:
    """
    Кумулятивный VWAP по свечам сессии (сбрасывается каждый торговый день —
    подавай свечи ОДНОЙ сессии). vwap[i] — средневзвешенная по объёму цена до i.
    Если объёмов нет (нули) — откатывается на типичную цену.
    """
    tp = typical_prices(highs, lows, closes)
    out: list[float] = []
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(len(tp)):
        v = volumes[i] if i < len(volumes) and volumes[i] else 0.0
        cum_pv += tp[i] * v
        cum_v += v
        out.append(round(cum_pv / cum_v, 6) if cum_v > 0 else round(tp[i], 6))
    return out


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """Истинный диапазон по каждой свече (с оглядкой на предыдущий close)."""
    n = min(len(highs), len(lows), len(closes))
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    return trs


def intraday_atr(highs: list[float], lows: list[float], closes: list[float],
                 period: int = 14) -> Optional[float]:
    """ATR как простое среднее истинных диапазонов за последние `period` свечей."""
    trs = true_ranges(highs, lows, closes)
    if not trs:
        return None
    window = trs[-period:]
    return round(sum(window) / len(window), 6)


def candle_anatomy(o: float, h: float, l: float, c: float) -> dict:
    """
    Анатомия свечи: тело, тени, доля тела в диапазоне.
    Помогает распознать разворотные/индесижн-свечи (длинные тени, малое тело).
    """
    rng = h - l
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    body_pct = (body / rng) if rng > 0 else 0.0
    return {
        "range": round(rng, 6),
        "body": round(body, 6),
        "upper_wick": round(upper, 6),
        "lower_wick": round(lower, 6),
        "body_pct": round(body_pct, 4),
        "direction": "up" if c > o else "down" if c < o else "flat",
        # индесижн: маленькое тело и заметные тени с обеих сторон
        "indecision": body_pct < 0.35 and upper > body and lower > body,
    }


# ─── Диапазон открытия и его пробой (ORB) ─────────────────────────────────────

def opening_range(highs: list[float], lows: list[float], bars: int = 6) -> Optional[dict]:
    """
    Диапазон открытия: хай/лоу первых `bars` свечей.
    Для 5-мин свечей bars=6 ≈ первые 30 минут сессии.
    """
    n = min(len(highs), len(lows))
    if n < bars or bars <= 0:
        return None
    return {
        "or_high": round(max(highs[:bars]), 6),
        "or_low": round(min(lows[:bars]), 6),
        "bars": bars,
    }


def orb_signal(price: float, or_high: float, or_low: float, atr: float,
               target_mult: float = 1.5) -> dict:
    """
    Пробой диапазона открытия. Стоп — за противоположную границу диапазона,
    цель — на target_mult·ATR от входа. R/R считаем честно.
    """
    if atr is None or atr <= 0:
        return {"signal": "none", "reason": "нет ATR"}
    if price > or_high:
        entry, stop = price, or_low
        target = round(price + target_mult * atr, 6)
        risk = entry - stop
        return _plan("long", entry, stop, target, risk, "пробой диапазона открытия вверх")
    if price < or_low:
        entry, stop = price, or_high
        target = round(price - target_mult * atr, 6)
        risk = stop - entry
        return _plan("short", entry, stop, target, risk, "пробой диапазона открытия вниз")
    return {"signal": "none", "reason": "цена внутри диапазона открытия"}


# ─── Сжатие → расширение волатильности (squeeze breakout) ─────────────────────

def volatility_state(highs: list[float], lows: list[float], closes: list[float],
                     period: int = 14, lookback: int = 50,
                     squeeze_pct: float = 0.25, expansion_pct: float = 0.80) -> dict:
    """
    Состояние волатильности через перцентиль текущего ATR за `lookback` свечей.
    - squeeze  (сжатие)  — ATR у минимумов (перцентиль <= squeeze_pct);
    - expansion (расширение) — ATR у максимумов (перцентиль >= expansion_pct);
    - normal — между.
    Сжатие часто предшествует импульсу → торгуем по факту расширения.
    """
    trs = true_ranges(highs, lows, closes)
    if len(trs) < max(period + 1, 5):
        return {"state": "unknown", "atr": None, "atr_rank": None}

    # серия ATR (скользящее среднее TR), затем перцентиль последнего значения
    atr_series = []
    for i in range(period, len(trs) + 1):
        window = trs[i - period:i]
        atr_series.append(sum(window) / len(window))
    atr_series = atr_series[-lookback:]
    cur = atr_series[-1]
    below = sum(1 for x in atr_series if x <= cur)
    rank = below / len(atr_series)

    state = "squeeze" if rank <= squeeze_pct else "expansion" if rank >= expansion_pct else "normal"
    return {"state": state, "atr": round(cur, 6), "atr_rank": round(rank, 3)}


# ─── Новостной вынос: детектор спайка + вход «на разрешении» ───────────────────

def detect_spike(opens: list[float], highs: list[float], lows: list[float],
                 closes: list[float], atr: Optional[float] = None,
                 k: float = 2.5) -> dict:
    """
    Детектор всплеска волатильности на последней свече: её диапазон против ATR.
    spike=True, если диапазон > k·ATR. reversal=True — свеча-индесижн (вынос
    в обе стороны с длинными тенями), типичная для новостного прострела.
    """
    if not highs or not lows or not closes or not opens:
        return {"spike": False, "range_ratio": None, "reversal": False}
    if atr is None:
        atr = intraday_atr(highs, lows, closes)
    if not atr or atr <= 0:
        return {"spike": False, "range_ratio": None, "reversal": False}

    anat = candle_anatomy(opens[-1], highs[-1], lows[-1], closes[-1])
    ratio = anat["range"] / atr
    return {
        "spike": ratio >= k,
        "range_ratio": round(ratio, 2),
        "reversal": anat["indecision"],
        "anatomy": anat,
    }


def classify_event(spike: bool, msg_zscore: Optional[float] = None,
                   has_fresh_news: bool = False) -> dict:
    """
    Классифицировать момент как «новостное событие»:
    спайк волатильности + аномальный объём сообщений и/или свежая новость.
    """
    msg_anomaly = (msg_zscore is not None and msg_zscore >= 2.0)
    is_event = bool(spike and (msg_anomaly or has_fresh_news))
    signals = []
    if spike:
        signals.append("спайк волатильности")
    if msg_anomaly:
        signals.append("аномальный объём сообщений")
    if has_fresh_news:
        signals.append("свежая новость")
    return {"event": is_event, "signals": signals}


def news_whipsaw_plan(event_high: float, event_low: float, price: float,
                      vwap_last: Optional[float], atr: Optional[float],
                      confirm_buffer_atr: float = 0.1) -> dict:
    """
    Вход «на разрешении» новостного выноса. Не ловим сам прострел: ждём выхода
    и удержания за границу пост-новостного диапазона (event_high/event_low),
    подтверждённого положением относительно VWAP.

    long  — price пробил event_high и держится выше VWAP;
    short — price пробил event_low и держится ниже VWAP;
    иначе — ждём (wait).
    Стоп — за противоположную границу выноса; цель — измеренное движение
    (высота выноса) от точки входа.
    """
    if event_high is None or event_low is None or event_high <= event_low:
        return {"signal": "wait", "reason": "нет валидного диапазона выноса"}
    buf = (atr or 0.0) * confirm_buffer_atr
    height = event_high - event_low

    above = price > event_high + buf and (vwap_last is None or price >= vwap_last)
    below = price < event_low - buf and (vwap_last is None or price <= vwap_last)

    if above:
        entry, stop = price, event_low
        target = round(price + height, 6)
        return _plan("long", entry, stop, target, entry - stop,
                     "разрешение выноса вверх (пробой+удержание над VWAP)")
    if below:
        entry, stop = price, event_high
        target = round(price - height, 6)
        return _plan("short", entry, stop, target, stop - entry,
                     "разрешение выноса вниз (пробой+удержание под VWAP)")
    return {"signal": "wait", "reason": "разрешение не подтверждено — наблюдаем"}


# ─── Фазы торговой сессии MOEX (время МСК) ────────────────────────────────────

def session_phase(minute_of_day: int) -> str:
    """
    Фаза сессии MOEX по минуте дня (МСК). Приблизительные границы:
      • pre      09:50–10:00 (аукцион открытия)
      • main     10:00–18:50 (основная сессия)
      • break    18:50–19:00
      • evening  19:00–23:50 (вечерняя сессия)
      • closed   остальное
    """
    def hm(h, m):
        return h * 60 + m
    if hm(9, 50) <= minute_of_day < hm(10, 0):
        return "pre"
    if hm(10, 0) <= minute_of_day < hm(18, 50):
        return "main"
    if hm(18, 50) <= minute_of_day < hm(19, 0):
        return "break"
    if hm(19, 0) <= minute_of_day < hm(23, 50):
        return "evening"
    return "closed"


def is_last_minutes(minute_of_day: int, buffer_min: int = 10) -> bool:
    """
    True, если до конца текущей сессии осталось <= buffer_min минут — в это
    время новые интрадей-входы обычно не открываем (флэт к закрытию).
    """
    def hm(h, m):
        return h * 60 + m
    for end in (hm(18, 50), hm(23, 50)):
        if 0 <= end - minute_of_day <= buffer_min:
            return True
    return False


# ─── helpers ──────────────────────────────────────────────────────────────────

def _plan(signal: str, entry: float, stop: float, target: float,
          risk: float, reason: str) -> dict:
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk and risk > 0 else None
    return {
        "signal": signal,
        "entry": round(entry, 6),
        "stop_loss": round(stop, 6),
        "take_profit": round(target, 6),
        "risk_reward": rr,
        "reason": reason,
    }


# ─── Настроение из стакана и потока сделок («реальные деньги») ─────────────────

def orderbook_sentiment(bid_ask_ratio: Optional[float],
                        buy_pct: Optional[float] = None) -> dict:
    """
    Перевести перекос стакана (bid/ask) и поток сделок (доля покупок) в сигнал
    настроения [-1..1]. Это «настроение реальных денег» — можно подавать в
    агрегатор наравне с настроением из чатов.

    bid_ask_ratio: суммарный объём заявок bid / ask (>1 — доминируют покупатели).
    buy_pct:       доля агрессивных покупок в потоке сделок (0..100), необязательно.
    """
    import math
    parts = []
    if bid_ask_ratio and bid_ask_ratio > 0:
        parts.append(math.tanh(math.log(bid_ask_ratio)))  # r=1→0, растёт/падает симметрично
    if buy_pct is not None:
        parts.append(max(-1.0, min(1.0, (buy_pct - 50.0) / 50.0)))
    if not parts:
        return {"signal": 0.0, "label": "neutral", "score": 0.0}
    signal = max(-1.0, min(1.0, sum(parts) / len(parts)))
    label = "positive" if signal > 0.15 else "negative" if signal < -0.15 else "neutral"
    return {"signal": round(signal, 3), "label": label, "score": round(abs(signal), 3)}


# ─── Профиль объёма и VWAP-полосы (интрадей-уровни) ───────────────────────────

def volume_profile(highs: list[float], lows: list[float], closes: list[float],
                   volumes: list[float], bins: int = 24) -> Optional[dict]:
    """
    Профиль объёма (объём по ценам) по свечам сессии. Объём каждой свечи
    распределяется по ценовым корзинам её диапазона [low, high]. Возвращает:
      poc     — цена корзины с макс. объёмом (Point of Control, «магнит»);
      val/vah — границы зоны стоимости (~70% объёма вокруг POC);
      nodes   — топ высокообъёмных ценовых узлов (уровни притяжения/отбоя).
    Это настоящие торговые уровни: где реально наторговали больше всего.
    """
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < 5:
        return None
    lo = min(lows[:n])
    hi = max(highs[:n])
    if hi <= lo or bins < 2:
        return None
    width = (hi - lo) / bins
    buckets = [0.0] * bins

    def _idx(price: float) -> int:
        return min(max(int((price - lo) / width), 0), bins - 1)

    for i in range(n):
        v = volumes[i] or 0
        if v <= 0:
            continue
        b_lo, b_hi = _idx(lows[i]), _idx(highs[i])
        share = v / (b_hi - b_lo + 1)
        for b in range(b_lo, b_hi + 1):
            buckets[b] += share

    total = sum(buckets)
    if total <= 0:
        return None

    def _price(b: int) -> float:
        return round(lo + (b + 0.5) * width, 4)

    poc_b = max(range(bins), key=lambda b: buckets[b])
    # Зона стоимости: расширяемся от POC в сторону большего объёма, пока не 70%
    covered = buckets[poc_b]
    left, right = poc_b - 1, poc_b + 1
    lo_b = hi_b = poc_b
    while covered < 0.7 * total and (left >= 0 or right < bins):
        tl = buckets[left] if left >= 0 else -1.0
        tr = buckets[right] if right < bins else -1.0
        if tr >= tl:
            covered += max(tr, 0.0); hi_b = right; right += 1
        else:
            covered += max(tl, 0.0); lo_b = left; left -= 1

    order = sorted(range(bins), key=lambda b: buckets[b], reverse=True)
    return {
        "poc": _price(poc_b),
        "val": _price(lo_b),
        "vah": _price(hi_b),
        "nodes": [_price(b) for b in order[:3]],
    }


def vwap_bands(highs: list[float], lows: list[float], closes: list[float],
               volumes: list[float], k: float = 1.0) -> Optional[dict]:
    """
    Сессионный VWAP + полосы ±k·σ (σ — объёмно-взвешенное стандартное отклонение
    типичной цены от VWAP). Ориентир «дорого/дёшево» относительно средневзвешенной.
    """
    import math
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < 5:
        return None
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    vsum = sum((volumes[i] or 0) for i in range(n))
    if vsum > 0:
        v = sum(tp[i] * (volumes[i] or 0) for i in range(n)) / vsum
        var = sum((volumes[i] or 0) * (tp[i] - v) ** 2 for i in range(n)) / vsum
    else:
        v = sum(tp) / n
        var = sum((x - v) ** 2 for x in tp) / n
    sigma = math.sqrt(var) if var > 0 else 0.0
    return {
        "vwap": round(v, 4),
        "upper": round(v + k * sigma, 4),
        "lower": round(v - k * sigma, 4),
        "sigma": round(sigma, 4),
    }
