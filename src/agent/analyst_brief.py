"""
Полная сводка по бумаге для аналитика — всё в одном месте.

ЗАЧЕМ. 30.07 владелец спросил, какие данные я брал при формировании сигналов, и
заметил, что стакана и новостей в рассуждениях не видно. Он был прав: я построил
оценку на цене и средних, а стакан, поток и новости не посмотрел вовсе — при том
что весь день чинил именно эти источники. Причина простая: они лежали в разных
местах, и чтобы собрать картину, нужно было сделать шесть разных запросов и ничего
не забыть.

Здесь один вызов отдаёт ВСЁ, что нужно для решения, и рядом с каждым числом стоит
его происхождение и свежесть. Забыть посмотреть больше нельзя: если данных нет,
это написано прямым текстом, а не выглядит как их отсутствие.

Отдельно возвращается «чего НЕ ХВАТАЕТ» — список пробелов. Молчаливое отсутствие
данных весь день было главным источником неверных выводов.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


async def build(ticker: str) -> dict:
    """Собрать полную сводку по одной бумаге."""
    from src.analysis import technical as ta
    from src.agent import intraday_analyst as ia
    from src import db

    ticker = ticker.upper()
    out: dict = {"ticker": ticker, "at": datetime.now(timezone.utc).isoformat(),
                 "gaps": []}

    # ── дневная техника ──────────────────────────────────────────────────────
    tech = None
    try:
        tech = await ta.analyze_ticker(ticker)
    except Exception as e:                           # noqa: BLE001
        out["gaps"].append(f"дневная техника недоступна: {type(e).__name__}")
    if tech:
        t = tech.to_dict()
        out["daily"] = {
            "price": t.get("price"), "change_1d": t.get("change_1d"),
            "change_7d": t.get("change_7d"), "rsi14": t.get("rsi14"),
            "adx": t.get("adx"), "regime_legacy": t.get("regime"),
            "sma20": t.get("sma20"), "sma50": t.get("sma50"),
            "range_position": t.get("range_position"),
            "volume": {"rel_turnover": t.get("rel_turnover"),
                       "pace_rel": t.get("pace_rel"),
                       "basis": t.get("volume_basis"),
                       "label": t.get("volume_label"),
                       "note": t.get("pace_note")},
            "trade_plan": t.get("trade_plan"),
        }
        # Прежний regime ненадёжен — помечаем прямо здесь, чтобы им не пользовались
        # по инерции: он пометил SMLT боковиком при цене на 23% выше SMA20.
        out["daily"]["regime_legacy_warning"] = (
            "прежний детектор режима ненадёжен: не смотрит на положение цены "
            "относительно средних. Пользуйтесь market_state ниже")
    else:
        out["gaps"].append("нет дневной техники")

    # ── интрадей-контекст: свечи, VWAP, ATR, сетапы, новости, состояние ──────
    ctx = None
    try:
        ctx = await ia.build_intraday_context(
            ticker, reference_price=(tech.price if tech else None))
    except Exception as e:                           # noqa: BLE001
        out["gaps"].append(f"интрадей недоступен: {type(e).__name__}")
    if ctx:
        out["intraday"] = {
            "price": ctx.get("price"), "vwap": ctx.get("vwap"),
            "vwap_rel": ctx.get("vwap_rel"), "atr": ctx.get("atr"),
            "phase": ctx.get("phase"), "levels": ctx.get("levels"),
            "day_structure": ctx.get("day_structure"),
            "volatility_state": ctx.get("volatility_state"),
            "setup": ctx.get("setup"), "plan": ctx.get("plan"),
            "observation": ctx.get("breakout_observation"),
            "blocked": {"orb": ctx.get("orb_blocked"),
                        "breakout": ctx.get("breakout_blocked")},
            "note": ctx.get("note"),
        }
        out["market_state"] = ctx.get("market_state")
        out["data_quality"] = {
            "source": ctx.get("source"), "delayed": ctx.get("delayed"),
            "age_min": ctx.get("age_min"), "stale": ctx.get("stale"),
            "mismatch": ctx.get("mismatch"),
            "price_mismatch_pct": ctx.get("price_mismatch_pct"),
        }
        out["news"] = {"count": ctx.get("news_count"),
                       "lag_to_spike_min": ctx.get("news_lag_min"),
                       "titles": ctx.get("news_titles"),
                       "sources": ctx.get("news_sources")}
        if ctx.get("stale"):
            out["gaps"].append("свечи устарели — сетапы по ним не строятся")
        if ctx.get("mismatch"):
            out["gaps"].append("серия не сходится с дневной ценой")
    else:
        out["gaps"].append("нет интрадей-контекста")

    # ── стакан и поток: РЕАЛЬНЫЙ спред, а не отношение объёмов ──────────────
    # 30.07 я принял bid_ask_ratio 1.7 за «спред 1.70%» при реальных 0.0257% и
    # получил ложный отказ риск-контура. Здесь оба числа названы своими именами.
    try:
        obs = await db.recent_events(ticker=ticker, source="tinkoff",
                                     kind="orderbook", since_minutes=60, limit=3)
        trs = await db.recent_events(ticker=ticker, source="tinkoff",
                                     kind="trades", since_minutes=60, limit=3)
    except Exception as e:                           # noqa: BLE001
        obs = trs = []
        out["gaps"].append(f"стакан и поток недоступны: {type(e).__name__}")

    def _pl(e):
        import json as _j
        p = e.get("payload")
        if isinstance(p, str):
            try:
                return _j.loads(p)
            except Exception:
                return {}
        return p or {}

    def _age(e):
        try:
            ts = datetime.fromisoformat(str(e["ts"]).replace("Z", "+00:00"))
            return round((datetime.now(timezone.utc) - ts).total_seconds() / 60, 1)
        except Exception:
            return None

    if obs:
        o = _pl(obs[0])
        ratios = [_pl(x).get("bid_ask_ratio") for x in obs
                  if _pl(x).get("bid_ask_ratio") is not None]
        out["orderbook"] = {
            "spread_pct": o.get("spread_pct"),
            "bid_ask_VOLUME_ratio": o.get("bid_ask_ratio"),
            "bid_ask_ratio_avg3": (round(sum(ratios) / len(ratios), 2) if ratios else None),
            "pressure": o.get("pressure"),
            "liquidity_score": o.get("liquidity_score"),
            "depth_near_mid": o.get("depth_near_mid"),
            "age_min": _age(obs[0]), "snapshots": len(obs),
            "_note": ("bid_ask_VOLUME_ratio — это отношение ОБЪЁМОВ заявок, "
                      "НЕ спред. Спред отдельным полем spread_pct"),
        }
        if (_age(obs[0]) or 0) > 10:
            out["gaps"].append(f"снимок стакана устарел на {_age(obs[0])} мин")
    else:
        out["orderbook"] = None
        out["gaps"].append("нет снимков стакана")

    if trs:
        t0 = _pl(trs[0])
        buys = [_pl(x).get("buy_pct") for x in trs if _pl(x).get("buy_pct") is not None]
        dels = [_pl(x).get("delta") for x in trs if _pl(x).get("delta") is not None]
        out["flow"] = {
            "order_flow": t0.get("order_flow"), "buy_pct": t0.get("buy_pct"),
            "delta": t0.get("delta"),
            "buy_pct_avg3": (round(sum(buys) / len(buys), 1) if buys else None),
            "delta_avg3": (round(sum(dels) / len(dels)) if dels else None),
            "age_min": _age(trs[0]), "snapshots": len(trs),
            "_note": ("встречный поток при движущейся цене — чаще ПОГЛОЩЕНИЕ, чем "
                      "разворот. 30.07 я дважды прочитал это как разворот и оба "
                      "раза ошибся"),
        }
        if (_age(trs[0]) or 0) > 10:
            out["gaps"].append(f"снимок потока устарел на {_age(trs[0])} мин")
    else:
        out["flow"] = None
        out["gaps"].append("нет данных по потоку сделок")

    out["ready"] = not out["gaps"]
    return out
