"""
MOODEX — Сканер сигналов

Фоновый обход тикеров: периодически считает полный анализ AI-агента
(настроение + режим + техника + геополитика) и кеширует результат.

Даёт:
- ранжированный список лучших торговых сетапов (вкладка «Сигналы»)
- быстрый доступ к режиму/позиции для карточек тикеров (без похода в MOEX
  на каждый запрос браузера).
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _quality(a: dict) -> float:
    """
    Качество сетапа [0..~1.2]: уверенность × нормированный R/R,
    с бонусом за чёткий разворот у самой границы боковика.
    """
    if a.get("direction") == "flat" or not a.get("technical"):
        return -1.0
    tp = (a.get("technical") or {}).get("trade_plan") or {}
    rr = tp.get("risk_reward") or 0.0
    conf = a.get("confidence") or 0.0
    q = conf * min(rr, 3.0) / 3.0
    if a.get("strategy") == "range_reversal":
        rp = a.get("range_position")
        if rp is not None and (rp <= 0.2 or rp >= 0.8):
            q += 0.15
    # Готовность входа прямо сейчас — сильный плюс
    tp = (a.get("technical") or {}).get("trade_plan") or {}
    if tp.get("entry_status") == "enter":
        q += 0.2
    return q


def _signal_row(a: dict) -> dict:
    tech = a.get("technical") or {}
    tp = tech.get("trade_plan") or {}
    return {
        "ticker": a["ticker"],
        "recommendation": a["recommendation"],
        "direction": a["direction"],
        "regime": a.get("regime"),
        "strategy": a.get("strategy"),
        "range_position": a.get("range_position"),
        "confidence": a.get("confidence"),
        "combined_score": a.get("combined_score"),
        "price": tech.get("price"),
        "risk_reward": tp.get("risk_reward"),
        "entry_low": tp.get("entry_low"),
        "entry_high": tp.get("entry_high"),
        "stop_loss": tp.get("stop_loss"),
        "take_profit_1": tp.get("take_profit_1"),
        "take_profit_2": tp.get("take_profit_2"),
        "support": tp.get("support"),
        "resistance": tp.get("resistance"),
        "entry_status": tp.get("entry_status"),
        "entry_note": tp.get("entry_note"),
        "current_rr": tp.get("current_rr"),
        "quality": round(_quality(a), 3),
        "reason": (a.get("reasons") or [""])[0],
    }


class SignalCache:
    def __init__(self):
        self.results: dict[str, dict] = {}
        self.updated_at: Optional[str] = None

    def update(self, ticker: str, analysis: dict):
        self.results[ticker.upper()] = analysis
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def ranked(self, limit: int = 20, min_rr: float = 1.5) -> list[dict]:
        rows = []
        for a in self.results.values():
            q = _quality(a)
            if q <= 0:
                continue
            tp = (a.get("technical") or {}).get("trade_plan") or {}
            if (tp.get("risk_reward") or 0) < min_rr:
                continue
            rows.append(_signal_row(a))
        rows.sort(key=lambda r: r["quality"], reverse=True)
        return rows[:limit]

    def by_ticker(self) -> dict:
        """Компактная карта тикер → {regime, range_position, direction} для карточек."""
        out = {}
        for t, a in self.results.items():
            out[t] = {
                "regime": a.get("regime"),
                "range_position": a.get("range_position"),
                "direction": a.get("direction"),
                "strategy": a.get("strategy"),
            }
        return out


CACHE = SignalCache()


async def scan_all(aggregator, tickers: Optional[list[str]] = None, save: bool = False) -> int:
    """Обойти тикеры, посчитать анализ и обновить кеш. Возвращает число обновлённых."""
    import asyncio
    from src.agent import analyst
    from config.settings import MOEX_TICKERS

    targets = tickers or list(MOEX_TICKERS.keys())
    updated = 0
    for t in targets:
        try:
            a = await analyst.analyze(t, aggregator, save=save)
            CACHE.update(t, a)
            updated += 1
        except Exception as e:
            logger.debug(f"scan {t}: {e}")
        await asyncio.sleep(0.3)  # бережём MOEX ISS
    logger.info(f"🔎 Скан завершён: обновлено {updated}/{len(targets)} тикеров")
    return updated


async def scan_interesting(aggregator, tickers=None, save: bool = True,
                           min_interest: float = 0.5, max_claude: int = 6) -> dict:
    """
    Триаж «словари+ML → Claude»: дёшево скринит рынок и зовёт Claude ТОЛЬКО на
    интересном. Так работает всегда в фоне (а не по кнопке) и не жжёт токены.

    1) screen.screen_all — интерес по каждому тикеру без Claude;
    2) для тех, у кого interest >= min_interest (не более max_claude), —
       полный analyst.analyze (с Claude), результат в кеш сигналов + журнал.
    """
    from src.agent import analyst, screen
    from src import db

    screened = await screen.screen_all(aggregator, tickers=tickers)
    # Пропускаем тикеры с УЖЕ открытым сигналом — не зовём по ним Claude и не плодим
    # дубли. Освободится тикер только когда сигнал отработает (target/stop/session).
    try:
        open_tickers = await db.get_open_signal_tickers()
    except Exception:
        open_tickers = set()
    candidates = [s for s in screened
                  if s["interest"] >= min_interest and s["ticker"] not in open_tickers]
    interesting = candidates[:max_claude]
    skipped_open = sorted({s["ticker"] for s in screened
                           if s["interest"] >= min_interest and s["ticker"] in open_tickers})

    confirmed = 0
    for s in interesting:
        try:
            a = await analyst.analyze(s["ticker"], aggregator, save=save)  # ← Claude подтверждает
            CACHE.update(s["ticker"], a)
            confirmed += 1
        except Exception as e:
            logger.debug(f"claude confirm {s['ticker']}: {e}")

    logger.info(
        f"🔎 Триаж: просканировано {len(screened)}, интересных "
        f"{len([s for s in screened if s['interest'] >= min_interest])}, "
        f"пропущено (открыт сигнал) {len(skipped_open)}, подтверждено Claude {confirmed}"
    )
    return {
        "screened": len(screened),
        "interesting": [{"ticker": s["ticker"], "interest": s["interest"],
                         "direction": s["direction"], "reasons": s["reasons"]}
                        for s in candidates],
        "skipped_open": skipped_open,
        "claude_confirmed": confirmed,
        "top": screened[:10],
    }


async def scan_batch(aggregator, tickers=None, save: bool = True, max_deep: int = 3) -> dict:
    """
    Batch-скрин Claude: ОДИН вызов по ВСЕМ тикерам («общая картина») → шортлист
    реальных сетапов → глубокий synthesize только по лучшим (max_deep). Дёшево
    (~5₽ на batch) и НИЧЕГО не теряется в триаже — Claude видит все бумаги.
    """
    from src.agent import analyst, screen
    from src.agent.claude_agent import ClaudeAgent, budget_state, can_afford_deep
    from src import db
    try:
        from config.settings import BATCH_SCAN_MAX_TOKENS, BATCH_SCAN_MAX_TICKERS
    except Exception:
        BATCH_SCAN_MAX_TOKENS, BATCH_SCAN_MAX_TICKERS = 700, 30

    screened = await screen.screen_all(aggregator, tickers=tickers)
    # В batch отправляем ТОП по интересу (дешевле input, не теряем сетапы).
    briefs = [s["brief"] for s in screened if s.get("brief")][:BATCH_SCAN_MAX_TICKERS]
    bud = await budget_state()
    if not briefs:
        return {"screened": len(screened), "batch_watch": 0, "claude_confirmed": 0,
                "shortlist": [], "interesting": [], "budget": bud}
    if bud["left"] <= 0:
        logger.warning(f"💸 Дневной бюджет Claude исчерпан ({bud['rub']:.1f}/{bud['budget']:.0f}₽) — пропускаем")
        return {"screened": len(screened), "batch_watch": 0, "claude_confirmed": 0,
                "shortlist": [], "interesting": [], "budget": bud,
                "skipped": "бюджет исчерпан"}

    # Общий фон — один раз в контекст batch (гео — сильный рыночный драйвер).
    market_ctx = ""
    try:
        from src.analysis import geopolitics as geo
        gs = geo.MONITOR.snapshot()
        market_ctx = f"Общий фон: геополитика {gs.get('label')} (score {gs.get('score')})."
    except Exception:
        pass

    verdicts = await ClaudeAgent().batch_scan(market_ctx, briefs,
                                              max_tokens=BATCH_SCAN_MAX_TOKENS)
    watch = [v for v in verdicts if v.get("watch")]

    try:
        open_tickers = await db.get_open_signal_tickers()
    except Exception:
        open_tickers = set()

    def _pri(v):   # моментум/тренд — вперёд
        reg = (v.get("regime") or "").lower()
        return 0 if "momentum" in reg else 1 if reg == "trend" else 2

    shortlist = []
    for v in sorted(watch, key=_pri):
        t = (v.get("ticker") or "").upper()
        if t and t not in open_tickers and t not in shortlist:
            shortlist.append(t)
        if len(shortlist) >= max_deep:
            break

    confirmed = 0
    deep_skipped = None
    for t in shortlist:
        if not await can_afford_deep():   # остатка мало → только дешёвый batch
            deep_skipped = "мало бюджета на глубокий разбор"
            logger.warning(f"💸 {t}: {deep_skipped} — пропуск")
            break
        try:
            a = await analyst.analyze(t, aggregator, save=save)   # ← глубокий разбор + сохранение
            CACHE.update(t, a)
            confirmed += 1
        except Exception as e:
            logger.debug(f"deep {t}: {e}")

    bud = await budget_state()
    logger.info(f"🔎 Batch-скрин: {len(screened)} тикеров → watch {len(watch)} → "
                f"глубоко {confirmed} {shortlist} · бюджет {bud['rub']:.1f}/{bud['budget']:.0f}₽")
    return {
        "screened": len(screened),
        "batch_watch": len(watch),
        "shortlist": shortlist,
        "claude_confirmed": confirmed,
        "budget": bud,
        "skipped": deep_skipped,
        "interesting": [{"ticker": v.get("ticker"), "bias": v.get("bias"),
                         "regime": v.get("regime"), "reason": v.get("reason")}
                        for v in watch][:25],
    }
