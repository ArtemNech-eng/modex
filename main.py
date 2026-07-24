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
from src.collector.pulse_collector import PulseCollector
from src.collector.rss_collector import RSSCollector
from src.nlp.sentiment_analyzer import SentimentAnalyzer, keyword_sentiment
from src.nlp.ticker_extractor import extract_tickers, is_market_related
from src.aggregator.aggregator import SentimentAggregator
from src.api.main import app, aggregator as api_aggregator, connected_websockets, analyzer as api_analyzer, set_collector
from config.settings import TELEGRAM_CHANNELS, TELEGRAM_API_ID, TELEGRAM_API_HASH
from src import db

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

    # Добавляем точку в агрегатор + пишем в базу знаний (история для Claude)
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
        await db.add_event({
            "source": "telegram", "kind": "message", "ticker": ticker,
            "channel": msg.channel, "text": msg.text,
            "label": sentiment.label, "score": sentiment.score,
            "signal": sentiment.signal,
        })

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
    # Без Telegram-кредов пропускаем канал целиком, НЕ роняя приложение:
    # дашборд/API, Пульс, RSS и интрадей продолжат работать.
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.warning(
            "⚠️  Telegram не настроен (нет TELEGRAM_API_ID / TELEGRAM_API_HASH) — "
            "пропускаю Telegram-канал. Настроение соберётся из Пульса и RSS; "
            "API, дашборд и интрадей работают в обычном режиме."
        )
        return

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


async def pulse_pipeline():
    """Пульс Т-Инвестиции → NLP → Aggregator"""
    pulse = PulseCollector(poll_interval=60)
    await pulse.start()
    logger.info("📱 Пульс pipeline запущен!")

    async for post in pulse.listen():
        try:
            tickers = [post.ticker] if post.ticker else extract_tickers(post.text)
            if not tickers:
                continue
            sentiment = keyword_sentiment(post.text)
            for ticker in tickers:
                api_aggregator.add_point(
                    ticker=ticker,
                    signal=sentiment.signal,
                    label=sentiment.label,
                    score=sentiment.score,
                    channel="pulse",
                    text=post.text,
                    timestamp=post.timestamp,
                )
                await db.add_event({
                    "source": "pulse", "kind": "message", "ticker": ticker,
                    "channel": getattr(post, "author", "") or "pulse", "text": post.text,
                    "label": sentiment.label, "score": sentiment.score,
                    "signal": sentiment.signal,
                })
            stats["messages_processed"] += 1
        except Exception as e:
            logger.error(f"Ошибка обработки Пульса: {e}")


async def rss_pipeline():
    """RSS новости → NLP → Aggregator"""
    rss = RSSCollector(poll_interval=300)
    await rss.start()
    logger.info("📰 RSS pipeline запущен!")

    seen_news: set = set()
    async for item in rss.listen():
        try:
            tickers = extract_tickers(item.full_text)
            if not tickers:
                continue
            # дедуп одинаковых новостей (одна и та же статья не должна дублироваться)
            key = f"{item.source}|{(item.full_text or '')[:120]}"
            if key in seen_news:
                continue
            seen_news.add(key)
            if len(seen_news) > 5000:
                seen_news.clear()
            sentiment = keyword_sentiment(item.full_text)
            # Новости взвешиваем по источнику
            weighted_signal = sentiment.signal * item.weight
            for ticker in tickers:
                api_aggregator.add_point(
                    ticker=ticker,
                    signal=max(-1, min(1, weighted_signal)),
                    label=sentiment.label,
                    score=sentiment.score,
                    channel=f"rss_{item.source}",
                    text=item.full_text[:200],
                    timestamp=item.timestamp,
                )
                await db.add_event({
                    "source": "rss", "kind": "news", "ticker": ticker,
                    "channel": item.source, "text": item.full_text[:500],
                    "label": sentiment.label, "score": sentiment.score,
                    "signal": max(-1, min(1, weighted_signal)),
                    "payload": {"published": item.timestamp.isoformat() if hasattr(item.timestamp, 'isoformat') else str(item.timestamp)},
                })
            stats["messages_processed"] += 1
        except Exception as e:
            logger.error(f"Ошибка обработки RSS: {e}")


