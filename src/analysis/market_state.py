"""
Единая классификация состояния рынка по бумаге.

ЗАЧЕМ. Все куски существовали по отдельности: regime в дневной технике,
volatility_state в интрадее, classify_event в детекторе новостей, ликвидность
внутри риск-контура. Ни одна функция не сводила их в ОДНО состояние, поэтому
потребитель — человек или модель — должен был сам держать в голове четыре поля и
их взаимодействие. Здесь состояние одно, а признаки, из которых оно получено,
записаны рядом.

ПОЧЕМУ НЕ ПРОСТО ВЗЯТЬ regime. Прежний детектор ненадёжен: 30.07 он пометил SMLT
«боковиком» при цене на 23% выше SMA20 и на 11% выше SMA50, потому что смотрел на
ADX и расхождение средних, но не на ПОЛОЖЕНИЕ ЦЕНЫ относительно них. Здесь тренд
требует СОГЛАСИЯ нескольких независимых признаков, и при разногласии честно
возвращается RANGE, а не выдуманный тренд.

ПОРЯДОК ВАЖЕН. Состояния проверяются по приоритету:
  1. ILLIQUID    — данные негодны или ликвидности нет. Это ВЕТО: пока оно стоит,
                   остальные вопросы не имеют смысла.
  2. NEWS_EVENT  — вынос с причинно связанной новостью. Поведение цены в такие
                   моменты другое, и торговать это надо иначе.
  3. TREND_UP / TREND_DOWN — при согласии признаков.
  4. RANGE       — во всех прочих случаях, честно.

Волатильность — ОТДЕЛЬНАЯ ось, а не состояние: «тренд вверх при высокой
волатильности» и «тренд вверх при сжатии» — разные вещи, и схлопывать их в один
ярлык значит терять информацию.
"""
from typing import Optional

# Пороги. ADX 25 — общепринятая граница выраженного тренда; ниже него направление
# средних ещё ничего не значит.
ADX_TREND = 25.0
# Насколько цена должна отстоять от средней, чтобы считать это позицией, а не шумом.
SMA_MARGIN_PCT = 0.5
# Спред, выше которого вход съедается издержками. По ликвидным бумагам MOEX
# нормальный спред 0.004-0.10%, поэтому 0.5% это уже опасно.
SPREAD_DANGER_PCT = 0.5

STATES = ("ILLIQUID", "NEWS_EVENT", "TREND_UP", "TREND_DOWN", "RANGE")
VOLS = ("HIGH", "LOW", "NORMAL")


