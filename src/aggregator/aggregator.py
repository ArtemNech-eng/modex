"""
MOODEX — Aggregator
Агрегация тональности сообщений в индекс настроения по тикерам.
Ядро продукта — именно здесь рождается торговый сигнал.
"""
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional
import statistics

from config.settings import SENTIMENT_WINDOW_MINUTES, MIN_MESSAGES_FOR_SIGNAL, ANOMALY_THRESHOLD

logger = logging.getLogger(__name__)


@dataclass
class SentimentPoint:
    """Одна точка настроения в истории"""
    timestamp: datetime
    ticker: str
    signal: float           # [-1, +1]
    label: str              # positive / negative / neutral
    score: float            # уверенность модели
    channel: str
    text_snippet: str       # первые 100 символов текста


@dataclass
class TickerIndex:
    """
    Индекс настроения для одного тикера.
    
    sentiment_index: 0–100, где:
        0-20   = сильный медвежий
        20-40  = умеренно медвежий
        40-60  = нейтральный
        60-80  = умеренно бычий
        80-100 = сильный бычий
    """
    ticker: str
    company_name: str
    sentiment_index: float          # 0-100
    avg_signal: float               # [-1, +1]
    message_count: int              # кол-во сообщений за окно
    positive_pct: float             # % позитивных
    negative_pct: float             # % негативных
    neutral_pct: float              # % нейтральных
    is_anomaly: bool                # аномальная активность
    anomaly_type: Optional[str]     # "activity_spike" / "sentiment_extreme"
    updated_at: datetime
    top_channels: list[str]         # топ-3 источника

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
            "ticker": self.ticker,
            "company_name": self.company_name,
            "sentiment_index": round(self.sentiment_index, 1),
            "avg_signal": round(self.avg_signal, 3),
            "message_count": self.message_count,
            "positive_pct": round(self.positive_pct, 1),
            "negative_pct": round(self.negative_pct, 1),
            "neutral_pct": round(self.neutral_pct, 1),
            "is_anomaly": self.is_anomaly,
            "anomaly_type": self.anomaly_type,
            "label": self.label,
            "updated_at": self.updated_at.isoformat(),
            "top_channels": self.top_channels,
        }


@dataclass
class MarketIndex:
    """Общий индекс настроения рынка (все тикеры)"""
    sentiment_index: float          # 0-100
    total_messages: int
    active_tickers: int             # тикеров с >= MIN_MESSAGES_FOR_SIGNAL
    top_bullish: list[str]          # топ-3 бычьих тикера
    top_bearish: list[str]          # топ-3 медвежьих тикера
    updated_at: datetime

    def to_dict(self) -> dict:
        return {
            "sentiment_index": round(self.sentiment_index, 1),
            "total_messages": self.total_messages,
            "active_tickers": self.active_tickers,
            "top_bullish": self.top_bullish,
            "top_bearish": self.top_bearish,
            "updated_at": self.updated_at.isoformat(),
        }


