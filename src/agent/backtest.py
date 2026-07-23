"""
MOODEX — Стратегия + бэктест

Чёткая алгоритмическая стратегия с риск-менеджментом, и движок, который
прогоняет её по историческим свечам MOEX и считает РЕАЛЬНЫЕ метрики
(доходность, винрейт, просадка, профит-фактор, матожидание в R).

Правила стратегии (только качественные сетапы, R/R ≥ 2):
  • Боковик (ADX<25): возврат к среднему — покупка у нижней границы при
    перепроданности, шорт у верхней при перекупленности.
  • Тренд (ADX≥25): по тренду — покупка на откате к SMA20 в аптренде,
    шорт на отскоке к SMA20 в даунтренде.
  • Стоп по ATR/структуре, цель по структуре/проекции, риск 1% капитала,
    без плеча, максимум 20 баров в позиции.

⚠️ Историческая доходность не гарантирует будущую. Не инвестрекомендация.
"""
import logging
from typing import Optional

from src.analysis.technical import sma, rsi, atr, adx

logger = logging.getLogger(__name__)

MIN_RR = 2.0
MAX_HOLD = 20
RISK_FRAC = 0.01
LOOKBACK = 20


def _mk(direction, entry, stop, target, regime, min_rr=MIN_RR) -> Optional[dict]:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        return None
    rr = reward / risk
    if rr < min_rr:
        return None
    return {"direction": direction, "entry": entry, "stop": stop,
            "target": target, "rr": round(rr, 2), "regime": regime}


def signal_at(closes, highs, lows, i, lookback=LOOKBACK, params=None, sentiment=None) -> Optional[dict]:
    """
    Сигнал на баре i, используя ТОЛЬКО данные до i включительно (без загляд. вперёд).
    params: {mode, min_rr, trend_filter, use_sentiment}
    sentiment: настроение толпы на дату бара [-1..1] (или None) — исторический фильтр.
    """
    p = params or {}
    mode = p.get("mode", "both")
    min_rr = p.get("min_rr", MIN_RR)
    trend_filter = p.get("trend_filter", False)
    use_sentiment = p.get("use_sentiment", False)

    if i < 55:
        return None
    c, h, l = closes[:i + 1], highs[:i + 1], lows[:i + 1]
    price = c[-1]
    s20, s50, r, a = sma(c, 20), sma(c, 50), rsi(c, 14), atr(h, l, c, 14)
    if not (s20 and s50 and r and a):
        return None
    support, resistance = min(l[-lookback:]), max(h[-lookback:])
    rng = resistance - support
    if rng <= 0:
        return None
    pos = (price - support) / rng
    sep = abs(s20 - s50) / price if price else 0
    adx_v = adx(h, l, c, 14)
    strong_trend = (adx_v is not None and adx_v >= 25) and sep >= 0.015

    long_ok = (not trend_filter) or price > s50
    short_ok = (not trend_filter) or price < s50

    # Фильтр настроения (конфлюенс): сделка только при ПОДТВЕРЖДЕНИИ толпой.
    # Нет данных о настроении за этот день → пропускаем (нечем подтвердить).
    if use_sentiment:
        if sentiment is None:
            return None
        if sentiment <= 0.05:
            long_ok = False       # толпа не подтверждает лонг
        if sentiment >= -0.05:
            short_ok = False      # толпа не подтверждает шорт

    if not strong_trend:  # боковик
        if mode == "trend":
            return None
        if pos <= 0.30 and r <= 45 and long_ok:
            return _mk("long", price, support - 0.5 * a, resistance - 0.15 * rng, "range", min_rr)
        if pos >= 0.70 and r >= 55 and short_ok:
            return _mk("short", price, resistance + 0.5 * a, support + 0.15 * rng, "range", min_rr)
        return None
    else:  # тренд
        if mode == "range":
            return None
        up = s20 >= s50
        if up and price <= s20 * 1.02 and price > s50 and long_ok:
            return _mk("long", price, price - 1.5 * a, price + 3 * a, "trend", min_rr)
        if (not up) and price >= s20 * 0.98 and price < s50 and short_ok:
            return _mk("short", price, price + 1.5 * a, price - 3 * a, "trend", min_rr)
        return None