def classify_market_state(
    price: Optional[float] = None,
    sma20: Optional[float] = None,
    sma50: Optional[float] = None,
    adx: Optional[float] = None,
    vwap: Optional[float] = None,
    intraday_structure: Optional[str] = None,   # вверх / вниз / боковик
    volatility_state: Optional[str] = None,     # squeeze / expansion / normal
    news_event: bool = False,
    news_lag_min: Optional[float] = None,
    spread_pct: Optional[float] = None,
    depth_near_mid: Optional[int] = None,
    stale: bool = False,
    mismatch: bool = False,
    age_min: Optional[float] = None,
) -> dict:
    """Одно состояние плюс признаки, из которых оно получено.

    Возвращает: state, volatility, tradeable, confidence, why (список причин),
    signals (что именно сработало).
    """
    why: list = []
    signals: dict = {}

    # ── 1. ВЕТО: данные негодны или ликвидности нет ──────────────────────────
    danger = []
    if mismatch:
        danger.append("серия не сходится с ценой — похоже на чужой инструмент")
    if stale:
        danger.append(f"данные устарели ({age_min:.0f} мин)" if age_min
                      else "данные устарели")
    if spread_pct is not None and spread_pct > SPREAD_DANGER_PCT:
        danger.append(f"спред {spread_pct:.2f}% выше предела {SPREAD_DANGER_PCT}%")
    if price is None:
        danger.append("нет цены")
    if danger:
        return {"state": "ILLIQUID", "volatility": "NORMAL", "tradeable": False,
                "confidence": 1.0, "why": danger,
                "signals": {"danger": danger},
                "note": "торговать нельзя: " + "; ".join(danger)}

    # ── Волатильность как отдельная ось ──────────────────────────────────────
    vol = {"expansion": "HIGH", "squeeze": "LOW"}.get(volatility_state or "", "NORMAL")

    # ── 2. Новостное событие ─────────────────────────────────────────────────
    if news_event:
        lag = (f"новость за {news_lag_min:.0f} мин до выноса" if news_lag_min and news_lag_min >= 0
               else (f"новость через {abs(news_lag_min):.0f} мин после выноса"
                     if news_lag_min is not None else "новостной вынос"))
        return {"state": "NEWS_EVENT", "volatility": vol, "tradeable": True,
                "confidence": 0.8, "why": [lag],
                "signals": {"news_lag_min": news_lag_min},
                "note": "новостной вынос: поведение цены отличается от обычного"}

    # ── 3. Тренд по СОГЛАСИЮ признаков ───────────────────────────────────────
    up = down = 0
    if price and sma20:
        d = (price - sma20) / sma20 * 100
        signals["price_vs_sma20_pct"] = round(d, 2)
        if d > SMA_MARGIN_PCT:
            up += 1; why.append(f"цена выше SMA20 на {d:.1f}%")
        elif d < -SMA_MARGIN_PCT:
            down += 1; why.append(f"цена ниже SMA20 на {abs(d):.1f}%")
    if price and sma50:
        d = (price - sma50) / sma50 * 100
        signals["price_vs_sma50_pct"] = round(d, 2)
        if d > SMA_MARGIN_PCT:
            up += 1; why.append(f"цена выше SMA50 на {d:.1f}%")
        elif d < -SMA_MARGIN_PCT:
            down += 1; why.append(f"цена ниже SMA50 на {abs(d):.1f}%")
    if sma20 and sma50:
        if sma20 > sma50:
            up += 1; why.append("SMA20 над SMA50")
        elif sma20 < sma50:
            down += 1; why.append("SMA20 под SMA50")
    if price and vwap:
        if price > vwap:
            up += 1; why.append("цена выше VWAP")
        elif price < vwap:
            down += 1; why.append("цена ниже VWAP")
    if intraday_structure == "вверх":
        up += 1; why.append("минимумы дня растут")
    elif intraday_structure == "вниз":
        down += 1; why.append("максимумы дня падают")

    signals["up_votes"], signals["down_votes"] = up, down
    signals["adx"] = adx
    strong_adx = adx is not None and adx >= ADX_TREND

    # Тренд объявляем только при ПЕРЕВЕСЕ голосов И выраженном ADX. Иначе RANGE —
    # именно на этом прежний детектор ошибался, называя трендом расхождение средних
    # без учёта положения цены, и наоборот.
    total = up + down
    if total and strong_adx and up >= 4 and up > down * 2:
        conf = round(min(0.95, 0.5 + 0.1 * up), 2)
        return {"state": "TREND_UP", "volatility": vol, "tradeable": True,
                "confidence": conf, "why": why, "signals": signals,
                "note": f"восходящий тренд: {up} признаков за, {down} против, ADX {adx:.0f}"}
    if total and strong_adx and down >= 4 and down > up * 2:
        conf = round(min(0.95, 0.5 + 0.1 * down), 2)
        return {"state": "TREND_DOWN", "volatility": vol, "tradeable": True,
                "confidence": conf, "why": why, "signals": signals,
                "note": f"нисходящий тренд: {down} признаков за, {up} против, ADX {adx:.0f}"}

    # ── 4. Всё остальное — честно диапазон ───────────────────────────────────
    reason = ("признаки расходятся" if total and abs(up - down) <= 1
              else (f"ADX {adx:.0f} ниже {ADX_TREND:.0f} — тренд не выражен"
                    if adx is not None and not strong_adx else "нет перевеса признаков"))
    why.append(reason)
    return {"state": "RANGE", "volatility": vol, "tradeable": True,
            "confidence": round(0.4 + 0.05 * abs(up - down), 2),
            "why": why, "signals": signals,
            "note": f"диапазон: {reason} (за рост {up}, за снижение {down})"}
