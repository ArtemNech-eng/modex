"""
MOODEX — Aggregator v2
Агрегация тональности сообщений в многофакторный индекс настроения.

Улучшения по сравнению с v1:
  1. Временной decay — свежие сообщения весят экспоненциально больше
  2. Взвешивание по уверенности модели — высокий score → больший вес
  3. Моментум — сравниваем текущее окно с предыдущим (тренд настроения)
  4. Штраф за концентрацию источников — 1 канал = ненадёжный сигнал
  5. Нормализованный объём — сам всплеск активности является сигналом
  6. Итоговый индекс = взвешенная сумма всех факторов
"""
import math
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional
import statistics

from config.settings import SENTIMENT_WINDOW_MINUTES, MIN_MESSAGES_FOR_SIGNAL, ANOMALY_THRESHOLD

logger = logging.getLogger(__name__)

# Период полураспада для временного decay (минуты)
# Сообщение 15 минут назад весит в 2 раза меньше чем только что
DECAY_HALF_LIFE_MINUTES = 15


@dataclass
class SentimentPoint:
    """Одна точка настроения в истории"""
    timestamp: datetime
    ticker:    str
    signal:    float    # [-1, +1]
    label:     str      # positive / negative / neutral
    score:     float    # уверенность модели [0, 1]
    channel:   str
    text_snippet: str   # первые 100 символов текста


@dataclass
class TickerIndex:
    """
    Многофакторный индекс настроения тикера.

    sentiment_index: 0–100, где:
        0-20   = сильный медвежий
        20-40  = умеренно медвежий
        40-60  = нейтральный
        60-80  = умеренно бычий
        80-100 = сильный бычий
    """
    ticker:          str
    company_name:    str
    sentiment_index: float       # итоговый взвешенный индекс 0-100
    avg_signal:      float       # взвешенный сигнал [-1, +1]
    message_count:   int
    positive_pct:    float
    negative_pct:    float
    neutral_pct:     float
    is_anomaly:      bool
    anomaly_type:    Optional[str]
    updated_at:      datetime
    top_channels:    list[str]

    # Новые поля
    momentum:        float       # тренд: >0 настроение улучшается, <0 ухудшается
    momentum_label:  str         # "растёт 📈" / "падает 📉" / "стабильно ➡️"
    source_diversity: float      # 0-1, 1 = много разных каналов
    volume_zscore:   float       # насколько активность отличается от нормы
    confidence:      float       # итоговая уверенность сигнала 0-1

    @property
    def label(self) -> str:
        if self.sentiment_index >= 70:
            return "Сильный бычий 🚀"
        elif self.sentiment_index >= 55:
            return "Умеренно бычий 📈"
        elif self.sentiment_index >= 45:
            return "Нейтральный ➡️"
        elif self.sentiment_index >= 30:
            return "Умеренно медвежий 📉"
        else:
            return "Сильный медвежий 🩸"

    def to_dict(self) -> dict:
        return {
            "ticker":           self.ticker,
            "company_name":     self.company_name,
            "sentiment_index":  round(self.sentiment_index, 1),
            "avg_signal":       round(self.avg_signal, 3),
            "message_count":    self.message_count,
            "positive_pct":     round(self.positive_pct, 1),
            "negative_pct":     round(self.negative_pct, 1),
            "neutral_pct":      round(self.neutral_pct, 1),
            "is_anomaly":       self.is_anomaly,
            "anomaly_type":     self.anomaly_type,
            "label":            self.label,
            "momentum":         round(self.momentum, 3),
            "momentum_label":   self.momentum_label,
            "source_diversity": round(self.source_diversity, 2),
            "volume_zscore":    round(self.volume_zscore, 2),
            "confidence":       round(self.confidence, 2),
            "updated_at":       self.updated_at.isoformat(),
            "top_channels":     self.top_channels,
        }


