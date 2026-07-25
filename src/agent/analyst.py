"""
MOODEX — AI-агент (аналитик)

Схема работы:
  1. rubert размечает каждое сообщение из Telegram/Пульса → агрегатор строит
     индексы настроения по тикерам (sentiment_index, avg_signal, топ-сообщения)
  2. Технический анализ (MOEX ISS: тренд, RSI, MACD, геополитика)
  3. Claude получает всё это и принимает финальное решение (buy/sell/hold)
     с обоснованием на русском языке
  4. Прогноз сохраняется в БД для последующего бэктеста и обучения

⚠️ Не является инвестиционной рекомендацией.
"""
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.analysis import technical as ta
from src.analysis import geopolitics as geo
from src.analysis.macro import get_macro_context
from src.analysis.fundamentals import get_fundamentals
from src.agent import predictor as pred
from src.agent.claude_agent import ClaudeAgent
from src.agent.context_builder import (
    build_ticker_context, build_price_context,
    build_memory_context, build_news_context, build_multiframe_context,
    build_lessons_context, build_knowledge_context, build_orderbook_context,
    build_levels_context,
)
from src.agent.chart_generator import generate_chart_b64
from src.collector.tinkoff_client import TinkoffClient
from src import db

_tinkoff = TinkoffClient()

logger = logging.getLogger(__name__)

DISCLAIMER = "Не является инвестиционной рекомендацией. Торговля сопряжена с риском."

# Один экземпляр Claude на весь модуль
_claude = ClaudeAgent()


def _recommendation(direction: str, confidence: float) -> str:
    if direction == "up":
        return "Покупать 🟢" if confidence >= 0.5 else "Накапливать 🟢"
    if direction == "down":
        return "Продавать 🔴" if confidence >= 0.5 else "Сокращать 🔴"
    return "Держать / нейтрально ⚪"


async def _load_weights() -> list[float]:
    raw = await db.get_setting(pred.WEIGHTS_KEY)
    return pred.weights_from_json(raw) if raw else pred.DEFAULT_WEIGHTS