def backtest(closes, highs, lows, dates=None,
             risk_frac=RISK_FRAC, max_hold=MAX_HOLD, start_equity=100_000.0,
             cost_pct=0.1, params=None, sentiment_map=None) -> dict:
    """
    Прогнать стратегию по свечам. sentiment_map — {дата 'YYYY-MM-DD': настроение[-1..1]}
    для исторического фильтра по настроению (если params.use_sentiment).
    """
    """
    Прогнать стратегию по свечам. Возвращает метрики, кривую капитала и сделки.
    cost_pct — издержки за круг (комиссия+проскальзывание), % от оборота.
    """
    if not closes or len(closes) < 60:
        return {"error": "мало данных для бэктеста", "trades": [], "equity_curve": []}
    dates = dates or [""] * len(closes)
    n_bars = len(closes)
    split_bar = int(n_bars * 0.6)   # граница in-sample / out-of-sample
    side_cost = (cost_pct / 100.0) / 2.0

    equity = start_equity
    peak = equity
    max_dd = 0.0
    pos = None
    trades: list[dict] = []
    curve: list[dict] = []

    def close_pos(exitp, reason, i):
        nonlocal equity, pos
        if pos["direction"] == "long":
            pnl = pos["shares"] * (exitp - pos["entry"])
        else:
            pnl = pos["shares"] * (pos["entry"] - exitp)
        # издержки: комиссия+проскальзывание с обеих сторон
        pnl -= pos["shares"] * (pos["entry"] + exitp) * side_cost
        equity += pnl
        r_mult = pnl / pos["risk_amount"] if pos["risk_amount"] else 0.0
        trades.append({
            "entry_bar": pos["entry_bar"], "date_in": dates[pos["entry_bar"]][:10],
            "date_out": dates[i][:10], "direction": pos["direction"], "regime": pos["regime"],
            "entry": round(pos["entry"], 2), "exit": round(exitp, 2),
            "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2), "reason": reason,
            "sample": "in" if pos["entry_bar"] < split_bar else "out",
        })
        pos = None

    for i in range(len(closes)):
        # 1) сопровождение открытой позиции по диапазону бара i
        if pos:
            if pos["direction"] == "long":
                if lows[i] <= pos["stop"]:
                    close_pos(pos["stop"], "stop", i)
                elif highs[i] >= pos["target"]:
                    close_pos(pos["target"], "target", i)
            else:
                if highs[i] >= pos["stop"]:
                    close_pos(pos["stop"], "stop", i)
                elif lows[i] <= pos["target"]:
                    close_pos(pos["target"], "target", i)
            if pos and (i - pos["entry_bar"]) >= max_hold:
                close_pos(closes[i], "time", i)

        # 2) кривая капитала + просадка
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        curve.append({"date": dates[i][:10], "equity": round(equity, 2)})

        # 3) вход по сигналу (вход по цене закрытия бара i)
        if not pos:
            sent = sentiment_map.get(dates[i][:10]) if sentiment_map else None
            sig = signal_at(closes, highs, lows, i, params=params, sentiment=sent)
            if sig:
                rps = abs(sig["entry"] - sig["stop"])
                if rps <= 0:
                    continue
                shares = (equity * risk_frac) / rps
                shares = min(shares, equity / sig["entry"])  # без плеча
                pos = {**sig, "entry_bar": i, "shares": shares,
                       "risk_amount": shares * rps}

    if pos:
        close_pos(closes[-1], "eod", len(closes) - 1)

    buy_hold = round((closes[-1] / closes[0] - 1) * 100, 2) if closes[0] else None
    return _metrics(trades, curve, equity, start_equity, max_dd,
                    buy_hold=buy_hold, cost_pct=cost_pct)


