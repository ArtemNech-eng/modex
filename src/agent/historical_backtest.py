"""
MOODEX — Настоящий исторический бэктест Claude (с риск-менеджментом и разбором ошибок)

Для каждой исторической даты:
  1. Берём данные ДОСТУПНЫЕ В ТОТ ДЕНЬ (свечи до этой даты, настроение)
  2. Реально вызываем Claude с историческим контекстом
  3. Записываем его решение (bullish/bearish/neutral + уверенность)
  4. Прогоняем сделку с ATR-стопом/целью (intrabar-выход) — как в реальной торговле
  5. Сравниваем с тем что реально произошло с ценой

Это честный тест — Claude не знает будущего, видит только прошлое.
Метрики считаются РЕАЛЬНЫЕ, никакие исходы не подгоняются. Win rate — это
то, что стратегия честно показала на истории; он НЕ гарантирует будущее.

Что нового по сравнению с базовой версией:
  • Риск-менеджмент: стоп и цель по ATR, выход внутри дня по high/low или по времени.
  • Фильтры качества: подтверждение техникой, запрет торговли против сильного тренда.
  • Разбор ошибок: каждая убыточная сделка категоризируется (против тренда,
    перекупленность, толпа обманула, выбило стопом и т.д.).
  • Калибровка: растёт ли реальная точность с ростом уверенности Claude.

⚠️  На 2 года данных, каждые 5 дней ≈ 100 вызовов Claude.
    Занимает 5-15 минут. Каждый вызов использует токены.
    Режим dry_run=True прогоняет ту же механику БЕЗ вызовов Claude
    (решение берётся из алгоритмического техсигнала) — для офлайн-проверки логики.
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


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return sum(trs[-period:]) / period


def _pct(closes: list[float], n: int) -> Optional[float]:
    if len(closes) <= n or closes[-n - 1] == 0:
        return None
    return round((closes[-1] / closes[-n - 1] - 1) * 100, 2)


def _regime(closes: list[float]) -> str:
    price = closes[-1]
    s20 = _sma(closes, 20)
    s50 = _sma(closes, 50)
    if not (s20 and s50):
        return "нет данных"
    if price > s20 > s50:
        return "восходящий тренд"
    if price < s20 < s50:
        return "нисходящий тренд"
    return "боковик"


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
    regime = _regime(closes_to_date)

    sent_block = "Настроение: нет данных"
    if sentiment:
        idx = sentiment.get("sentiment_index", 50)
        sig = sentiment.get("avg_signal", 0)
        cnt = sentiment.get("msg_count", 0)
        mood = "бычье" if idx > 60 else "медвежье" if idx < 40 else "нейтральное"
        sent_block = f"Настроение толпы: {idx:.0f}/100 ({mood}), {cnt} сообщений, сигнал {sig:+.3f}"

    def _f(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "—"

    system = """Ты трейдер на Московской бирже. Дата фиксирована — не знаешь что будет после.
Оцени акцию и дай прогноз на ближайшие торговые дни.
Отвечай ТОЛЬКО JSON, без пояснений."""

    user = f"""Дата: {date}
Акция: {ticker} ({company})
Цена: {price:.2f} ₽
SMA20: {_f(s20)} | SMA50: {_f(s50)}
RSI(14): {_f(r)}
Изменение: день {_f(ch1, '%')} | неделя {_f(ch5, '%')} | месяц {_f(ch20, '%')}
Режим: {regime}
{sent_block}
Доступно свечей: {len(closes_to_date)}

Прогноз:
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


def _rule_based_decision(
    closes_to: list[float],
    highs_to: list[float],
    lows_to: list[float],
    sentiment: Optional[dict],
) -> dict:
    """
    Алгоритмическое решение из технического анализа (без вызова Claude).
    Используется в режиме dry_run для офлайн-проверки всей механики бэктеста.
    Уверенность — из абсолютной силы техсигнала.
    """
    try:
        from src.analysis import technical as ta
        tech = ta.compute_from_series("X", closes_to, highs_to, lows_to)
        signal = tech.signal
        confidence = int(min(100, abs(tech.score) * 100))
        reason = "; ".join(tech.reasons[:2]) if tech.reasons else "техсигнал"
    except Exception:
        signal, confidence, reason = "neutral", 0, "нет данных"
    return {"signal": signal, "confidence": confidence, "reason": reason}


