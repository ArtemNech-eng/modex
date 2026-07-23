"""
MOODEX — Context Builder для Claude

Два вида контекста которые получает Claude перед принятием решения:

1. build_ticker_context()  — история настроений → движение цены
   Паттерны: "когда настроение было X, цена шла Y"

2. build_price_context()   — срез 2 лет ценовой истории
   Не сырые свечи, а аналитический дайджест:
   — Доходность за 1н/1м/3м/6м/1г/2г
   — Текущее положение относительно диапазонов
   — Волатильность (текущая vs историческая)
   — Крупные движения (>5% за день) — что случалось и когда
   — Ключевые уровни: 52н хай/лой, 2г хай/лой, текущая позиция %
   — Фазы тренда: сколько раз разворачивался, средняя продолжительность

Claude не получает 730 строк данных — только то, что реально помогает решению.
"""
import logging
import math
from typing import Optional
from src import db
from src.analysis import technical as ta

logger = logging.getLogger(__name__)


async def build_price_context(ticker: str) -> str:
    """
    Аналитический дайджест 2 лет ценовой истории для Claude.
    Возвращает готовый текстовый блок для промпта.
    """
    try:
        data = await ta.fetch_candles(ticker, days=730)
    except Exception as e:
        return f"Ценовая история недоступна: {e}"

    closes = data.get("close", [])
    highs  = data.get("high", [])
    lows   = data.get("low", [])
    dates  = data.get("dates", [])

    if len(closes) < 60:
        return "Недостаточно исторических данных (< 60 дней)."

    price = closes[-1]
    n     = len(closes)

    # ── 1. Доходность за разные периоды ──────────────────────────────────────
    def ret(days: int) -> Optional[str]:
        if n <= days or closes[-days - 1] == 0:
            return None
        r = (closes[-1] / closes[-days - 1] - 1) * 100
        arrow = "↑" if r > 0 else "↓"
        return f"{arrow}{abs(r):.1f}%"

    periods = [
        ("1 неделя",  5),
        ("1 месяц",   21),
        ("3 месяца",  63),
        ("6 месяцев", 126),
        ("1 год",     252),
        ("2 года",    min(500, n - 1)),
    ]
    returns_lines = []
    for label, d in periods:
        r = ret(d)
        if r:
            returns_lines.append(f"  {label}: {r}")

    # ── 2. Диапазоны: 52-недельный и 2-летний ────────────────────────────────
    hi_52w  = max(highs[-252:])  if len(highs)  >= 252 else max(highs)
    lo_52w  = min(lows[-252:])   if len(lows)   >= 252 else min(lows)
    hi_2y   = max(highs)
    lo_2y   = min(lows)

    pos_52w = (price - lo_52w) / (hi_52w - lo_52w) * 100 if hi_52w != lo_52w else 50
    pos_2y  = (price - lo_2y)  / (hi_2y  - lo_2y)  * 100 if hi_2y  != lo_2y  else 50

    dist_from_hi_52w = (price / hi_52w - 1) * 100
    dist_from_lo_52w = (price / lo_52w - 1) * 100

    # ── 3. Волатильность ──────────────────────────────────────────────────────
    def vol_period(n_days: int) -> Optional[float]:
        if len(closes) < n_days + 1:
            return None
        rets = [closes[i] / closes[i-1] - 1 for i in range(len(closes)-n_days, len(closes))]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        std  = math.sqrt(sum((r - mean)**2 for r in rets) / (len(rets)-1))
        return round(std * math.sqrt(252) * 100, 1)

    vol_20d  = vol_period(20)
    vol_252d = vol_period(252)
    vol_line = ""
    if vol_20d and vol_252d:
        regime_v = "повышенная" if vol_20d > vol_252d * 1.3 else \
                   "пониженная" if vol_20d < vol_252d * 0.7 else "нормальная"
        vol_line = f"  Текущая (20д): {vol_20d}% годовых | Историческая (1г): {vol_252d}% → {regime_v}"

    # ── 4. Крупные движения >5% за день (последние 2 года) ───────────────────
    big_moves = []
    for i in range(1, min(n, 500)):
        if closes[i-1] == 0:
            continue
        move = (closes[i] / closes[i-1] - 1) * 100
        if abs(move) >= 5.0:
            d = dates[i] if i < len(dates) else "?"
            direction = "🔴" if move < 0 else "🟢"
            big_moves.append(f"  {direction} {d}: {move:+.1f}%")
    big_moves = big_moves[-8:]  # последние 8 крупных событий

    # ── 5. Фазы тренда (простая детекция разворотов) ─────────────────────────
    def detect_phases(closes: list[float], threshold_pct: float = 15.0) -> list[dict]:
        """Находит фазы бычьего/медвежьего рынка (движения > threshold_pct%)."""
        if len(closes) < 20:
            return []
        phases = []
        peak = trough = closes[0]
        peak_i = trough_i = 0
        direction = None

        for i, c in enumerate(closes[1:], 1):
            if direction is None:
                if c > peak:
                    peak, peak_i = c, i
                elif c < trough:
                    trough, trough_i = c, i
                if peak > 0 and (peak - trough) / trough * 100 >= threshold_pct:
                    direction = "bull" if peak_i > trough_i else "bear"

            elif direction == "bull":
                if c > peak:
                    peak, peak_i = c, i
                elif peak > 0 and (peak - c) / peak * 100 >= threshold_pct:
                    move = (peak / trough - 1) * 100
                    phases.append({"type": "bull", "move": round(move, 1), "bars": peak_i - trough_i})
                    trough, trough_i = c, i
                    direction = "bear"

            elif direction == "bear":
                if c < trough:
                    trough, trough_i = c, i
                elif trough > 0 and (c - trough) / trough * 100 >= threshold_pct:
                    move = (trough / peak - 1) * 100
                    phases.append({"type": "bear", "move": round(move, 1), "bars": trough_i - peak_i})
                    peak, peak_i = c, i
                    direction = "bull"
        return phases

    phases = detect_phases(closes[-500:] if len(closes) > 500 else closes)
    phase_lines = []
    for ph in phases[-5:]:
        emoji = "📈" if ph["type"] == "bull" else "📉"
        label = "рост" if ph["type"] == "bull" else "снижение"
        phase_lines.append(f"  {emoji} {label}: {ph['move']:+.1f}% за ~{ph['bars']} торговых дней")

    # ── Текущий тренд из последних 252 свечей ────────────────────────────────
    if len(closes) >= 252:
        trend_1y = (closes[-1] / closes[-252] - 1) * 100
        trend_label = "восходящий" if trend_1y > 5 else "нисходящий" if trend_1y < -5 else "боковой"
    else:
        trend_label = "нет данных"

    # ── Собираем итоговый текст ───────────────────────────────────────────────
    lines = [
        f"📈 ЦЕНОВАЯ ИСТОРИЯ {ticker} (последние 2 года, {n} торговых дней):",
        f"  ⚠️ Все цены ниже — исторические. Текущую цену смотри в блоке MOEX.",
        "",
        f"  Последняя цена в истории: {price:.2f} ₽  (может быть устаревшей — используй MOEX данные)",
        f"  Тренд за год: {trend_label}",
        "",
        "  Доходность:",
    ] + returns_lines + [
        "",
        "  Позиция в диапазонах:",
        f"  52-нед. хай: {hi_52w:.2f} (от хая: {dist_from_hi_52w:.1f}%)",
        f"  52-нед. лой: {lo_52w:.2f} (от лоя: {dist_from_lo_52w:+.1f}%)",
        f"  Позиция в 52н диапазоне: {pos_52w:.0f}%  (0%=дно, 100%=вершина)",
        f"  Позиция в 2г диапазоне:  {pos_2y:.0f}%",
        "",
        "  Волатильность:",
        vol_line or "  нет данных",
    ]

    if big_moves:
        lines += ["", f"  Крупные движения >5% за день (последние {len(big_moves)}):"] + big_moves

    if phase_lines:
        lines += ["", "  Фазы рынка (движения >15%):"] + phase_lines

    return "\n".join(lines)
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


