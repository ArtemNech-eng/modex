"""
MOODEX — Historical Backtest для стратегии Claude

Walk-forward тест: для каждого торгового дня в истории
берём данные которые были доступны НА ТОТ МОМЕНТ
(не заглядываем в будущее!) и симулируем решение.

Метрики:
  - Точность направления (up/down)
  - Суммарная доходность vs buy-and-hold
  - Sharpe ratio
  - Max drawdown
  - Win rate по сделкам
  - Доходность при разных порогах уверенности
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _sma(closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100 - 100 / (1 + ag / al) if al != 0 else 100


def _simulate_signal(
    closes: list[float],
    sentiment: Optional[float],    # 0-100
    sentiment_signal: Optional[float],  # -1..+1
) -> tuple[str, float]:
    """
    Симулируем решение Claude на исторических данных.
    Используем те же факторы что и реальная система:
      - Технический сигнал (SMA cross, RSI)
      - Настроение (из sentiment_daily)
    Возвращает (direction, confidence).
    """
    if len(closes) < 50:
        return "flat", 0.0

    price = closes[-1]
    s20   = _sma(closes, 20)
    s50   = _sma(closes, 50)
    r     = _rsi(closes, 14)

    tech_score = 0.0
    if s20 and s50:
        tech_score += 0.5 if s20 > s50 else -0.5
    if price and s20:
        tech_score += 0.3 if price > s20 else -0.3
    if r:
        if r < 30:   tech_score += 0.3
        elif r > 70: tech_score -= 0.3

    sent_score = 0.0
    if sentiment_signal is not None:
        sent_score = sentiment_signal  # -1..+1

    # Взвешиваем: техника 60%, настроение 40%
    combined = 0.6 * tech_score + 0.4 * sent_score
    combined = max(-1.0, min(1.0, combined))

    if combined > 0.15:
        return "up", abs(combined)
    elif combined < -0.15:
        return "down", abs(combined)
    return "flat", abs(combined)


async def run_historical_backtest(
    ticker: str,
    min_confidence: float = 0.3,
    hold_days: int = 5,
    commission_pct: float = 0.05,
) -> dict:
    """
    Полный walk-forward бэктест стратегии на истории.

    Алгоритм:
      1. Берём 2 года дневных свечей MOEX
      2. Берём историю настроений из БД (sentiment_daily)
      3. Для каждого дня симулируем сигнал на данных ДОСТУПНЫХ В ТОТ ДЕНЬ
      4. Открываем позицию на следующий день по цене открытия
      5. Закрываем через hold_days дней
      6. Считаем P&L
    """
    from src.analysis import technical as ta
    from src import db

    # Загружаем данные
    try:
        candles = await ta.fetch_candles(ticker, days=730)
    except Exception as e:
        return {"error": f"Не удалось загрузить свечи: {e}"}

    closes = candles.get("close", [])
    opens  = candles.get("open",  [])
    dates  = candles.get("dates", [])

    if len(closes) < 100:
        return {"error": "Мало исторических данных (< 100 дней)"}

    # История настроений
    hist = await db.sentiment_history(ticker=ticker, limit=1000)
    sent_by_date = {h["date"]: h for h in hist}

    # Walk-forward: каждые 5 дней генерируем сигнал
    trades = []
    equity = [1.0]   # нормированный капитал
    capital = 1.0
    in_position = None  # {"direction", "entry_price", "entry_date", "confidence"}

    for i in range(60, len(closes) - hold_days - 1):
        date = dates[i][:10] if i < len(dates) else ""

        # Закрываем позицию если пора
        if in_position and i >= in_position["exit_idx"]:
            exit_price = opens[i] if i < len(opens) else closes[i]
            entry_price = in_position["entry_price"]
            direction   = in_position["direction"]

            ret = (exit_price / entry_price - 1) * 100
            if direction == "down":
                ret = -ret
            ret -= commission_pct * 2  # комиссия туда-обратно

            correct = ret > 0
            trades.append({
                "date":        in_position["entry_date"],
                "ticker":      ticker,
                "direction":   direction,
                "confidence":  in_position["confidence"],
                "entry":       entry_price,
                "exit":        exit_price,
                "return_pct":  round(ret, 2),
                "correct":     correct,
                "hold_days":   hold_days,
            })
            capital *= (1 + ret / 100)
            equity.append(capital)
            in_position = None

        # Сигнал раз в 5 дней (не каждый день — реалистично)
        if i % 5 != 0 or in_position:
            continue

        sent = sent_by_date.get(date, {})
        direction, confidence = _simulate_signal(
            closes[:i + 1],
            sent.get("sentiment_index"),
            sent.get("avg_signal"),
        )

        if direction == "flat" or confidence < min_confidence:
            continue

        entry_price = opens[i + 1] if (i + 1) < len(opens) else closes[i]
        in_position = {
            "direction":   direction,
            "entry_price": entry_price,
            "entry_date":  date,
            "confidence":  round(confidence, 3),
            "exit_idx":    i + 1 + hold_days,
        }

    if not trades:
        return {"error": "Недостаточно сделок для статистики"}

    # ── Метрики ──────────────────────────────────────────────────────────────
    total      = len(trades)
    winners    = [t for t in trades if t["correct"]]
    losers     = [t for t in trades if not t["correct"]]
    win_rate   = round(len(winners) / total * 100, 1)
    avg_win    = round(sum(t["return_pct"] for t in winners) / len(winners), 2) if winners else 0
    avg_loss   = round(sum(t["return_pct"] for t in losers)  / len(losers),  2) if losers  else 0
    total_ret  = round((capital - 1) * 100, 2)
    profit_factor = round(-sum(t["return_pct"] for t in winners) /
                          sum(t["return_pct"] for t in losers), 2) \
                    if losers and sum(t["return_pct"] for t in losers) != 0 else None

    # Buy & Hold
    bh_ret = round((closes[-1] / closes[60] - 1) * 100, 2)

    # Max Drawdown
    peak = 1.0
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Sharpe (упрощённый)
    rets = [t["return_pct"] / 100 for t in trades]
    if len(rets) > 1:
        import math
        mean_r = sum(rets) / len(rets)
        std_r  = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets))
        sharpe = round(mean_r / std_r * math.sqrt(252 / hold_days), 2) if std_r > 0 else 0
    else:
        sharpe = 0

    # Разбивка по уровням уверенности
    high_conf = [t for t in trades if t["confidence"] >= 0.5]
    hc_wr = round(sum(1 for t in high_conf if t["correct"]) /
                  len(high_conf) * 100, 1) if high_conf else None

    return {
        "ticker":         ticker,
        "period_days":    len(closes),
        "total_trades":   total,
        "win_rate":       win_rate,
        "total_return":   total_ret,
        "buy_hold_return": bh_ret,
        "alpha":          round(total_ret - bh_ret, 2),
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "profit_factor":  profit_factor,
        "max_drawdown":   round(max_dd, 2),
        "sharpe":         sharpe,
        "high_conf_trades": len(high_conf),
        "high_conf_win_rate": hc_wr,
        "recent_trades":  trades[-10:],
        "equity_curve":   [round(e, 4) for e in equity[-100:]],
        "params": {
            "min_confidence": min_confidence,
            "hold_days":      hold_days,
            "commission_pct": commission_pct,
        },
    }
