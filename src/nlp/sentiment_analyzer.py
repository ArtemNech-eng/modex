"""
MOODEX — Sentiment Analyzer
Анализ тональности сообщений на русском языке.
Используем rubert-tiny-sentiment-balanced (быстрая, 45MB).
"""
import logging
import asyncio
from typing import Literal
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

logger = logging.getLogger(__name__)

SentimentLabel = Literal["positive", "negative", "neutral"]


@dataclass
class SentimentResult:
    """Результат анализа тональности одного сообщения"""
    text: str
    label: SentimentLabel       # positive / negative / neutral
    score: float                # уверенность модели [0, 1]
    
    # Нормализованный сигнал: +1 (бычий) ... -1 (медвежий)
    @property
    def signal(self) -> float:
        if self.label == "positive":
            return +self.score
        elif self.label == "negative":
            return -self.score
        else:
            return 0.0

    def __repr__(self):
        arrow = "📈" if self.signal > 0 else "📉" if self.signal < 0 else "➡️"
        return f"{arrow} [{self.label.upper():<8} {self.score:.2f}] {self.text[:60]}"


class SentimentAnalyzer:
    """
    Анализатор тональности текста.
    
    Модель: cointegrated/rubert-tiny-sentiment-balanced
    - Обучена на русскоязычных данных
    - 3 класса: positive / negative / neutral
    - Размер: ~45MB, быстрая (CPU ~5ms/текст)
    
    Usage:
        analyzer = SentimentAnalyzer()
        await analyzer.load()
        
        result = await analyzer.analyze("Сбер летит вверх, покупаю!")
        print(result)  # 📈 [POSITIVE  0.92] Сбер летит вверх...
        
        results = await analyzer.analyze_batch([...])
    """

    MODEL_NAME = "cointegrated/rubert-tiny-sentiment-balanced"
    
    # Маппинг меток модели → наши метки
    LABEL_MAP = {
        "positive": "positive",
        "negative": "negative",
        "neutral": "neutral",
        "POSITIVE": "positive",
        "NEGATIVE": "negative",
        "NEUTRAL": "neutral",
        "LABEL_0": "neutral",    # некоторые модели используют LABEL_N
        "LABEL_1": "positive",
        "LABEL_2": "negative",
    }

    def __init__(self, model_name: str = None):
        self.model_name = model_name or self.MODEL_NAME
        self._pipeline = None
        self._executor = ThreadPoolExecutor(max_workers=2)

    async def load(self):
        """Загрузить модель (делается один раз при старте)"""
        logger.info(f"⏳ Загружаем NLP-модель: {self.model_name}")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._load_sync)
        logger.info(f"✅ NLP-модель загружена: {self.model_name}")

    def _load_sync(self):
        """Синхронная загрузка модели (в thread pool)"""
        try:
            from transformers import pipeline
            self._pipeline = pipeline(
                task="text-classification",
                model=self.model_name,
                tokenizer=self.model_name,
                top_k=1,
                truncation=True,
                max_length=512,
            )
        except ImportError:
            logger.warning("⚠️ transformers не установлен — используем словарный метод")
            self._pipeline = None
        except Exception as e:
            logger.warning(f"⚠️ Не удалось загрузить NLP-модель: {e} — используем словарный метод")
            self._pipeline = None

    def _predict_sync(self, texts: list[str]) -> list[dict]:
        """Синхронный предикт (в thread pool)"""
        if not self._pipeline:
            raise RuntimeError("Модель не загружена. Вызовите load() сначала.")
        results = self._pipeline(texts, batch_size=32)
        # pipeline с top_k=1 возвращает [[{label, score}], ...]
        return [r[0] if isinstance(r, list) else r for r in results]

    async def analyze(self, text: str) -> SentimentResult:
        """Проанализировать одно сообщение"""
        results = await self.analyze_batch([text])
        return results[0]

    async def analyze_batch(self, texts: list[str]) -> list[SentimentResult]:
        """
        Пакетный анализ списка текстов (быстрее, чем по одному).
        
        Args:
            texts: список строк для анализа
            
        Returns:
            Список SentimentResult в том же порядке
        """
        if not texts:
            return []

        loop = asyncio.get_event_loop()
        raw_results = await loop.run_in_executor(
            self._executor, self._predict_sync, texts
        )

        sentiment_results = []
        for text, raw in zip(texts, raw_results):
            label_raw = raw.get("label", "neutral")
            label = self.LABEL_MAP.get(label_raw, "neutral")
            score = float(raw.get("score", 0.5))
            sentiment_results.append(SentimentResult(
                text=text,
                label=label,
                score=score
            ))

        return sentiment_results

    def unload(self):
        """Выгрузить модель из памяти"""
        self._pipeline = None
        self._executor.shutdown(wait=False)


# ─── Словарный fallback (без нейросети) ──────────────────────────────────────
# Используется для демо-режима без загрузки модели

BULLISH_WORDS = [
    "растёт", "рост", "растет", "вверх", "лонг", "покупаю", "купил",
    "отскок", "ралли", "пробой", "летит", "сильный", "позитив",
    "хороший", "отлично", "лучше", "выгодно", "прибыль", "заработал",
    "up", "buy", "bull", "moon", "pump", "🚀", "📈", "💹", "🟢",
    "превысил", "обновил", "максимум", "рекорд", "оптимист", "бычий",
]

BEARISH_WORDS = [
    "падает", "падение", "вниз", "шорт", "продаю", "продал",
    "слив", "обвал", "коррекция", "дно", "медвежий", "слабый",
    "плохой", "убыток", "потерял", "стоп", "рискованно", "опасно",
    "down", "sell", "bear", "crash", "dump", "📉", "🔴", "🩸",
    "снизился", "минимум", "пессимист", "страх", "паника", "распродажа",
]


def keyword_sentiment(text: str) -> SentimentResult:
    """
    Простой словарный анализ тональности (без нейросети).
    Используется для быстрого демо или как fallback.
    """
    text_lower = text.lower()
    
    bull_count = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear_count = sum(1 for w in BEARISH_WORDS if w in text_lower)

    if bull_count == 0 and bear_count == 0:
        return SentimentResult(text=text, label="neutral", score=0.5)
    
    total = bull_count + bear_count
    if bull_count > bear_count:
        return SentimentResult(text=text, label="positive", score=bull_count / total)
    elif bear_count > bull_count:
        return SentimentResult(text=text, label="negative", score=bear_count / total)
    else:
        return SentimentResult(text=text, label="neutral", score=0.5)
