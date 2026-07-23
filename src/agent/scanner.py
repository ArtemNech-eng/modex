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
