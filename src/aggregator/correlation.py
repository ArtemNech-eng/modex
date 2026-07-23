"""
MOODEX — Correlation Analyzer
Находит связь между индексом настроения толпы и движением цены.

Ключевые метрики:
- Корреляция Пирсона: насколько направление настроения совпадает с ценой
- Lead time: на сколько минут настроение ОПЕРЕЖАЕТ цену (главный инсайт!)
- Signal accuracy: % сделок, где настроение предсказало направление верно
- Alpha: дополнительная доходность при следовании за сигналом
"""
import logging
import statistics
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional

from src.collector.moex_price_collector import Candle
from src.aggregator.aggregator import SentimentPoint

logger = logging.getLogger(__name__)


@dataclass
class CorrelationPoint:
    timestamp: datetime
    sentiment_signal: float     # [-1, +1]
    price_change_pct: float     # % изменение цены
    sentiment_index: float      # [0, 100]


@dataclass
class CorrelationResult:
    ticker: str
    company_name: str
    correlation: float              # [-1, +1] Пирсон
    lead_minutes: int               # настроение опережает цену на N минут
    signal_accuracy: float          # % правильных предсказаний
    avg_price_after_bull: float     # средн. изменение цены после бычьего сигнала
    avg_price_after_bear: float     # средн. изменение цены после медвежьего сигнала
    sample_size: int                # кол-во точек
    is_significant: bool            # статистически значимо (sample >= 10)

    @property
    def strength(self) -> str:
        a = abs(self.correlation)
        if a >= 0.7:
            return "Сильная 🔥"
        elif a >= 0.4:
            return "Умеренная ✅"
        elif a >= 0.2:
            return "Слабая ⚠️"
        else:
            return "Нет связи ❌"

    @property
    def direction(self) -> str:
        if self.correlation > 0.1:
            return "Прямая 📈"
        elif self.correlation < -0.1:
            return "Обратная 📉"
        else:
            return "Нейтральная ➡️"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "correlation": round(self.correlation, 3),
            "lead_minutes": self.lead_minutes,
            "signal_accuracy": round(self.signal_accuracy, 1),
            "avg_price_after_bull": round(self.avg_price_after_bull, 3),
            "avg_price_after_bear": round(self.avg_price_after_bear, 3),
            "sample_size": self.sample_size,
            "is_significant": self.is_significant,
            "strength": self.strength,
            "direction": self.direction,
        }


