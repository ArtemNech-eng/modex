"""
MOODEX — Context Builder для Claude

Собирает исторический контекст из БД (sentiment_daily + MOEX свечи),
чтобы Claude мог принимать решения на основе реальной истории рынка.

Принцип: in-context learning.
  — Ищем похожие ситуации в прошлом (похожий sentiment + похожая техника)
  — Смотрим что происходило с ценой через 1/5/10 дней
  — Отдаём Claude эти примеры как "обучающие данные" в промпте

Claude не дообучается в смысле весов — но видит реальные паттерны
конкретного рынка (MOEX) и делает решения на их основе.
"""
import logging
from typing import Optional
from src import db
from src.analysis import technical as ta

logger = logging.getLogger(__name__)


async def build_ticker_context(
    ticker: str,
    current_sentiment: Optional[float] = None,
    max_examples: int = 40,
) -> dict:
    """
    Строим исторический контекст для Claude по тикеру.

    Возвращает словарь с:
      - patterns: список исторических примеров (sentiment → движение цены)
      - stats: агрегированная статистика (точность, средний доход)
      - summary: готовый текст для промпта Claude
    """
    # 1. Получаем историю настроений из БД
    hist = await db.sentiment_history(ticker=ticker, limit=500)
    if len(hist) < 10:
        return {"patterns": [], "stats": {}, "summary": "История настроений недостаточна (< 10 дней)."}

    # 2. Получаем исторические свечи с MOEX
    try:
        candles = await ta.fetch_candles(ticker, days=600)
        closes = candles.get("close", [])
        dates  = candles.get("dates", [])
    except Exception as e:
        logger.warning(f"Не удалось получить свечи {ticker}: {e}")
        return {"patterns": [], "stats": {}, "summary": "Нет исторических данных MOEX."}

    if len(closes) < 20:
        return {"patterns": [], "stats": {}, "summary": "Мало исторических свечей MOEX."}

    # Строим словарь цен по дате
    price_by_date: dict[str, float] = dict(zip(dates, closes))

    # 3. Строим примеры: для каждого дня с настроением смотрим что было потом
    patterns = []
    for entry in hist:
        date         = entry["date"]
        sentiment    = entry["sentiment_index"]   # 0–100
        avg_signal   = entry["avg_signal"]         # -1..+1
        msg_count    = entry["msg_count"]

        if msg_count < 3:
            continue  # мало данных — ненадёжно

        price_today = price_by_date.get(date)
        if not price_today:
            continue

        # Ищем цену через 1, 5, 10 дней
        sorted_dates = sorted(price_by_date.keys())
        try:
            idx = sorted_dates.index(date)
        except ValueError:
            continue

        def get_return(days_ahead: int) -> Optional[float]:
            future_idx = idx + days_ahead
            if future_idx >= len(sorted_dates):
                return None
            future_price = price_by_date.get(sorted_dates[future_idx])
            if not future_price:
                return None
            return round((future_price / price_today - 1) * 100, 2)

        ret_1d  = get_return(1)
        ret_5d  = get_return(5)
        ret_10d = get_return(10)

        if ret_1d is None:
            continue

        # Категоризируем настроение
        if sentiment >= 65:
            mood = "бычье"
        elif sentiment <= 35:
            mood = "медвежье"
        else:
            mood = "нейтральное"

        patterns.append({
            "date":       date,
            "sentiment":  round(sentiment, 1),
            "mood":       mood,
            "msg_count":  msg_count,
            "ret_1d":     ret_1d,
            "ret_5d":     ret_5d,
            "ret_10d":    ret_10d,
        })

    if not patterns:
        return {"patterns": [], "stats": {}, "summary": "Не удалось сопоставить настроение с ценами."}

    # 4. Считаем статистику
    bull_patterns = [p for p in patterns if p["mood"] == "бычье"]
    bear_patterns = [p for p in patterns if p["mood"] == "медвежье"]

    def avg_return(pts, key):
        vals = [p[key] for p in pts if p[key] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def win_rate(pts, key):
        vals = [p[key] for p in pts if p[key] is not None]
        if not vals:
            return None
        return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)

    stats = {
        "total_days":         len(patterns),
        "bull_days":          len(bull_patterns),
        "bear_days":          len(bear_patterns),
        "bull_avg_ret_1d":    avg_return(bull_patterns, "ret_1d"),
        "bull_win_rate_1d":   win_rate(bull_patterns, "ret_1d"),
        "bull_avg_ret_5d":    avg_return(bull_patterns, "ret_5d"),
        "bear_avg_ret_1d":    avg_return(bear_patterns, "ret_1d"),
        "bear_win_rate_1d":   win_rate(bear_patterns, "ret_1d"),
        "bear_avg_ret_5d":    avg_return(bear_patterns, "ret_5d"),
    }

    # 5. Отбираем похожие примеры (по близости к текущему sentiment)
    if current_sentiment is not None:
        # Сортируем по близости к текущему настроению
        examples = sorted(patterns, key=lambda p: abs(p["sentiment"] - current_sentiment))[:max_examples]
    else:
        # Берём последние N примеров
        examples = sorted(patterns, key=lambda p: p["date"], reverse=True)[:max_examples]

    # 6. Форматируем текст для промпта Claude
    lines = []
    for p in sorted(examples, key=lambda x: x["date"])[-30:]:
        direction_1d = "↑" if (p["ret_1d"] or 0) > 0 else "↓"
        direction_5d = "↑" if (p["ret_5d"] or 0) > 0 else "↓"
        lines.append(
            f"  {p['date']} | настроение {p['sentiment']}/100 ({p['mood']}, {p['msg_count']} сообщ.) "
            f"→ завтра {direction_1d}{abs(p['ret_1d'] or 0):.1f}%"
            + (f", через 5 дней {direction_5d}{abs(p['ret_5d'] or 0):.1f}%" if p["ret_5d"] else "")
        )

    summary_parts = [
        f"📊 ИСТОРИЧЕСКИЕ ПАТТЕРНЫ {ticker} ({len(patterns)} торговых дней):",
        "",
        "  При бычьем настроении (>65/100):",
        f"    — Средний доход на следующий день: {stats['bull_avg_ret_1d']}%",
        f"    — Процент выигрышных дней: {stats['bull_win_rate_1d']}%",
        f"    — Средний доход за 5 дней: {stats['bull_avg_ret_5d']}%",
        "",
        "  При медвежьем настроении (<35/100):",
        f"    — Средний доход на следующий день: {stats['bear_avg_ret_1d']}%",
        f"    — Процент выигрышных дней: {stats['bear_win_rate_1d']}%",
        f"    — Средний доход за 5 дней: {stats['bear_avg_ret_5d']}%",
        "",
        f"📋 Похожие исторические ситуации (по близости к текущему настроению):",
    ] + lines

    return {
        "patterns": examples,
        "stats":    stats,
        "summary":  "\n".join(summary_parts),
    }
