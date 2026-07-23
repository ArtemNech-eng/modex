"""
MOODEX — Технический анализ (MOEX ISS)

Тянет дневные свечи с публичного ISS API Московской биржи (без ключа) и
считает индикаторы: SMA, EMA, RSI, MACD, изменение цены, волатильность.
На их основе формирует технический сигнал bullish / bearish / neutral.

ISS API (пример):
    https://iss.moex.com/iss/engines/stock/markets/shares/securities/SBER/candles.json
        ?interval=24&from=2024-01-01&till=2024-04-01
"""
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ISS_CANDLES_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/"
    "securities/{ticker}/candles.json"
)


# ─── Чистые функции-индикаторы (без сети, легко тестируются) ──────────────────

def sma(values: list[float], period: int) -> Optional[float]:
    """Простая скользящая средняя за последние `period` значений."""
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    """Экспоненциальная скользящая средняя (весь ряд)."""
    if not values or period <= 0:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: list[float], period: int) -> Optional[float]:
    """Последнее значение EMA."""
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Индекс относительной силы (RSI) по Уайлдеру. Диапазон 0–100."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # Начальные средние по первым `period` изменениям
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Сглаживание Уайлдера по остальным
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD. Возвращает (macd_line, signal_line, histogram) по последней точке.
    """
    if len(closes) < slow + signal:
        return None, None, None
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    macd_line_series = [f - s for f, s in zip(fast_e, slow_e)]
    signal_series = ema_series(macd_line_series, signal)
    macd_line = macd_line_series[-1]
    signal_line = signal_series[-1]
    return macd_line, signal_line, macd_line - signal_line


def pct_change(closes: list[float], periods: int) -> Optional[float]:
    """Изменение цены (%) за `periods` свечей назад."""
    if len(closes) < periods + 1 or closes[-periods - 1] == 0:
        return None
    return (closes[-1] / closes[-periods - 1] - 1) * 100


def volatility(closes: list[float], period: int = 20) -> Optional[float]:
    """Годовая волатильность (%) по дневным доходностям за `period`."""
    if len(closes) < period + 1:
        return None
    rets = [
        closes[i] / closes[i - 1] - 1
        for i in range(len(closes) - period, len(closes))
        if closes[i - 1] != 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    daily_std = var ** 0.5
    return daily_std * (252 ** 0.5) * 100


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
    """Average True Range — средний истинный диапазон (мера волатильности в цене)."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


# ─── Режим рынка: тренд vs боковик (ADX) ──────────────────────────────────────

