"""
MOODEX — Триаж («что интересного?»): дешёвый слой перед Claude.

Идея (3 слоя):
  1) СЛОВАРИ — собирают данные и дают дешёвую тональность (уже агрегировано);
  2) МАШИННОЕ ОБУЧЕНИЕ/ИНДИКАТОРЫ — формируют черновой сигнал (техника + интрадей);
  3) CLAUDE — подтверждает и выдаёт точный вердикт.

Этот модуль отвечает за слои 1–2: постоянно (в фоне) считает «интересность»
каждого тикера БЕЗ вызова Claude. Только тикеры выше порога уходят на
подтверждение Claude (см. scanner.scan_interesting). Так экономим токены и, что
важнее для дневной торговли, реагируем быстро и автоматически, а не по кнопке.

`interest_score` — чистая функция (легко тестируется). Не инвестрекомендация.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def interest_score(sentiment: Optional[dict],
                   technical: Optional[dict],
                   intraday: Optional[dict]) -> dict:
    """
    Насколько ситуация «интересна» прямо сейчас (стоит ли будить Claude).
    Возвращает {"interest": 0..1, "direction": long/short/flat, "reasons": [...]}.
    Чистая функция: на вход — уже посчитанные словари/индикаторы/интрадей.
    """
    score = 0.0
    reasons: list[str] = []
    votes: list[str] = []

    # ── Слой интрадей (сильнее всего для дневной торговли) ────────────────────
    if intraday:
        setup = intraday.get("setup")
        if setup == "news_resolution":
            score += 0.5
            reasons.append("интрадей: разрешение новостного выноса")
            votes.append(intraday.get("signal"))
        elif setup == "orb":
            score += 0.35
            reasons.append("интрадей: пробой диапазона открытия")
            votes.append(intraday.get("signal"))
        ev = (intraday.get("event") or {})
        if ev.get("event"):
            score += 0.2
            reasons.append("интрадей: новостное событие/спайк")
        if intraday.get("volatility_state") == "expansion":
            score += 0.1
            reasons.append("расширение волатильности")
        # «наблюдение» = делать сейчас нечего → интерес ниже
        if intraday.get("observe"):
            score *= 0.4

    # ── Слой индикаторов/ML ───────────────────────────────────────────────────
    if technical:
        tp = technical.get("trade_plan") or {}
        if tp.get("entry_status") == "enter":
            score += 0.25
            reasons.append("техника: точка входа активна")
        rr = tp.get("risk_reward") or 0
        if rr >= 2:
            score += 0.1
            reasons.append(f"хороший R/R {rr}")
        tscore = technical.get("score") or 0
        if abs(tscore) >= 0.5:
            score += 0.1
            votes.append("long" if tscore > 0 else "short")
        rp = technical.get("range_position")
        if rp is not None and (rp <= 0.15 or rp >= 0.85):
            score += 0.1
            reasons.append("цена у границы диапазона")

    # ── Слой словарей (настроение толпы) ──────────────────────────────────────
    if sentiment:
        if sentiment.get("is_anomaly"):
            score += 0.2
            reasons.append("аномалия настроения")
        vz = sentiment.get("volume_zscore")
        if vz is not None and vz >= 2:
            score += 0.15
            reasons.append("всплеск объёма сообщений")
        si = sentiment.get("sentiment_index")
        if si is not None and (si >= 70 or si <= 30):
            score += 0.1
            votes.append("long" if si >= 70 else "short")

    ups = votes.count("long")
    downs = votes.count("short")
    direction = "long" if ups > downs else "short" if downs > ups else "flat"
    score = round(max(0.0, min(1.0, score)), 3)
    return {"interest": score, "direction": direction, "reasons": reasons}


# ─── I/O: посчитать интерес по тикеру без Claude ──────────────────────────────

async def screen_ticker(ticker: str, aggregator) -> Optional[dict]:
    """
    Дешёвый скрин одного тикера БЕЗ Claude: словари (агрегатор) + индикаторы
    (technical) + интрадей-детекторы. Возвращает интерес и черновое направление.
    """
    from src.analysis import technical as ta
    try:
        from config.settings import (INTRADAY_MODE, INTRADAY_TF_MIN,
                                      INTRADAY_OPENING_RANGE_BARS)
    except Exception:
        INTRADAY_MODE, INTRADAY_TF_MIN, INTRADAY_OPENING_RANGE_BARS = True, 5, 6

    ticker = ticker.upper()

    idx = aggregator.get_ticker_index(ticker)
    sentiment = idx.to_dict() if idx else None

    try:
        tech = await ta.analyze_ticker(ticker)
    except Exception:
        tech = None
    technical = tech.to_dict() if tech else None

    intraday = None
    if INTRADAY_MODE:
        try:
            from src.agent import intraday_analyst as ia
            msg_z = (sentiment or {}).get("volume_zscore")
            intraday = await ia.build_intraday_context(
                ticker, tf_min=INTRADAY_TF_MIN, msg_zscore=msg_z,
                opening_range_bars=INTRADAY_OPENING_RANGE_BARS)
        except Exception:
            intraday = None

    if not (sentiment or technical or intraday):
        return None

    res = interest_score(sentiment, technical, intraday)
    res["ticker"] = ticker
    res["setup"] = (intraday or {}).get("setup")
    return res


async def screen_all(aggregator, tickers: Optional[list[str]] = None) -> list[dict]:
    """Скрин всех тикеров (без Claude), отсортированный по «интересности»."""
    import asyncio
    from config.settings import MOEX_TICKERS
    targets = tickers or list(MOEX_TICKERS.keys())
    out = []
    for t in targets:
        try:
            s = await screen_ticker(t, aggregator)
            if s:
                out.append(s)
        except Exception as e:
            logger.debug(f"screen {t}: {e}")
        await asyncio.sleep(0.15)   # бережём источники данных
    out.sort(key=lambda x: x["interest"], reverse=True)
    return out