async def build_memory_context(ticker: str) -> str:
    """
    Память Claude — его прошлые прогнозы по этому тикеру.
    Показывает точность, паттерны ошибок, уверенность.
    """
    try:
        preds = await db.list_recent_predictions(limit=20, ticker=ticker)
        stats = await db.accuracy_stats(ticker=ticker)
    except Exception as e:
        return f"Память недоступна: {e}"

    if not preds:
        return f"По тикеру {ticker} прогнозов ещё не было — это первый анализ."

    lines = [f"🧠 МОЯ ПАМЯТЬ — прошлые прогнозы по {ticker}:"]

    # Общая статистика
    total     = stats.get("total", 0)
    evaluated = stats.get("evaluated", 0)
    accuracy  = stats.get("accuracy")

    if evaluated > 0 and accuracy is not None:
        acc_pct = round(accuracy * 100, 1)
        comment = "хорошая" if acc_pct >= 60 else "плохая — пересмотри подход" if acc_pct < 40 else "средняя"
        lines.append(f"  Всего прогнозов: {total} | Оценено: {evaluated} | "
                     f"Точность: {acc_pct}% ({comment})")
    else:
        lines.append(f"  Всего прогнозов: {total} | Оценённых пока нет (ждём истечения горизонта)")

    # Последние 5 прогнозов
    lines.append("  Последние прогнозы:")
    for p in preds[:5]:
        direction = {"up": "↑ ВВЕРХ", "down": "↓ ВНИЗ", "flat": "→ БОКОМ"}.get(
            p.get("direction", "flat"), "?")
        conf      = round((p.get("confidence") or 0) * 100)
        correct   = p.get("correct")
        if correct is True:
            result = "✅"
        elif correct is False:
            result = "❌"
        else:
            result = "⏳"
        ret = p.get("realized_return")
        ret_str = f" ({ret:+.1f}%)" if ret is not None else ""
        date = (p.get("created_at") or "")[:10]
        lines.append(f"    {date}: {direction} уверенность {conf}% → {result}{ret_str}")

    # Предупреждение если точность низкая
    if evaluated >= 5 and accuracy is not None and accuracy < 0.45:
        lines.append("  ⚠️ Точность ниже 45% — рынок вёл себя непредсказуемо, будь осторожен.")

    return "\n".join(lines)


