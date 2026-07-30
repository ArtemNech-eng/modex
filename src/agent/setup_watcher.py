"""
Быстрый наблюдатель сетапов — БЕЗ Claude, то есть бесплатно.

ЗАЧЕМ. Сетап пробоя консолидации по Мечелу 30.07 срабатывал в 13:25 (вход 38.35).
Сканер с подтверждением Claude ходит раз в 45 минут, поэтому этот сигнал был бы
увиден в 14:10, когда цена уже 39.16. Фактический вход состоялся в 14:34 по 39.33 и
дал 4 088₽ на 4048 акциях, тогда как вход 13:25 при том же выходе дал бы 8 056₽ —
ровно вдвое. Опоздание сканера стоило половины прибыли.

ПОЧЕМУ ИНТЕРВАЛ 5 МИНУТ, А НЕ ЧАЩЕ. Сетапы считаются по ЗАКРЫТИЮ пятиминутного
бара. Опрашивать чаще, чем раз в бар, бессмысленно: данные не изменятся, а лимиты
Tinkoff израсходуются. Ровно один проход на бар — это и максимальная скорость,
которая имеет смысл, и минимальная нагрузка.

ЧЕГО ЗДЕСЬ НЕТ. Никакого обращения к Claude: наблюдатель только ловит момент и
записывает его. Подтверждение и разбор — отдельный, платный контур. Смысл
разделения в том, что ловить момент нужно быстро и дёшево, а рассуждать можно
медленно и дорого.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Сетапы, которые наблюдатель считает поводом для сигнала. Диапазон открытия сюда
# НЕ входит: он живёт первые 90 минут и уже покрыт основным сканером, а его
# просроченные срабатывания 30.07 дали 36 сетапов, из которых 35 имели R/R < 1.
WATCHED_SETUPS = ("consolidation_breakout", "news_resolution")

_status: dict = {
    "enabled": False, "running": False, "interval_min": 5,
    "last_pass": None, "next_pass": None, "passes": 0,
    "checked": 0, "fired": 0, "errors": 0, "last_error": None,
}
_recent: list = []          # последние срабатывания (для выдачи в API)
_seen: dict = {}            # (тикер, сетап) -> время последнего срабатывания


def status() -> dict:
    out = dict(_status)
    out["recent"] = list(reversed(_recent[-25:]))
    return out


def _cfg():
    try:
        from config.settings import (SETUP_WATCH_INTERVAL_MIN, SETUP_WATCH_DEDUP_MIN,
                                     SETUP_WATCH_PACING_SEC, MOEX_TICKERS)
        return (SETUP_WATCH_INTERVAL_MIN, SETUP_WATCH_DEDUP_MIN,
                SETUP_WATCH_PACING_SEC, list(MOEX_TICKERS.keys()))
    except Exception:                                # noqa: BLE001
        return 5, 30, 0.2, []


def _is_duplicate(ticker: str, setup: str, dedup_min: int) -> bool:
    """Один и тот же сетап по одной бумаге не повторяем в течение окна.

    Без этого пробой, который держится несколько баров, дал бы сигнал на каждом
    проходе, и журнал заполнился бы копиями одного события.
    """
    key = (ticker, setup)
    last = _seen.get(key)
    now = datetime.now(timezone.utc)
    if last and (now - last) < timedelta(minutes=dedup_min):
        return True
    _seen[key] = now
    return False


async def check_ticker(ticker: str) -> Optional[dict]:
    """Проверить одну бумагу. Возвращает срабатывание или None."""
    from src.agent import intraday_analyst as ia
    from src.analysis import technical as ta
    try:
        from config.settings import INTRADAY_TF_MIN, INTRADAY_OPENING_RANGE_BARS
    except Exception:                                # noqa: BLE001
        INTRADAY_TF_MIN, INTRADAY_OPENING_RANGE_BARS = 5, 6

    ref = None
    try:
        tech = await ta.analyze_ticker(ticker)
        ref = tech.price if tech else None
    except Exception:                                # noqa: BLE001
        ref = None

    ctx = await ia.build_intraday_context(
        ticker, tf_min=INTRADAY_TF_MIN,
        opening_range_bars=INTRADAY_OPENING_RANGE_BARS, reference_price=ref)
    if not ctx or ctx.get("setup") not in WATCHED_SETUPS:
        return None
    if ctx.get("stale") or ctx.get("mismatch"):
        return None                                  # данные не годятся — не сигналим
    plan = ctx.get("plan") or {}
    if plan.get("signal") not in ("long", "short"):
        return None
    return {
        "ticker": ticker, "setup": ctx["setup"], "signal": plan["signal"],
        "entry": plan.get("entry"), "stop": plan.get("stop_loss"),
        "target": plan.get("take_profit"), "rr": plan.get("risk_reward"),
        "reason": plan.get("reason") or ctx.get("note"),
        "width_atr": plan.get("width_atr"), "vol_x": plan.get("vol_x"),
        "price": ctx.get("price"), "vwap": ctx.get("vwap"),
        "source": ctx.get("source"), "age_min": ctx.get("age_min"),
        "at": datetime.now(timezone.utc).isoformat(),
    }


async def _record(fire: dict) -> None:
    """Положить срабатывание в базу знаний: событие должно быть проверяемым."""
    try:
        from src import db
        await db.add_event({
            "source": "setup_watch", "kind": "setup", "ticker": fire["ticker"],
            "channel": fire["setup"], "text": fire.get("reason"),
            "payload": fire,
        })
    except Exception as e:                           # noqa: BLE001
        logger.debug(f"setup_watch add_event: {e}")


async def one_pass() -> list:
    """Один проход по списку наблюдения. Возвращает список срабатываний."""
    interval, dedup_min, pacing, tickers = _cfg()
    fires = []
    _status["checked"] = 0
    for t in tickers:
        try:
            fire = await check_ticker(t)
            _status["checked"] += 1
            if fire and not _is_duplicate(t, fire["setup"], dedup_min):
                fires.append(fire)
                _recent.append(fire)
                await _record(fire)
                logger.info(f"⚡ сетап {fire['setup']} по {t}: вход {fire['entry']} "
                            f"стоп {fire['stop']} цель {fire['target']} R/R {fire['rr']}")
        except Exception as e:                       # noqa: BLE001
            _status["errors"] += 1
            _status["last_error"] = f"{t}: {type(e).__name__}: {str(e)[:80]}"
        await asyncio.sleep(pacing)
    _status["passes"] += 1
    _status["fired"] += len(fires)
    _status["last_pass"] = datetime.now(timezone.utc).isoformat()
    _status["next_pass"] = (datetime.now(timezone.utc)
                            + timedelta(minutes=interval)).isoformat()
    return fires


async def _loop() -> None:
    interval, *_ = _cfg()
    _status["interval_min"] = interval
    while _status["enabled"]:
        try:
            from src.analysis.intraday import session_phase
            msk = datetime.now(timezone.utc) + timedelta(hours=3)
            phase = session_phase(msk.hour * 60 + msk.minute)
            if phase in ("morning", "main", "evening"):
                await one_pass()
            else:
                _status["last_pass"] = None
                _status["next_pass"] = (datetime.now(timezone.utc)
                                        + timedelta(minutes=interval)).isoformat()
        except Exception as e:                       # noqa: BLE001
            _status["errors"] += 1
            _status["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            logger.warning(f"setup_watch: {e}")
        await asyncio.sleep(max(60, interval * 60))
    _status["running"] = False


def start(interval_min: Optional[int] = None) -> dict:
    if _status["running"]:
        return status()
    if interval_min:
        _status["interval_min"] = int(interval_min)
    _status["enabled"] = True
    _status["running"] = True
    asyncio.create_task(_loop())
    logger.info(f"⚡ наблюдатель сетапов запущен, интервал "
                f"{_status['interval_min']} мин, БЕЗ Claude")
    return status()


def stop() -> dict:
    _status["enabled"] = False
    return status()