def _tech_context(closes_to: list[float], highs_to: list[float], lows_to: list[float]) -> dict:
    """Контекст для фильтров и разбора ошибок: техсигнал, режим, RSI, ATR."""
    ctx = {"tech_signal": "neutral", "regime": "нет данных", "rsi": None, "atr": None}
    try:
        from src.analysis import technical as ta
        tech = ta.compute_from_series("X", closes_to, highs_to, lows_to)
        ctx["tech_signal"] = tech.signal
        ctx["regime"] = {"uptrend": "восходящий тренд", "downtrend": "нисходящий тренд",
                         "range": "боковик"}.get(tech.regime, tech.regime)
        ctx["rsi"] = tech.rsi14
    except Exception:
        ctx["regime"] = _regime(closes_to)
        ctx["rsi"] = _rsi(closes_to, 14)
    ctx["atr"] = _atr(highs_to, lows_to, closes_to, 14)
    return ctx


def _simulate_trade(
    direction: str,          # "up" (лонг) или "down" (шорт)
    entry_price: float,
    atr_val: float,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    entry_i: int,
    max_hold: int,
    atr_stop_mult: float,
    atr_target_mult: float,
    commission_pct: float,
) -> Optional[dict]:
    """
    Прогнать сделку с риск-менеджментом: стоп и цель по ATR, выход внутри дня
    по high/low, иначе по времени (закрытие последнего бара окна удержания).

    Возвращает {exit, ret_pct, r_multiple, exit_reason, bars_held} или None.
    """
    if entry_price <= 0 or not atr_val or atr_val <= 0:
        return None

    risk = atr_stop_mult * atr_val
    reward = atr_target_mult * atr_val
    if direction == "up":
        stop, target = entry_price - risk, entry_price + reward
    else:
        stop, target = entry_price + risk, entry_price - reward

    n = len(closes)
    last_i = min(entry_i + max_hold, n - 1)
    exit_price, exit_reason = None, None

    for j in range(entry_i, last_i + 1):
        hi = highs[j] if j < len(highs) else closes[j]
        lo = lows[j]  if j < len(lows)  else closes[j]
        if direction == "up":
            if lo <= stop:
                exit_price, exit_reason = stop, "стоп"
                break
            if hi >= target:
                exit_price, exit_reason = target, "цель"
                break
        else:
            if hi >= stop:
                exit_price, exit_reason = stop, "стоп"
                break
            if lo <= target:
                exit_price, exit_reason = target, "цель"
                break

    bars_held = (j - entry_i) + 1
    if exit_price is None:
        exit_price, exit_reason = closes[last_i], "время"
        bars_held = last_i - entry_i + 1

    gross = (exit_price - entry_price) if direction == "up" else (entry_price - exit_price)
    side = commission_pct / 100.0
    cost = (entry_price + exit_price) * side
    net = gross - cost
    ret_pct = round(net / entry_price * 100, 2)
    r_multiple = round(net / risk, 2) if risk > 0 else 0.0

    return {
        "exit":       round(exit_price, 2),
        "stop":       round(stop, 2),
        "target":     round(target, 2),
        "ret_pct":    ret_pct,
        "r_multiple": r_multiple,
        "exit_reason": exit_reason,
        "bars_held":  bars_held,
    }


def _classify_error(trade: dict) -> str:
    """Категория ошибки для убыточной сделки — чтобы видеть, ГДЕ Claude ошибается."""
    direction = trade["direction"]
    regime    = trade.get("regime", "")
    rsi       = trade.get("rsi")
    agreement = trade.get("agreement")
    exit_reason = trade.get("exit_reason")
    sent      = trade.get("sentiment_index")

    if agreement is False:
        return "техника не подтвердила"
    if regime == "восходящий тренд" and direction == "down":
        return "против тренда (шорт в аптренде)"
    if regime == "нисходящий тренд" and direction == "up":
        return "против тренда (лонг в даунтренде)"
    if direction == "up" and rsi is not None and rsi >= 70:
        return "покупка в перекупленности (RSI≥70)"
    if direction == "down" and rsi is not None and rsi <= 30:
        return "шорт в перепроданности (RSI≤30)"
    if sent is not None and direction == "up" and sent >= 70:
        return "толпа обманула (эйфория развернулась)"
    if sent is not None and direction == "down" and sent <= 30:
        return "толпа обманула (паника развернулась)"
    if exit_reason == "стоп":
        return "выбило стопом (волатильность)"
    return "рыночный шум"