def _subset(trades: list[dict]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    rs = [t["r_multiple"] for t in trades]
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "expectancy_r": round(sum(rs) / n, 3) if n else None,
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "sum_r": round(sum(rs), 2) if n else 0,
    }


def _metrics(trades, curve, equity, start_equity, max_dd, buy_hold=None, cost_pct=0.1) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    r_mults = [t["r_multiple"] for t in trades]

    in_s = _subset([t for t in trades if t["sample"] == "in"])
    out_s = _subset([t for t in trades if t["sample"] == "out"])
    # Вердикт устойчивости: положительно ли матожидание вне выборки
    if out_s["trades"] < 5 or out_s["expectancy_r"] is None:
        robust = "insufficient"
    elif out_s["expectancy_r"] > 0:
        robust = "robust"
    else:
        robust = "overfit_risk"

    return {
        "trades_count": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "total_return_pct": round((equity / start_equity - 1) * 100, 2),
        "buy_hold_pct": buy_hold,
        "cost_pct": cost_pct,
        "max_drawdown_pct": round(max_dd, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy_r": round(sum(r_mults) / n, 3) if n else None,
        "avg_win_r": round(sum(t["r_multiple"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss_r": round(sum(t["r_multiple"] for t in losses) / len(losses), 2) if losses else None,
        "in_sample": in_s,
        "out_sample": out_s,
        "robustness": robust,
        "final_equity": round(equity, 2),
        "start_equity": start_equity,
        "equity_curve": curve,
        "trades": trades[-60:],
    }


# ─── Лаборатория: сравнение вариантов стратегии по портфелю ────────────────────

VARIANTS = {
    "base": {"label": "Базовая (тренд+боковик)", "params": {"mode": "both"}},
    "trend_filter": {"label": "+ Фильтр старшего тренда (SMA50)", "params": {"mode": "both", "trend_filter": True}},
    "trend_only": {"label": "Только тренд", "params": {"mode": "trend"}},
    "range_only": {"label": "Только боковик (возврат к среднему)", "params": {"mode": "range"}},
    "rr3": {"label": "Только сделки с R/R ≥ 3", "params": {"mode": "both", "min_rr": 3.0}},
}


def evaluate_variant(portfolio: list[dict], params: dict) -> dict:
    """
    Прогнать вариант стратегии по всем бумагам портфеля и посчитать агрегаты
    ОТДЕЛЬНО in-sample и out-of-sample (взвешенно по числу сделок).
    portfolio: список {ticker, closes, highs, lows, dates}.
    """
    in_subs, out_subs, per_ticker = [], [], []
    for inst in portfolio:
        res = backtest(inst["closes"], inst["highs"], inst["lows"], inst["dates"], params=params)
        if res.get("error"):
            continue
        io = res.get("in_sample", {}) or {}
        oo = res.get("out_sample", {}) or {}
        in_subs.append(io)
        out_subs.append(oo)
        per_ticker.append({
            "ticker": inst["ticker"],
            "out_expectancy_r": oo.get("expectancy_r"),
            "out_trades": oo.get("trades", 0),
            "out_win_rate": oo.get("win_rate"),
        })

    def agg(subs):
        tot_tr = sum(s.get("trades", 0) for s in subs)
        tot_r = sum((s.get("expectancy_r") or 0) * s.get("trades", 0) for s in subs)
        wins = sum((s.get("win_rate") or 0) * s.get("trades", 0) for s in subs)
        return {
            "trades": tot_tr,
            "expectancy_r": round(tot_r / tot_tr, 3) if tot_tr else None,
            "win_rate": round(wins / tot_tr, 1) if tot_tr else None,
        }

    return {
        "in_sample": agg(in_subs),
        "out_sample": agg(out_subs),
        "per_ticker": sorted(per_ticker, key=lambda x: (x["out_expectancy_r"] or -9), reverse=True),
    }
