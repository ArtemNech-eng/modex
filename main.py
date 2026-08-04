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
from datetime import datetime, timezone, timedelta

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
from src.analysis import geopolitics as geo

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


async def _ingest_geopolitics(text, channel, timestamp):
    """
    Прогнать входящий текст через геополитический скоринг и, если есть сигнал,
    записать его в базу знаний отдельной категорией (source="geopolitics").
    Фон рынка рыночно-широкий (ticker=None). Вызывается ДО тикер-фильтра, т.к.
    новость про санкции/переговоры может не содержать тикера. Ошибки глушим —
    сбор данных не должен падать из-за геомодуля.
    """
    try:
        ev = geo.ingest_event(text or "", channel=channel or "", timestamp=timestamp)
        if ev:
            await db.add_event(ev)
    except Exception as e:
        logger.debug(f"geo ingest failed: {e}")


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

    # Геополитический фон: скорим ДО тикер/рыночного фильтра — санкции, переговоры,
    # ставка ЦБ и т.п. могут не содержать тикера, но важны Claude как фон рынка.
    await _ingest_geopolitics(msg.text, getattr(msg, "channel", ""),
                              getattr(msg, "timestamp", None))

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
            # Время СООБЩЕНИЯ, а не обработки: детектор новостных событий
            # сверяет текст со свечой выноса, и время обработки даёт мнимую
            # точность. В агрегатор timestamp сообщения уже передавался.
            "ts": msg.timestamp,
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
            await _ingest_geopolitics(post.text, getattr(post, "author", "") or "pulse",
                                      getattr(post, "timestamp", None))
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
                    "ts": post.timestamp,     # время поста, не время обработки
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
            await _ingest_geopolitics(item.full_text, getattr(item, "source", "") or "rss",
                                      getattr(item, "timestamp", None))
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
                # ts = ВРЕМЯ ПУБЛИКАЦИИ, а не время сбора. Раньше ts не
                # передавался и подставлялось now(), поэтому один и тот же
                # заголовок при каждом цикле опроса получал новую «свежесть».
                # Детектор новостных событий сверяет новость со свечой выноса —
                # на времени сбора эта проверка давала мнимую точность.
                # guid нужен для дедупликации, которая переживает перезапуск.
                await db.add_event({
                    "source": "rss", "kind": "news", "ticker": ticker,
                    "channel": item.source, "text": item.full_text[:500],
                    "label": sentiment.label, "score": sentiment.score,
                    "signal": max(-1, min(1, weighted_signal)),
                    "ts": item.timestamp,
                    "payload": {
                        "published": item.timestamp.isoformat() if hasattr(item.timestamp, 'isoformat') else str(item.timestamp),
                        "guid": getattr(item, "item_id", None),
                    },
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
    from config.settings import (TINKOFF_TOKEN, MOEX_TICKERS, SNAPSHOT_MAX,
                                  SNAPSHOT_PACING_SEC, SNAPSHOT_CORE,
                                  SNAPSHOT_TAIL_PER_CYCLE, FLOW_MINUTE_KEEP_DAYS,
                                  STREAM_ENABLED, BOOK_MINUTE_KEEP_DAYS)
    from src.analysis.intraday import session_phase
    from src.analysis.universe import cached_universe, split_core_tail
    await db.setup_db()

    # Состав берётся ИЗ ОБОРОТА НА БИРЖЕ, а не из константы в конфиге.
    #
    # 31.07 лидером роста стал MVID (+8.29%, оборот 390 млн ₽), а лидером
    # падения SGZH (−4.28%, 635 млн ₽). Ни того, ни другого в зашитом списке
    # из 48 тикеров не было, поэтому по обеим бумагам не собралось ни одного
    # снимка стакана. Рукописный список стареет МОЛЧА: бумага набирает
    # обороты, а система её не видит и об этом не сообщает.
    static_tickers = list(MOEX_TICKERS.keys())

    def _plan():
        u = cached_universe(max_n=(SNAPSHOT_MAX if SNAPSHOT_MAX > 0 else 80),
                            fallback=static_tickers)
        c, t = split_core_tail(u["rows"], pinned=SNAPSHOT_CORE,
                               fallback_tickers=u["tickers"])
        return u, c, t

    def _msk_day():
        return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")

    # Запрос к бирже синхронный (таймаут до 30 с) — уводим в поток, иначе на
    # старте он держит цикл событий и задерживает подъём API и healthcheck.
    uni, core, tail = await asyncio.to_thread(_plan)
    plan_day = _msk_day()
    tail_ptr = 0
    logger.info(f"🧠 База знаний: состав из оборота ({uni['source']}) — "
                f"{len(uni['tickers'])} бумаг; ядро {len(core)}/цикл, "
                f"хвост {len(tail)} по {SNAPSHOT_TAIL_PER_CYCLE}/цикл")
    _new = [t for t in uni["tickers"] if t not in set(static_tickers)]
    if _new:
        logger.info(f"🧠 Сверх статического списка ({len(_new)}): {', '.join(_new[:20])}"
                    + (" …" if len(_new) > 20 else ""))
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
        written = {"orderbook": 0, "quote": 0, "trades": 0, "deal": 0, "flow_min": 0}
        first_err = None
        # Фаза сессии: в торговые часы опрашиваем часто, вне — редко (данные не меняются).
        _mm = datetime.now(timezone.utc) + timedelta(hours=3)
        open_now = session_phase(_mm.hour * 60 + _mm.minute,
                                 _mm.weekday()) in ("main", "evening")
        # Состав пересобирается раз в сутки: контейнер живёт неделями, и без
        # этого он до перезапуска смотрел бы на вчерашних лидеров.
        if _msk_day() != plan_day:
            try:
                uni, core, tail = await asyncio.to_thread(_plan)
                plan_day, tail_ptr = _msk_day(), 0
                logger.info(f"🧠 Состав на {plan_day} ({uni['source']}): "
                            f"{len(uni['tickers'])} бумаг, ядро {len(core)}")
            except Exception as e:                              # noqa: BLE001
                logger.warning(f"🧠 Состав не пересобрался ({e}) — работаю прежним")
        # Тикеры этого цикла: всё ЯДРО + срез ХВОСТА по кругу (ограничиваем нагрузку).
        if tail and SNAPSHOT_TAIL_PER_CYCLE > 0:
            n = min(SNAPSHOT_TAIL_PER_CYCLE, len(tail))
            slice_ = [tail[(tail_ptr + i) % len(tail)] for i in range(n)]
            tail_ptr = (tail_ptr + n) % len(tail)
        else:
            slice_ = tail
        cycle_watch = core + slice_
        try:
            if tk:
                for t in cycle_watch:
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
                        # Кумулятивный footprint за сессию: копим НОВЫЕ сделки с
                        # дедупом по watermark. Сырьё (raw) — транзитное, в событие
                        # НЕ пишем (иначе раздует базу).
                        raw = tr.pop("raw", None)
                        if raw:
                            day = (ts + timedelta(hours=3)).strftime("%Y-%m-%d")  # МСК-дата сессии
                            try:
                                from src.collector.tinkoff_client import _footprint_increment
                                prev = await db.get_session_footprint(t, day)
                                inc = _footprint_increment(raw, (prev or {}).get("watermark"))
                                if inc["new"]:
                                    await db.merge_session_footprint(
                                        day, t, inc["buckets"], inc["watermark"])
                            except Exception as e:
                                logger.debug(f"session footprint {t}: {e}")
                            # Поток с МИНУТНЫМ разрешением — отдельно от footprint.
                            # Footprint группирует по цене и живёт три дня; из него
                            # нельзя получить ни 1m/5m/15m, ни число сделок, ни
                            # размеры. Здесь ключ — минута, хранение 90 дней.
                            #
                            # При включённом стриме ЭТУ запись делает он, и
                            # опрос сюда не пишет: обе ветки складывают объём в
                            # ту же минуту, и вместе они дали бы двойной счёт.
                            if not STREAM_ENABLED:
                                try:
                                    from src.collector.tinkoff_client import minute_buckets
                                    wm = await db.get_flow_watermark(t, day)
                                    mb = minute_buckets(raw, wm)
                                    if mb["new"]:
                                        n = await db.merge_flow_minutes(
                                            t, mb["rows"], mb["watermark"])
                                        written["flow_min"] = (
                                            written.get("flow_min", 0) + n)
                                except Exception as e:
                                    logger.debug(f"flow_minute {t}: {e}")
                        await db.add_event({"source": "tinkoff", "kind": "trades",
                                            "ticker": t, "payload": tr, "ts": ts})
                        written["trades"] += 1
                    # «Настроение реальных денег» из стакана+потока → в агрегатор,
                    # чтобы индекс рынка и сетка тикеров жили даже без чатов/Пульса.
                    if ob or tr:
                        from src.analysis.intraday import orderbook_sentiment
                        # Индекс стакана — ТОЛЬКО по книге заявок (bid/ask), без
                        # потока: поток на ретейл-фиде шумный и создаёт ложные
                        # противоречия. Поток идёт Claude отдельным сигналом
                        # (build_orderbook_context), а не в индекс.
                        sent = orderbook_sentiment(
                            ob.get("bid_ask_ratio") if ob else None)
                        api_aggregator.add_point(
                            ticker=t, signal=sent["signal"], label=sent["label"],
                            score=max(0.5, sent["score"]), channel="orderbook",
                            text=f"стакан: {ob.get('pressure') if ob else '—'} · "
                                 f"поток: {tr.get('order_flow') if tr else '—'}",
                            timestamp=ts,
                        )
                    await asyncio.sleep(SNAPSHOT_PACING_SEC)   # бережём лимиты Tinkoff

            # Сделки трейдеров Пульса больше НЕ собираем здесь: публичный веб-API
            # Пульса закрыт анти-ботом. Сделки заливает агент-скрейпер (браузер)
            # через POST /api/ingest/deals → market_events(source=pulse_deal) →
            # панель Smart Money + Claude. См. _smart_money_snapshot (читает из БД).

            # Лог результата цикла — видно, наполняется ли база из Tinkoff/Пульса
            if tk and written["orderbook"] == 0 and first_err:
                logger.warning(f"🧠 База знаний: Tinkoff не отдал данные ({first_err}). "
                               f"Проверь права токена/часы торгов.")
            else:
                logger.info(f"🧠 База знаний: +Tinkoff стакан {written['orderbook']}, "
                            f"цена {written['quote']}, поток {written['trades']}, "
                            f"минут потока {written.get('flow_min', 0)}; "
                            f"сделки {written['deal']}")

            # Ретеншн: раз в ~50 циклов чистим старше 14 дней
            if cycle % 50 == 1:
                await db.prune_events(keep_days=14)
                await db.prune_session_footprint(keep_days=3)
                # Минутный поток живёт дольше: ради него всё и затевалось,
                # на трёх днях проверить нельзя ничего.
                await db.prune_flow_minute(keep_days=FLOW_MINUTE_KEEP_DAYS)
                await db.prune_book_minute(keep_days=BOOK_MINUTE_KEEP_DAYS)
                await db.prune_candle_minute(keep_days=BOOK_MINUTE_KEEP_DAYS)
                await db.prune_micro_minute(keep_days=BOOK_MINUTE_KEEP_DAYS)
                await db.prune_level_minute(keep_days=BOOK_MINUTE_KEEP_DAYS)
        except Exception as e:
            logger.debug(f"market snapshot: {e}")
        # В сессию — частые снимки (90с); вне сессии — редкие (300с): экономим API,
        # т.к. стакан/поток вне торгов не меняются.
        await asyncio.sleep(90 if open_now else 300)


async def stream_pipeline():
    """
    Постоянное соединение с биржей: сделки и стакан по ВСЕМ бумагам сразу.

    Заменяет опрос по кругу, который давал ядру снимок раз в ~5 минут, а хвосту
    раз в ~43 (замер 01.08). Опрос при этом продолжает работать рядом: он
    собирает свечи и котировки и остаётся страховкой, если стрим не пойдёт.
    Чтобы поток сделок не записался дважды, при включённом стриме опрос НЕ
    пишет flow_minute — см. market_snapshot_pipeline.
    """
    from config.settings import (TINKOFF_TOKEN, STREAM_ENABLED, STREAM_DEPTH,
                                 STREAM_FLUSH_SEC, MOEX_TICKERS, SNAPSHOT_MAX)
    if not STREAM_ENABLED:
        logger.info("Стрим выключен (STREAM_ENABLED=false), работает опрос")
        return
    if not TINKOFF_TOKEN:
        logger.warning("Стрим включён, но токена нет — пропускаю")
        return

    from src.analysis.universe import cached_universe
    from src.collector.stream import MarketStream, msk_minute
    from src.collector.tinkoff_client import TinkoffClient

    static_tickers = list(MOEX_TICKERS.keys())
    uni = await asyncio.to_thread(
        cached_universe,
        max_n=(SNAPSHOT_MAX if SNAPSHOT_MAX > 0 else 80),
        fallback=static_tickers)
    tickers = uni["tickers"]

    # FIGI резолвится ТОЛЬКО через API. Рукописной таблицы здесь нет: 30.07
    # выяснилось, что в прежней 22 записи из 43 указывали на чужие инструменты.
    client = TinkoffClient(TINKOFF_TOKEN)
    figis: dict = {}
    # ПОВТОР, А НЕ ВЫХОД. 03.08 в 15:57 поток не поднялся после деплоя и не
    # поднялся сам: `if not figis: return` ниже уводил конвейер молча, и прод
    # стоял без данных 15 минут, пока я не заметил. FIGI разрешается ТОЛЬКО через
    # Tinkoff API, 80 вызовов при старте, и на выкате их легко придушить лимитом —
    # старый контейнер ещё держит соединение, новый резолвит.
    #
    # Транзиентный отказ не должен убивать сбор до следующего деплоя.
    for attempt in range(1, 6):
        for t in tickers:
            if t in figis:
                continue
            try:
                f = await client.get_figi(t)
                if f:
                    figis[t] = f
            except Exception as e:                               # noqa: BLE001
                logger.debug(f"figi {t}: {e}")
            await asyncio.sleep(0.1)
        if figis:
            if attempt > 1:
                logger.warning("Стрим: FIGI разрешились с попытки %d", attempt)
            break
        wait = min(5 * 2 ** (attempt - 1), 60)
        logger.error("Стрим: FIGI не разрешился ни по одной бумаге, "
                     "попытка %d, пауза %d с", attempt, wait)
        await asyncio.sleep(wait)
    logger.info(f"Стрим: разрешено {len(figis)} из {len(tickers)} бумаг")

    # ЛОТНОСТЬ из ISS. Нужна, чтобы отдавать объёмы в рублях: у SBER лот 1, у
    # GAZP 10, у UGLD 1000 — «1000 лотов» у них отличается на три порядка и само
    # по себе не значит ничего. ISS отдаёт LOTSIZE бесплатно и без токена, одним
    # запросом на всю доску, поэтому лишней зависимости от Tinkoff тут нет.
    def _lots():
        import urllib.request as u, json as j
        url = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
               "TQBR/securities.json?iss.meta=off"
               "&securities.columns=SECID,LOTSIZE,MINSTEP")
        d = j.load(u.urlopen(url, timeout=30))["securities"]
        i = {k: n for n, k in enumerate(d["columns"])}
        lt, st = {}, {}
        for r in d["data"]:
            sid = r[i["SECID"]]
            lt[sid] = int(r[i["LOTSIZE"]] or 1)
            st[sid] = float(r[i["MINSTEP"]] or 0)
        return lt, st
    try:
        lots, steps = await asyncio.to_thread(_lots)
        logger.info(f"Из ISS: лотность и шаг цены по {len(lots)} бумагам")
    except Exception as e:                                       # noqa: BLE001
        logger.warning(f"Справочник ISS не получен ({e}) — объёмы в лотах, "
                       f"«возле лучшей цены» выродится в «точно по цене»")
        lots, steps = {}, {}
    if not figis:
        # ПРИЧИНА ВИДНА СНАРУЖИ, а не только в логе: /api/stream/health говорил
        # «включён, но ещё не поднялся или упал при старте» — фраза, под которую
        # подходит и падение, и молчаливый выход, и они требуют разного.
        #
        # ВТОРАЯ ИТЕРАЦИЯ, 03.08 вечер. Первая называла ДВЕ причины через «или»:
        # «Tinkoff недоступен или токен придушен лимитом». Прод простоял четыре
        # часа, владелец перевыпустил токен — не помогло и НЕ СООБЩИЛО НИЧЕГО,
        # потому что выбрать между версиями было нечем. Теперь причина не
        # угадывается, а измеряется: probe снимает фильтры по одному и отвечает,
        # отказ на стороне Tinkoff или наш запрос отсеивает всё сам.
        import src.collector.stream as _st
        try:
            probe = await client.probe()
            verdict = probe.get("verdict") or "причина не определена"
        except Exception as e:                                   # noqa: BLE001
            verdict = f"проверка причины сорвалась: {type(e).__name__}: {e}"
        _st.START_ERROR = (f"FIGI не разрешился ни по одной бумаге за 5 попыток. "
                           f"Причина: {verdict}")
        logger.error("Стрим: %s. Перезапуск конвейера через 60 с", _st.START_ERROR)
        await asyncio.sleep(60)
        return await stream_pipeline()

    async def _flush(flow: dict, book: dict, candle: dict, micro: dict):
        """
        Карты приходят видом источник -> тикер -> строки. Биржевое и дилерское
        пишутся в РАЗНЫЕ строки, а не складываются: от смешивания бесполезны
        обе половины.
        """
        counts: dict = {}
        for src, per_ticker in flow.items():
            for tk, rows in per_ticker.items():
                n = await db.merge_flow_minutes(tk, rows, None, source=src,
                                                instance=stream.instance)
                counts[f"поток/{src}"] = counts.get(f"поток/{src}", 0) + n
        for src, per_ticker in book.items():
            for tk, rows in per_ticker.items():
                n = await db.merge_book_minutes(tk, rows, source=src,
                                                instance=stream.instance)
                counts[f"стакан/{src}"] = counts.get(f"стакан/{src}", 0) + n
        for tk, rows in candle.items():
            n = await db.merge_candle_minutes(tk, rows)
            counts["свечи"] = counts.get("свечи", 0) + n
            # МИНУТНАЯ ИСТОРИЯ в память — для широты рынка. Считать её из базы
            # значило бы 80 запросов на каждую карточку, а ответ один и тот же
            # для всех. Здесь он готов и стоит полторы тысячи чисел.
            for r in rows:
                stream.note_minute(tk, r)
        for src, per_ticker in micro.items():
            for tk, rows in per_ticker.items():
                n = await db.merge_micro_minutes(tk, rows, source=src)
                counts[f"секунды/{src}"] = counts.get(f"секунды/{src}", 0) + n

        # ИСТОРИЯ УРОВНЕЙ. Секунды остаются в памяти — двадцать цен десять раз в
        # секунду на 80 бумагах не помещаются никуда. В базу идёт минутный итог,
        # и только по уровням, где что-то происходило выше порога в рублях.
        #
        # Берутся ЗАВЕРШЁННЫЕ минуты: текущая ещё набирается, и записать её
        # половину значило бы потом дописывать остаток второй строкой.
        try:
            now_min = msk_minute(datetime.now(timezone.utc))
            done = stream.levels.history.drop_completed(now_min)
            if done:
                rows = []
                for key, minute, tot in done:
                    tk_src, side, price = key
                    tk, _, src = tk_src.partition("|")
                    row = {**tot, "ts": minute, "ticker": tk,
                           "source": src or "exchange", "side": side,
                           "price": price,
                           "lot": (stream.lots or {}).get(tk) or 1}
                    # Счётчики тестов живут на уровне, а не в журнале событий:
                    # тест это приход ЦЕНЫ, а не изменение размера. Берём их
                    # состоянием на конец минуты — без них нечем будет измерить,
                    # значит ли «выдержал» хоть что-то для будущего.
                    lv = stream.levels.levels.get(key)
                    if lv:
                        row.update(tests=lv.get("tests", 0),
                                   test_held=lv.get("test_held", 0),
                                   test_failed=lv.get("test_failed", 0),
                                   alive_sec=lv.get("alive_sec", 0))
                    rows.append(row)
                res = await db.merge_level_minutes(rows)
                # Отброшенное логируется рядом с записанным: без второго числа
                # нельзя понять, порог отсекает шум или половину полезного.
                counts["уровни"] = res.get("written", 0)
                counts["уровни/ниже порога"] = res.get("skipped", 0)
                # Ошибка записи — предупреждением, а не в debug. Именно так
                # отсутствие таблицы час просидело незамеченным: merge ловил
                # исключение, возвращал ноль, и ноль выглядел как «тихий рынок».
                if res.get("error"):
                    logger.warning("запись уровней не удалась: %s", res["error"])
        except Exception as e:                                   # noqa: BLE001
            logger.debug(f"история уровней: {e}")

        if counts:
            logger.debug("стрим записал: %s", counts)

    # stream упоминается внутри _flush выше: замыкание разрешает имя в момент
    # ВЫЗОВА, а первый вызов случится не раньше первого сброса.
    stream = MarketStream(TINKOFF_TOKEN, figis, depth=STREAM_DEPTH,
                          flush_sec=STREAM_FLUSH_SEC, on_flush=_flush)
    # ДНЕВНОЙ ATR. Он нужен для риска: стоп и цель считаются от дневного хода, а
    # не от минутного. В минутных данных его нет — на карточке до сих пор был
    # средний диапазон за 14 МИНУТ, что для стопа бесполезно.
    #
    # Берём дневные свечи из ISS: бесплатно, без токена, по бумаге сразу все дни.
    # Запрос «все бумаги за дату» тоже есть, но он листается по 100 штук, и на 14
    # дней вышло бы 70 запросов против 80 — выгоды нет, а кода больше.
    #
    # Считается в фоне и не задерживает стрим: без ATR карточка работает, просто
    # без одного поля.
    async def _atr_background():
        import urllib.request as u, json as j
        from src.analysis.intraday import volatility_state
        base = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
                "TQBR/securities/{}/candles.json?iss.meta=off&interval=24"
                "&from={}")
        frm = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        ok = 0
        for tk in tickers:
            try:
                def _one(t=tk):
                    d = j.load(u.urlopen(base.format(t, frm), timeout=30))["candles"]
                    i = {k: n for n, k in enumerate(d["columns"])}
                    return ([r[i["high"]] for r in d["data"]],
                            [r[i["low"]] for r in d["data"]],
                            [r[i["close"]] for r in d["data"]])
                h, l, c = await asyncio.to_thread(_one)
                v = volatility_state(h, l, c)
                if v.get("atr"):
                    stream.atr[tk] = {"atr": v["atr"], "state": v.get("state"),
                                      "rank": v.get("atr_rank"), "days": len(c)}
                    ok += 1
            except Exception as e:                               # noqa: BLE001
                logger.debug(f"дневной ATR {tk}: {e}")
            await asyncio.sleep(0.3)
        logger.info(f"Дневной ATR посчитан по {ok} бумагам")

    stream.lots = lots
    # Шаг цены у бумаг разный: SBER 0.01, VTBR 0.005, MVID 0.05, UGLD 0.0001.
    # Единый порог «возле лучшей цены» был бы неверен почти везде.
    stream.steps = steps
    stream.atr = {}

    # КОНТЕКСТ РЫНКА: индекс IMOEX и отраслевая принадлежность бумаг.
    #
    # Оба берутся из ISS — бесплатно и без токена, как лотность и дневные свечи.
    # Сектор берётся из отраслевых индексов САМОЙ биржи, а не из моей догадки,
    # кто чем занимается.
    #
    # Индекс опрашивается, а не приходит потоком, поэтому у него ЕСТЬ ВОЗРАСТ, и
    # он отдаётся наружу: на быстром движении задержка способна перевернуть знак
    # разницы «бумага против рынка».
    async def _market_background():
        import urllib.request as u, json as j
        from datetime import datetime as _dt
        SECTORS = {
            "MOEXOG": "нефть и газ", "MOEXEU": "электроэнергетика",
            "MOEXTL": "телекомы", "MOEXMM": "металлы и добыча",
            "MOEXFN": "финансы", "MOEXCN": "потребительский",
            "MOEXCH": "химия", "MOEXTN": "транспорт",
            "MOEXIT": "информационные технологии", "MOEXRE": "строительство",
        }

        def _fetch_sectors():
            out = {}
            base = ("https://iss.moex.com/iss/statistics/engines/stock/markets/"
                    "index/analytics/{}.json?iss.meta=off&limit=100")
            for idx, name in SECTORS.items():
                try:
                    d = j.load(u.urlopen(base.format(idx), timeout=25))
                    a = d.get("analytics") or {}
                    cols = {c: i for i, c in enumerate(a.get("columns") or [])}
                    if "ticker" not in cols:
                        continue
                    for row in a.get("data") or []:
                        t = row[cols["ticker"]]
                        if t:
                            out[str(t).upper()] = name
                except Exception:
                    continue
            return out

        def _fetch_index():
            url = ("https://iss.moex.com/iss/engines/stock/markets/index/"
                   "securities/IMOEX.json?iss.meta=off")
            d = j.load(u.urlopen(url, timeout=25))
            md = d.get("marketdata") or {}
            cols = {c: i for i, c in enumerate(md.get("columns") or [])}
            rows = md.get("data") or []
            if not rows:
                return {}
            r = rows[0]
            def g(k):
                i = cols.get(k)
                return r[i] if i is not None else None
            out = {"name": "IMOEX", "value": g("CURRENTVALUE"),
                   "change_pct": g("LASTCHANGEPRC"), "ts": g("SYSTIME")}
            # Направление за 1/5/15 минут — из МИНУТНЫХ свечей индекса, а не из
            # дневного изменения: это разные вопросы.
            try:
                today = (datetime.now(timezone.utc) + timedelta(hours=3)
                         ).strftime("%Y-%m-%d")
                cu = ("https://iss.moex.com/iss/engines/stock/markets/index/"
                      f"securities/IMOEX/candles.json?iss.meta=off&interval=1"
                      f"&from={today}")
                cd = (j.load(u.urlopen(cu, timeout=25)).get("candles") or {})
                cc = {c: i for i, c in enumerate(cd.get("columns") or [])}
                data = cd.get("data") or []
                closes = [row[cc["close"]] for row in data if cc.get("close") is not None]
                ch = {}
                for n in (1, 5, 15):
                    if len(closes) > n and closes[-n - 1]:
                        ch[n] = round((closes[-1] - closes[-n - 1])
                                      / closes[-n - 1] * 100, 4)
                if ch:
                    out["changes"] = ch
                    out["minutes_today"] = len(closes)
            except Exception:
                pass
            return out

        try:
            sect = await asyncio.to_thread(_fetch_sectors)
            if sect:
                stream.sectors = sect
                logger.info(f"Из ISS: сектор известен по {len(sect)} бумагам")
        except Exception as e:                                   # noqa: BLE001
            logger.debug(f"секторы: {e}")
        while True:
            try:
                got = await asyncio.to_thread(_fetch_index)
                if got:
                    got["fetched_at"] = datetime.now(timezone.utc).timestamp()
                    stream.imoex = got
            except Exception as e:                               # noqa: BLE001
                logger.debug(f"IMOEX: {e}")
            await asyncio.sleep(30)

    # ЗАСЕВ МИНУТНОЙ ПАМЯТИ ИЗ БАЗЫ.
    #
    # Найдено на живых данных 02.08: карточка SBER показывала 7 событий цены, а
    # сканер по всем бумагам — ноль. Причина не в рынке: карточка читает базу
    # (877 закрытых баров), а сканер память, которая при перезапуске контейнера
    # обнуляется. Событию нужно шесть закрытых баров, пятнадцатиминутному —
    # полтора часа, и всё это время сканер молчал бы после каждого деплоя.
    #
    # Восемьдесят запросов ОДИН раз при старте против получаса слепоты.
    async def _seed_minutes():
        day = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
        ok = 0
        for tk in tickers:
            try:
                rows = await db.candle_series(tk, day, "1m")
            except Exception:                                # noqa: BLE001
                continue
            # Столько же, сколько держит память: MINUTES_KEPT.
            for r in rows[-240:]:
                stream.note_minute(tk, r)
            if rows:
                ok += 1
        logger.info(f"Сканер: минутная история засеяна по {ok} бумагам из базы")

    # НОРМА ОБЪЁМА ПО ВРЕМЕНИ СУТОК. Считается из базы раз в час.
    #
    # Артём просил сравнивать «с обычным объёмом этой акции в это же время», и
    # это правильно: у объёма сильная внутридневная форма — открытие и закрытие
    # тяжёлые, середина дня пустая. Скользящая норма этого не различает.
    #
    # НО СЕЙЧАС ЕЁ НЕ ИЗ ЧЕГО СТРОИТЬ. Стрим поднялся 01.08, и оба дня с тех пор
    # выходные: 583 и 1188 минут ДИЛЕРСКИХ котировок при нуле биржевых за более
    # ранние даты. Поэтому day_profile молчит, пока дней меньше MIN_DAYS, и
    # сканер честно помечает, по какой норме посчитано.
    async def _volume_profiles():
        from src.analysis.volume_events import day_profile, profile_gap, profile_note, MIN_DAYS
        while True:
            try:
                today = (datetime.now(timezone.utc) + timedelta(hours=3)
                         ).strftime("%Y-%m-%d")
                days = [(datetime.now(timezone.utc) + timedelta(hours=3)
                         - timedelta(days=i)).strftime("%Y-%m-%d")
                        for i in range(1, 31)]        # ПРОШЛЫЕ дни, без сегодня
                built = 0
                gaps = []
                for tk in tickers:
                    per_day = {}
                    for d in days:
                        if d == today:
                            continue
                        try:
                            rows = await db.candle_series(tk, d, "1m")
                        except Exception:              # noqa: BLE001
                            continue
                        if rows:
                            per_day[d] = rows
                    prof = day_profile(per_day, lot=(stream.lots or {}).get(tk) or 1,
                                       min_days=MIN_DAYS)
                    if prof:
                        stream.vol_profiles[tk] = prof
                        built += 1
                    else:
                        gaps.append(profile_gap(per_day, min_days=MIN_DAYS))
                if built:
                    logger.info(f"Норма объёма по времени суток: {built} бумаг")
                else:
                    stream.profile_note = profile_note(gaps)
                    logger.info("Норма объёма по времени суток %s",
                                stream.profile_note)
            except Exception as e:                     # noqa: BLE001
                logger.debug(f"нормы объёма: {e}")
            await asyncio.sleep(3600)

    asyncio.create_task(_seed_minutes())
    asyncio.create_task(_volume_profiles())
    asyncio.create_task(_atr_background())
    asyncio.create_task(_market_background())
    await stream.run()


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
        _safe("stream", stream_pipeline()),
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