def _breakdown(trades: list[dict], key_fn) -> list[dict]:
    """Разбивка винрейта по произвольному ключу."""
    groups: dict = {}
    for t in trades:
        k = key_fn(t)
        groups.setdefault(k, []).append(t)
    out = []
    for k, items in groups.items():
        wins = sum(1 for t in items if t["correct"])
        out.append({
            "group":    k,
            "trades":   len(items),
            "wins":     wins,
            "win_rate": round(wins / len(items) * 100, 1),
            "avg_r":    round(sum(t["r_multiple"] for t in items) / len(items), 2),
        })
    return sorted(out, key=lambda x: -x["trades"])


def _confidence_calibration(trades: list[dict]) -> list[dict]:
    """Растёт ли реальная точность с ростом уверенности Claude? (честная калибровка)"""
    buckets = [(0, 40), (40, 60), (60, 80), (80, 101)]
    out = []
    for lo, hi in buckets:
        items = [t for t in trades if lo <= t["confidence"] < hi]
        if not items:
            continue
        wins = sum(1 for t in items if t["correct"])
        out.append({
            "range":    f"{lo}–{hi if hi <= 100 else 100}%",
            "trades":   len(items),
            "win_rate": round(wins / len(items) * 100, 1),
            "avg_r":    round(sum(t["r_multiple"] for t in items) / len(items), 2),
        })
    return out