class CorrelationAnalyzer:
    """
    Анализирует корреляцию между настроением толпы и ценой акции.

    Алгоритм:
    1. Берём историю настроения по тикеру (из агрегатора)
    2. Берём исторические свечи цены (из MOEX API)
    3. Синхронизируем по времени
    4. Ищем оптимальный lag (на сколько минут настроение опережает цену)
    5. Считаем корреляцию при этом lag-е
    6. Считаем accuracy: % случаев где рост настроения → рост цены
    """

    def __init__(self, lags_to_test: list[int] = None):
        # Проверяем лаги от 0 до 60 минут
        self.lags_to_test = lags_to_test or [0, 5, 10, 15, 20, 30, 45, 60]

    def _pearson(self, x: list[float], y: list[float]) -> float:
        """Коэффициент корреляции Пирсона"""
        if len(x) < 3:
            return 0.0
        try:
            n = len(x)
            mx, my = statistics.mean(x), statistics.mean(y)
            sx = statistics.stdev(x)
            sy = statistics.stdev(y)
            if sx == 0 or sy == 0:
                return 0.0
            cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / n
            return cov / (sx * sy)
        except Exception:
            return 0.0

    def _align_series(
        self,
        sentiment_points: list[SentimentPoint],
        candles: list[Candle],
        bucket_minutes: int = 10,
        lag_minutes: int = 0,
    ) -> list[CorrelationPoint]:
        """
        Выровнять временные ряды настроения и цены.

        Группируем в бакеты по N минут.
        lag_minutes: смещаем настроение вперёд (проверяем, опережает ли оно цену)
        """
        if not sentiment_points or not candles:
            return []

        # Группируем настроение в бакеты
        sentiment_buckets: dict[datetime, list[float]] = {}
        for pt in sentiment_points:
            bucket = pt.timestamp.replace(
                minute=(pt.timestamp.minute // bucket_minutes) * bucket_minutes,
                second=0, microsecond=0
            )
            if bucket not in sentiment_buckets:
                sentiment_buckets[bucket] = []
            sentiment_buckets[bucket].append(pt.signal)

        # Группируем цены в бакеты
        price_buckets: dict[datetime, Candle] = {}
        for c in candles:
            bucket = c.timestamp.replace(
                minute=(c.timestamp.minute // bucket_minutes) * bucket_minutes,
                second=0, microsecond=0
            )
            price_buckets[bucket] = c

        # Сопоставляем с учётом lag
        lag = timedelta(minutes=lag_minutes)
        points = []

        for sent_time, signals in sentiment_buckets.items():
            price_time = sent_time + lag  # ищем цену через lag минут
            if price_time not in price_buckets:
                continue

            candle = price_buckets[price_time]
            avg_signal = statistics.mean(signals)
            price_change = candle.change_pct
            sentiment_index = (avg_signal + 1) / 2 * 100

            points.append(CorrelationPoint(
                timestamp=sent_time,
                sentiment_signal=avg_signal,
                price_change_pct=price_change,
                sentiment_index=sentiment_index,
            ))

        return sorted(points, key=lambda p: p.timestamp)

    def analyze(
        self,
        ticker: str,
        company_name: str,
        sentiment_points: list[SentimentPoint],
        candles: list[Candle],
    ) -> Optional[CorrelationResult]:
        """
        Провести полный анализ корреляции для тикера.
        """
        if len(sentiment_points) < 5 or len(candles) < 5:
            return None

        best_corr = 0.0
        best_lag = 0
        best_points = []

        # Ищем лучший lag
        for lag in self.lags_to_test:
            pts = self._align_series(sentiment_points, candles, lag_minutes=lag)
            if len(pts) < 3:
                continue

            signals = [p.sentiment_signal for p in pts]
            changes = [p.price_change_pct for p in pts]
            corr = self._pearson(signals, changes)

            if abs(corr) > abs(best_corr):
                best_corr = corr
                best_lag = lag
                best_points = pts

        if not best_points:
            return None

        # Считаем accuracy
        correct = 0
        bull_changes, bear_changes = [], []

        for pt in best_points:
            if pt.sentiment_signal > 0.1:
                bull_changes.append(pt.price_change_pct)
                if pt.price_change_pct > 0:
                    correct += 1
            elif pt.sentiment_signal < -0.1:
                bear_changes.append(pt.price_change_pct)
                if pt.price_change_pct < 0:
                    correct += 1

        directional = [p for p in best_points if abs(p.sentiment_signal) > 0.1]
        accuracy = (correct / len(directional) * 100) if directional else 50.0

        avg_bull = statistics.mean(bull_changes) if bull_changes else 0.0
        avg_bear = statistics.mean(bear_changes) if bear_changes else 0.0

        return CorrelationResult(
            ticker=ticker,
            company_name=company_name,
            correlation=round(best_corr, 3),
            lead_minutes=best_lag,
            signal_accuracy=round(accuracy, 1),
            avg_price_after_bull=round(avg_bull, 3),
            avg_price_after_bear=round(avg_bear, 3),
            sample_size=len(best_points),
            is_significant=len(best_points) >= 10,
        )

    def analyze_all(
        self,
        sentiment_history: dict[str, list[SentimentPoint]],
        price_history: dict[str, list[Candle]],
        ticker_names: dict[str, str],
    ) -> list[CorrelationResult]:
        """Анализ корреляции для всех тикеров"""
        results = []
        for ticker, points in sentiment_history.items():
            candles = price_history.get(ticker, [])
            if not candles:
                continue
            result = self.analyze(
                ticker=ticker,
                company_name=ticker_names.get(ticker, ticker),
                sentiment_points=points,
                candles=candles,
            )
            if result:
                results.append(result)

        # Сортируем по силе корреляции
        return sorted(results, key=lambda r: abs(r.correlation), reverse=True)