@dataclass
class MarketIndex:
    """Общий индекс настроения рынка (все тикеры)"""
    sentiment_index: float
    total_messages:  int
    active_tickers:  int
    top_bullish:     list[str]
    top_bearish:     list[str]
    updated_at:      datetime

    def to_dict(self) -> dict:
        return {
            "sentiment_index": round(self.sentiment_index, 1),
            "total_messages":  self.total_messages,
            "active_tickers":  self.active_tickers,
            "top_bullish":     self.top_bullish,
            "top_bearish":     self.top_bearish,
            "updated_at":      self.updated_at.isoformat(),
        }


def _decay_weight(age_minutes: float, half_life: float = DECAY_HALF_LIFE_MINUTES) -> float:
    """
    Экспоненциальный decay: сообщение возрастом half_life минут весит в 2 раза меньше свежего.
    age=0  → weight=1.0
    age=15 → weight=0.5  (при half_life=15)
    age=60 → weight=0.06
    """
    return math.exp(-math.log(2) * age_minutes / half_life)


class SentimentAggregator:
    """
    Многофакторный агрегатор настроения.

    Факторы при расчёте сигнала:
      - Временной decay (свежесть сообщения)
      - Уверенность модели (score из rubert)
      - Моментум (сравнение с предыдущим получасом)
      - Разнообразие источников (штраф за монополию одного канала)
      - Z-score объёма (всплеск сам по себе несёт информацию)
    """

    def __init__(
        self,
        window_minutes:    int   = SENTIMENT_WINDOW_MINUTES,
        min_messages:      int   = MIN_MESSAGES_FOR_SIGNAL,
        anomaly_threshold: float = ANOMALY_THRESHOLD,
    ):
        self.window            = timedelta(minutes=window_minutes)
        self.half_window       = timedelta(minutes=window_minutes // 2)
        self.min_messages      = min_messages
        self.anomaly_threshold = anomaly_threshold

        self._history: dict[str, deque[SentimentPoint]] = defaultdict(
            lambda: deque(maxlen=50_000)
        )
        self._baseline_counts: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=168)   # 7 дней по часам
        )

    def add_point(
        self,
        ticker:    str,
        signal:    float,
        label:     str,
        score:     float,
        channel:   str,
        text:      str,
        timestamp: Optional[datetime] = None,
    ):
        ts = timestamp or datetime.now(timezone.utc)
        self._history[ticker].append(SentimentPoint(
            timestamp=ts, ticker=ticker,
            signal=signal, label=label, score=score,
            channel=channel, text_snippet=text[:100],
        ))

    def _get_window_points(self, ticker: str) -> list[SentimentPoint]:
        now    = datetime.now(timezone.utc)
        cutoff = now - self.window
        return [p for p in self._history[ticker] if p.timestamp >= cutoff]

    # ── Ключевая функция: взвешенный сигнал ────────────────────────────────

    def _weighted_signal(self, points: list[SentimentPoint]) -> tuple[float, float]:
        """
        Считает взвешенный средний сигнал.

        Вес каждого сообщения = decay(возраст) × confidence(score модели)

        Возвращает (avg_signal, total_weight_normalized).
        """
        now = datetime.now(timezone.utc)
        total_weight  = 0.0
        weighted_sum  = 0.0

        for p in points:
            age_min = (now - p.timestamp).total_seconds() / 60
            # Временной decay
            w_time  = _decay_weight(age_min)
            # Уверенность модели (score 0.5 = случайно, 1.0 = уверен)
            # Нормализуем: 0.5 → 0.0, 1.0 → 1.0
            w_conf  = max(0.0, (p.score - 0.5) * 2)
            # Итоговый вес
            weight  = w_time * (0.4 + 0.6 * w_conf)   # минимум 40% от decay даже при низкой уверенности

            weighted_sum  += p.signal * weight
            total_weight  += weight

        if total_weight == 0:
            return 0.0, 0.0

        avg = weighted_sum / total_weight
        return max(-1.0, min(1.0, avg)), total_weight

    def _momentum(self, ticker: str, current_signal: float) -> float:
        """
        Моментум = разница между сигналом в текущем и предыдущем полуокне.
        >0 → настроение улучшается, <0 → ухудшается
        """
        now     = datetime.now(timezone.utc)
        cutoff  = now - self.window
        mid     = now - self.half_window

        prev_points = [p for p in self._history[ticker]
                       if cutoff <= p.timestamp < mid]
        if len(prev_points) < 3:
            return 0.0

        prev_signal, _ = self._weighted_signal(prev_points)
        return round(current_signal - prev_signal, 3)

    def _source_diversity(self, points: list[SentimentPoint]) -> float:
        """
        Нормализованная энтропия Shannon каналов.
        1.0 = равномерно по всем каналам (максимальное доверие)
        0.0 = все сообщения из одного канала (минимальное доверие)
        """
        if not points:
            return 0.0
        counts: dict[str, int] = defaultdict(int)
        for p in points:
            counts[p.channel] += 1
        n      = len(points)
        k      = len(counts)
        if k == 1:
            return 0.0
        entropy = -sum((c / n) * math.log2(c / n) for c in counts.values())
        max_entropy = math.log2(k)
        return entropy / max_entropy if max_entropy > 0 else 0.0

    def _volume_zscore(self, ticker: str, current_count: int) -> float:
        """
        Z-score текущего объёма сообщений относительно исторической нормы.
        """
        history = list(self._baseline_counts[ticker])
        if len(history) < 5:
            return 0.0
        mean = statistics.mean(history)
        std  = statistics.stdev(history) if len(history) > 1 else 0.0
        if std == 0:
            return 0.0
        return round((current_count - mean) / std, 2)

    # ── Публичные методы ────────────────────────────────────────────────────

    def get_ticker_index(self, ticker: str) -> Optional[TickerIndex]:
        from config.settings import MOEX_TICKERS

        points = self._get_window_points(ticker)
        if len(points) < self.min_messages:
            return None

        # 1. Взвешенный сигнал
        avg_signal, total_weight = self._weighted_signal(points)
        sentiment_index = (avg_signal + 1) / 2 * 100

        # 2. Моментум
        momentum = self._momentum(ticker, avg_signal)
        if momentum > 0.05:
            momentum_label = "растёт 📈"
        elif momentum < -0.05:
            momentum_label = "падает 📉"
        else:
            momentum_label = "стабильно ➡️"

        # 3. Разнообразие источников
        diversity = self._source_diversity(points)

        # 4. Z-score объёма
        vol_z = self._volume_zscore(ticker, len(points))
        self._baseline_counts[ticker].append(len(points))

        # 5. Итоговая уверенность сигнала
        # Учитываем: разнообразие источников + количество сообщений (насыщение)
        msg_saturation = min(1.0, len(points) / 30)   # 30+ сообщений → насыщение
        confidence = round(
            0.4 * msg_saturation +
            0.4 * diversity +
            0.2 * min(1.0, abs(avg_signal) * 2),      # сила сигнала тоже добавляет уверенности
            3
        )

        # 6. Простая статистика по меткам
        labels = [p.label for p in points]
        pos = labels.count("positive") / len(labels) * 100
        neg = labels.count("negative") / len(labels) * 100
        neu = labels.count("neutral")  / len(labels) * 100

        # 7. Топ-каналы
        channel_counts: dict[str, int] = defaultdict(int)
        for p in points:
            channel_counts[p.channel] += 1
        top_channels = sorted(channel_counts, key=channel_counts.get, reverse=True)[:3]

        # 8. Аномалии
        is_anomaly, anomaly_type = self._detect_anomaly(ticker, points, vol_z)

        return TickerIndex(
            ticker=ticker,
            company_name=MOEX_TICKERS.get(ticker, ticker),
            sentiment_index=round(sentiment_index, 1),
            avg_signal=round(avg_signal, 3),
            message_count=len(points),
            positive_pct=round(pos, 1),
            negative_pct=round(neg, 1),
            neutral_pct=round(neu, 1),
            is_anomaly=is_anomaly,
            anomaly_type=anomaly_type,
            updated_at=datetime.now(timezone.utc),
            top_channels=top_channels,
            momentum=momentum,
            momentum_label=momentum_label,
            source_diversity=round(diversity, 3),
            volume_zscore=vol_z,
            confidence=confidence,
        )

    def get_market_index(self) -> MarketIndex:
        from config.settings import MOEX_TICKERS
        now = datetime.now(timezone.utc)

        ticker_indices = []
        total_messages = 0
        for ticker in MOEX_TICKERS:
            idx = self.get_ticker_index(ticker)
            if idx:
                ticker_indices.append(idx)
                total_messages += idx.message_count

        if not ticker_indices:
            return MarketIndex(
                sentiment_index=50.0, total_messages=0,
                active_tickers=0, top_bullish=[], top_bearish=[], updated_at=now,
            )

        # Взвешенный рыночный индекс (вес = кол-во сообщений × уверенность)
        total_weight = sum(idx.message_count * idx.confidence for idx in ticker_indices)
        if total_weight > 0:
            weighted_index = sum(
                idx.sentiment_index * idx.message_count * idx.confidence / total_weight
                for idx in ticker_indices
            )
        else:
            weighted_index = statistics.mean(idx.sentiment_index for idx in ticker_indices)

        sorted_by = sorted(ticker_indices, key=lambda x: x.sentiment_index)
        top_bearish = [x.ticker for x in sorted_by[:3]]
        top_bullish = [x.ticker for x in sorted_by[-3:][::-1]]

        return MarketIndex(
            sentiment_index=round(weighted_index, 1),
            total_messages=total_messages,
            active_tickers=len(ticker_indices),
            top_bullish=top_bullish,
            top_bearish=top_bearish,
            updated_at=now,
        )

    def _detect_anomaly(
        self, ticker: str,
        points: list[SentimentPoint],
        vol_z: float,
    ) -> tuple[bool, Optional[str]]:
        # Всплеск активности по z-score
        if vol_z > self.anomaly_threshold:
            return True, "activity_spike"

        # Экстремальное настроение при достаточном количестве сообщений
        if len(points) >= self.min_messages:
            avg = statistics.mean(p.signal for p in points)
            if abs(avg) > 0.7:
                return True, "sentiment_extreme"

        return False, None

    def get_all_indices(self) -> dict[str, TickerIndex]:
        from config.settings import MOEX_TICKERS
        result = {}
        for ticker in MOEX_TICKERS:
            idx = self.get_ticker_index(ticker)
            if idx:
                result[ticker] = idx
        return result

    def get_recent_points(self, ticker: str, limit: int = 20) -> list[dict]:
        points = self._get_window_points(ticker)
        now    = datetime.now(timezone.utc)
        return [
            {
                "timestamp":    p.timestamp.isoformat(),
                "signal":       round(p.signal, 3),
                "label":        p.label,
                "score":        round(p.score, 3),
                "channel":      p.channel,
                "text_snippet": p.text_snippet,
                "age_minutes":  round((now - p.timestamp).total_seconds() / 60, 1),
                "decay_weight": round(_decay_weight((now - p.timestamp).total_seconds() / 60), 3),
            }
            for p in sorted(points, key=lambda x: x.timestamp, reverse=True)[:limit]
        ]

    def get_stats(self) -> dict:
        total_points   = sum(len(v) for v in self._history.values())
        active_tickers = sum(
            1 for t in self._history
            if len(self._get_window_points(t)) >= self.min_messages
        )
        return {
            "total_points_stored": total_points,
            "active_tickers":      active_tickers,
            "window_minutes":      int(self.window.total_seconds() / 60),
            "tracked_tickers":     len(self._history),
        }
