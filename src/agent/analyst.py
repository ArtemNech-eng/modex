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
from datetime import datetime, timezone
from typing import Optional

from src.analysis import technical as ta
from src.analysis import geopolitics as geo
from src.agent import predictor as pred
from src.agent.claude_agent import ClaudeAgent
from src.agent.context_builder import build_ticker_context
from src import db

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

    try:
        from config.settings import MOEX_TICKERS
        company = MOEX_TICKERS.get(ticker, ticker)

        # Строим исторический контекст (паттерны настроение → цена)
        hist_ctx = await build_ticker_context(
            ticker=ticker,
            current_sentiment=sentiment_block["sentiment_index"] if sentiment_block else None,
        )

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
        )

        # Переводим сигнал Claude в направление
        signal_map = {"bullish": "up", "bearish": "down", "neutral": "flat"}
        direction  = signal_map.get(claude_result.get("signal", "neutral"), "flat")
        confidence = round(claude_result.get("confidence", 0) / 100, 3)
        narrative  = claude_result.get("summary", "")

        logger.info(f"🤖 Claude → {ticker}: {direction} (уверенность {confidence})")

    except Exception as e:
        logger.warning(f"Claude недоступен для {ticker}, используем fallback-модель: {e}")
        claude_result = None

    recommendation = _recommendation(direction, confidence)

    # Корректируем рекомендацию по точке входа (технический анализ)
    entry_status = None
    if tech and tech.trade_plan:
        entry_status = tech.trade_plan.get("entry_status")
    if entry_status:
        bias = "лонг" if direction == "up" else "шорт" if direction == "down" else "нейтрально"
        if entry_status in ("late", "invalid"):
            recommendation = "⚪ Наблюдать — точка входа упущена"
        elif entry_status in ("wait", "above", "below"):
            recommendation = f"⏳ Ждать входа ({bias})"

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
        "claude": claude_result,          # полный ответ Claude
        "narrative": narrative,            # краткий вывод Claude
        "reasons": reasons,
        "model_weights": [round(w, 3) for w in weights],
        "decision_by": "claude" if claude_result else "fallback_model",
        "disclaimer": DISCLAIMER,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # ── 7. Сохраняем прогноз в БД (память агента) ───────────────────────────
    if save:
        try:
            pred_id = await db.add_prediction({
                "ticker": ticker,
                "horizon_hours": int(os.getenv("PREDICTION_HORIZON_HOURS", "24")),
                "sentiment_index": sentiment_block["sentiment_index"] if sentiment_block else None,
                "sentiment_signal": sentiment_signal,
                "technical_score": technical_score,
                "combined_score": combined,
                "confidence": confidence,
                "direction": direction,
                "price_at": tech.price if tech else None,
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
        try:
            closes = await ta.fetch_closes(p.ticker, days=10)
        except Exception:
            continue
        if not closes:
            continue
        realized_price  = closes[-1]
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

    retrained = await retrain()
    return {"evaluated": evaluated, "retrained": retrained}


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
