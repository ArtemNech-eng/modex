"""
MOODEX — Загрузчик истории для бэктестинга
Скачивает последние N сообщений из всех каналов и прогоняет через pipeline.
Позволяет "наполнить" систему данными при запуске, не ждать накопления.

Запуск:
    python scripts/load_history.py --limit 500
    python scripts/load_history.py --limit 1000 --channel markettwits
"""
import asyncio
import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("moodex.history")

from src.collector.telegram_collector import TelegramCollector
from src.nlp.sentiment_analyzer import keyword_sentiment
from src.nlp.ticker_extractor import extract_tickers, is_market_related
from src.aggregator.aggregator import SentimentAggregator
from config.settings import TELEGRAM_CHANNELS


async def load_history(limit: int = 500, channel: str = None):
    collector = TelegramCollector()
    agg = SentimentAggregator(window_minutes=60 * 24)  # 24 часа для истории

    await collector.start()

    channels = [channel] if channel else TELEGRAM_CHANNELS
    total_processed = 0

    for ch in channels:
        logger.info(f"📥 Загружаем историю из @{ch} (limit={limit})...")

        try:
            messages = await collector.fetch_history(ch, limit=limit)
        except Exception as e:
            logger.warning(f"Не удалось загрузить @{ch}: {e}")
            continue

        processed = 0
        for msg in messages:
            if not is_market_related(msg.text):
                continue

            tickers = extract_tickers(msg.text)
            if not tickers:
                continue

            sentiment = keyword_sentiment(msg.text)

            for ticker in tickers:
                agg.add_point(
                    ticker=ticker,
                    signal=sentiment.signal,
                    label=sentiment.label,
                    score=sentiment.score,
                    channel=msg.channel,
                    text=msg.text,
                    timestamp=msg.timestamp,
                )
            processed += 1

        total_processed += processed
        logger.info(f"  ✅ @{ch}: {processed}/{len(messages)} рыночных сообщений")

    await collector.stop()

    # Показываем итоговые индексы
    print("\n" + "═" * 55)
    print("  📊 ИНДЕКСЫ ПО ИСТОРИЧЕСКИ ДАННЫМ:")
    print("═" * 55)

    indices = agg.get_all_indices()
    for idx in sorted(indices.values(), key=lambda x: x.sentiment_index, reverse=True):
        bar = "█" * int(idx.sentiment_index / 5) + "░" * (20 - int(idx.sentiment_index / 5))
        print(f"  {idx.ticker:<6} [{bar}] {idx.sentiment_index:5.1f}  ({idx.message_count} сообщ.)")

    market = agg.get_market_index()
    print(f"\n  🌍 РЫНОК: {market.sentiment_index:.1f}/100 | {total_processed} сообщений обработано")
    print("═" * 55 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Загрузка истории MOODEX")
    parser.add_argument("--limit", type=int, default=500, help="Сообщений на канал (default: 500)")
    parser.add_argument("--channel", type=str, default=None, help="Один конкретный канал")
    args = parser.parse_args()

    asyncio.run(load_history(args.limit, args.channel))