async def run_real_claude_backtest(
    ticker: str,
    step_days: int = 5,
    hold_days: int = 10,
    min_confidence: int = 50,
    max_calls: int = 60,
    commission_pct: float = 0.05,
    atr_stop_mult: float = 1.5,
    atr_target_mult: float = 3.0,
    require_tech_agreement: bool = True,
    block_counter_trend: bool = True,
    dry_run: bool = False,
    progress_callback=None,
) -> dict:
    """
    Настоящий бэктест — Claude реально анализирует каждую историческую дату,
    сделки исполняются с риск-менеджментом (ATR-стоп/цель, intrabar-выход).

    step_days:              каждые N дней генерируем сигнал
    hold_days:              максимум баров в позиции (потом выход по времени)
    min_confidence:         минимальная уверенность Claude для входа (0-100)
    max_calls:              максимум вызовов Claude (ограничение по стоимости)
    atr_stop_mult:          стоп = вход ∓ N·ATR
    atr_target_mult:        цель = вход ± N·ATR (R/R = target/stop)
    require_tech_agreement: торговать только когда Claude согласен с техсигналом
    block_counter_trend:    не торговать против сильного тренда
    dry_run:                НЕ вызывать Claude — взять решение из техсигнала (офлайн-тест)
    """
    from src.analysis import technical as ta
    from src.agent.claude_agent import ClaudeAgent
    from src import db
    from config.settings import MOEX_TICKERS

    claude = None if dry_run else ClaudeAgent()
    company = MOEX_TICKERS.get(ticker, ticker)

    # Загружаем все свечи
    try:
        candles = await ta.fetch_candles(ticker, days=730)
    except Exception as e:
        return {"error": f"Не удалось загрузить свечи: {e}"}

    closes = candles.get("close", [])
    opens  = candles.get("open",  [])
    highs  = candles.get("high",  [])
    lows   = candles.get("low",   [])
    dates  = candles.get("dates", [])

    if len(closes) < 100:
        return {"error": "Недостаточно данных (< 100 дней)"}

    # История настроений
    hist      = await db.sentiment_history(ticker=ticker, limit=1000)
    sent_map  = {h["date"]: h for h in hist}

    # Выбираем точки для анализа (минимум 60 свечей для индикаторов)
    analysis_points = []
    i = 60
    while i < len(closes) - hold_days - 1 and len(analysis_points) < max_calls:
        analysis_points.append(i)
        i += step_days

    if not analysis_points:
        return {"error": "Нет точек для анализа"}

    calls_done = 0
    trades     = []
    capital    = 1.0
    equity     = [1.0]
    decisions  = []
    filtered   = {"нейтрально": 0, "низкая уверенность": 0,
                  "техника не подтвердила": 0, "против тренда": 0, "нет ATR/окна": 0}

    signal_map = {"bullish": "up", "bearish": "down", "neutral": "flat"}

    for idx, point_i in enumerate(analysis_points):
        date        = dates[point_i][:10] if point_i < len(dates) else ""
        closes_to   = closes[:point_i + 1]
        highs_to    = highs[:point_i + 1]
        lows_to     = lows[:point_i + 1]
        sentiment   = sent_map.get(date)

        if progress_callback:
            progress_callback({
                "done": idx + 1,
                "total": len(analysis_points),
                "date": date,
                "calls": calls_done,
            })

        # Решение: Claude или (в dry_run) алгоритмический техсигнал
        if dry_run:
            decision = _rule_based_decision(closes_to, highs_to, lows_to, sentiment)
        else:
            decision = await _ask_claude_historical(
                claude, ticker, company, date, closes_to, sentiment
            )
            calls_done += 1

        direction  = signal_map.get(decision["signal"], "flat")
        confidence = decision["confidence"]

        # Контекст для фильтров и разбора ошибок
        tctx = _tech_context(closes_to, highs_to, lows_to)
        agreement = (
            (direction == "up"   and tctx["tech_signal"] == "bullish") or
            (direction == "down" and tctx["tech_signal"] == "bearish")
        )
        sent_idx = sentiment.get("sentiment_index") if sentiment else None

        decisions.append({
            "date":       date,
            "direction":  direction,
            "confidence": confidence,
            "reason":     decision.get("reason", ""),
            "tech_signal": tctx["tech_signal"],
            "regime":     tctx["regime"],
        })

        # ── Фильтры входа ──
        if direction == "flat":
            filtered["нейтрально"] += 1
            if not dry_run:
                await asyncio.sleep(0.3)
            continue
        if confidence < min_confidence:
            filtered["низкая уверенность"] += 1
            if not dry_run:
                await asyncio.sleep(0.3)
            continue
        if require_tech_agreement and not agreement:
            filtered["техника не подтвердила"] += 1
            if not dry_run:
                await asyncio.sleep(0.3)
            continue
        if block_counter_trend and (
            (tctx["regime"] == "восходящий тренд" and direction == "down") or
            (tctx["regime"] == "нисходящий тренд" and direction == "up")
        ):
            filtered["против тренда"] += 1
            if not dry_run:
                await asyncio.sleep(0.3)
            continue

        # ── Исполнение сделки с риск-менеджментом ──
        entry_i = point_i + 1
        if entry_i >= len(closes):
            filtered["нет ATR/окна"] += 1
            continue
        entry_price = opens[entry_i] if entry_i < len(opens) else closes[entry_i]
        atr_val = tctx["atr"]

        sim = _simulate_trade(
            direction, entry_price, atr_val, highs, lows, closes,
            entry_i, hold_days, atr_stop_mult, atr_target_mult, commission_pct,
        )
        if sim is None:
            filtered["нет ATR/окна"] += 1
            if not dry_run:
                await asyncio.sleep(0.3)
            continue

        ret = sim["ret_pct"]
        correct = ret > 0
        capital *= (1 + ret / 100)
        equity.append(round(capital, 4))

        trade = {
            "date":        date,
            "direction":   direction,
            "confidence":  confidence,
            "reason":      decision.get("reason", ""),
            "entry":       round(entry_price, 2),
            "exit":        sim["exit"],
            "stop":        sim["stop"],
            "target":      sim["target"],
            "return_pct":  ret,
            "r_multiple":  sim["r_multiple"],
            "exit_reason": sim["exit_reason"],
            "bars_held":   sim["bars_held"],
            "correct":     correct,
            "regime":      tctx["regime"],
            "rsi":         tctx["rsi"],
            "tech_signal": tctx["tech_signal"],
            "agreement":   agreement,
            "sentiment_index": sent_idx,
        }
        if not correct:
            trade["error_category"] = _classify_error(trade)
        trades.append(trade)

        if not dry_run:
            await asyncio.sleep(0.3)

    if not trades:
        return {
            "error":      "Ни одного торгового сигнала не прошло фильтры "
                          "(нейтрально / низкая уверенность / нет подтверждения / против тренда)",
            "decisions":  decisions,
            "filtered":   filtered,
            "calls_made": calls_done,
            "mode":       "dry_run" if dry_run else "real_claude",
        }

    # ── Метрики (честные, ничего не подгоняется) ──
    total     = len(trades)
    winners   = [t for t in trades if t["correct"]]
    losers    = [t for t in trades if not t["correct"]]
    win_rate  = round(len(winners) / total * 100, 1)
    avg_win   = round(sum(t["return_pct"] for t in winners) / len(winners), 2) if winners else 0
    avg_loss  = round(sum(t["return_pct"] for t in losers)  / len(losers),  2) if losers  else 0
    avg_win_r = round(sum(t["r_multiple"] for t in winners) / len(winners), 2) if winners else 0
    avg_loss_r = round(sum(t["r_multiple"] for t in losers) / len(losers),  2) if losers  else 0
    expectancy_r = round(sum(t["r_multiple"] for t in trades) / total, 3)
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
    sharpe = round(mean_r / std_r * math.sqrt(252 / max(hold_days, 1)), 2) if std_r > 0 else 0

    gross_win  = sum(t["return_pct"] for t in winners)
    gross_loss = sum(t["return_pct"] for t in losers)
    pf = round(gross_win / -gross_loss, 2) if gross_loss < 0 else None

    high = [t for t in trades if t["confidence"] >= 60]
    hc_wr = round(sum(1 for t in high if t["correct"]) / len(high) * 100, 1) if high else None

    # ── Разбор ошибок (прозрачность) ──
    error_categories: dict = {}
    for t in losers:
        cat = t.get("error_category", "рыночный шум")
        error_categories[cat] = error_categories.get(cat, 0) + 1
    error_breakdown = sorted(
        [{"category": k, "count": v} for k, v in error_categories.items()],
        key=lambda x: -x["count"],
    )

    worst_trades = sorted(trades, key=lambda t: t["return_pct"])[:5]

    error_analysis = {
        "by_direction": _breakdown(trades, lambda t: "лонг" if t["direction"] == "up" else "шорт"),
        "by_regime":    _breakdown(trades, lambda t: t.get("regime", "нет данных")),
        "by_exit":      _breakdown(trades, lambda t: t.get("exit_reason", "?")),
        "error_categories": error_breakdown,
        "calibration":  _confidence_calibration(trades),
        "worst_trades": [{
            "date":        t["date"],
            "direction":   t["direction"],
            "confidence":  t["confidence"],
            "return_pct":  t["return_pct"],
            "r_multiple":  t["r_multiple"],
            "regime":      t.get("regime"),
            "rsi":         t.get("rsi"),
            "exit_reason": t.get("exit_reason"),
            "category":    t.get("error_category", "—"),
            "reason":      t.get("reason", ""),
        } for t in worst_trades],
    }

    return {
        "ticker":             ticker,
        "mode":               "dry_run" if dry_run else "real_claude",
        "calls_made":         calls_done,
        "analysis_points":    len(analysis_points),
        "total_trades":       total,
        "win_rate":           win_rate,
        "total_return":       total_ret,
        "buy_hold_return":    bh_ret,
        "alpha":              round(total_ret - bh_ret, 2),
        "avg_win":            avg_win,
        "avg_loss":           avg_loss,
        "avg_win_r":          avg_win_r,
        "avg_loss_r":         avg_loss_r,
        "expectancy_r":       expectancy_r,
        "profit_factor":      pf,
        "max_drawdown":       round(max_dd, 2),
        "sharpe":             sharpe,
        "high_conf_trades":   len(high),
        "high_conf_win_rate": hc_wr,
        "equity_curve":       equity,
        "recent_trades":      trades[-15:],
        "all_decisions":      decisions[-20:],
        "filtered":           filtered,
        "error_analysis":     error_analysis,
        "params": {
            "step_days":            step_days,
            "hold_days":            hold_days,
            "min_confidence":       min_confidence,
            "commission_pct":       commission_pct,
            "atr_stop_mult":        atr_stop_mult,
            "atr_target_mult":      atr_target_mult,
            "require_tech_agreement": require_tech_agreement,
            "block_counter_trend":  block_counter_trend,
        },
    }