def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """Сглаживание Уайлдера (для ADX)."""
    if len(values) < period:
        return []
    out = [sum(values[:period])]
    for i in range(period, len(values)):
        out.append(out[-1] - out[-1] / period + values[i])
    return out


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
    """
    ADX (Average Directional Index) — сила тренда.
    ADX < 20 → рынок в боковике; ADX > 25 → выраженный тренд.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < 2 * period + 1:
        return None
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    atr_s = _wilder_smooth(tr, period)
    pdm_s = _wilder_smooth(plus_dm, period)
    mdm_s = _wilder_smooth(minus_dm, period)
    if not atr_s:
        return None

    dx = []
    for i in range(len(atr_s)):
        if atr_s[i] == 0:
            dx.append(0.0)
            continue
        pdi = 100 * pdm_s[i] / atr_s[i]
        mdi = 100 * mdm_s[i] / atr_s[i]
        s = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / s if s > 0 else 0.0)

    if len(dx) < period:
        return None
    adx_val = sum(dx[:period]) / period
    for i in range(period, len(dx)):
        adx_val = (adx_val * (period - 1) + dx[i]) / period
    return adx_val


def detect_regime(
    closes: list[float], highs: list[float], lows: list[float], lookback: int = 20
) -> dict:
    """
    Определить режим рынка и положение цены в диапазоне.

    Возвращает: regime (range/uptrend/downtrend), range_position [0..1]
    (0 = у нижней границы, 1 = у верхней), adx, support, resistance.
    """
    price = closes[-1]
    support = min(lows[-lookback:])
    resistance = max(highs[-lookback:])
    rng = resistance - support
    range_position = (price - support) / rng if rng > 0 else 0.5

    adx_val = adx(highs, lows, closes, 14)
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)

    # Разделение средних (насколько выражен тренд по MA)
    sma_sep = abs(s20 - s50) / price if (s20 and s50 and price) else 0.0
    strong_trend = (adx_val is not None and adx_val >= 25) and sma_sep >= 0.015

    if not strong_trend:
        regime = "range"
    elif s20 and s50 and s20 >= s50:
        regime = "uptrend"
    else:
        regime = "downtrend"

    return {
        "regime": regime,
        "range_position": round(range_position, 3),
        "adx": round(adx_val, 1) if adx_val is not None else None,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
    }


def _entry_verdict(direction: str, price: float, elow: float, ehigh: float,
                   stop: float, tp1: float) -> tuple[str, str, Optional[float]]:
    """
    Вердикт тайминга по текущей цене относительно зоны входа.
    Возвращает (status, note, current_rr).
    status: enter / wait / late / above / below / invalid
    """
    if direction == "long":
        crr = round((tp1 - price) / (price - stop), 2) if (price > stop and tp1 > price) else None
        if price <= stop:
            return "invalid", "Цена уже у/ниже стопа — сетап отработан, не входить.", crr
        if price < elow:
            return "below", (f"Цена ({price}) ниже зоны входа {elow}–{ehigh} — риск слома вниз. "
                             f"Не входить, дождаться возврата в зону."), crr
        if price <= ehigh:
            return "enter", f"✅ ВХОД СЕЙЧАС: цена в зоне покупки {elow}–{ehigh}.", crr
        return "wait", (f"⏳ РАНО/ДОРОГО: цена ({price}) выше зоны входа. "
                        f"Ждать отката к {elow}–{ehigh} (к SMA20)."), crr
    else:  # short
        crr = round((price - tp1) / (stop - price), 2) if (stop > price and price > tp1) else None
        if price >= stop:
            return "invalid", "Цена уже у/выше стопа — сетап отработан, не входить.", crr
        if price > ehigh:
            return "above", (f"Цена ({price}) выше зоны входа {elow}–{ehigh} — близко к стопу, риск высок. "
                             f"Лучше дождаться возврата в зону."), crr
        if price >= elow:
            return "enter", f"✅ ВХОД СЕЙЧАС: цена в зоне шорта {elow}–{ehigh}.", crr
        return "late", (f"⚠️ ПОЗДНО: цена ({price}) уже ниже зоны входа {elow}–{ehigh} — "
                        f"движение вниз в основном отработано. Ждать отскока к зоне или пропустить."), crr


def compute_levels(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    signal: str,
    sma20: Optional[float],
    regime: str = "trend_follow",
    range_pos: float = 0.5,
    lookback: int = 20,
) -> dict:
    """
    Торговый план с уровнями, ПРИВЯЗАННЫМИ К СТРУКТУРЕ (SMA20 / границы диапазона),
    а не к текущей цене — чтобы система умела сказать «входить сейчас», «ждать
    отката» или «уже поздно». Числа — алгоритмические ориентиры, не гарантия.
    """
    price = closes[-1]
    a = atr(highs, lows, closes, 14) or (price * 0.02)
    support = min(lows[-lookback:])
    resistance = max(highs[-lookback:])

    plan: dict = {
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "atr": round(a, 2),
        "regime": regime,
        "price": round(price, 2),
    }

    def finalize(direction, elow, ehigh, stop, tp1, tp2, entry_rule, exit_rule):
        planned = (elow + ehigh) / 2
        if direction == "long":
            rr = round((tp1 - planned) / max(planned - stop, 1e-9), 2)
        else:
            rr = round((planned - tp1) / max(stop - planned, 1e-9), 2)
        status, note, crr = _entry_verdict(
            direction, round(price, 2), round(elow, 2), round(ehigh, 2),
            round(stop, 2), round(tp1, 2))
        plan.update({
            "direction": direction,
            "entry_low": round(elow, 2), "entry_high": round(ehigh, 2),
            "stop_loss": round(stop, 2), "take_profit_1": round(tp1, 2), "take_profit_2": round(tp2, 2),
            "risk_reward": rr, "current_rr": crr,
            "entry_status": status, "entry_note": note,
            "entry_rule": entry_rule, "exit_rule": exit_rule,
        })

    def flat(note):
        plan.update({
            "direction": "flat", "entry_low": None, "entry_high": None, "stop_loss": None,
            "take_profit_1": None, "take_profit_2": None, "risk_reward": None, "current_rr": None,
            "entry_status": "wait", "entry_note": note,
            "entry_rule": note, "exit_rule": "Ждать чёткого сигнала у границы/уровня.",
        })

    # ── Боковик: торговля от границ ──
    if regime == "range":
        if signal == "bullish":       # лонг у нижней границы
            elow, ehigh = support, support + 0.8 * a
            stop = support - 0.8 * a
            tp1 = resistance - 0.15 * (resistance - support)
            tp2 = resistance
            finalize("long", elow, ehigh, stop, tp1, tp2,
                     f"Боковик: покупка у нижней границы, зона {round(elow,2)}–{round(ehigh,2)}.",
                     f"Цель — верх диапазона {round(tp1,2)}/{round(tp2,2)}. Стоп {round(stop,2)}.")
        elif signal == "bearish":     # шорт у верхней границы
            elow, ehigh = resistance - 0.8 * a, resistance
            stop = resistance + 0.8 * a
            tp1 = support + 0.15 * (resistance - support)
            tp2 = support
            finalize("short", elow, ehigh, stop, tp1, tp2,
                     f"Боковик: шорт у верхней границы, зона {round(elow,2)}–{round(ehigh,2)}.",
                     f"Цель — низ диапазона {round(tp1,2)}/{round(tp2,2)}. Стоп {round(stop,2)}.")
        else:
            flat(f"Боковик, цена в середине ({plan['support']}–{plan['resistance']}). Ждать подхода к границе.")
        return plan

    # ── Тренд: вход на откате к SMA20 ──
    if signal == "bullish":
        base = sma20 if (sma20 and sma20 < price) else price - a  # зона покупки ниже цены (откат)
        ehigh, elow = base, base - 0.6 * a
        stop = elow - 1.2 * a
        tp1 = resistance if resistance > price * 1.02 else price + 3 * a
        tp2 = tp1 + (tp1 - base)
        finalize("long", elow, ehigh, stop, tp1, tp2,
                 f"Тренд вверх: покупка на откате к SMA20, зона {round(elow,2)}–{round(ehigh,2)}.",
                 f"Цель {round(tp1,2)} (затем {round(tp2,2)}) / RSI>70. Стоп {round(stop,2)}.")
    elif signal == "bearish":
        base = sma20 if (sma20 and sma20 > price) else price + a  # зона шорта выше цены (отскок)
        elow, ehigh = base, base + 0.6 * a
        stop = ehigh + 1.2 * a
        tp1 = support if support < price * 0.98 else price - 3 * a
        tp2 = tp1 - (base - tp1)
        finalize("short", elow, ehigh, stop, tp1, tp2,
                 f"Тренд вниз: шорт на отскоке к SMA20, зона {round(elow,2)}–{round(ehigh,2)}.",
                 f"Цель {round(tp1,2)} (затем {round(tp2,2)}). Стоп {round(stop,2)}.")
    else:
        flat(f"Чёткого сигнала нет — коридор {plan['support']}–{plan['resistance']}.")
    return plan


# ─── Результат анализа ────────────────────────────────────────────────────────

@dataclass
class TechnicalAnalysis:
    ticker: str
    price: Optional[float]
    sma20: Optional[float]
    sma50: Optional[float]
    rsi14: Optional[float]
    macd_hist: Optional[float]
    change_1d: Optional[float]
    change_7d: Optional[float]
    volatility: Optional[float]
    regime: str          # range / uptrend / downtrend
    range_position: Optional[float]  # 0=нижняя граница, 1=верхняя
    adx: Optional[float]
    strategy: str        # описание сетапа
    signal: str          # "bullish" / "bearish" / "neutral"
    score: float         # [-1, +1] техническая оценка
    reasons: list[str]   # человекочитаемые обоснования
    trade_plan: dict     # уровни входа/выхода
    candles_used: int
    updated_at: str

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("price", "sma20", "sma50", "rsi14", "macd_hist",
                  "change_1d", "change_7d", "volatility"):
            if d[k] is not None:
                d[k] = round(d[k], 3)
        d["score"] = round(d["score"], 3)
        return d


def compute_from_series(
    ticker: str,
    closes: list[float],
    highs: Optional[list[float]] = None,
    lows: Optional[list[float]] = None,
) -> TechnicalAnalysis:
    """
    Полный технический анализ по рядам цен, с учётом режима рынка.
    В боковике применяется стратегия возврата к среднему (mean-reversion):
    сигнал у нижней границы = покупка, у верхней = продажа.
    """
    if highs is None:
        highs = closes
    if lows is None:
        lows = closes

    price = closes[-1] if closes else None
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r = rsi(closes, 14)
    _, _, hist = macd(closes)
    ch1 = pct_change(closes, 1)
    ch7 = pct_change(closes, 7)
    vol = volatility(closes, 20)

    reg = detect_regime(closes, highs, lows)
    regime = reg["regime"]
    rpos = reg["range_position"]

    votes: list[float] = []
    reasons: list[str] = []

    if regime == "range":
        # ── Стратегия боковика (mean-reversion) ──
        pct = int(rpos * 100)
        if rpos <= 0.30:
            strength = 0.5 + (0.30 - rpos)      # ближе к границе — сильнее
            votes.append(min(0.9, strength))
            reasons.append(f"Боковик: цена у НИЖНЕЙ границы ({pct}% диапазона) — ожидается отскок вверх")
            if r is not None and r <= 40:
                votes.append(0.3)
                reasons.append(f"RSI={r:.0f} подтверждает перепроданность")
        elif rpos >= 0.70:
            strength = 0.5 + (rpos - 0.70)
            votes.append(-min(0.9, strength))
            reasons.append(f"Боковик: цена у ВЕРХНЕЙ границы ({pct}% диапазона) — ожидается откат вниз")
            if r is not None and r >= 60:
                votes.append(-0.3)
                reasons.append(f"RSI={r:.0f} подтверждает перекупленность")
        else:
            reasons.append(f"Боковик: цена в середине диапазона ({pct}%) — ждать подхода к границе")
        strategy = "range_reversal"
    else:
        # ── Трендследящая логика ──
        strategy = "trend_follow"
        if price is not None and s20 is not None:
            if price > s20:
                votes.append(0.5); reasons.append("Тренд: цена выше SMA20")
            else:
                votes.append(-0.5); reasons.append("Тренд: цена ниже SMA20")
        if s20 is not None and s50 is not None:
            if s20 > s50:
                votes.append(0.6); reasons.append("SMA20 выше SMA50 (тренд бычий)")
            else:
                votes.append(-0.6); reasons.append("SMA20 ниже SMA50 (тренд медвежий)")
        if hist is not None and price:
            macd_eps = price * 0.0005   # мёртвая зона: почти нулевой MACD не голосует
            if hist > macd_eps:
                votes.append(0.5); reasons.append("MACD-импульс вверх")
            elif hist < -macd_eps:
                votes.append(-0.5); reasons.append("MACD-импульс вниз")
        if r is not None:
            if r >= 70:
                votes.append(-0.3); reasons.append(f"RSI={r:.0f} — перекупленность (риск отката)")
            elif r <= 30:
                votes.append(0.3); reasons.append(f"RSI={r:.0f} — перепроданность")

    if reg["adx"] is not None:
        reasons.append(f"ADX={reg['adx']} → {'слабый тренд/боковик' if reg['adx']<20 else 'выраженный тренд'}")

    score = max(-1.0, min(1.0, sum(votes) / len(votes))) if votes else 0.0
    if score > 0.2:
        signal = "bullish"
    elif score < -0.2:
        signal = "bearish"
    else:
        signal = "neutral"

    trade_plan = compute_levels(closes, highs, lows, signal, s20, regime=regime, range_pos=rpos) if price else {}

    return TechnicalAnalysis(
        ticker=ticker, price=price, sma20=s20, sma50=s50, rsi14=r, macd_hist=hist,
        change_1d=ch1, change_7d=ch7, volatility=vol,
        regime=regime, range_position=rpos, adx=reg["adx"], strategy=strategy,
        signal=signal, score=score, reasons=reasons, trade_plan=trade_plan,
        candles_used=len(closes), updated_at=datetime.now(timezone.utc).isoformat(),
    )


# Совместимость: старое имя
def compute_from_closes(ticker: str, closes: list[float]) -> TechnicalAnalysis:
    return compute_from_series(ticker, closes)


# ─── Загрузка свечей с MOEX ISS ───────────────────────────────────────────────

async def fetch_ohlc(ticker: str, days: int = 120) -> tuple[list[float], list[float], list[float]]:
    """Скачать дневные (close, high, low) за последние `days` дней с MOEX ISS."""
    full = await fetch_candles(ticker, days=days)
    return full["close"], full["high"], full["low"]


async def fetch_candles(ticker: str, days: int = 120) -> dict:
    """Полные дневные свечи MOEX ISS: параллельные массивы dates/open/high/low/close."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {"interval": "24", "from": start}
    url = ISS_CANDLES_URL.format(ticker=ticker.upper())

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    candles = data.get("candles", {})
    columns = candles.get("columns", [])
    rows = candles.get("data", [])
    out = {"dates": [], "open": [], "high": [], "low": [], "close": []}
    if "close" not in columns:
        return out
    ci = columns.index("close")
    oi = columns.index("open") if "open" in columns else ci
    hi = columns.index("high") if "high" in columns else ci
    li = columns.index("low") if "low" in columns else ci
    bi = columns.index("begin") if "begin" in columns else None
    for row in rows:
        if row[ci] is None:
            continue
        out["close"].append(row[ci])
        out["open"].append(row[oi] if row[oi] is not None else row[ci])
        out["high"].append(row[hi] if row[hi] is not None else row[ci])
        out["low"].append(row[li] if row[li] is not None else row[ci])
        out["dates"].append(row[bi] if bi is not None else "")
    return out


async def fetch_closes(ticker: str, days: int = 120) -> list[float]:
    """Только цены закрытия (используется при оценке результата прогноза)."""
    closes, _, _ = await fetch_ohlc(ticker, days=days)
    return closes


async def analyze_ticker(ticker: str, days: int = 120) -> Optional[TechnicalAnalysis]:
    """Полный технический анализ тикера по данным MOEX. None при ошибке/нехватке данных."""
    try:
        closes, highs, lows = await fetch_ohlc(ticker, days=days)
    except Exception as e:
        logger.warning(f"MOEX ISS: не удалось загрузить свечи {ticker}: {e}")
        return None
    if len(closes) < 30:
        logger.info(f"MOEX ISS: мало свечей для {ticker} ({len(closes)})")
        return None
    return compute_from_series(ticker.upper(), closes, highs, lows)