async def analyze(ticker: str, aggregator, save: bool = True) -> dict:
    """
    Полный анализ тикера:
      - rubert → агрегатор → индекс настроения + топ-сообщения
      - MOEX → технический анализ
      - Claude → финальное решение и обоснование
    """
    ticker = ticker.upper()

    # ── 1. Настроение (собрано rubert-ом через агрегатор) ─────────────────────
    idx = aggregator.get_ticker_index(ticker)
    if idx:
        sentiment_signal  = idx.avg_signal
        sentiment_block   = idx.to_dict()
        # Топ-сообщения для Claude
        points       = list(aggregator._history.get(ticker, []))[-20:]
        top_messages = [p.text_snippet for p in points if p.text_snippet]
    else:
        sentiment_signal  = None
        sentiment_block   = None
        top_messages      = []

    # ── 2. Технический анализ ────────────────────────────────────────────────
    tech           = await ta.analyze_ticker(ticker)
    technical_score = tech.score if tech else None
    technical_block = tech.to_dict() if tech else None

    # ── 3. Геополитический фон ───────────────────────────────────────────────
    geo_snap  = geo.MONITOR.snapshot()
    geo_score = geo_snap["score"]

    # ── 4. Логистическая модель (fallback если Claude недоступен) ────────────
    weights  = await _load_weights()
    fusion   = pred.fuse(sentiment_signal, technical_score, weights)
    combined = max(-1.0, min(1.0, fusion.combined_score + 0.3 * geo_score))

    if combined > 0.15:
        fallback_direction  = "up"
    elif combined < -0.15:
        fallback_direction  = "down"
    else:
        fallback_direction  = "flat"
    fallback_confidence = abs(combined)

    # ── 5. Claude принимает финальное решение ────────────────────────────────
    claude_result = None
    direction     = fallback_direction
    confidence    = fallback_confidence
    narrative     = None
    intraday_ctx  = None

    try:
        from config.settings import MOEX_TICKERS
        company = MOEX_TICKERS.get(ticker, ticker)

        # Строим исторический контекст (паттерны настроение → цена)
        hist_ctx = await build_ticker_context(
            ticker=ticker,
            current_sentiment=sentiment_block["sentiment_index"] if sentiment_block else None,
        )

        # Строим ценовой дайджест за 2 года
        price_ctx = await build_price_context(ticker)

        # Параллельно: макро + фундаментал + память + мультитаймфрейм + уроки
        import asyncio
        macro_ctx, fund_ctx, memory_ctx, multiframe_ctx, lessons_ctx, knowledge_ctx, levels_ctx = await asyncio.gather(
            get_macro_context(),
            get_fundamentals(ticker),
            build_memory_context(ticker),
            build_multiframe_context(ticker),
            build_lessons_context(ticker),
            build_knowledge_context(ticker),
            build_levels_context(ticker),
            return_exceptions=True,
        )
        macro_ctx      = macro_ctx      if not isinstance(macro_ctx, Exception)      else {}
        fund_ctx       = fund_ctx       if not isinstance(fund_ctx, Exception)       else {}
        memory_ctx     = memory_ctx     if not isinstance(memory_ctx, Exception)     else ""
        multiframe_ctx = multiframe_ctx if not isinstance(multiframe_ctx, Exception) else ""
        lessons_ctx    = lessons_ctx    if not isinstance(lessons_ctx, Exception)    else ""
        knowledge_ctx  = knowledge_ctx  if not isinstance(knowledge_ctx, Exception)  else ""
        levels_ctx     = levels_ctx     if not isinstance(levels_ctx, Exception)     else ""

        # Стакан для Claude как отдельный сигнал: индекс (bid/ask, без потока) +
        # синтез-контекст с трендом/стенами/абсорбцией/ликвидностью из истории БЗ.
        try:
            ob_ctx = await build_orderbook_context(ticker)
        except Exception:
            ob_ctx = ""
        try:
            ob_idx = aggregator.get_orderbook_index(ticker)
        except Exception:
            ob_idx = None
        head = []
        if ob_idx:
            head.append(f"📊 ИНДЕКС СТАКАНА (bid/ask, без потока): "
                        f"{ob_idx['orderbook_index']}/100 — {ob_idx['label']} "
                        f"({ob_idx['snapshot_count']} снимков за час)")
        if ob_ctx:
            head.append(ob_ctx)
        if levels_ctx:
            head.append(levels_ctx)
        if head:
            knowledge_ctx = "\n".join(head + ([knowledge_ctx] if knowledge_ctx else []))

        # Интрадей-контекст (VWAP, диапазон открытия, волатильность, вынос) —
        # именно он ведёт внутридневное решение; дневная техника выше остаётся
        # как контекст старшего таймфрейма.
        intraday_ctx = None
        try:
            from config.settings import (
                INTRADAY_MODE, INTRADAY_TF_MIN, INTRADAY_OPENING_RANGE_BARS)
            if INTRADAY_MODE:
                from src.agent import intraday_analyst as ia
                intraday_ctx = await ia.build_intraday_context(
                    ticker, tf_min=INTRADAY_TF_MIN,
                    msg_zscore=(sentiment_block or {}).get("volume_zscore"),
                    opening_range_bars=INTRADAY_OPENING_RANGE_BARS,
                )
        except Exception as e:
            logger.debug(f"intraday context failed for {ticker}: {e}")

        # Tinkoff: стакан + поток сделок + объём
        tinkoff_snap = await _tinkoff.get_full_snapshot(ticker)

        # «Умные деньги»: реальные сделки отслеживаемых трейдеров Пульса
        smart_money_ctx = None
        try:
            from src.api import main as _api
            sm_snap = await _api._smart_money_snapshot()
            from src.collector.pulse_author_collector import PulseAuthorTracker
            smart_money_ctx = PulseAuthorTracker([]).context_for(ticker, sm_snap)
        except Exception as e:
            logger.debug(f"smart-money context failed for {ticker}: {e}")

        # Визуальный разбор графика (Claude Vision) — это ВТОРОЙ вызов Claude на
        # тикер + дорогой input-image, поэтому по умолчанию ВЫКЛЮЧЕН (экономия).
        # Включается флагом CHART_ANALYSIS_ENABLED. Структурных данных и так вагон.
        chart_analysis = None
        try:
            from config.settings import CHART_ANALYSIS_ENABLED
            if CHART_ANALYSIS_ENABLED:
                candles = await ta.fetch_candles(ticker, days=120)
                chart_b64 = await generate_chart_b64(
                    ticker=ticker,
                    closes=candles.get("close", []),
                    highs=candles.get("high", []),
                    lows=candles.get("low", []),
                    opens=candles.get("open", []),
                    dates=candles.get("dates", []),
                    days=120,
                )
                if chart_b64:
                    chart_analysis = await _claude.analyze_chart(
                        ticker=ticker,
                        image_b64=chart_b64,
                        sentiment_index=sentiment_block["sentiment_index"] if sentiment_block else None,
                        extra_context=price_ctx,
                    )
                    logger.info(f"📊 Claude chart → {ticker}: {chart_analysis.get('chart_signal')}")
        except Exception as e:
            logger.warning(f"Chart analysis failed for {ticker}: {e}")

        # Разбор графика (Claude Vision) — ВХОД для Claude, а не отдельный голос:
        chart_ctx = None
        if chart_analysis and chart_analysis.get("chart_signal"):
            chart_ctx = (f"📈 РАЗБОР ГРАФИКА (Vision): {chart_analysis.get('chart_signal')} "
                         f"(уверенность {chart_analysis.get('chart_confidence')}%)")

        claude_result = await _claude.synthesize_ticker(
            ticker=ticker,
            company=company,
            sentiment_index=sentiment_block["sentiment_index"] if sentiment_block else 50.0,
            message_count=sentiment_block["message_count"] if sentiment_block else 0,
            positive_pct=sentiment_block.get("positive_pct", 0) if sentiment_block else 0,
            negative_pct=sentiment_block.get("negative_pct", 0) if sentiment_block else 0,
            top_messages=top_messages,
            price_change_1d=technical_block.get("price_change_1d") if technical_block else None,
            rsi=technical_block.get("rsi") if technical_block else None,
            trend=technical_block.get("regime") if technical_block else None,
            historical_context=hist_ctx.get("summary") if hist_ctx["patterns"] else None,
            price_context=price_ctx,
            tinkoff_context=tinkoff_snap.get("summary") if tinkoff_snap else None,
            macro_context=macro_ctx.get("summary") if macro_ctx else None,
            fundamental_context=fund_ctx.get("summary") if fund_ctx else None,
            memory_context=memory_ctx or None,
            multiframe_context=multiframe_ctx or None,
            smart_money_context=smart_money_ctx,
            lessons_context=lessons_ctx or None,
            intraday_context=(intraday_ctx or {}).get("summary") if intraday_ctx else None,
            knowledge_context=knowledge_ctx or None,
            chart_context=chart_ctx,
            momentum=sentiment_block.get("momentum") if sentiment_block else None,
            momentum_label=sentiment_block.get("momentum_label") if sentiment_block else None,
            source_diversity=sentiment_block.get("source_diversity") if sentiment_block else None,
            volume_zscore=sentiment_block.get("volume_zscore") if sentiment_block else None,
            signal_confidence=sentiment_block.get("confidence") if sentiment_block else None,
        )

        # Claude по плейбуку — ЕДИНСТВЕННЫЙ, кто решает направление и уверенность
        # (график он уже увидел как вход через chart_context / Vision, отдельного
        # голосования больше нет).
        signal_map = {"bullish": "up", "bearish": "down", "neutral": "flat"}
        direction  = signal_map.get(claude_result.get("signal", "neutral"), "flat")
        confidence = round((claude_result.get("confidence", 0) or 0) / 100, 3)
        narrative  = claude_result.get("summary", "")
        # Метрики согласованы С РЕШЕНИЕМ CLAUDE (а не из fallback-модели):
        combined   = confidence if direction == "up" else (-confidence if direction == "down" else 0.0)

        logger.info(f"🤖 Claude → {ticker}: {direction} (уверенность {confidence})")

    except Exception as e:
        logger.warning(f"Claude недоступен для {ticker}, используем fallback-модель: {e}")
        claude_result = None

    recommendation = _recommendation(direction, confidence)

    # ── План сделки — ИЗ РЕШЕНИЯ CLAUDE (инвалидация-first) ──────────────────────
    # Вход/стоп/цель берём у Claude, а не у rule-движков. Интрадей/техника уже
    # ушли Claude как ВХОДНЫЕ данные в промпт и направление больше не назначают.
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    claude_trade_plan = None
    if claude_result and direction != "flat":
        c_entry, c_stop = _num(claude_result.get("entry")), _num(claude_result.get("stop"))
        c_target, c_rr = _num(claude_result.get("target")), _num(claude_result.get("rr"))
        if c_entry and c_stop:
            if c_rr is None and c_target is not None and c_entry != c_stop:
                c_rr = round(abs(c_target - c_entry) / abs(c_entry - c_stop), 2)
            claude_trade_plan = {
                "direction": "long" if direction == "up" else "short",
                "entry_low": c_entry, "entry_high": c_entry,
                "price": intraday_ctx.get("price") if intraday_ctx else (tech.price if tech else None),
                "stop_loss": c_stop,
                "take_profit_1": c_target, "take_profit_2": None,
                "risk_reward": c_rr, "current_rr": c_rr,
                "entry_status": "enter",
                "entry_note": claude_result.get("setup") or "",
                "size": claude_result.get("size"),
                "invalidation": claude_result.get("invalidation"),
                "atr": intraday_ctx.get("atr") if intraday_ctx else None,
                "entry_rule": claude_result.get("setup") or "",
                "exit_rule": "Тайм-стоп к закрытию сессии; сопровождение по VWAP.",
            }
            reg = claude_result.get("regime")
            if reg and reg != "unclear":
                recommendation = f"{recommendation} · режим: {reg}"

    # ── Гвард безопасности: ТОЛЬКО жёсткое вето (направление НЕ назначает) ───────
    # Claude решает; гвард лишь запрещает НОВЫЙ вход, когда небезопасно: рынок
    # закрыт / пауза / пре-аукцион или последние минуты сессии (флэт к закрытию).
    try:
        from src.analysis import intraday as _iv
        _msk = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3)))
        _mod = _msk.hour * 60 + _msk.minute
        _phase = _iv.session_phase(_mod)
        _near_close = _iv.is_last_minutes(_mod, buffer_min=15)
    except Exception:
        _phase, _near_close = "main", False
    if direction != "flat" and (_phase in ("closed", "break", "pre") or _near_close):
        _why = ("рынок закрыт / пауза" if _phase in ("closed", "break", "pre")
                else "конец сессии — флэт к закрытию")
        direction, confidence, combined = "flat", 0.0, 0.0
        claude_trade_plan = None
        recommendation = f"⚪ Наблюдать — {_why}"

    # ── 6. Обоснование ──────────────────────────────────────────────────────
    reasons: list[str] = []
    if sentiment_block:
        reasons.append(
            f"Настроение толпы: {sentiment_block['sentiment_index']}/100 "
            f"({sentiment_block['label']}), {sentiment_block['message_count']} сообщений."
        )
    else:
        reasons.append("Настроение: недостаточно сообщений за окно.")

    if tech:
        regime_ru = {"range": "боковик", "uptrend": "восходящий тренд",
                     "downtrend": "нисходящий тренд"}.get(tech.regime, tech.regime)
        reasons.append(f"Режим рынка: {regime_ru} (ADX {tech.adx}).")
        reasons.extend(tech.reasons)
    else:
        reasons.append("Технический анализ: нет данных MOEX.")

    if geo_snap["events_analyzed"]:
        reasons.append(
            f"Геополитический фон: {geo_snap['label']} (score {geo_score})."
        )

    if claude_result:
        if claude_result.get("key_insight"):
            reasons.append(f"Claude: {claude_result['key_insight']}")
        if claude_result.get("risk"):
            reasons.append(f"Риск: {claude_result['risk']}")

    if intraday_ctx:
        reasons.append(
            f"Интрадей: {intraday_ctx.get('vwap_rel') or 'VWAP н/д'}, "
            f"волатильность {intraday_ctx.get('volatility_state')}, "
            f"фаза {intraday_ctx.get('phase')}"
            + (f", сетап {intraday_ctx.get('setup')}" if intraday_ctx.get('setup') not in (None, 'none') else "")
        )
        if intraday_ctx.get("delayed"):
            reasons.append("⚠️ Интрадей-данные MOEX с задержкой ~15 мин (нет реалтайм-фида).")

    result = {
        "ticker": ticker,
        "recommendation": recommendation,
        "direction": direction,
        "confidence": confidence,
        "combined_score": round(combined, 3),
        "prob_up": round((combined + 1) / 2, 3),
        "regime": tech.regime if tech else None,
        "strategy": tech.strategy if tech else None,
        "range_position": tech.range_position if tech else None,
        "sentiment": sentiment_block,
        "technical": technical_block,
        "geopolitics": geo_snap,
        "claude": claude_result,
        "chart_analysis": chart_analysis,
        "tinkoff": {
            "orderbook": tinkoff_snap.get("orderbook"),
            "trades":    tinkoff_snap.get("trades"),
        } if tinkoff_snap else None,
        "intraday": {
            "setup": intraday_ctx.get("setup"),
            "signal": intraday_ctx.get("signal"),
            "plan": intraday_ctx.get("plan"),
            "observe": intraday_ctx.get("observe"),
            "vwap": intraday_ctx.get("vwap"),
            "vwap_rel": intraday_ctx.get("vwap_rel"),
            "atr": intraday_ctx.get("atr"),
            "volatility_state": intraday_ctx.get("volatility_state"),
            "phase": intraday_ctx.get("phase"),
            "delayed": intraday_ctx.get("delayed"),
            "note": intraday_ctx.get("note"),
            "levels": intraday_ctx.get("levels"),
        } if intraday_ctx else None,
        "narrative": narrative,
        "reasons": reasons,
        "model_weights": [round(w, 3) for w in weights],
        "decision_by": "claude" if claude_result else "fallback_model",
        "disclaimer": DISCLAIMER,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # План сделки на карточке — ИЗ РЕШЕНИЯ CLAUDE (если это вход, не «наблюдать»).
    if claude_trade_plan:
        result["trade_plan"] = claude_trade_plan

    # ── 7. Сохраняем прогноз в БД (память агента) ───────────────────────────
    if save:
        try:
            # Снимок ключевых драйверов на момент прогноза — основа для будущего
            # разбора ошибок (post-mortem): по нему станет ясно, на чём стоял сигнал.
            context_snapshot = {
                "direction": direction,
                "confidence": confidence,
                "decision_by": "claude" if claude_result else "fallback_model",
                "sentiment_index": sentiment_block["sentiment_index"] if sentiment_block else None,
                "sentiment_label": sentiment_block.get("label") if sentiment_block else None,
                "sentiment_signal": sentiment_signal,
                "message_count": sentiment_block.get("message_count") if sentiment_block else 0,
                "regime": tech.regime if tech else None,
                "rsi": technical_block.get("rsi") if technical_block else None,
                "technical_score": technical_score,
                "strategy": tech.strategy if tech else None,
                "regime_claude": (claude_result or {}).get("regime") if claude_result else None,
                "confluence_score": (claude_result or {}).get("confluence_score") if claude_result else None,
                "setup": (claude_result or {}).get("setup") if claude_result else None,
                "geo_score": geo_score,
                "smart_money": smart_money_ctx,
                "chart_signal": (chart_analysis or {}).get("chart_signal") if chart_analysis else None,
                "key_insight": (claude_result or {}).get("key_insight") if claude_result else None,
                "risk": (claude_result or {}).get("risk") if claude_result else None,
                "narrative": narrative,
                "intraday_setup": intraday_ctx.get("setup") if intraday_ctx else None,
                "intraday_vwap_rel": intraday_ctx.get("vwap_rel") if intraday_ctx else None,
                "intraday_volatility": intraday_ctx.get("volatility_state") if intraday_ctx else None,
                "intraday_phase": intraday_ctx.get("phase") if intraday_ctx else None,
                "intraday_delayed": intraday_ctx.get("delayed") if intraday_ctx else None,
            }
            # В интрадей-режиме горизонт прогноза короткий (часы), а не сутки —
            # это меняет и момент оценки результата (внутри сессии).
            try:
                from config.settings import INTRADAY_MODE, INTRADAY_HORIZON_HOURS
                default_h = INTRADAY_HORIZON_HOURS if INTRADAY_MODE else 24
            except Exception:
                default_h = 24
            horizon_h = int(os.getenv("PREDICTION_HORIZON_HOURS", str(default_h)))
            pred_id = await db.add_prediction({
                "ticker": ticker,
                "horizon_hours": horizon_h,
                "sentiment_index": sentiment_block["sentiment_index"] if sentiment_block else None,
                "sentiment_signal": sentiment_signal,
                "technical_score": technical_score,
                "combined_score": combined,
                "confidence": confidence,
                "direction": direction,
                "price_at": tech.price if tech else None,
                "regime": (claude_result or {}).get("regime") if claude_result else None,
                "confluence_score": (claude_result or {}).get("confluence_score") if claude_result else None,
                "entry": _num((claude_result or {}).get("entry")) if claude_result else None,
                "stop": _num((claude_result or {}).get("stop")) if claude_result else None,
                "target": _num((claude_result or {}).get("target")) if claude_result else None,
                "rr_planned": _num((claude_result or {}).get("rr")) if claude_result else None,
                "context": context_snapshot,
            })
            result["prediction_id"] = pred_id
        except Exception as e:
            logger.warning(f"Не удалось сохранить прогноз {ticker}: {e}")

    return result


# ─── Обучение на результатах ──────────────────────────────────────────────────

async def evaluate_due_predictions() -> dict:
    """Оценить прогнозы с истёкшим горизонтом по фактической цене MOEX."""
    due = await db.get_due_predictions()
    evaluated = 0
    for p in due:
        if not p.price_at:
            continue
        realized_price = None
        # Интрадей-прогнозы (короткий горизонт) оцениваем по интрадей-цене на
        # момент created_at + horizon; иначе/при отсутствии данных — дневной close.
        try:
            from config.settings import INTRADAY_MODE
            if INTRADAY_MODE and p.horizon_hours and p.horizon_hours < 24 and p.created_at:
                from src.agent import intraday_analyst as ia
                realized_price = await ia.realized_price_after(
                    p.ticker, p.created_at.isoformat(), float(p.horizon_hours))
        except Exception:
            realized_price = None
        if realized_price is None:
            try:
                closes = await ta.fetch_closes(p.ticker, days=10)
            except Exception:
                continue
            if not closes:
                continue
            realized_price = closes[-1]
        realized_return = (realized_price / p.price_at - 1) * 100
        actual_up = realized_return > 0
        if p.direction == "up":
            correct = actual_up
        elif p.direction == "down":
            correct = not actual_up
        else:
            correct = abs(realized_return) < 1.0

        await db.evaluate_prediction(p.id, realized_price, realized_return, correct)
        evaluated += 1

    # Разбор ошибок по свежеоценённым (и любым неразобранным) прогнозам
    analyzed = await generate_post_mortems()

    retrained = await retrain()
    return {"evaluated": evaluated, "post_mortems": analyzed, "retrained": retrained}


async def generate_post_mortems(limit: int = 25) -> int:
    """
    Для оценённых, но ещё не разобранных прогнозов сформулировать причину
    успеха/провала и урок, и сохранить их в БД. Возвращает число разборов.

    Это замыкает цикл обучения: выводы копятся в журнале и через
    build_lessons_context() попадают в промпт будущих прогнозов.
    """
    import json
    pending = await db.get_predictions_for_post_mortem(limit=limit)
    done = 0
    for p in pending:
        context = {}
        raw = p.get("context_json")
        if raw:
            try:
                context = json.loads(raw)
            except Exception:
                context = {}
        try:
            pm = await _claude.post_mortem(
                ticker=p["ticker"],
                direction=p.get("direction", "flat"),
                confidence=p.get("confidence", 0.0),
                context=context,
                realized_return=p.get("realized_return"),
                correct=p.get("correct"),
                horizon_hours=p.get("horizon_hours", 24),
            )
            await db.set_post_mortem(
                p["id"], pm.get("cause", ""), pm.get("lesson", ""), pm.get("tags", []))
            done += 1
        except Exception as e:
            logger.warning(f"Не удалось разобрать прогноз id={p.get('id')}: {e}")
    if done:
        logger.info(f"🔎 Разобрано закрытых сигналов (post-mortem): {done}")
    return done


async def retrain() -> bool:
    """Переобучить веса fallback-модели на оценённых прогнозах."""
    rows    = await db.get_evaluated_predictions()
    samples = []
    for r in rows:
        if r.get("realized_return") is None:
            continue
        samples.append({
            "sentiment_signal": r.get("sentiment_signal"),
            "technical_score":  r.get("technical_score"),
            "label": 1 if r["realized_return"] > 0 else 0,
        })

    if len(samples) < 10:
        return False

    new_weights = pred.train_weights(samples)
    await db.set_setting(pred.WEIGHTS_KEY, pred.weights_to_json(new_weights))
    logger.info(f"🧠 Fallback-модель переобучена на {len(samples)} примерах: {new_weights}")
    return True
