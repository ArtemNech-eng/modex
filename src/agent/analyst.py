"""
MOODEX — AI-агент (аналитик)

Сводит воедино все сигналы по тикеру и выдаёт рекомендацию с обоснованием:
  1. Настроение толпы (sentiment из агрегатора: Telegram + Пульс + новости)
  2. Технический анализ (MOEX ISS: тренд, RSI, MACD)
  3. Обучаемая логистическая модель (веса из БД) сводит признаки в прогноз
  4. Человекочитаемое обоснование (+ опциональный LLM-нарратив)

Агент сохраняет каждый прогноз в БД (память) и умеет оценивать прошлые
прогнозы по факту, обучаясь на результатах.

⚠️ Не является инвестиционной рекомендацией.
"""
import os
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.analysis import technical as ta
from src.analysis import geopolitics as geo
from src.agent import predictor as pred
from src import db

logger = logging.getLogger(__name__)

DISCLAIMER = "Не является инвестиционной рекомендацией. Торговля сопряжена с риском."


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
    Полный анализ тикера AI-агентом.
    aggregator — экземпляр SentimentAggregator (передаём, чтобы избежать циклов).
    """
    ticker = ticker.upper()

    # 1. Настроение
    idx = aggregator.get_ticker_index(ticker)
    if idx:
        sentiment_signal = idx.avg_signal
        sentiment_block = idx.to_dict()
    else:
        sentiment_signal = None
        sentiment_block = None

    # 2. Технический анализ
    tech = await ta.analyze_ticker(ticker)
    technical_score = tech.score if tech else None
    technical_block = tech.to_dict() if tech else None

    # 3. Обучаемая модель сводит признаки
    weights = await _load_weights()
    fusion = pred.fuse(sentiment_signal, technical_score, weights)

    # 3.5. Геополитический фон корректирует итог (сильно влияет на рынок РФ)
    geo_snap = geo.MONITOR.snapshot()
    geo_score = geo_snap["score"]
    combined = max(-1.0, min(1.0, fusion.combined_score + 0.3 * geo_score))
    if combined > 0.15:
        direction = "up"
    elif combined < -0.15:
        direction = "down"
    else:
        direction = "flat"
    confidence = abs(combined)

    recommendation = _recommendation(direction, confidence)

    # Приводим рекомендацию в соответствие с таймингом входа, чтобы не было
    # противоречия «Продавать», когда по цене входить уже поздно.
    entry_status = None
    if tech and tech.trade_plan:
        entry_status = tech.trade_plan.get("entry_status")
    if entry_status:
        bias = "лонг" if direction == "up" else "шорт" if direction == "down" else "нейтрально"
        if entry_status in ("late", "invalid"):
            recommendation = "⚪ Наблюдать — точка входа упущена"
        elif entry_status in ("wait", "above", "below"):
            recommendation = f"⏳ Ждать входа ({bias})"
        # entry_status == "enter" → оставляем прямую рекомендацию

    # 4. Обоснование
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
        reasons.append("Технический анализ: нет данных MOEX (тикер/сессия).")
    if geo_snap["events_analyzed"]:
        reasons.append(
            f"Геополитический фон: {geo_snap['label']} (score {geo_score}), учтён в оценке."
        )

    result = {
        "ticker": ticker,
        "recommendation": recommendation,
        "direction": direction,
        "confidence": round(confidence, 3),
        "combined_score": round(combined, 3),
        "prob_up": round((combined + 1) / 2, 3),
        "regime": tech.regime if tech else None,
        "strategy": tech.strategy if tech else None,
        "range_position": tech.range_position if tech else None,
        "sentiment": sentiment_block,
        "technical": technical_block,
        "geopolitics": geo_snap,
        "reasons": reasons,
        "model_weights": [round(w, 3) for w in weights],
        "disclaimer": DISCLAIMER,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Опциональный LLM-нарратив
    narrative = await _llm_narrative(result)
    if narrative:
        result["narrative"] = narrative

    # 5. Память: сохраняем прогноз
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


async def _llm_narrative(analysis: dict) -> Optional[str]:
    """
    Сгенерировать связный текст-вывод через LLM (OpenAI-совместимый API).
    Работает только если задан OPENAI_API_KEY. Иначе — тихо пропускаем.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or os.getenv("USE_LLM_FALLBACK", "false").lower() != "true":
        return None

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")

    prompt = (
        "Ты — биржевой аналитик. По данным ниже дай краткий вывод (3-4 предложения) "
        "на русском: что происходит с бумагой и на что смотреть. Без гарантий и хайпа, "
        "с оговоркой о рисках.\n\n"
        f"Тикер: {analysis['ticker']}\n"
        f"Рекомендация модели: {analysis['recommendation']} "
        f"(уверенность {analysis['confidence']})\n"
        f"Настроение: {analysis.get('sentiment')}\n"
        f"Технический анализ: {analysis.get('technical')}\n"
        f"Факторы: {'; '.join(analysis['reasons'])}\n"
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.4,
                    "max_tokens": 260,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.info(f"LLM-нарратив недоступен: {e}")
        return None


# ─── Обучение на результатах ───────────────────────────────────────────────────

async def evaluate_due_predictions() -> dict:
    """
    Оценить прогнозы, у которых истёк горизонт: сравнить направление
    с фактическим движением цены (по MOEX). Заполняет correct/realized_*.
    """
    due = await db.get_due_predictions()
    evaluated = 0
    for p in due:
        if not p.price_at:
            # Нечего сравнивать — считаем flat/пропуск, помечаем как неоценимый нейтрал
            continue
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
            # flat считаем верным, если движение было небольшим (< 1%)
            correct = abs(realized_return) < 1.0

        await db.evaluate_prediction(p.id, realized_price, realized_return, correct)
        evaluated += 1

    retrained = await retrain()
    return {"evaluated": evaluated, "retrained": retrained}


async def retrain() -> bool:
    """Переобучить веса модели на оценённых прогнозах. Возвращает True, если обновили."""
    rows = await db.get_evaluated_predictions()
    samples = []
    for r in rows:
        if r.get("realized_return") is None:
            continue
        samples.append({
            "sentiment_signal": r.get("sentiment_signal"),
            "technical_score": r.get("technical_score"),
            "label": 1 if r["realized_return"] > 0 else 0,
        })

    if len(samples) < 10:
        return False

    new_weights = pred.train_weights(samples)
    await db.set_setting(pred.WEIGHTS_KEY, pred.weights_to_json(new_weights))
    logger.info(f"🧠 Модель переобучена на {len(samples)} примерах: {new_weights}")
    return True