async def build_news_context(ticker: str, news_cache: list) -> str:
    """
    Последние новости по тикеру из RSS-коллектора.
    news_cache — список NewsItem из RSSCollector (передаётся из агрегатора).
    """
    if not news_cache:
        return ""

    from src.nlp.ticker_extractor import extract_tickers
    relevant = []
    for item in news_cache[-200:]:   # смотрим последние 200 новостей
        text = getattr(item, "full_text", "") or ""
        tickers_in_news = extract_tickers(text)
        if ticker in tickers_in_news or ticker.lower() in text.lower():
            relevant.append(item)

    if not relevant:
        return ""

    relevant = sorted(relevant, key=lambda x: x.timestamp, reverse=True)[:5]

    lines = [f"📰 ПОСЛЕДНИЕ НОВОСТИ по {ticker}:"]
    for item in relevant:
        age_h = int((
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            - item.timestamp
        ).total_seconds() / 3600)
        age_str = f"{age_h}ч назад" if age_h < 24 else f"{age_h // 24}д назад"
        lines.append(f"  [{age_str}] {item.source}: {item.title[:100]}")

    return "\n".join(lines)


async def build_multiframe_context(ticker: str) -> str:
    """
    Мультитаймфреймовый анализ: день + неделя + месяц.
    Даёт Claude понимание на каком тренде находится цена глобально.
    """
    import httpx
    from datetime import datetime, timedelta, timezone

    ISS = ("https://iss.moex.com/iss/engines/stock/markets/shares"
           "/securities/{ticker}/candles.json")
    url = ISS.format(ticker=ticker.upper())

    async def fetch_tf(interval: int, days: int) -> list[float]:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params={"interval": str(interval), "from": start})
                resp.raise_for_status()
            data = resp.json().get("candles", {})
            cols = data.get("columns", [])
            rows = data.get("data", [])
            if "close" not in cols:
                return []
            ci = cols.index("close")
            return [r[ci] for r in rows if r[ci] is not None]
        except Exception:
            return []

    import asyncio
    weekly, monthly = await asyncio.gather(
        fetch_tf(7, 365),    # недельные свечи за год
        fetch_tf(31, 730),   # месячные свечи за 2 года
    )

    lines = [f"📊 МУЛЬТИТАЙМФРЕЙМ {ticker}:"]

    def tf_summary(closes: list[float], label: str) -> Optional[str]:
        if len(closes) < 4:
            return None
        from src.analysis.technical import sma, rsi
        last = closes[-1]
        s20  = sma(closes, min(20, len(closes) // 2))
        r    = rsi(closes, min(14, len(closes) - 1))
        trend = "↑ вверх" if (s20 and last > s20) else "↓ вниз"
        change = (closes[-1] / closes[0] - 1) * 100
        result = f"  {label}: тренд {trend}, изменение {change:+.1f}%"
        if r:
            result += f", RSI={r:.0f}"
        return result

    w = tf_summary(weekly, "Недельный (1г)")
    m = tf_summary(monthly, "Месячный (2г)")
    if w:
        lines.append(w)
    if m:
        lines.append(m)

    if not w and not m:
        return ""

    # Проверяем согласованность таймфреймов
    if w and m:
        w_bull = "вверх" in w
        m_bull = "вверх" in m
        if w_bull == m_bull:
            direction = "бычий" if w_bull else "медвежий"
            lines.append(f"  ✅ Все таймфреймы согласованы — {direction} тренд")
        else:
            lines.append("  ⚠️ Таймфреймы расходятся — высокая неопределённость")

    return "\n".join(lines)
