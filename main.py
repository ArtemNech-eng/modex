"""
MOODEX — Главный pipeline реального времени
Соединяет: Telegram → NLP → Aggregator → API (WebSocket broadcast)

Запуск:
    python main.py

Что происходит:
    1. Подключаемся к Telegram
    2. Слушаем новые сообщения из торговых чатов
    3. Извлекаем тикеры + анализируем тональность
    4. Обновляем индексы в агрегаторе
    5. Транслируем обновления всем клиентам дашборда через WebSocket
"""
import asyncio
import logging
import signal
import sys
import os

# Чтобы импорты работали из корня проекта
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from src.collector.telegram_collector import TelegramCollector
from src.nlp.sentiment_analyzer import SentimentAnalyzer, keyword_sentiment
from src.nlp.ticker_extractor import extract_tickers, is_market_related
from src.aggregator.aggregator import SentimentAggregator
from src.api.main import app, aggregator as api_aggregator, connected_websockets, analyzer as api_analyzer, set_collector
from config.settings import TELEGRAM_CHANNELS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("moodex.main")

# ── Статистика ────────────────────────────────────────────────────────────────
stats = {
    "messages_received": 0,
    "messages_processed": 0,
    "messages_skipped": 0,
    "tickers_found": 0,
}


async def process_message(msg, analyzer: SentimentAnalyzer):
    """
    Обработать одно сообщение:
    1. Проверить — рыночное ли оно?
    2. Извлечь тикеры
    3. Проанализировать тональность
    4. Добавить в агрегатор
    5. Если аномалия — broadcast алерт через WebSocket
    """
    stats["messages_received"] += 1

    # Пропускаем нерыночные сообщения (экономим GPU/CPU)
    if not is_market_related(msg.text):
        stats["messages_skipped"] += 1
        return

    tickers = extract_tickers(msg.text)
    if not tickers:
        stats["messages_skipped"] += 1
        return

    # NLP-анализ (нейросеть если загружена, иначе словарный)
    if analyzer._pipeline:
        sentiment = await analyzer.analyze(msg.text)
    else:
        sentiment = keyword_sentiment(msg.text)

    # Добавляем точку в агрегатор для каждого упомянутого тикера
    for ticker in tickers:
        api_aggregator.add_point(
            ticker=ticker,
            signal=sentiment.signal,
            label=sentiment.label,
            score=sentiment.score,
            channel=msg.channel,
            text=msg.text,
            timestamp=msg.timestamp,
        )

    stats["messages_processed"] += 1
    stats["tickers_found"] += len(tickers)

    # Логируем интересные сообщения
    arrow = "📈" if sentiment.signal > 0.3 else "📉" if sentiment.signal < -0.3 else "➡️"
    logger.info(
        f"{arrow} [{msg.channel:<20}] "
        f"[{', '.join(tickers):<12}] "
        f"[{sentiment.label:<8} {sentiment.score:.2f}] "
        f"{msg.text[:60]}..."
    )

    # Проверяем аномалии и рассылаем алерт через WebSocket
    for ticker in tickers:
        idx = api_aggregator.get_ticker_index(ticker)
        if idx and idx.is_anomaly:
            await broadcast_anomaly(ticker, idx)


async def broadcast_anomaly(ticker: str, idx):
    """Разослать алерт об аномалии всем подключённым клиентам дашборда"""
    if not connected_websockets:
        return

    alert = {
        "type": "anomaly_alert",
        "ticker": ticker,
        "company_name": idx.company_name,
        "sentiment_index": idx.sentiment_index,
        "anomaly_type": idx.anomaly_type,
        "message_count": idx.message_count,
        "label": idx.label,
    }

    dead = []
    for ws in connected_websockets:
        try:
            await ws.send_json(alert)
        except Exception:
            dead.append(ws)

    for ws in dead:
        connected_websockets.remove(ws)

    logger.warning(
        f"⚠️  АНОМАЛИЯ: {ticker} | {idx.anomaly_type} | "
        f"индекс={idx.sentiment_index:.1f} | сообщ.={idx.message_count}"
    )


async def stats_reporter():
    """Каждые 60 секунд выводить статистику в лог"""
    while True:
        await asyncio.sleep(60)
        market = api_aggregator.get_market_index()
        logger.info(
            f"📊 СТАТИСТИКА | "
            f"получено={stats['messages_received']} "
            f"обработано={stats['messages_processed']} "
            f"пропущено={stats['messages_skipped']} | "
            f"рынок={market.sentiment_index:.1f}/100 "
            f"тикеров={market.active_tickers} "
            f"сообщ/час={market.total_messages}"
        )


async def telegram_pipeline():
    """Основной цикл: Telegram → NLP → Aggregator"""
    collector = TelegramCollector(channels=TELEGRAM_CHANNELS)

    logger.info("⏳ Подключаемся к Telegram...")
    await collector.start()
    set_collector(collector)  # даём API доступ к коллектору
    logger.info(f"✅ Подключено! Слушаем {len(TELEGRAM_CHANNELS)} каналов.")

    # Загружаем NLP-модель
    logger.info("⏳ Загружаем NLP-модель (первый запуск скачает ~45MB)...")
    try:
        await api_analyzer.load()
        logger.info("✅ NLP-модель загружена (RuBERT)")
    except Exception as e:
        logger.warning(f"⚠️  NLP-модель не загружена ({e}). Используем словарный метод.")

    # Запускаем репортер статистики
    asyncio.create_task(stats_reporter())

    logger.info("🎯 Pipeline запущен! Ждём сообщения из чатов...")
    logger.info("   Открой дашборд: http://localhost:8000")

    # Главный цикл
    async for msg in collector.listen():
        try:
            await process_message(msg, api_analyzer)
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)


async def run():
    """Запустить Telegram pipeline + FastAPI сервер параллельно"""

    # Конфигурация uvicorn (без reload — он мешает asyncio)
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",   # только ошибки от uvicorn, наш лог чище
    )
    server = uvicorn.Server(config)

    logger.info("=" * 55)
    logger.info("  🚀 MOODEX — Market Mood Index")
    logger.info("  Запуск в режиме РЕАЛЬНОГО ВРЕМЕНИ")
    logger.info("=" * 55)

    # Запускаем оба компонента параллельно
    await asyncio.gather(
        server.serve(),
        telegram_pipeline(),
    )


def main():
    # Graceful shutdown по Ctrl+C
    loop = asyncio.new_event_loop()

    def _shutdown(sig, frame):
        logger.info("\n🛑 Остановка MOODEX...")
        loop.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(run())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("✅ MOODEX остановлен.")


if __name__ == "__main__":
    main()
