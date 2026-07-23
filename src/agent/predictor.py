"""
MOODEX — Предиктор (обучаемое ядро агента)

Лёгкая логистическая регрессия на чистом Python (без numpy/sklearn), которая
учится связывать признаки [sentiment_signal, technical_score] с фактическим
направлением движения цены.

- fuse():   свести признаки в combined_score ∈ [-1, 1], направление, уверенность
- train():  обучить веса на оценённых прогнозах из БД
- Веса хранятся в таблице settings (ключ "model_weights") и переживают рестарт.

Честно: это baseline-модель. Она измеряет свою точность на истории и
подстраивает веса, но не «гарантирует» предсказание рынка.
"""
import json
import math
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

WEIGHTS_KEY = "model_weights"
# Признаки: [bias, sentiment_signal, technical_score]
DEFAULT_WEIGHTS = [0.0, 1.0, 1.0]


def _sigmoid(z: float) -> float:
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


@dataclass
class Fusion:
    prob_up: float          # вероятность роста [0, 1]
    combined_score: float   # [-1, +1]
    direction: str          # up / down / flat
    confidence: float       # [0, 1]


def fuse(sentiment_signal, technical_score, weights=None, flat_band: float = 0.15) -> Fusion:
    """
    Свести признаки в единый прогноз через логистическую модель.
    None-признаки трактуются как 0 (нет данных → нейтрально).
    """
    w = weights or DEFAULT_WEIGHTS
    s = sentiment_signal if sentiment_signal is not None else 0.0
    t = technical_score if technical_score is not None else 0.0

    z = w[0] + w[1] * s + w[2] * t
    prob_up = _sigmoid(z)
    combined = 2 * prob_up - 1  # [-1, +1]

    if combined > flat_band:
        direction = "up"
    elif combined < -flat_band:
        direction = "down"
    else:
        direction = "flat"

    confidence = abs(combined)
    return Fusion(
        prob_up=prob_up,
        combined_score=combined,
        direction=direction,
        confidence=confidence,
    )


def train_weights(samples: list[dict], epochs: int = 400, lr: float = 0.1) -> list[float]:
    """
    Обучить веса логистической регрессии.

    samples: список dict с ключами sentiment_signal, technical_score, label
             где label = 1 (цена выросла) или 0 (упала).
    Возвращает новые веса [bias, w_sent, w_tech].
    """
    # Отбираем валидные примеры
    data = []
    for s in samples:
        label = s.get("label")
        if label is None:
            continue
        x1 = s.get("sentiment_signal") or 0.0
        x2 = s.get("technical_score") or 0.0
        data.append((x1, x2, float(label)))

    if len(data) < 10:
        # Недостаточно данных для осмысленного обучения
        return DEFAULT_WEIGHTS

    w = [0.0, 0.5, 0.5]
    n = len(data)
    for _ in range(epochs):
        g0 = g1 = g2 = 0.0
        for x1, x2, y in data:
            pred = _sigmoid(w[0] + w[1] * x1 + w[2] * x2)
            err = pred - y
            g0 += err
            g1 += err * x1
            g2 += err * x2
        # L2-регуляризация, чтобы веса не разъезжались
        reg = 0.01
        w[0] -= lr * (g0 / n)
        w[1] -= lr * (g1 / n + reg * w[1])
        w[2] -= lr * (g2 / n + reg * w[2])

    return w


def weights_to_json(weights: list[float]) -> str:
    return json.dumps({"weights": weights})


def weights_from_json(raw: str) -> list[float]:
    try:
        data = json.loads(raw)
        w = data.get("weights")
        if isinstance(w, list) and len(w) == 3:
            return [float(x) for x in w]
    except Exception:
        pass
    return DEFAULT_WEIGHTS
