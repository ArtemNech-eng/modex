"""
MOODEX — Настоящий исторический бэктест Claude

Для каждой исторической даты:
  1. Берём данные ДОСТУПНЫЕ В ТОТ ДЕНЬ (свечи до этой даты, настроение)
  2. Реально вызываем Claude с историческим контекстом
  3. Записываем его решение (up/down/flat + уверенность)
  4. Сравниваем с тем что реально произошло с ценой

Это честный тест — Claude не знает будущего, видит только прошлое.

⚠️  На 2 года данных, каждые 5 дней ≈ 100 вызовов Claude.
    Занимает 5-15 минут. Каждый вызов использует токены.
"""
import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _sma(closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


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
    return round(100 - 100 / (1 + ag / al), 1) if al != 0 else 100.0


def _pct(closes: list[float], n: int) -> Optional[float]:
    if len(closes) <= n or closes[-n - 1] == 0:
        return None
    return round((closes[-1] / closes[-n - 1] - 1) * 100, 2)


async def _ask_claude_historical(
    claude_agent,
    ticker: str,
    company: str,
    date: str,
    closes_to_date: list[float],
    sentiment: Optional[dict],
) -> dict:
    """
    Вызываем Claude с данными которые были доступны на конкретную дату.
    Используем облегчённый промпт чтобы экономить токены.
    """
    price  = closes_to_date[-1]
    s20    = _sma(closes_to_date, 20)
    s50    = _sma(closes_to_date, 50)
    r      = _rsi(closes_to_date, 14)
    ch1    = _pct(closes_to_date, 1)
    ch5    = _pct(closes_to_date, 5)
    ch20   = _pct(closes_to_date, 20)

    # Режим рынка
    if s20 and s50:
        if price > s20 > s50:
            regime = "восходящий тренд"
        elif price < s20 < s50:
            regime = "нисходящий тренд"
        else:
            regime = "боковик"
    else:
        regime = "нет данных"

    sent_block = ""
    if sentiment:
        idx = sentiment.get("sentiment_index", 50)
        sig = sentiment.get("avg_signal", 0)
        cnt = sentiment.get("msg_count", 0)
        mood = "бычье" if idx > 60 else "медвежье" if idx < 40 else "нейтральное"
        sent_block = f"Настроение толпы: {idx:.0f}/100 ({mood}), {cnt} сообщений, сигнал {sig:+.3f}"
    else:
        sent_block = "Настроение: нет данных"

    system = """Ты трейдер на Московской бирже. Дата фиксирована — не знаешь что будет после.
Оцени акцию и дай прогноз на 5 торговых дней.
Отвечай ТОЛЬКО JSON, без пояснений."""

    user = f"""Дата: {date}
Акция: {ticker} ({company})
Цена: {price:.2f} ₽
SMA20: {s20} | SMA50: {s50}
RSI(14): {r}
Изменение: день {ch1:+.1f}% | неделя {ch5:+.1f}% | месяц {ch20:+.1f}%
Режим: {regime}
{sent_block}
Доступно свечей: {len(closes_to_date)}

Прогноз на 5 дней:
{{"signal":"bullish|bearish|neutral","confidence":0-100,"reason":"1 предложение"}}"""

    try:
        result = await claude_agent._ask(system, user, max_tokens=150)
        import json
        start = result.find("{")
        end   = result.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(result[start:end])
            return {
                "signal":     data.get("signal", "neutral"),
                "confidence": data.get("confidence", 0),
                "reason":     data.get("reason", ""),
            }
    except Exception as e:
        logger.debug(f"Claude historical call failed {date}: {e}")

    return {"signal": "neutral", "confidence": 0, "reason": "ошибка"}


async def run_real_claude_backtest(
    ticker: str,
    step_days: int = 5,
    hold_days: int = 5,
    min_confidence: int = 40,
    max_calls: int = 60,
    commission_pct: float = 0.05,
    progress_callback=None,
) -> dict:
    """
    Настоящий бэктест — Claude реально анализирует каждую историческую дату.

    step_days:      каждые N дней генерируем сигнал
    hold_days:      удерживаем позицию N дней
    min_confidence: минимальная уверенность Claude для входа (0-100)
    max_calls:      максимум вызовов Claude (ограничение по стоимости)
    """
    from src.analysis import technical as ta
    from src.agent.claude_agent import ClaudeAgent
    from src import db
    from config.settings import MOEX_TICKERS

    claude = ClaudeAgent()
    company = MOEX_TICKERS.get(ticker, ticker)

    # Загружаем все свечи
    try:
        candles = await ta.fetch_candles(ticker, days=730)
    except Exception as e:
        return {"error": f"Не удалось загрузить свечи: {e}"}

    closes = candles.get("close", [])
    opens  = candles.get("open",  [])
    dates  = candles.get("dates", [])

    if len(closes) < 100:
        return {"error": "Недостаточно данных (< 100 дней)"}

    # История настроений
    hist      = await db.sentiment_history(ticker=ticker, limit=1000)
    sent_map  = {h["date"]: h for h in hist}

    # Выбираем точки для анализа
    # Идём с конца чтобы взять свежие данные, не старые
    analysis_points = []
    i = 60   # минимум 60 свечей для индикаторов
    while i < len(closes) - hold_days - 1 and len(analysis_points) < max_calls:
        analysis_points.append(i)
        i += step_days

    if not analysis_points:
        return {"error": "Нет точек для анализа"}

    # Запускаем анализ
    calls_done = 0
    trades     = []
    capital    = 1.0
    equity     = [1.0]
    decisions  = []

    for idx, point_i in enumerate(analysis_points):
        date         = dates[point_i][:10] if point_i < len(dates) else ""
        closes_to    = closes[:point_i + 1]
        sentiment    = sent_map.get(date)

        if progress_callback:
            progress_callback({
                "done": idx + 1,
                "total": len(analysis_points),
                "date": date,
                "calls": calls_done,
            })

        # Вызываем Claude
        decision = await _ask_claude_historical(
            claude, ticker, company, date, closes_to, sentiment
        )
        calls_done += 1
        decision["date"] = date

        signal_map = {"bullish": "up", "bearish": "down", "neutral": "flat"}
        direction  = signal_map.get(decision["signal"], "flat")
        confidence = decision["confidence"]

        decisions.append({
            "date":       date,
            "direction":  direction,
            "confidence": confidence,
            "reason":     decision.get("reason", ""),
        })

        # Пропускаем если уверенность ниже порога или нейтрально
        if direction == "flat" or confidence < min_confidence:
            continue

        # Цена входа — следующий день открытие
        entry_i = point_i + 1
        exit_i  = point_i + 1 + hold_days
        if exit_i >= len(closes):
            continue

        entry_price = opens[entry_i] if entry_i < len(opens) else closes[entry_i]
        exit_price  = opens[exit_i]  if exit_i  < len(opens) else closes[exit_i]

        if entry_price == 0:
            continue

        ret = (exit_price / entry_price - 1) * 100
        if direction == "down":
            ret = -ret
        ret -= commission_pct * 2

        correct = ret > 0
        capital *= (1 + ret / 100)
        equity.append(round(capital, 4))

        trades.append({
            "date":       date,
            "direction":  direction,
            "confidence": confidence,
            "reason":     decision.get("reason", ""),
            "entry":      round(entry_price, 2),
            "exit":       round(exit_price, 2),
            "return_pct": round(ret, 2),
            "correct":    correct,
        })

        # Небольшая пауза чтобы не перегружать API
        await asyncio.sleep(0.3)

    if not trades:
        return {
            "error":     "Claude не дал ни одного торгового сигнала (всё нейтрально или ниже порога)",
            "decisions": decisions,
            "calls_made": calls_done,
        }

    # Метрики
    total     = len(trades)
    winners   = [t for t in trades if t["correct"]]
    losers    = [t for t in trades if not t["correct"]]
    win_rate  = round(len(winners) / total * 100, 1)
    avg_win   = round(sum(t["return_pct"] for t in winners) / len(winners), 2) if winners else 0
    avg_loss  = round(sum(t["return_pct"] for t in losers)  / len(losers),  2) if losers  else 0
    total_ret = round((capital - 1) * 100, 2)

    bh_start = closes[60] if len(closes) > 60 else closes[0]
    bh_ret   = round((closes[-1] / bh_start - 1) * 100, 2)

    peak = 1.0
    max_dd = 0.0
    for e in equity:
        peak   = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)

    rets   = [t["return_pct"] / 100 for t in trades]
    mean_r = sum(rets) / len(rets)
    std_r  = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets)) if len(rets) > 1 else 0
    sharpe = round(mean_r / std_r * math.sqrt(252 / hold_days), 2) if std_r > 0 else 0

    pf = round(-sum(t["return_pct"] for t in winners) /
               sum(t["return_pct"] for t in losers), 2) \
         if losers and sum(t["return_pct"] for t in losers) != 0 else None

    # Статистика по уверенности
    high = [t for t in trades if t["confidence"] >= 60]
    hc_wr = round(sum(1 for t in high if t["correct"]) / len(high) * 100, 1) if high else None

    return {
        "ticker":             ticker,
        "mode":               "real_claude",
        "calls_made":         calls_done,
        "analysis_points":    len(analysis_points),
        "total_trades":       total,
        "win_rate":           win_rate,
        "total_return":       total_ret,
        "buy_hold_return":    bh_ret,
        "alpha":              round(total_ret - bh_ret, 2),
        "avg_win":            avg_win,
        "avg_loss":           avg_loss,
        "profit_factor":      pf,
        "max_drawdown":       round(max_dd, 2),
        "sharpe":             sharpe,
        "high_conf_trades":   len(high),
        "high_conf_win_rate": hc_wr,
        "equity_curve":       equity,
        "recent_trades":      trades[-15:],
        "all_decisions":      decisions[-20:],
        "params": {
            "step_days":      step_days,
            "hold_days":      hold_days,
            "min_confidence": min_confidence,
            "commission_pct": commission_pct,
        },
    }