class SentimentAggregator:
    """
    Агрегирует поток SentimentPoint в индексы настроения.
    
    Использует скользящее окно (по умолчанию 60 минут).
    Отдельный индекс для каждого тикера + общий рыночный.
    
    Usage:
        agg = SentimentAggregator()
        
        # Добавляем точки
        agg.add_point(ticker="SBER", signal=0.8, label="positive", ...)
        
        # Получаем индекс
        index = agg.get_ticker_index("SBER")
        print(f"SBER: {index.sentiment_index}/100 — {index.label}")
    """

    def __init__(
        self,
        window_minutes: int = SENTIMENT_WINDOW_MINUTES,
        min_messages: int = MIN_MESSAGES_FOR_SIGNAL,
        anomaly_threshold: float = ANOMALY_THRESHOLD,
    ):
        self.window = timedelta(minutes=window_minutes)
        self.min_messages = min_messages
        self.anomaly_threshold = anomaly_threshold

        # История по тикерам: ticker → deque[SentimentPoint]
        self._history: dict[str, deque[SentimentPoint]] = defaultdict(
            lambda: deque(maxlen=50_000)
        )
        # Базовый уровень активности (для детекции аномалий)
        self._baseline_counts: dict[str, deque[int]] = defaultdict(
            lambda: deque(maxlen=168)  # 7 дней по часам
        )

    def add_point(
        self,
        ticker: str,
        signal: float,
        label: str,
        score: float,
        channel: str,
        text: str,
        timestamp: Optional[datetime] = None,
    ):
        """Добавить новую точку настроения"""
        ts = timestamp or datetime.now(timezone.utc)
        point = SentimentPoint(
            timestamp=ts,
            ticker=ticker,
            signal=signal,
            label=label,
            score=score,
            channel=channel,
            text_snippet=text[:100],
        )
        self._history[ticker].append(point)

    def _get_window_points(self, ticker: str) -> list[SentimentPoint]:
        """Получить точки из текущего временного окна"""
        now = datetime.now(timezone.utc)
        cutoff = now - self.window
        return [
            p for p in self._history[ticker]
            if p.timestamp >= cutoff
        ]

    def get_ticker_index(self, ticker: str) -> Optional[TickerIndex]:
        """
        Рассчитать индекс настроения для тикера.
        
        Returns None если недостаточно данных.
        """
        from config.settings import MOEX_TICKERS
        
        points = self._get_window_points(ticker)
        
        if len(points) < self.min_messages:
            return None

        signals = [p.signal for p in points]
        labels = [p.label for p in points]
        channels = [p.channel for p in points]

        # Средний сигнал [-1, +1]
        avg_signal = statistics.mean(signals)
        
        # Нормализуем в индекс 0-100: signal=-1 → 0, signal=0 → 50, signal=+1 → 100
        sentiment_index = (avg_signal + 1) / 2 * 100

        # Процентное соотношение
        pos = labels.count("positive") / len(labels) * 100
        neg = labels.count("negative") / len(labels) * 100
        neu = labels.count("neutral") / len(labels) * 100

        # Топ-3 канала по количеству сообщений
        channel_counts: dict[str, int] = defaultdict(int)
        for ch in channels:
            channel_counts[ch] += 1
        top_channels = sorted(channel_counts, key=channel_counts.get, reverse=True)[:3]

        # Детекция аномалий
        is_anomaly, anomaly_type = self._detect_anomaly(ticker, points)

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
        )

    def get_market_index(self) -> MarketIndex:
        """Рассчитать общий рыночный индекс настроения"""
        from config.settings import MOEX_TICKERS
        
        now = datetime.now(timezone.utc)
        total_messages = 0
        ticker_indices = []

        for ticker in MOEX_TICKERS:
            idx = self.get_ticker_index(ticker)
            if idx:
                ticker_indices.append(idx)
                total_messages += idx.message_count

        active = len(ticker_indices)
        
        if not ticker_indices:
            return MarketIndex(
                sentiment_index=50.0,
                total_messages=0,
                active_tickers=0,
                top_bullish=[],
                top_bearish=[],
                updated_at=now,
            )

        # Взвешенный индекс (больше сообщений → больший вес)
        total_weight = sum(idx.message_count for idx in ticker_indices)
        weighted_index = sum(
            idx.sentiment_index * idx.message_count / total_weight
            for idx in ticker_indices
        )

        # Топ бычьих/медвежьих
        sorted_by_sentiment = sorted(ticker_indices, key=lambda x: x.sentiment_index)
        top_bearish = [x.ticker for x in sorted_by_sentiment[:3]]
        top_bullish = [x.ticker for x in sorted_by_sentiment[-3:][::-1]]

        return MarketIndex(
            sentiment_index=round(weighted_index, 1),
            total_messages=total_messages,
            active_tickers=active,
            top_bullish=top_bullish,
            top_bearish=top_bearish,
            updated_at=now,
        )

    def _detect_anomaly(
        self,
        ticker: str,
        current_points: list[SentimentPoint]
    ) -> tuple[bool, Optional[str]]:
        """
        Детектировать аномальную активность.
        
        Типы аномалий:
        - activity_spike: резкий рост числа сообщений (возможный pump/dump)
        - sentiment_extreme: индекс близок к 0 или 100 (экстремальная паника/эйфория)
        """
        count = len(current_points)
        
        # 1. Аномальная активность
        history = self._baseline_counts[ticker]
        if len(history) >= 24:  # нужна хотя бы суточная история
            avg_count = statistics.mean(history) if history else 0
            if avg_count > 0 and count > avg_count * self.anomaly_threshold:
                return True, "activity_spike"

        # Обновляем базовый уровень
        self._baseline_counts[ticker].append(count)

        # 2. Экстремальное настроение
        if count >= self.min_messages:
            signals = [p.signal for p in current_points]
            avg = statistics.mean(signals)
            if abs(avg) > 0.7:  # ближе к ±1
                return True, "sentiment_extreme"

        return False, None

    def get_all_indices(self) -> dict[str, TickerIndex]:
        """Получить индексы для всех тикеров с достаточным числом сообщений"""
        from config.settings import MOEX_TICKERS
        result = {}
        for ticker in MOEX_TICKERS:
            idx = self.get_ticker_index(ticker)
            if idx:
                result[ticker] = idx
        return result

    def get_stats(self) -> dict:
        """Статистика агрегатора"""
        total_points = sum(len(v) for v in self._history.values())
        active_tickers = sum(
            1 for t in self._history
            if len(self._get_window_points(t)) >= self.min_messages
        )
        return {
            "total_points_stored": total_points,
            "active_tickers": active_tickers,
            "window_minutes": int(self.window.total_seconds() / 60),
            "tracked_tickers": len(self._history),
        }