async def market_snapshot_pipeline():
    """
    Периодически наполняет базу знаний рыночными снимками:
    Tinkoff (стакан/поток/цена) + реальные сделки трейдеров Пульса.
    Всё пишется в market_events с меткой времени (реалтайм + история для Claude).
    """
    from config.settings import TINKOFF_TOKEN, MOEX_TICKERS
    await db.setup_db()
    watch = list(MOEX_TICKERS.keys())[:8]
    tk = None
    if TINKOFF_TOKEN:
        from src.collector.tinkoff_client import TinkoffClient
        tk = TinkoffClient()
        logger.info("🧠 База знаний: снимки Tinkoff включены")
    else:
        logger.info("🧠 База знаний: TINKOFF_TOKEN не задан — стакан/цены Tinkoff недоступны")

    seen_deals: set = set()
    cycle = 0
    while True:
        cycle += 1
        written = {"orderbook": 0, "quote": 0, "trades": 0, "deal": 0}
        first_err = None
        try:
            if tk:
                for t in watch:
                    ts = datetime.now(timezone.utc)
                    # Лёгкие последовательные вызовы (без тяжёлых 365-дневных
                    # свечей и без пачки concurrent-запросов) — не упираемся в
                    # лимиты Tinkoff API. get_orderbook уже проверен диагностикой.
                    try:
                        ob = await tk.get_orderbook(t)
                    except Exception as e:
                        first_err = first_err or str(e)[:160]
                        ob = None
                    if ob:
                        await db.add_event({"source": "tinkoff", "kind": "orderbook",
                                            "ticker": t, "payload": ob, "ts": ts})
                        written["orderbook"] += 1
                        bid, ask = ob.get("best_bid"), ob.get("best_ask")
                        if bid and ask:
                            await db.add_event({"source": "tinkoff", "kind": "quote", "ticker": t,
                                                "payload": {"last": round((bid + ask) / 2, 4),
                                                            "bid": bid, "ask": ask}, "ts": ts})
                            written["quote"] += 1
                    try:
                        tr = await tk.get_last_trades(t)
                    except Exception as e:
                        first_err = first_err or str(e)[:160]
                        tr = None
                    if tr:
                        await db.add_event({"source": "tinkoff", "kind": "trades",
                                            "ticker": t, "payload": tr, "ts": ts})
                        written["trades"] += 1
                    # «Настроение реальных денег» из стакана+потока → в агрегатор,
                    # чтобы индекс рынка и сетка тикеров жили даже без чатов/Пульса.
                    if ob or tr:
                        from src.analysis.intraday import orderbook_sentiment
                        sent = orderbook_sentiment(
                            ob.get("bid_ask_ratio") if ob else None,
                            tr.get("buy_pct") if tr else None)
                        api_aggregator.add_point(
                            ticker=t, signal=sent["signal"], label=sent["label"],
                            score=max(0.5, sent["score"]), channel="orderbook",
                            text=f"стакан: {ob.get('pressure') if ob else '—'} · "
                                 f"поток: {tr.get('order_flow') if tr else '—'}",
                            timestamp=ts,
                        )
                    await asyncio.sleep(0.4)   # бережём лимиты Tinkoff

            # Сделки трейдеров Пульса — реже (Пульс часто блокируется в РФ),
            # чтобы не долбить заблокированный эндпоинт каждый цикл
            if cycle % 4 == 1:
                try:
                    from src.api.main import _smart_money_snapshot
                    snap = await _smart_money_snapshot(ttl=0)
                    for d in (snap.get("deals") or [])[:80]:
                        sig = f"{d.get('author')}|{d.get('ticker')}|{d.get('action')}|{d.get('timestamp')}|{d.get('price')}"
                        if sig in seen_deals:
                            continue
                        seen_deals.add(sig)
                        await db.add_event({
                            "source": "pulse_deal", "kind": "deal", "ticker": d.get("ticker"),
                            "channel": d.get("author"), "payload": d,
                            "text": f"{d.get('author')} {d.get('action')} {d.get('ticker')}"
                                    f"{(' @ ' + str(d.get('price'))) if d.get('price') else ''}",
                        })
                        written["deal"] += 1
                    if len(seen_deals) > 5000:
                        seen_deals.clear()
                except Exception as e:
                    logger.debug(f"pulse deals snapshot: {e}")

            # Лог результата цикла — видно, наполняется ли база из Tinkoff/Пульса
            if tk and written["orderbook"] == 0 and first_err:
                logger.warning(f"🧠 База знаний: Tinkoff не отдал данные ({first_err}). "
                               f"Проверь права токена/часы торгов.")
            else:
                logger.info(f"🧠 База знаний: +Tinkoff стакан {written['orderbook']}, "
                            f"цена {written['quote']}, поток {written['trades']}; сделки {written['deal']}")

            # Ретеншн: раз в ~50 циклов чистим старше 14 дней
            if cycle % 50 == 1:
                await db.prune_events(keep_days=14)
        except Exception as e:
            logger.debug(f"market snapshot: {e}")
        await asyncio.sleep(90)


async def run():
    """Запустить все pipeline + FastAPI параллельно"""

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

    # Устойчивость: сбой любого фонового пайплайна логируем, но НЕ роняем
    # веб-сервер — иначе healthcheck/деплой падает целиком.
    async def _safe(name, coro):
        try:
            await coro
        except Exception as e:
            logger.error(f"Пайплайн «{name}» остановлен из-за ошибки: {e}", exc_info=True)

    # Запускаем все компоненты параллельно
    await asyncio.gather(
        server.serve(),
        _safe("telegram", telegram_pipeline()),
        _safe("pulse", pulse_pipeline()),
        _safe("rss", rss_pipeline()),
        _safe("market", market_snapshot_pipeline()),
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
