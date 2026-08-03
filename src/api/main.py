"""
MOODEX — FastAPI Backend
REST API + WebSocket для дашборда и внешних интеграций.
"""
import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, BackgroundTasks, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.aggregator.aggregator import SentimentAggregator, TickerIndex, MarketIndex
from src.nlp.sentiment_analyzer import SentimentAnalyzer, keyword_sentiment
from src.nlp.ticker_extractor import extract_tickers, get_ticker_name
from src.analysis import technical as ta
from src.analysis import geopolitics as geo
from src.agent import analyst
from src.agent import scanner
from src.agent import backtest as bt
from src.agent import backfill as bf
from src.agent import research as rs
from src.agent.claude_agent import ClaudeAgent
from config.settings import MOEX_TICKERS

logger = logging.getLogger(__name__)

# Инициализируем Claude агента
claude = ClaudeAgent()

# ─── Приложение ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="MOODEX API",
    description="Market Mood Index + AI-агент для Московской биржи",
    version="0.2.0",
    docs_url="/api/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Глобальные синглтоны ─────────────────────────────────────────────────────
aggregator = SentimentAggregator()
analyzer = SentimentAnalyzer()
connected_websockets: list[WebSocket] = []

# ─── Pydantic-модели ──────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    text: str
    use_model: bool = True  # False = словарный fallback (без нейросети)


class AnalyzeResponse(BaseModel):
    text: str
    label: str
    score: float
    signal: float
    tickers: list[str]


class AlertConfig(BaseModel):
    ticker: str
    threshold_above: Optional[float] = None
    threshold_below: Optional[float] = None
    anomaly_only: bool = False


class ChannelRequest(BaseModel):
    username: str   # например "markettwits" или "https://t.me/markettwits"


class TraderRequest(BaseModel):
    nickname: str            # ник трейдера в Пульсе, напр. "Rostislavzzz"
    note: Optional[str] = None


# ─── Хранилище каналов (в БД, см. src/db.py) ─────────────────────────────────
from src import db

_collector_ref = None   # ссылка на TelegramCollector, устанавливается из main.py


def set_collector(collector):
    """Вызывается из main.py чтобы дать API доступ к коллектору"""
    global _collector_ref
    _collector_ref = collector


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("🚀 MOODEX API запускается...")
    # Загружаем NLP-модель
    try:
        await analyzer.load()
    except Exception as e:
        logger.warning(f"NLP-модель не загружена (используем словарный метод): {e}")
    
    # Готовим БД и переносим старые каналы из JSON (если были)
    await db.setup_db()

    # Демо-данные только если явно включено (DEMO_MODE) — иначе «Рынок» честно
    # показывает реальные источники, а не выдуманные сообщения.
    from config.settings import DEMO_MODE
    if DEMO_MODE:
        _fill_demo_data()
        logger.info("🧪 DEMO_MODE: агрегатор наполнен демо-данными")

    # Быстрый наблюдатель сетапов — БЕЗ Claude, поэтому может стартовать всегда:
    # ловить момент нужно быстро и дёшево, рассуждать можно медленно и дорого.
    try:
        from config.settings import SETUP_WATCH_ENABLED, SETUP_WATCH_INTERVAL_MIN
        if SETUP_WATCH_ENABLED:
            from src.agent import setup_watcher
            setup_watcher.start(SETUP_WATCH_INTERVAL_MIN)
    except Exception as e:                            # noqa: BLE001
        logger.warning(f"наблюдатель сетапов не запущен: {e}")

    # Контур ОБУЧЕНИЯ (оценка прогнозов, БЕЗ Claude) — стартует ВСЕГДА и работает
    # независимо от сканера: самообучение (точность / R / regime-stats) не прерывается,
    # даже когда сканер выключен. БД уже готова.
    try:
        from config.settings import LEARNING_AUTOSTART, LEARNING_INTERVAL_MIN
        if LEARNING_AUTOSTART and not _learning_status.get("running"):
            _learning_status.update({"enabled": True,
                                     "interval_min": max(5, LEARNING_INTERVAL_MIN),
                                     "error": None})
            asyncio.create_task(_learning_loop())
            logger.info("🟢 Контур обучения (оценка прогнозов) авто-запущен")
    except Exception as e:
        logger.warning(f"learning autostart: {e}")

    # СКАНЕР (Claude-сигналы) — по умолчанию РУЧНОЙ: включаешь, когда садишься
    # торговать, выключаешь при выходе. Автозапуск только если LIVE_SIGNALS_AUTOSTART=true.
    try:
        from config.settings import LIVE_SIGNALS_AUTOSTART, LIVE_SIGNALS_INTERVAL_MIN
        if LIVE_SIGNALS_AUTOSTART and not _live_status.get("running"):
            _interval = max(5, LIVE_SIGNALS_INTERVAL_MIN)
            _live_status.update({"enabled": True, "interval_min": _interval,
                                 "tickers": None, "error": None})
            asyncio.create_task(_live_loop())
            logger.info(f"🟢 Сканер (Claude) авто-запущен (интервал {_interval} мин)")
    except Exception as e:
        logger.warning(f"scanner autostart: {e}")

    logger.info("✅ MOODEX API готов")


def _fill_demo_data():
    """Наполнить агрегатор демо-данными для тестирования без Telegram"""
    import random
    from datetime import timedelta

    demo_messages = [
        ("SBER", "Сбер сегодня очень сильный, покупаю на всё!", "positive", 0.88),
        ("SBER", "Сбербанк пробил сопротивление, отличный вход", "positive", 0.82),
        ("SBER", "Держу Сбер, дивиденды хорошие будут", "positive", 0.75),
        ("SBER", "Продаю Сбер, рынок нестабильный", "negative", 0.70),
        ("GAZP", "Газпром летит вниз, шорчу", "negative", 0.91),
        ("GAZP", "Газик слабый, на фоне новостей давление", "negative", 0.85),
        ("GAZP", "Газпром, думаю, ещё поупадёт до поддержки", "negative", 0.78),
        ("GAZP", "Купил немного Газпрома на долгосрок", "positive", 0.65),
        ("LKOH", "Лукойл держится хорошо, нефть поддерживает", "positive", 0.80),
        ("LKOH", "LKOH без изменений, жду пробоя", "neutral", 0.60),
        ("YNDX", "Яндекс ракета 🚀, покупаю ещё", "positive", 0.93),
        ("YNDX", "Яндекс стрельнул на новостях, держу", "positive", 0.88),
        ("YNDX", "Берёт хай, отличный импульс у Яндекса", "positive", 0.85),
        ("YNDX", "Зафиксировал прибыль по Яндексу", "neutral", 0.55),
        ("VTBR", "ВТБ всё, пора избавляться 📉", "negative", 0.89),
        ("VTBR", "ВТБ слабый банк, не держу", "negative", 0.82),
        ("OZON", "OZON хороший потенциал для роста", "positive", 0.75),
        ("TCSG", "Тинькофф снова обновил максимум!", "positive", 0.91),
        ("TCSG", "Т-банк летит, молодцы ребята", "positive", 0.86),
        ("MAGN", "ММК под давлением, продаю", "negative", 0.72),
        ("PLZL", "Полюс держится, золото растёт → Полюс растёт", "positive", 0.83),
        ("NLMK", "НЛМК нейтрально, жду отчёта", "neutral", 0.58),
        ("AFLT", "Аэрофлот слабый, осторожно", "negative", 0.76),
        ("ROSN", "Роснефть норм, нефть держится", "neutral", 0.62),
        ("NVTK", "Новатэк отличный актив на долгосрок", "positive", 0.79),
    ]

    now = datetime.now(timezone.utc)
    channels = ["markettwits", "rdv_investor", "smart_lab", "daytrader"]
    random.seed(42)

    for i, (ticker, text, label, score) in enumerate(demo_messages):
        # Распределяем по последним 60 минутам
        ts = now - timedelta(minutes=random.randint(1, 59))
        signal = score if label == "positive" else (-score if label == "negative" else 0.0)
        aggregator.add_point(
            ticker=ticker,
            signal=signal,
            label=label,
            score=score,
            channel=random.choice(channels),
            text=text,
            timestamp=ts,
        )


# ─── REST Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/market", response_model=dict, summary="Общий индекс рынка")
async def get_market_index():
    """Получить общий индекс настроения рынка"""
    index = aggregator.get_market_index()
    return index.to_dict()


@app.get("/api/tickers", summary="Индексы всех тикеров")
async def get_all_tickers():
    """Получить индексы настроения для всех активных тикеров"""
    indices = aggregator.get_all_indices()
    return {
        "tickers": [idx.to_dict() for idx in indices.values()],
        "count": len(indices),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/orderbook-index", summary="Индекс настроения по стакану (реальные деньги)")
async def get_orderbook_index_all():
    """
    Отдельный индекс настроения ПО СТАКАНУ (только channel='orderbook' из Tinkoff),
    НЕ смешанный с чатами/новостями: по всему рынку + разбивка по тикерам.
    """
    market = aggregator.get_market_orderbook_index()
    tickers = list(aggregator.get_all_orderbook_indices().values())
    tickers.sort(key=lambda x: x["orderbook_index"], reverse=True)
    return {
        "market": market,
        "tickers": tickers,
        "count": len(tickers),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def ticker_known(ticker: str) -> bool:
    """
    Тикер известен, если он в справочнике подписей ИЛИ в живом списке по
    обороту. Логика и обстоятельства — в src/analysis/universe.is_known:
    31.07 эндпоинты отвечали 404 по MVID, лидеру роста дня.
    """
    from src.analysis.universe import is_known
    return is_known(ticker, MOEX_TICKERS)


def company_name(ticker: str) -> str:
    """Подпись компании; для бумаг вне справочника — сам тикер."""
    return MOEX_TICKERS.get((ticker or "").upper(), ticker)


@app.get("/api/orderbook-index/{ticker}", summary="Индекс стакана по тикеру")
async def get_orderbook_index_ticker(ticker: str):
    """Индекс настроения по стакану для конкретного тикера (реальные деньги, без чатов)."""
    ticker = ticker.upper()
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    idx = aggregator.get_orderbook_index(ticker)
    if not idx:
        return {
            "ticker": ticker,
            "company_name": MOEX_TICKERS.get(ticker, ticker),
            "orderbook_index": None,
            "snapshot_count": 0,
            "status": "insufficient_data",
            "message": "Недостаточно снимков стакана (нужно накопить несколько за час)",
        }
    return idx


@app.get("/api/ticker/{ticker}", summary="Индекс конкретного тикера")
async def get_ticker_index(ticker: str):
    """Получить индекс настроения для тикера (например, SBER, GAZP)"""
    ticker = ticker.upper()
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    
    index = aggregator.get_ticker_index(ticker)
    if not index:
        return {
            "ticker": ticker,
            "company_name": MOEX_TICKERS.get(ticker, ticker),
            "sentiment_index": None,
            "message_count": 0,
            "status": "insufficient_data",
            "message": f"Недостаточно данных (нужно минимум 5 сообщений за час)",
        }
    
    return index.to_dict()


@app.get("/api/ticker/{ticker}/messages", summary="Сообщения по тикеру")
async def get_ticker_messages(ticker: str, limit: int = 20):
    """
    Последние сообщения, повлиявшие на индекс тикера (прозрачность оценки).
    Показывает текст, источник, тональность и вклад каждого сообщения.
    """
    ticker = ticker.upper()
    points = aggregator.get_recent_points(ticker, limit=limit)
    idx = aggregator.get_ticker_index(ticker)
    return {
        "ticker": ticker,
        "company_name": MOEX_TICKERS.get(ticker, ticker),
        "index": idx.to_dict() if idx else None,
        "messages": points,
        "explanation": (
            "Индекс настроения = среднее тональности сообщений за окно, "
            "приведённое к шкале 0–100: каждое сообщение получает сигнал от "
            "−1 (негатив) до +1 (позитив); среднее (avg_signal) переводится "
            "как (avg+1)/2·100. Технический сигнал считается отдельно по "
            "свечам MOEX (SMA/RSI/MACD)."
        ),
    }


@app.post("/api/analyze", response_model=AnalyzeResponse, summary="Анализ текста")
async def analyze_text(req: AnalyzeRequest):
    """
    Проанализировать произвольный текст:
    - Определить тональность (позитив/негатив/нейтрал)
    - Извлечь упомянутые тикеры
    """
    if req.use_model and analyzer._pipeline:
        result = await analyzer.analyze(req.text)
    else:
        result = keyword_sentiment(req.text)
    
    tickers = extract_tickers(req.text)
    
    return AnalyzeResponse(
        text=req.text,
        label=result.label,
        score=round(result.score, 3),
        signal=round(result.signal, 3),
        tickers=tickers,
    )


@app.get("/api/anomalies", summary="Текущие аномалии")
async def get_anomalies():
    """Получить тикеры с аномальной активностью прямо сейчас"""
    indices = aggregator.get_all_indices()
    anomalies = [
        idx.to_dict() for idx in indices.values()
        if idx.is_anomaly
    ]
    return {"anomalies": anomalies, "count": len(anomalies)}


@app.get("/api/stats", summary="Статистика системы")
async def get_stats():
    """Статистика работы агрегатора"""
    return aggregator.get_stats()


# ─── База знаний (реальное время + история) ───────────────────────────────────

@app.get("/api/feed", summary="Лента базы знаний (события с метками времени)")
async def get_feed(ticker: Optional[str] = None, source: Optional[str] = None,
                   kind: Optional[str] = None, minutes: Optional[int] = None,
                   limit: int = 200):
    """
    Единая лента данных для дашборда «Рынок» и Claude: сообщения из чатов,
    новости, Пульс, сделки трейдеров и снимки Tinkoff (стакан/цена/поток).
    Фильтры: ticker, source (telegram|pulse|rss|pulse_deal|tinkoff), kind, minutes.
    """
    events = await db.recent_events(ticker=ticker, source=source, kind=kind,
                                    since_minutes=minutes, limit=limit)
    return {"events": events, "count": len(events)}


# exchange — биржа, dealer — внутренний рынок брокера, mixed — собранное до
# 01.08 без различения источника, all — всё вместе (для сверки, не для анализа).
FLOW_SOURCES = ("exchange", "dealer", "mixed", "all")


@app.get("/api/flow/{ticker}", summary="Поток сделок по минутам, с дедупом")
async def get_flow(ticker: str, day: Optional[str] = None, res: str = "1m",
                   source: str = "exchange"):
    """
    Поток сделок: buy/sell, дельта, НАКОПЛЕННАЯ дельта, число сделок, средний
    размер, крупнейшая сделка, дисбаланс, VWAP минуты.

    res: 1m | 5m | 15m | 30m | session
    day: YYYY-MM-DD по МСК, по умолчанию сегодня

    ПОЧЕМУ ОТДЕЛЬНЫЙ МАРШРУТ, А НЕ /api/feed. Лента отдаёт СНИМКИ, каждый из
    которых содержит перекрывающееся окно сделок. Суммировать их нельзя:
    31.07 накопленная дельта, посчитанная сложением снимков, оказалась завышена
    в разы, и её пришлось выбросить. Здесь данные дедуплицированы на записи —
    по времени последней учтённой сделки.

    Производные считаются на чтении, а не хранятся: при смене определения
    «крупной сделки» историю не придётся переписывать.

    source: exchange (по умолчанию) | dealer | mixed | all

    Дилерские сделки — внутренний рынок брокера, цена там не формируется
    биржевым стаканом. 01.08 при ЗАКРЫТОЙ бирже пришло 2812 таких сделок, и
    старый код записал бы их как настоящие. mixed — собранное до 01.08, когда
    источник не различался вовсе.
    """
    if source not in FLOW_SOURCES:
        raise HTTPException(status_code=400,
                            detail=f"source: {', '.join(sorted(FLOW_SOURCES))}")
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    if res not in ("1m", "5m", "15m", "30m", "session"):
        raise HTTPException(status_code=400,
                            detail="res должен быть 1m, 5m, 15m, 30m или session")
    d = day or (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    rows = await db.flow_series(ticker, d, res, source=source)
    return {"ticker": ticker.upper(), "day": d, "res": res, "source": source,
            "count": len(rows), "rows": rows}


@app.get("/api/book/{ticker}", summary="Стакан по минутам: кого больше, покупателей или продавцов")
async def get_book(ticker: str, day: Optional[str] = None, res: str = "1m",
                   source: str = "exchange"):
    """
    Перекос стакана с разрешением до минуты.

    bid_share — доля покупателей в объёме стакана. Это прямой ответ на вопрос
    «объёмы продавцы или покупатели».

    flipped — был ли внутри интервала разворот перекоса через середину. Одно
    среднее это скрывает: минута, где стакан был сначала 80% на покупку, а
    потом 20%, даёт то же среднее, что и ровные 50%.

    ОТКУДА ДАННЫЕ. Только из постоянного соединения (STREAM_ENABLED). Опрос
    REST сюда не пишет: он давал один снимок раз в 5-43 минуты, а по одной
    точке нельзя увидеть ни перекоса в течение минуты, ни разворота.

    res: 1m | 5m | 15m | 30m | session
    source: exchange (по умолчанию) | dealer | mixed | all

    Подписка отдаёт стакан «биржевой И дилера» вместе, поэтому источник
    разбирается по каждому пакету. Дилерский — котировки брокера.
    """
    if source not in FLOW_SOURCES:
        raise HTTPException(status_code=400,
                            detail=f"source: {', '.join(sorted(FLOW_SOURCES))}")
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    if res not in ("1m", "5m", "15m", "30m", "session"):
        raise HTTPException(status_code=400,
                            detail="res должен быть 1m, 5m, 15m, 30m или session")
    d = day or (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    rows = await db.book_series(ticker, d, res, source=source)
    return {"ticker": ticker.upper(), "day": d, "res": res, "source": source,
            "count": len(rows), "rows": rows}


@app.get("/api/candles/{ticker}", summary="Минутные бары из потока (OHLC)")
async def get_stream_candles(ticker: str, day: Optional[str] = None,
                             res: str = "1m"):
    """
    OHLC каждой минуты из ПОТОКА, без задержки.

    Зачем отдельно от /api/technical/{ticker}/candles. Тот маршрут ходит в REST
    и ISS; ISS отдаёт минутки с задержкой около 15 минут, что для интрадея
    бесполезно. Здесь бар обновляется по ходу минуты.

    volume_buy и volume_sell приходят от биржи В САМОЙ СВЕЧЕ. Это независимая
    сверка нашего разбора направлений в /api/flow: расхождение означает ошибку
    в одном из двух.

    res: 1m | 5m | 15m | 30m | session
    """
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    if res not in ("1m", "5m", "15m", "30m", "session"):
        raise HTTPException(status_code=400,
                            detail="res должен быть 1m, 5m, 15m, 30m или session")
    d = day or (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    rows = await db.candle_series(ticker, d, res)
    return {"ticker": ticker.upper(), "day": d, "res": res,
            "count": len(rows), "rows": rows}


@app.get("/api/flow/{ticker}/check", summary="Сверка нашего потока с объёмом свечи биржи")
async def get_flow_check(ticker: str, day: Optional[str] = None,
                         source: str = "exchange"):
    """
    Два независимых счёта одного и того же: наш разбор сделок против объёма,
    который биржа кладёт в свечу. Расхождение означает ошибку в одном из них.

    suspect выставляется при ПРЕВЫШЕНИИ нашего объёма над свечным больше чем на
    5% — это подпись задвоения. Недосчёт законен: контейнер мог подняться
    посреди минуты.

    Сверять осмысленно только source=exchange: дилерские сделки в биржевую свечу
    не входят вовсе.
    """
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    if source not in FLOW_SOURCES:
        raise HTTPException(status_code=400,
                            detail=f"source: {', '.join(sorted(FLOW_SOURCES))}")
    d = day or (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    return await db.flow_candle_check(ticker, d, source=source)


@app.get("/api/events/{ticker}", summary="События стакана: что произошло")
async def get_book_events(ticker: str, day: Optional[str] = None,
                          source: str = "exchange", kind: Optional[str] = None):
    """
    Детектор событий стакана. ОПИСЫВАЕТ, а не предсказывает.

    «В 11:34 было поглощение» — факт о данных, проверяемый по числам, которые
    приложены к каждому событию. «Значит цена пойдёт вверх» — утверждение,
    которого здесь нет и не будет: в событии отсутствуют поля направления и силы
    сигнала.

    Зачем тогда. Вопрос «предшествует ли перекос стакана движению цены» нельзя
    проверить, пока события не помечены. Сначала разметка, потом измерение, и
    только потом правило — если измерение его поддержит. Неделей раньше порядок
    был обратный: семь шагов с десятью метриками, придуманные до измерений, не
    дали ничего и были удалены.

    Типы: поглощение, съедание уровня, крупная заявка, снятие ликвидности,
    восстановление уровня, агрессивные покупки и продажи, ускорение сделок,
    расхождение цены и стакана, пробой после поглощения, ложный пробой,
    истощение агрессора.

    ВСЕ ПОРОГИ — ДОГАДКИ, ни один не измерен. Задаются параметрами запроса с тем
    же именем, что в DEFAULTS модуля, например vol_mult=1.8.
    """
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    if source not in FLOW_SOURCES:
        raise HTTPException(status_code=400,
                            detail=f"source: {', '.join(sorted(FLOW_SOURCES))}")
    from src.analysis.book_events import detect, summarize, KINDS
    if kind and kind not in KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind: {', '.join(KINDS)}")
    d = day or (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    rows = await db.minute_rows(ticker, d, source=source)
    events = detect(rows)
    if kind:
        events = [e for e in events if e["kind"] == kind]
    return {"ticker": ticker.upper(), "day": d, "source": source,
            "minutes": len(rows), "summary": summarize(events),
            "events": events}


#  Имя пути осталось историческим. Страница называется «Наблюдатель рынка», но
#  адрес API не переименован сознательно: на него ссылается сама страница, мои
#  проверочные скрипты и всё, что могло быть настроено снаружи. Переименование
#  здесь ничего не улучшает и ломает работающее.
@app.get("/api/book-live/{ticker}",
         summary="Наблюдатель рынка: одна карточка по бумаге")
async def get_book_live(ticker: str, light: bool = False):
    """
    Всё про бумагу СЕЙЧАС одной карточкой: цена, событие стакана, жизнь
    крупного уровня, односторонность потока, структура последних минут,
    расстояние от VWAP.

    ДВЕ РАЗНЫЕ СВЕЖЕСТИ, и это важно понимать.
        доли секунды   цена, уровни, текущая минута — прямо из памяти стрима
        до 20 секунд   всё, что считается по прошлым минутам: 3м, 5м, VWAP
    Вторая группа за 20 секунд осмысленно не меняется, поэтому задержка там
    безобидна. Первая — то, ради чего всё делалось.

    ЧЕГО ЗДЕСЬ НЕТ. Направления, цели, рекомендации. Карточка описывает
    состояние. Что из этого предсказывает движение — не измерено, и до
    измерения таких полей не появится.

    СТРУКТУРА последних минут отдаётся как ОПИСАНИЕ, а не как совет. Отдельно
    стоит помнить: 31.07 требование «структура вверх» для покупки на откате
    измерялось ВРЕДНЫМ — t=-12.57, положительных дней 16%. Наличие поля не
    означает, что по нему надо действовать.
    """
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    tk = ticker.upper()
    from src.collector.stream import CURRENT
    from src.analysis.book_events import detect
    from config.settings import STREAM_ENABLED

    now = datetime.now(timezone.utc) + timedelta(hours=3)
    out = {"ticker": tk, "at": now.strftime("%H:%M:%S"), "live": False}
    if CURRENT is None:
        out["reason"] = ("стрим выключен" if not STREAM_ENABLED
                         else "стрим ещё не поднялся")
        return out
    out["live"] = True

    # ── из памяти: доли секунды ──────────────────────────────────────────────
    snap = CURRENT.agg.snapshot(tk)
    age = CURRENT.last_msg.get(tk)
    out["packet_age_sec"] = (round((datetime.now(timezone.utc) - age).total_seconds(), 2)
                             if age else None)
    out["minute_now"] = snap.get("candle")
    lot = int((CURRENT.lots or {}).get(tk) or 1)
    out["lot"] = lot
    out["step"] = float((getattr(CURRENT, "steps", None) or {}).get(tk) or 0)
    # УРОВЕНЬ ВМЕСТЕ С ЕГО ИСТОРИЕЙ. Не «BID сейчас 882 тыс.», а что с ним
    # происходило последние 60 секунд: вырос, съели, сняли, вернулся.
    #
    # Идёт в ЛЁГКИЙ ответ сознательно: он опрашивается раз в секунду, а это
    # ровно та частота, на которой история уровня и имеет смысл. В базу за ней
    # ходить не надо — она целиком в памяти.
    #
    # Источник как у секундного ряда: на закрытой бирже биржевых уровней нет.
    lvl_src = ("exchange" if CURRENT.levels.notable(tk, lot=lot, top=1)
               else "dealer")
    out["level_source"] = lvl_src
    out["levels"] = CURRENT.levels.with_history(
        tk, int(datetime.now(timezone.utc).timestamp()),
        lot=lot, top=1, source=lvl_src)

    # СЕКУНДНЫЕ ПОКАЗАТЕЛИ — из кольца в памяти, доли секунды.
    #
    # До них минутная корзина сохраняла из десяти пакетов в секунду только размах
    # перекоса, и три вещи были недоступны: изменение перекоса за 10 и 30 секунд,
    # скорость ликвидности, исполнение возле лучших цен. Данные приходили, но не
    # сохранялись.
    #
    # Источник выбирается по наличию: на закрытой бирже биржевого ряда нет вовсе.
    now_sec = int(datetime.now(timezone.utc).timestamp())
    tick_src = "exchange"
    if not CURRENT.ticks.deltas(tk, "exchange", now_sec):
        tick_src = "dealer"
    out["tick_source"] = tick_src
    out["imbalance"] = CURRENT.ticks.deltas(tk, tick_src, now_sec)
    out["liquidity_speed"] = CURRENT.ticks.speed(tk, tick_src, now_sec)
    out["execution"] = CURRENT.ticks.near_best(tk, tick_src)

    # ЛЕНТА СДЕЛОК. Отвечает на вопрос «реальное давление или просто большая
    # заявка в стакане»: заявку можно снять за секунду ничего не потратив, а
    # сделку отменить нельзя.
    #
    # Источник берётся тот же, что у секундного ряда: дилерская сделка — сделка
    # с брокером, а не с рынком, и считать её давлением рынка нельзя.
    tape = getattr(CURRENT, "tape", None)
    if tape is not None:
        out["tape_30s"] = tape.window(tk, tick_src, now_sec, back=30)
        out["tape_5m"] = tape.window(tk, tick_src, now_sec, back=300)
        # СЕРИЯ односторонних крупных сделок — то, ради чего лента. Само
        # количество крупных ничего не значит: порог это верхние проценты, их
        # всегда примерно столько же. Значит сгущение.
        out["big_streak"] = tape.streak(tk, tick_src, now_sec, back=60)
        out["big_trades"] = tape.big_trades(tk, tick_src, now_sec, back=60,
                                            top=6)
        thr = tape.big_threshold(tk, tick_src)
        if thr:
            out["big_threshold_lots"] = thr
        # ДАВЛЕНИЕ ПРОТИВ СТОЯЩЕГО: два числа рядом, без вывода о том, какое
        # из них важнее — этого никто не мерил.
        resting = sum(x.get("now_lots") or 0 for x in (out.get("levels") or []))
        if resting:
            out["pressure"] = tape.pressure_vs_resting(
                tk, tick_src, now_sec, resting_lots=resting, back=60)

    # КОНТЕКСТ РЫНКА. «IMOEX −0.4%, а SBER +0.8%» говорит больше, чем «SBER
    # +0.8%»: одно и то же движение бумаги означает разное в зависимости от
    # того, куда идёт всё остальное.
    #
    # ДВА ЭТАЛОНА, и путать их нельзя. Медиана корзины считается из НАШЕГО
    # потока — та же секунда, задержки нет, но это наши восемьдесят бумаг
    # равным весом. IMOEX настоящий и взвешенный, но опрошенный, и у него есть
    # ВОЗРАСТ. На быстром движении задержка переворачивает знак разницы.
    mins = getattr(CURRENT, "minutes", None)
    if mins:
        from src.analysis.market_context import context
        idx = dict(getattr(CURRENT, "imoex", None) or {})
        # ВОЗРАСТ ДАННЫХ, А НЕ ЗАПРОСА. Первая версия считала, сколько прошло с
        # моего обращения к ISS, и на закрытой бирже показывала «11 секунд» для
        # значения двухдневной давности. Вопрос был ровно обратный: когда это
        # было правдой, а не когда я спросил.
        if idx.get("fetched_at"):
            idx["fetch_age_sec"] = round(
                datetime.now(timezone.utc).timestamp() - idx["fetched_at"], 1)
        if idx.get("ts"):
            try:
                # SYSTIME от биржи идёт в московском времени, без зоны.
                seen = datetime.strptime(str(idx["ts"]), "%Y-%m-%d %H:%M:%S")
                now_msk = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)
                idx["age_sec"] = max(0.0, round((now_msk - seen).total_seconds(), 1))
            except (ValueError, TypeError):
                pass
        out["market"] = context(mins, tk,
                                sectors=getattr(CURRENT, "sectors", None),
                                index=idx or None)

    # ── по прошлым минутам: до 20 секунд ─────────────────────────────────────
    #
    # light=true отдаёт ТОЛЬКО память и не ходит в базу.
    #
    # Зачем. Экран обновляется раз в секунду; на восьми бумагах полная карточка
    # дала бы 24 запроса к SQLite в секунду — на три таблицы каждая. При этом
    # всё, что считается по прошлым минутам (3м, 5м, VWAP, структура), за
    # секунду осмысленно не меняется. Поэтому страница берёт лёгкую версию
    # каждую секунду, а полную раз в десять.
    if light:
        return out
    day = now.strftime("%Y-%m-%d")
    src = "exchange"
    rows = await db.minute_rows(tk, day, source=src)

    # ОТКАТ НА ДИЛЕРСКИЕ проверяется по НАЛИЧИЮ ПОТОКА И СТАКАНА, а не по тому,
    # пусты ли строки вообще.
    #
    # Здесь была ошибка. Свечи источником не фильтруются — они одни на бумагу.
    # Поэтому при закрытой бирже minute_rows возвращал 51 строку из одних свечей,
    # проверка «если строк нет» не срабатывала, и наружу шла картина без потока,
    # без стакана и без VWAP — но и без пометки, что биржевых данных нет.
    # Молчаливая пустота хуже честной пометки.
    def _has_flow(rs):
        return any(r.get("buy_volume") or r.get("bid_share") for r in rs)
    if not _has_flow(rows):
        dealer = await db.minute_rows(tk, day, source="dealer")
        if _has_flow(dealer):
            rows, src = dealer, "dealer"
            out["source_note"] = "биржевых данных нет, показаны дилерские"
        else:
            out["source_note"] = "ни биржевых, ни дилерских данных потока нет"
    out["source"] = src
    # Полный путь знает источник точнее — он проверен по НАЛИЧИЮ потока в базе,
    # а не по наличию уровней в памяти. Историю прикладываем и здесь.
    out["level_source"] = src
    out["levels"] = CURRENT.levels.with_history(
        tk, int(datetime.now(timezone.utc).timestamp()),
        lot=lot, top=1, source=src)

    closes = [r["close"] for r in rows if r.get("close")]
    out["price"] = closes[-1] if closes else None
    for n in (1, 3, 5, 15):
        if len(closes) > n and closes[-n - 1]:
            out[f"change_{n}m_pct"] = round(
                (closes[-1] - closes[-n - 1]) / closes[-n - 1] * 100, 3)

    # СТРУКТУРА последних пяти минут: описание, не совет.
    last5 = [r for r in rows[-5:] if r.get("high") and r.get("low")]
    if len(last5) >= 3:
        hh = last5[-1]["high"] > last5[0]["high"]
        hl = last5[-1]["low"] > last5[0]["low"]
        out["structure_5m"] = ("выше по максимумам и минимумам" if hh and hl
                               else "ниже по максимумам и минимумам"
                               if not hh and not hl else "смешанная")

    # ОДНОСТОРОННОСТЬ ПОТОКА за пять минут, относительно своей же нормы.
    tail = rows[-5:]
    b = sum((r.get("buy_volume") or 0) for r in tail)
    sv = sum((r.get("sell_volume") or 0) for r in tail)
    if b + sv > 0:
        out["flow_5m"] = {"buy": b, "sell": sv,
                          "buy_share": round(b / (b + sv), 4),
                          "delta": b - sv}

    # РАССТОЯНИЕ ОТ VWAP. В процентах и в средних минутных диапазонах — не в
    # дневном ATR: дневного здесь нет, и подменять одно другим нельзя.
    vw = [r for r in rows if r.get("vwap")]
    if vw and closes:
        v = vw[-1]["vwap"]
        rngs = [r["high"] - r["low"] for r in rows[-14:]
                if r.get("high") and r.get("low") and r["high"] > r["low"]]
        out["vwap"] = v
        out["vwap_dist_pct"] = round((closes[-1] - v) / v * 100, 3)
        if rngs:
            atr = sum(rngs) / len(rngs)
            out["vwap_dist_atr14m"] = round((closes[-1] - v) / atr, 2) if atr else None

    # КРАЙНОСТИ ДНЯ со временем. Время важно: максимум, поставленный на открытии,
    # и максимум минуту назад — разные ситуации. 30.07 по FLOT «максимум дня»
    # стоял в 10:00, а сигнал на его пробой выдали в 23:30.
    from src.analysis.price_levels import day_extremes, flow_change, levels
    out["day"] = day_extremes(rows)

    # ИЗМЕНЕНИЕ ПОТОКА, а не сам поток. Дельта −500 после −2000 означает, что
    # давление СЛАБЕЕТ, а −500 после +300 — что оно только началось.
    out["flow_change"] = flow_change(rows, window=5)

    # ЛОКАЛЬНЫЕ УРОВНИ С ГРАФИКА. Уровень в стакане — чья-то заявка сейчас, её
    # могут снять за секунду. Уровень на графике — место, где цена уже
    # разворачивалась, и оно остаётся, даже когда в стакане там пусто.
    # Пометок «сильный» и «слабый» здесь нет: сколько касаний делает уровень
    # значимым — не измерено.
    out["price_levels"] = levels(rows, tick=out.get("step") or 0.01,
                                 price_now=out.get("price"), top=6)

    # ПЯТЬ ТАЙМФРЕЙМОВ и их СОГЛАСИЕ. «1м вверх, 5м вниз, 15м вниз» — другая
    # ситуация, чем «все вверх», а одна цифра изменения их не различает.
    #
    # Структура и направление считаются только по ЗАКРЫТЫМ барам: тридцатиминутный
    # бар на второй минуте и завершённый — не одно и то же. Текущий отдаётся
    # отдельным полем forming.
    from src.analysis.timeframes import profile
    out["timeframes"] = profile(rows)

    # СОБЫТИЯ ЦЕНЫ этой бумаги. Те же восемь, что в сканере, но для одной: когда
    # смотришь SBER, надо видеть, что сделала именно его цена.
    #
    # Считаются по ЗАКРЫТЫМ барам, поэтому обновляются раз в минуту, а не раз в
    # секунду: пока минута не закрылась, события не существует.
    try:
        # ТА ЖЕ функция и ТЕ ЖЕ входные данные, что у сканера. Раньше карточка
        # считала по всей истории дня и шести уровням, а сканер по шестидесяти
        # барам и четырём — и по одной бумаге выходили разные ответы: RAGR 8
        # против 4, POSI 3 против 9. Оба были правы, и это худший вид ошибки.
        from src.analysis.price_events import events_for
        out["price_events"] = events_for(rows, tick=out.get("step") or 0.01)
    except Exception as e:                                   # noqa: BLE001
        logger.debug(f"price_events {tk}: {e}")

    # ИСТОРИЯ ОБЪЁМА и его СВЯЗКА С ЦЕНОЙ. «+0.4% при объёме ×1.2» и «+0.4% при
    # ×4.8 с новым максимумом» — разные картины, а объём сам по себе их не
    # различает.
    #
    # У ТЕКУЩЕГО бара объём ЧАСТИЧНЫЙ: пятиминутка на первой минуте набрала пятую
    # часть. Поэтому для него считается ТЕМП на минуту, а кратность к норме
    # относится к последнему ЗАКРЫТОМУ бару.
    #
    # ОГОВОРКА: 31.07 одиночный RVOL как фильтр измерялся ПЛОСКИМ на всех
    # порогах. Связку цены с объёмом никто не мерил — это описание, не правило.
    from src.analysis.volume_history import profile as vol_profile
    out["volume"] = vol_profile(rows)

    # ДНЕВНОЙ ATR — от него считается риск. На карточке до этого был средний
    # диапазон за 14 МИНУТ, что для стопа бесполезно; он остаётся отдельным полем.
    a = (getattr(CURRENT, "atr", None) or {}).get(tk)
    if a:
        out["atr_day"] = a["atr"]
        out["atr_state"] = a.get("state")
        out["atr_days"] = a.get("days")
        if out.get("vwap") and out.get("price") and a["atr"]:
            out["vwap_dist_atr_day"] = round(
                (out["price"] - out["vwap"]) / a["atr"], 3)
        if out.get("price") and a["atr"]:
            out["atr_pct"] = round(a["atr"] / out["price"] * 100, 2)

    # СОБЫТИЯ последних минут, свежайшее первым.
    events = detect(rows)
    out["events"] = list(reversed(events))[:5]
    out["minutes_today"] = len(rows)
    return out


@app.get("/api/micro/{ticker}", summary="Секундная микроструктура, свёрнутая по минутам")
async def get_micro(ticker: str, day: Optional[str] = None,
                    source: str = "exchange"):
    """
    Производные СЕКУНДНОГО ряда стакана: размах перекоса за 10 и 30 секунд,
    сколько лотов пришло и ушло, пиковая скорость за секунду, исполнение у лучшей
    цены и глубже.

    ПОЧЕМУ ПРОИЗВОДНЫЕ, А НЕ САМ РЯД. Каждую секунду по каждой бумаге — 4.9 млн
    строк в день, 440 млн за 90 дней, около 35 ГБ при 20 ГБ свободных. Не
    помещается. Здесь 80 тысяч строк в день и 0.9 ГБ за 90 дней.

    Теряется точный ряд трёхнедельной давности. Сохраняется главное: НАСКОЛЬКО
    быстро и НАСКОЛЬКО сильно менялся стакан.

    Живой секундный ряд — в /api/book-live/{ticker}, там задержка доли секунды.
    """
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    if source not in FLOW_SOURCES:
        raise HTTPException(status_code=400,
                            detail=f"source: {', '.join(sorted(FLOW_SOURCES))}")
    d = day or (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    rows = await db.micro_series(ticker, d, source=source)
    return {"ticker": ticker.upper(), "day": d, "source": source,
            "count": len(rows), "rows": rows}


def _scanner_note() -> str:
    """
    Почему у сканеров нет баров. Три разные причины, а не одна фраза; одна из
    них — остановка сбора при открытом рынке, и прятать её за «стрим только
    поднялся» нельзя.
    """
    from datetime import datetime, timedelta, timezone
    from src.collector.stream import CURRENT
    from src.analysis.intraday import session_phase, no_data_note
    msk = datetime.now(timezone.utc) + timedelta(hours=3)
    try:
        phase = session_phase(msk.hour * 60 + msk.minute, msk.weekday())
    except Exception:                                        # noqa: BLE001
        phase = "?"
    fresh = 0
    if CURRENT is not None:
        try:
            fresh = int(CURRENT.health().get("tickers_fresh_60s") or 0)
        except Exception:                                    # noqa: BLE001
            fresh = 0
    return no_data_note(CURRENT is not None, phase, fresh)


@app.get("/api/price-scan", summary="Сканер цены: что делает цена по всем бумагам")
async def get_price_scan(steps: str = "1,5,15", limit: int = 40):
    """
    Восемь событий ЦЕНЫ по всем бумагам разом: резкое ускорение вверх и вниз,
    начало движения, остановка, откат, пробой уровня, ложный пробой, смена
    направления.

    ТОЛЬКО ЦЕНА. Без стакана и без потока: вопрос здесь «что делает сама цена»,
    безотносительно того, кто её двигает. События, которым нужен стакан, живут в
    /api/events.

    ПЕРЕСЧЁТ ПРИ ЗАКРЫТИИ МИНУТЫ, а не по таймеру. События существуют только на
    ЗАКРЫТЫХ барах: пока минута не закрылась, «резкого ускорения» нет — есть
    бар, границы которого ещё изменятся.

    В выдаче есть `rates` — у какой доли бумаг сработал каждый вид. Это не
    украшение: событие, срабатывающее почти везде, не отмечает ничего, и видеть
    это надо прямо. Замер на случайном ряду: откат находился у 61% бумаг, пока
    определение не потребовало ИДУЩЕГО встречного движения вместо положения
    цены в диапазоне.

    Порядок — по числу событий. Не по важности: какое событие важнее, не
    измерено, и придумывать вес значило бы выдать догадку за знание.
    """
    from src.collector.stream import CURRENT
    from src.analysis.price_events import scan, rates, rates_by_step, board
    from src.analysis.price_levels import levels as chart_levels
    mins = getattr(CURRENT, "minutes", None)
    if not mins:
        return {"scanned": 0, "results": [], "rates": {},
                "note": _scanner_note()}
    try:
        want = tuple(int(x) for x in steps.split(",") if x.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="steps: например 1,5,15")
    ticks = dict(getattr(CURRENT, "steps", None) or {})
    # Уровни С ГРАФИКА, а не из стакана: заявку снимают за секунду, а место,
    # где цена разворачивалась, остаётся.
    lv = {}
    for tk, rows in mins.items():
        try:
            lv[tk] = chart_levels(list(rows), tick=ticks.get(tk) or 0.01,
                                  price_now=(list(rows) or [{}])[-1].get("close"),
                                  top=4)
        except Exception:                                    # noqa: BLE001
            continue
    found = scan(mins, ticks=ticks, levels=lv, steps=want)
    return {"scanned": len(mins), "with_events": len(found),
            "steps": list(want), "rates": rates(found, len(mins)),
            "rates_by_step": rates_by_step(found, len(mins)),
            # Направление и структура по ВСЕМ бумагам, не только по сработавшим:
            # «строит ли цена движение» — вопрос про каждую, а не про событие.
            "board": board(mins, steps=want),
            "results": found[:limit]}


@app.get("/api/volume-scan", summary="Сканер объёма: пришли ли деньги")
async def get_volume_scan(steps: str = "1,5", limit: int = 40):
    """
    Всплеск оборота и его УСКОРЕНИЕ по всем бумагам разом.

    Отвечает на вопрос «действительно ли в движение пришли деньги», и отдельно —
    на более интересный: в какой МОМЕНТ они пошли. «Сегодня большой объём» это
    состояние, «оборот растёт четвёртую минуту подряд» — событие со временем.

    В РУБЛЯХ. Лот у SBER 1, у UGLD 1000, и список по лотам сравнивал бы
    несравнимое. Оборот считается как лоты × лотность × закрытие бара — это
    приближение: настоящий оборот берут по цене каждой сделки.

    ДВЕ НОРМЫ, и в каждом событии написано, по какой посчитано. Скользящая
    (медиана последних баров) есть всегда, но ПОЛЗЁТ вместе с растущим объёмом.
    Норма по времени суток этого не делает и потому лучше — но она появится
    только когда наберётся десять торговых дней. Сейчас в базе два дня, оба
    выходные с дилерскими котировками, и строить по ним «обычный объём 14:30»
    значило бы выдумать норму.

    Порядок — по КРАТНОСТИ к норме, а не по рублям: миллиард у SBER это обычный
    день, а сто миллионов у DATA — событие.

    НОРМА СЧИТАЕТСЯ ПО СВОЕЙ СЕССИИ. Утренняя и основная — разные рынки:
    основная тяжелее утренней в 2.5 раза по медиане, у VTBR в 11.8. Сравнивать
    минуту основной сессии с утренними значило бы объявлять всплеском само
    открытие; замер 03.08 давал 7-8 ложных событий в первые минуты. Пока своих
    баров мало, событий нет, а число `warming_up` говорит, у скольких бумаг так.

    ПОЛ ПО ОБОРОТУ. Единственный порог, взятый не из самой бумаги, — и взятый
    из размера позиции Артёма. Минута тише его позиции это минута, где он был бы
    всей ликвидностью, и «оборот в 99.7 раза выше нормы» на 39 469 ₽ ничего не
    значит. Отсев показан числом `below_floor`: пустая таблица должна читаться
    как «всё выброшено полом», а не как «на рынке спокойно».
    """
    from src.collector.stream import CURRENT
    from src.analysis.volume_events import (scan, rates, below_floor,
                                         warming_up, FLOOR_RUB)
    # Доли по шагам считает общая функция: форма события одна и та же,
    # а обманывает общая доля одинаково у обоих сканеров.
    from src.analysis.price_events import rates_by_step
    mins = getattr(CURRENT, "minutes", None)
    if not mins:
        return {"scanned": 0, "results": [], "rates": {},
                "note": _scanner_note()}
    try:
        want = tuple(int(x) for x in steps.split(",") if x.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="steps: например 1,5")
    profiles = dict(getattr(CURRENT, "vol_profiles", None) or {})
    found = scan(mins, lots=dict(getattr(CURRENT, "lots", None) or {}),
                 profiles=profiles, steps=want)  # lots ниже — тот же словарь
    lots = dict(getattr(CURRENT, "lots", None) or {})
    return {"scanned": len(mins), "with_events": len(found),
            "steps": list(want), "rates": rates(found, len(mins)),
            "rates_by_step": rates_by_step(found, len(mins)),
            "baseline": "время суток" if profiles else "скользящая",
            "profiles_ready": len(profiles),
            "floor_rub": FLOOR_RUB,
            "below_floor": below_floor(mins, lots=lots),
            "warming_up": warming_up(mins),
            "results": found[:limit]}


@app.get("/api/levels/{ticker}", summary="История ценовых уровней по минутам")
async def get_levels(ticker: str, day: Optional[str] = None,
                     source: str = "exchange", limit: int = 400):
    """
    Жизнь ценовых уровней бумаги за день: сколько долили, сколько СЪЕЛИ и сколько
    СНЯЛИ на каждой цене.

    Съедено и снято — разные вещи, и в этом весь смысл таблицы. Уменьшение
    заявки на 300 лотов значит либо «покупатель забрал предложение», либо
    «продавец передумал». Разность размеров их не различает, различает только
    объём сделок на этой цене между пакетами стакана.

    ПИШЕТСЯ НЕ ВСЁ. Строка появляется, только если оборот по уровню за минуту
    выше порога в рублях (LEVEL_FLOOR_RUB) либо уровень восстанавливался. Иначе
    на каждую минуту пришлось бы по шесть уровней на бумагу на источник — под
    полмиллиона строк в день. Порог — догадка, его надо калибровать по факту.

    Секундная история живого уровня — в /api/book-live/{ticker}, поле timeline:
    там задержка доли секунды, но глубина всего минута и только в памяти.

    Объёмы в ЛОТАХ, как в базе. Рубли надо считать через лотность: у UGLD лот
    1000, у SBER 1.
    """
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    if source not in FLOW_SOURCES:
        raise HTTPException(status_code=400,
                            detail=f"source: {', '.join(sorted(FLOW_SOURCES))}")
    d = day or (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    try:
        rows = await db.level_series(ticker, d, source=source, limit=limit)
    except db.LevelStoreError as e:
        # ПРИЧИНА наружу, а не голая пятисотка. «Таблицы нет» и «база занята» —
        # разные беды, и Internal Server Error не отличает их ничем.
        made = await db.ensure_level_table()
        if made.get("ok"):
            rows = await db.level_series(ticker, d, source=source, limit=limit)
        else:
            return {"ticker": ticker.upper(), "day": d, "source": source,
                    "count": 0, "rows": [], "error": str(e),
                    "create_attempt": made}
    # CURRENT импортируется ЛОКАЛЬНО в каждом маршруте, который им пользуется:
    # это ссылка на живой стрим, и на момент импорта модуля её ещё нет. Без этой
    # строки здесь был NameError и голая пятисотка — она и держала маршрут
    # сломанным, а вовсе не отсутствие таблицы, как я решил сначала.
    from src.collector.stream import CURRENT
    lot = int((getattr(CURRENT, "lots", None) or {}).get(ticker.upper()) or 1)
    total = {"traded_lots": sum(r["traded"] for r in rows),
             "pulled_lots": sum(r["pulled"] for r in rows),
             "added_lots": sum(r["added"] for r in rows),
             "restored": sum(r["restored"] for r in rows),
             "gone": sum(r["gone"] for r in rows)}
    # ТЕСТЫ накопительные на уровень, поэтому по каждой цене берётся максимум за
    # день, а не сумма по минутам — иначе один тест сосчитался бы столько раз,
    # сколько минут прожил уровень.
    per_level: dict = {}
    for r in rows:
        k = (r["side"], r["price"])
        cur = per_level.get(k) or {}
        for f in ("tests", "test_held", "test_failed"):
            cur[f] = max(cur.get(f, 0), int(r.get(f) or 0))
        per_level[k] = cur
    for f in ("tests", "test_held", "test_failed"):
        total[f] = sum(v.get(f, 0) for v in per_level.values())
    total["levels"] = len(per_level)
    return {"ticker": ticker.upper(), "day": d, "source": source, "lot": lot,
            "count": len(rows), "totals": total,
            "floor_rub": db.LEVEL_MINUTE_FLOOR_RUB, "rows": rows}


@app.get("/api/live/{ticker}", summary="Прямо сейчас: из памяти, без ожидания сброса")
async def get_live(ticker: str):
    """
    Текущая минута ИЗ ПАМЯТИ накопителя, минуя базу.

    Сброс в базу идёт раз в 20 секунд, поэтому маршруты /api/flow, /api/book и
    /api/candles отстают на эти 20 секунд. Здесь отдаётся то, что накопилось
    прямо сейчас — минута ещё не закрыта и данные частичные, зато свежие.

    Замер 01.08: отставание базы от реальности 16-64 секунды. Здесь — доли
    секунды.
    """
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    from src.collector.stream import CURRENT
    from config.settings import STREAM_ENABLED
    if CURRENT is None:
        return {"ticker": ticker.upper(), "live": False,
                "reason": ("стрим выключен" if not STREAM_ENABLED
                           else "стрим ещё не поднялся")}
    snap = CURRENT.agg.snapshot(ticker)
    age = CURRENT.last_msg.get(ticker.upper())
    return {
        "ticker": ticker.upper(), "live": True,
        "last_packet_sec": (round((datetime.now(timezone.utc) - age).total_seconds(), 2)
                            if age else None),
        **snap,
    }


@app.get("/api/stream/health", summary="Жив ли поток и по каким бумагам")
async def stream_health():
    """
    Возраст последнего пакета ПО КАЖДОЙ бумаге.

    Без этого «работает ли сбор» узнаётся только по косвенным признакам. На
    прошлой неделе полдня ушло на то, чтобы понять, что сбор молча стоял, и
    ещё раз — что стакана нет у половины списка. Здесь это видно сразу.

    Ключевые поля: tickers_fresh_60s (сколько бумаг обновлялись за последнюю
    минуту), oldest_sec (самая застоявшаяся), reconnects (обрывы — каждый это
    ДЫРА в данных, а не задвоение).
    """
    from src.collector.stream import CURRENT
    from config.settings import STREAM_ENABLED
    if CURRENT is None:
        return {"enabled": STREAM_ENABLED, "running": False,
                "reason": ("выключен флагом STREAM_ENABLED" if not STREAM_ENABLED
                           else "включён, но ещё не поднялся или упал при старте")}
    return {"enabled": True, "running": True, **CURRENT.health()}


@app.get("/api/feed/sources", summary="Активность источников базы знаний")
async def get_feed_sources(minutes: int = 60):
    """Сколько событий по каждому источнику за последние N минут (панель «Источники»)."""
    return await db.event_source_stats(since_minutes=minutes)


@app.get("/api/health/sources", summary="Живая диагностика источников данных")
async def health_sources():
    """
    Проверяет каждый источник вживую и возвращает статус/ошибки (без секретов).
    Открой этот адрес, чтобы понять, почему какой-то источник пуст.
    """
    from config.settings import (TINKOFF_TOKEN, TELEGRAM_API_ID,
                                  TELEGRAM_STRING_SESSION, TELEGRAM_PROXY)
    out: dict = {}

    # Tinkoff Invest API (по токену; обычно доступен без прокси)
    tk = {"token_set": bool(TINKOFF_TOKEN)}
    if TINKOFF_TOKEN:
        try:
            from src.collector.tinkoff_client import TinkoffClient
            ob = await TinkoffClient().get_orderbook("SBER")
            tk["orderbook_ok"] = bool(ob)
            tk["pressure"] = ob.get("pressure") if ob else None
            if not ob:
                tk["note"] = "нет стакана (рынок закрыт или у токена нет прав на маркет-данные)"
        except Exception as e:
            tk["error"] = str(e)[:250]
    else:
        tk["note"] = "TINKOFF_TOKEN не задан в окружении контейнера"
    out["tinkoff"] = tk

    # MOEX ISS (бесплатно, с задержкой)
    moex = {}
    try:
        from src.collector.moex_price_collector import MOEXPriceCollector
        price = await MOEXPriceCollector().get_current_price("SBER")
        moex["ok"] = price is not None
        moex["sber_last"] = price
    except Exception as e:
        moex["error"] = str(e)[:250]
    out["moex_iss"] = moex

    # Telegram / Пульс — конфиг-подсказки (сеть проверяется в пайплайне)
    out["telegram"] = {
        "api_configured": bool(TELEGRAM_API_ID),
        "string_session": bool(TELEGRAM_STRING_SESSION),
        "proxy_set": bool(TELEGRAM_PROXY),
    }

    # Фаза сессии MOEX (МСК)
    try:
        from src.analysis import intraday as iv
        from datetime import timedelta
        msk = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3)))
        out["session_phase_msk"] = iv.session_phase(msk.hour * 60 + msk.minute,
                                                    msk.weekday())
    except Exception:
        out["session_phase_msk"] = "?"

    # Что реально попало в базу знаний за 60 минут
    counts = (await db.event_source_stats(60)).get("counts", {})
    out["events_60m"] = counts

    # Состояние RSS-источников по последнему обходу. Раньше отказ был невидим:
    # коллектор возвращал пустой список молча, и 30.07 два источника из пяти были
    # мертвы (Финам HTTP 403, БКС страница анти-бота), а health показывал только
    # суммарное число событий.
    try:
        import json as _j
        raw = await db.get_setting("rss_source_health")
        health = _j.loads(raw) if raw else {}
    except Exception:
        health = {}
    if health:
        dead = [n for n, h in health.items() if h.get("status") != "ok"]
        out["rss"] = {"sources": health, "total": len(health),
                      "dead_count": len(dead), "dead": dead}
    else:
        out["rss"] = {"note": "обход RSS ещё не выполнялся в этом процессе"}

    # НАСТРОЕН и ПРОИЗВОДИТ — разные вещи. Telegram может иметь ключи и сессию и
    # при этом не давать ни одного события: 30.07 было 0 при 21 активном канале.
    out["telegram"]["events_60m"] = counts.get("telegram", 0)
    out["telegram"]["producing"] = counts.get("telegram", 0) > 0
    out["pulse"] = {"events_60m": counts.get("pulse", 0) + counts.get("pulse_deal", 0),
                    "producing": (counts.get("pulse", 0) + counts.get("pulse_deal", 0)) > 0}
    silent = [k for k in ("telegram", "pulse") if not out[k]["producing"]]
    if silent:
        out["silent_sources"] = silent
    return out


@app.get("/api/health/figi", summary="Диагностика: какие тикеры реально торгуются (FIGI)")
async def health_figi():
    """
    ЖИВОЙ резолв каждого тикера MOEX_TICKERS через FindInstrument (в обход
    статического кэша) — ловит переименования/делистинги (YNDX→YDEX, POLY/DSKY
    и т.п.). Работает при закрытом рынке. Показывает, по каким символам стакан
    в принципе НЕ соберётся, пока не поправим их в MOEX_TICKERS.
    """
    from config.settings import TINKOFF_TOKEN, MOEX_TICKERS
    if not TINKOFF_TOKEN:
        return {"error": "TINKOFF_TOKEN не задан"}
    from src.collector.tinkoff_client import TinkoffClient
    tk = TinkoffClient()
    live, dead = {}, []
    for t in MOEX_TICKERS:
        try:
            info = await tk.resolve_live(t)
        except Exception:
            info = None
        if info and info.get("figi"):
            live[t] = {"figi": info["figi"], "name": info.get("name")}
        else:
            dead.append(t)
        await asyncio.sleep(0.1)
    return {"total": len(MOEX_TICKERS), "live": len(live), "dead_count": len(dead),
            "dead_tickers": dead, "live_map": live}


@app.get("/api/health/ai", summary="Диагностика Claude/AI-провайдера (сырой ответ)")
async def health_ai():
    """
    Делает ОДИН минимальный запрос к AI-провайдеру и возвращает СЫРОЙ результат/ошибку
    (статус-код + тело), чтобы точно понять причину «Claude недоступен»: неверный ключ
    (401/403), нет модели (404), пустой баланс (402), не тот URL и т.п. Ключ не раскрываем.
    """
    import os as _os, time as _t
    from src.agent.claude_agent import ClaudeAgent, _PROVIDER, _BASE_URL, _MODEL
    key = _os.getenv("ANTHROPIC_API_KEY", "")
    info = {"provider": _PROVIDER, "base_url": _BASE_URL, "model": _MODEL,
            "key_set": bool(key), "key_len": len(key)}
    ag = ClaudeAgent()
    # 1) МАЛЫЙ запрос
    t0 = _t.time()
    try:
        txt = await ag._ask("Ты тест.", "Ответь одним словом: ok", max_tokens=5)
        info["small"] = {"ok": True, "ms": int((_t.time() - t0) * 1000), "reply": (txt or "")[:60]}
    except Exception as e:
        info["small"] = {"ok": False, "ms": int((_t.time() - t0) * 1000), "error": str(e)[:300]}
    # 2) РЕАЛЬНЫЙ синтез: тот же JSON-схема из 17 полей. Тестируем при 1300 (старый
    #    лимит — воспроизводим обрезку) и 3000 (новый). json_ok/ends_brace покажут суть.
    import json as _json
    schema = ("Верни СТРОГО валидный JSON по акции SBER с полями (заполни осмысленным "
              "русским текстом): risk_gate, htf_bias, regime, setup, confluence_score, "
              "confluence_factors, signal, confidence, entry, stop, target, rr, size, "
              "invalidation, summary(2-3 предложения), key_insight(развёрнуто), risk(развёрнуто).")
    for tag, mt in (("synth_1300", 1300), ("synth_3000", 3000)):
        t0 = _t.time()
        try:
            txt = await ag._ask("Ты интрадей-трейдер MOEX.", schema, max_tokens=mt)
            s, e = txt.find("{"), txt.rfind("}") + 1
            json_ok = False
            if s >= 0 and e > s:
                try:
                    _json.loads(txt[s:e]); json_ok = True
                except Exception:
                    json_ok = False
            info[tag] = {"ok": True, "ms": int((_t.time() - t0) * 1000),
                         "reply_len": len(txt or ""), "ends_brace": (txt or "").rstrip().endswith("}"),
                         "json_ok": json_ok}
        except Exception as ex:
            info[tag] = {"ok": False, "ms": int((_t.time() - t0) * 1000), "error": str(ex)[:300]}
    return info


@app.get("/api/health/pulse", summary="Диагностика Пульса: пробить анти-бот через curl_cffi")
async def health_pulse(ticker: str = "SBER"):
    """
    Тестирует, реально ли достать ленту Пульса из ПРОДА (RF-хост, стабильная сеть)
    разными стратегиями curl_cffi (impersonate chrome). Читает — не пишет. По
    результату решаем: переписывать ли коллекторы на curl_cffi или нужен headless.
    """
    try:
        from curl_cffi import requests as cr
    except Exception as e:
        return {"error": f"curl_cffi недоступен: {e}"}

    def _hdrs(host, app=False):
        h = {"Accept": "application/json", "Accept-Language": "ru-RU,ru;q=0.9",
             "Origin": f"https://www.{host}", "Referer": f"https://www.{host}/invest/social/"}
        if app:
            h.update({"x-app-name": "invest", "x-app-version": "1.0.0", "x-request-id": "moodex-diag"})
        return h

    def _api(host):
        return (f"https://www.{host}/api/invest-gw/social/v1/post/instrument"
                f"?ticker={ticker}&limit=3")

    results = []

    def _run(label, fn):
        import time as _t
        t0 = _t.time()
        try:
            r = fn()
            ms = int((_t.time() - t0) * 1000)
            info = {"strategy": label, "http": r.status_code, "ms": ms}
            try:
                d = r.json(); p = d.get("payload") or {}
                it = p.get("items")
                info.update({"status": d.get("status"),
                             "msg": (p.get("message") if isinstance(p, dict) else None),
                             "items": len(it) if isinstance(it, list) else None})
            except Exception:
                info["body"] = r.text[:120]
            results.append(info)
        except Exception as e:
            results.append({"strategy": label, "error": str(e)[:140]})

    for host in ("tbank.ru", "tinkoff.ru"):
        _run(f"{host}|chrome", lambda h=host: cr.get(_api(h), headers=_hdrs(h), impersonate="chrome", timeout=15))
        _run(f"{host}|chrome+apphdrs", lambda h=host: cr.get(_api(h), headers=_hdrs(h, app=True), impersonate="chrome", timeout=15))

        def _primed(h=host):
            s = cr.Session(impersonate="chrome")
            s.get(f"https://www.{h}/invest/social/", timeout=15)   # прогрев куки
            return s.get(_api(h), headers=_hdrs(h, app=True), timeout=15)
        _run(f"{host}|primed", _primed)

    ok = [r for r in results if r.get("items")]
    return {"ticker": ticker, "any_success": bool(ok),
            "winning_strategies": [r["strategy"] for r in ok], "results": results}


@app.get("/api/knowledge/{ticker}", summary="Срез базы знаний по тикеру (для Claude)")
async def get_knowledge(ticker: str, minutes: int = 240):
    """
    Компактный срез знаний по тикеру: последние сообщения/новости/Пульс/сделки +
    последний стакан/цена/поток Tinkoff. Именно это Claude берёт как контекст.
    """
    return await db.knowledge_snapshot(ticker.upper(), since_minutes=minutes)


# ─── Управление каналами ──────────────────────────────────────────────────────

@app.get("/api/channels", summary="Список каналов")
async def get_channels():
    """Получить список всех подключённых каналов"""
    from config.settings import TELEGRAM_CHANNELS
    # Объединяем дефолтные (из конфига) и добавленные вручную (из БД)
    all_channels = []
    for ch in TELEGRAM_CHANNELS:
        all_channels.append({
            "username": ch,
            "status": "active",
            "source": "config",
        })
    all_channels.extend(await db.list_channels())
    return {"channels": all_channels, "count": len(all_channels)}


@app.post("/api/channels", summary="Добавить канал")
async def add_channel(req: ChannelRequest):
    """
    Добавить новый Telegram-канал для мониторинга.
    Аккаунт автоматически вступает в канал.
    """
    if not _collector_ref:
        raise HTTPException(status_code=503, detail="Telegram коллектор не запущен")

    # Очищаем username
    username = req.username.strip()
    username = username.replace("https://t.me/", "").replace("@", "").strip("/")

    # Проверяем дубликаты (в БД и в конфиге)
    from config.settings import TELEGRAM_CHANNELS
    if username in TELEGRAM_CHANNELS or await db.channel_exists(username):
        raise HTTPException(status_code=400, detail=f"Канал @{username} уже подключён")

    # Пробуем вступить в канал
    try:
        entity = await _collector_ref.client.get_entity(username)
        title = getattr(entity, "title", username)
        members = getattr(entity, "participants_count", None)

        # Вступаем в канал
        try:
            from telethon.tl.functions.channels import JoinChannelRequest
            await _collector_ref.client(JoinChannelRequest(entity))
            joined = True
        except Exception:
            joined = False  # Может быть уже состоим или публичный канал

        # Сохраняем в БД
        channel_info = {
            "username": username,
            "title": title,
            "members": members,
            "status": "active",
            "source": "manual",
            "joined": joined,
        }
        await db.upsert_channel(channel_info)

        # Подписываем коллектор на новый канал (перерегистрирует обработчик)
        await _collector_ref.add_channel(username)

        logger.info(f"✅ Канал добавлен: @{username} ({title}), вступили: {joined}")
        return {"success": True, "channel": channel_info}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось добавить @{username}: {str(e)}")


@app.delete("/api/channels/{username}", summary="Удалить канал")
async def remove_channel(username: str):
    """Удалить канал из мониторинга"""
    deleted = await db.delete_channel(username)

    if _collector_ref and username in _collector_ref.channels:
        await _collector_ref.remove_channel(username)

    if deleted:
        return {"success": True, "message": f"Канал @{username} удалён"}
    else:
        raise HTTPException(status_code=404, detail=f"Канал @{username} не найден")


@app.get("/api/channels/search/{query}", summary="Поиск канала в Telegram")
async def search_channel(query: str):
    """Найти канал по username и получить информацию о нём"""
    if not _collector_ref:
        raise HTTPException(status_code=503, detail="Telegram коллектор не запущен")

    username = query.replace("https://t.me/", "").replace("@", "").strip("/")

    try:
        entity = await _collector_ref.client.get_entity(username)
        return {
            "username": username,
            "title": getattr(entity, "title", username),
            "members": getattr(entity, "participants_count", None),
            "about": getattr(entity, "about", ""),
            "found": True,
        }
    except Exception as e:
        return {"found": False, "error": str(e)}


# ─── Технический анализ ────────────────────────────────────────────────────────

@app.get("/api/technical/{ticker}", summary="Технический анализ (MOEX)")
async def get_technical(ticker: str):
    """Технический анализ тикера по данным Московской биржи (свечи, RSI, MACD, тренд)."""
    ticker = ticker.upper()
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    result = await ta.analyze_ticker(ticker)
    if not result:
        raise HTTPException(
            status_code=503,
            detail="Не удалось получить данные MOEX (нет свечей или биржа недоступна)",
        )
    return result.to_dict()


@app.get("/api/technical/{ticker}/candles", summary="Свечи MOEX для графика")
async def get_candles(ticker: str, days: int = 120):
    """Дневные свечи с MOEX ISS для построения графика на дашборде."""
    ticker = ticker.upper()
    try:
        data = await ta.fetch_candles(ticker, days=days)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MOEX недоступен: {e}")
    return {"ticker": ticker, **data}


@app.get("/api/technical/{ticker}/intraday", summary="Интрадей-свечи для графика")
async def get_intraday_candles(ticker: str, tf_min: int = 5):
    """
    Внутридневные свечи (реалтайм Tinkoff при наличии токена, иначе MOEX ISS с
    задержкой). Для интрадей-режима графика на карточке AI-агента.
    """
    ticker = ticker.upper()
    from src.agent import intraday_analyst as ia
    data = await ia.fetch_intraday(ticker, tf_min=tf_min)
    if not data or not data.get("close"):
        raise HTTPException(status_code=503, detail="Нет интрадей-данных (биржа закрыта или нет доступа)")
    return {
        "ticker": ticker, "tf_min": tf_min,
        "delayed": bool(data.get("_delayed")), "source": data.get("_source"),
        "dates": data.get("dates", []), "open": data.get("open", []),
        "high": data.get("high", []), "low": data.get("low", []),
        "close": data.get("close", []), "volume": data.get("volume", []),
    }


@app.get("/api/screen", summary="Триаж: что интересного прямо сейчас (без Claude)")
async def get_screen(limit: int = 25):
    """
    Дешёвый скрин рынка (словари + индикаторы + интрадей) без Claude —
    ранжированный список «интересности». Показывает, что фоновый движок
    отправит на подтверждение Claude.
    """
    from src.agent import screen as screener
    ranked = await screener.screen_all(aggregator)
    return {"screened": len(ranked), "results": ranked[:limit]}


@app.get("/api/universe", summary="Кого вообще смотреть: список по обороту")
async def get_universe(min_turnover_mln: float = None, max_n: int = 80):
    """
    Список бумаг, построенный по ФАКТУ оборота на бирже, и расхождение с
    рукописным списком в настройках.

    Зачем. 31.07 владелец прислал скриншот «Взлёты дня»: десять из пятнадцати
    лидеров роста отсутствовали в системе. MVID рос на 8.29% при обороте
    390 млн, SGZH падал на 4.21% при обороте 637 млн — обеих в списке не было,
    а я в это время докладывал, что лидер дня SMLT с +4.92%.

    Рукописный список стареет молча. Этот маршрут делает расхождение видимым:
    поле `missing` — что биржа торгует, а система не смотрит.

    Порог по умолчанию — ПРОИЗВОДНЫЙ от размера позиции (10 млн ₽ при позиции
    200 тыс.), а не круглые 100 млн, которые стояли здесь раньше. Из-за них
    маршрут отдавал 25 бумаг, и я полдня искал причину в деплое.
    """
    from config.settings import MOEX_TICKERS
    from src.analysis.universe import (MIN_TURNOVER_RUB, cached_universe,
                                       diff_against)

    static = list(MOEX_TICKERS.keys())
    floor = (MIN_TURNOVER_RUB if min_turnover_mln is None
             else min_turnover_mln * 1e6)
    # Обращение к бирже синхронное — уводим в поток, чтобы не блокировать
    # цикл событий на время запроса (таймаут до 30 секунд).
    u = await asyncio.to_thread(cached_universe, floor, max_n, static)
    try:
        d = await asyncio.to_thread(diff_against, static)
    except Exception as e:                                   # noqa: BLE001
        d = {"error": str(e)[:80]}
    return {
        "source": u.get("source"),
        "turnover_floor_mln": round(floor / 1e6, 1),
        "count": len(u.get("tickers") or []),
        "tickers": u.get("tickers"),
        "rows": (u.get("rows") or [])[:max_n],
        "vs_static": d,
    }


@app.get("/api/geopolitics", summary="Геополитический фон рынка")
async def get_geopolitics():
    """Текущий геополитический фон (влияет на весь рынок РФ)."""
    return geo.MONITOR.snapshot()


# ─── AI-агент ──────────────────────────────────────────────────────────────────

@app.get("/api/agent/{ticker}", summary="AI-анализ тикера")
async def get_agent_analysis(ticker: str, save: bool = True):
    """
    Полный анализ AI-агента: настроение + технический анализ → рекомендация
    с обоснованием. Прогноз сохраняется в память (БД) для последующего обучения.
    """
    ticker = ticker.upper()
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    # stage="manual": ручной вызов не должен искажать воронку сканера в журнале.
    return await analyst.analyze(ticker, aggregator, save=save, stage="manual")


@app.get("/api/signals", summary="Лучшие торговые сетапы")
async def get_signals(limit: int = 20, min_rr: float = 1.5):
    """
    Ранжированный список лучших сетапов из фонового сканера:
    боковик у границы / тренд с хорошим R/R и уверенностью.
    Обновляется сканером в фоне (не ходит в MOEX на каждый запрос).
    """
    return {
        "signals": scanner.CACHE.ranked(limit=limit, min_rr=min_rr),
        "by_ticker": scanner.CACHE.by_ticker(),
        "updated_at": scanner.CACHE.updated_at,
        "scanned": len(scanner.CACHE.results),
    }


@app.get("/api/predictions", summary="Прогнозы агента (память)")
async def get_predictions(limit: int = 50, ticker: Optional[str] = None):
    """Последние прогнозы агента с фактическими результатами (если оценены)."""
    preds = await db.list_recent_predictions(limit=limit, ticker=ticker)
    stats = await db.accuracy_stats(ticker=ticker)
    return {"predictions": preds, "stats": stats}


# ─── Бэктест стратегии ──────────────────────────────────────────────────────────

_bt_cache: dict = {}   # {key: (timestamp, result)}
_backfill_status: dict = {"running": False, "message": "не запускался", "summary": None}


async def _run_backfill_task(days: int, per_channel_limit: int, source: str = "telegram"):
    _backfill_status.update({"running": True, "message": f"бэкфилл ({source})..."})
    try:
        def progress(p):
            _backfill_status["message"] = (
                f"[{source}] {p['done']}/{p['total']} ({p['channel']}), "
                f"сообщений: {p['messages']}")
        summaries = []
        if source in ("telegram", "both"):
            if _collector_ref:
                summaries.append(await bf.run_backfill(
                    _collector_ref, days=days, per_channel_limit=per_channel_limit, progress=progress))
            else:
                summaries.append({"telegram": "коллектор не запущен"})
        if source in ("pulse", "both"):
            summaries.append(await bf.run_pulse_backfill(days=days, progress=progress))
        _backfill_status.update({"running": False, "summary": summaries,
                                 "message": f"готово: {summaries}"})
    except Exception as e:
        _backfill_status.update({"running": False, "message": f"ошибка: {e}"})


@app.post("/api/backfill", summary="Выкачать историю настроений из чатов")
async def start_backfill(days: int = 730, per_channel_limit: int = 3000, source: str = "telegram"):
    """
    Запустить в фоне выкачивание истории и разметку дневного настроения в БД.
    source: telegram | pulse | both. Telegram требует подключённый коллектор.
    """
    if source in ("telegram", "both") and not _collector_ref:
        if source == "telegram":
            raise HTTPException(status_code=503, detail="Telegram коллектор не запущен")
    if _backfill_status["running"]:
        return {"status": "already_running", **_backfill_status}
    asyncio.create_task(_run_backfill_task(days, per_channel_limit, source))
    return {"status": "started", "days": days, "source": source}


@app.get("/api/backfill/status", summary="Статус бэкфилла")
async def get_backfill_status():
    days = await db.sentiment_history_days()
    return {**_backfill_status, "sentiment_history_days": days}


@app.get("/api/backtest/{ticker}", summary="Бэктест стратегии по тикеру")
async def run_backtest(ticker: str, days: int = 500, sentiment: bool = False):
    """
    Прогнать стратегию по историческим свечам MOEX. При sentiment=true и наличии
    накопленной истории настроений сравнивает результат БЕЗ и С фильтром настроения.
    """
    ticker = ticker.upper()
    try:
        data = await ta.fetch_candles(ticker, days=days)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MOEX недоступен: {e}")
    if len(data.get("close", [])) < 60:
        raise HTTPException(status_code=503, detail="Мало исторических данных для бэктеста")

    base = bt.backtest(data["close"], data["high"], data["low"], data["dates"])
    result = {"ticker": ticker, "days": days, **base}

    if sentiment:
        hist = await db.sentiment_history(ticker=ticker)
        smap = {h["date"]: h["avg_signal"] for h in hist}
        if smap:
            with_sent = bt.backtest(
                data["close"], data["high"], data["low"], data["dates"],
                params={"mode": "both", "use_sentiment": True}, sentiment_map=smap)
            result["with_sentiment"] = {
                "total_return_pct": with_sent["total_return_pct"],
                "trades_count": with_sent["trades_count"],
                "win_rate": with_sent["win_rate"],
                "expectancy_r": with_sent["expectancy_r"],
                "out_sample": with_sent["out_sample"],
                "profit_factor": with_sent["profit_factor"],
            }
            result["sentiment_days_available"] = len(smap)
        else:
            result["sentiment_days_available"] = 0
    return result


@app.get("/api/backtest-portfolio", summary="Бэктест по топ-бумагам")
async def run_backtest_portfolio(days: int = 500):
    """
    Прогнать стратегию по корзине ликвидных бумаг и вернуть сводку.
    Результат кешируется на час (тяжёлая операция).
    """
    import time
    key = f"pf:{days}"
    now = time.time()
    if key in _bt_cache and now - _bt_cache[key][0] < 3600:
        return _bt_cache[key][1]

    tickers = ["SBER", "GAZP", "LKOH", "GMKN", "YNDX", "ROSN", "VTBR",
               "TATN", "PLZL", "MGNT", "NVTK", "CHMF"]
    rows = []
    for t in tickers:
        try:
            data = await ta.fetch_candles(t, days=days)
            if len(data.get("close", [])) < 60:
                continue
            r = bt.backtest(data["close"], data["high"], data["low"], data["dates"])
            rows.append({
                "ticker": t, "trades": r["trades_count"], "win_rate": r["win_rate"],
                "return_pct": r["total_return_pct"], "max_dd": r["max_drawdown_pct"],
                "profit_factor": r["profit_factor"], "expectancy_r": r["expectancy_r"],
            })
        except Exception as e:
            logger.debug(f"backtest {t}: {e}")

    total_trades = sum(r["trades"] for r in rows)
    wr = [r["win_rate"] for r in rows if r["win_rate"] is not None]
    exp = [r["expectancy_r"] for r in rows if r["expectancy_r"] is not None]
    ret = [r["return_pct"] for r in rows if r["return_pct"] is not None]
    dd = [r["max_dd"] for r in rows if r["max_dd"] is not None]
    summary = {
        "tickers": rows,
        "aggregate": {
            "instruments": len(rows),
            "total_trades": total_trades,
            "avg_win_rate": round(sum(wr) / len(wr), 1) if wr else None,
            "avg_expectancy_r": round(sum(exp) / len(exp), 3) if exp else None,
            "avg_return_pct": round(sum(ret) / len(ret), 2) if ret else None,
            "avg_max_dd_pct": round(sum(dd) / len(dd), 2) if dd else None,
        },
        "days": days,
    }
    _bt_cache[key] = (now, summary)
    return summary


@app.get("/api/sentiment-study", summary="Связь настроения и цены за 2 года")
async def sentiment_study(ticker: Optional[str] = None, days: int = 730):
    """
    Изучить на истории, как дневное настроение связано с последующим движением
    цены (горизонты 1/5/10 дней). Пул по корзине или конкретный тикер. Кеш 1ч.
    """
    import time
    key = f"study:{ticker}:{days}"
    now = time.time()
    if key in _bt_cache and now - _bt_cache[key][0] < 3600:
        return _bt_cache[key][1]

    tickers = [ticker.upper()] if ticker else \
        ["SBER", "GAZP", "LKOH", "GMKN", "YNDX", "ROSN", "VTBR",
         "TATN", "PLZL", "MGNT", "NVTK", "CHMF"]
    horizons = [1, 5, 10]
    pooled = {h: [] for h in horizons}
    used = 0
    for t in tickers:
        try:
            hist = await db.sentiment_history(ticker=t)
            if len(hist) < 15:
                continue
            data = await ta.fetch_candles(t, days=days)
            if len(data.get("close", [])) < 30:
                continue
            used += 1
            for h in horizons:
                pooled[h].extend(rs.forward_samples(hist, data["dates"], data["close"], h))
        except Exception as e:
            logger.debug(f"study {t}: {e}")

    results = []
    for h in horizons:
        summ = rs.summarize(pooled[h])
        results.append({"horizon": h, **summ, "interpretation": rs.interpret(summ.get("corr"))})

    result = {
        "scope": ticker.upper() if ticker else "portfolio",
        "instruments_used": used,
        "horizons": results,
        "note": "Изучена реальная связь настроения с будущим движением цены. "
                "Знак корреляции: моментум (толпа права) или контртренд (толпа против).",
    }
    _bt_cache[key] = (now, result)
    return result


@app.get("/api/strategy-lab", summary="Лаборатория стратегий (сравнение вне выборки)")
async def strategy_lab(days: int = 600):
    """
    Прогнать несколько принципиально разных вариантов стратегии по корзине бумаг
    и сравнить их по метрике ВНЕ ВЫБОРКИ (честный поиск преимущества, не подгонка).
    Кешируется на час.
    """
    import time
    key = f"lab:{days}"
    now = time.time()
    if key in _bt_cache and now - _bt_cache[key][0] < 3600:
        return _bt_cache[key][1]

    tickers = ["SBER", "GAZP", "LKOH", "GMKN", "YNDX", "ROSN", "VTBR",
               "TATN", "PLZL", "MGNT", "NVTK", "CHMF"]
    portfolio = []
    for t in tickers:
        try:
            data = await ta.fetch_candles(t, days=days)
            if len(data.get("close", [])) >= 120:
                portfolio.append({"ticker": t, "closes": data["close"],
                                  "highs": data["high"], "lows": data["low"], "dates": data["dates"]})
        except Exception as e:
            logger.debug(f"lab fetch {t}: {e}")

    variants = []
    for vid, v in bt.VARIANTS.items():
        res = bt.evaluate_variant(portfolio, v["params"])
        out = res["out_sample"]
        variants.append({
            "id": vid, "label": v["label"],
            "in_sample": res["in_sample"], "out_sample": out,
            "best_tickers": [p for p in res["per_ticker"]
                             if (p["out_expectancy_r"] or -9) > 0 and p["out_trades"] >= 3][:5],
        })
    # Рейтинг по матожиданию вне выборки (при достаточном числе сделок)
    def rank_key(v):
        o = v["out_sample"]
        if not o["trades"] or o["trades"] < 20 or o["expectancy_r"] is None:
            return -9
        return o["expectancy_r"]
    variants.sort(key=rank_key, reverse=True)

    result = {
        "instruments": len(portfolio), "days": days, "variants": variants,
        "note": "Судим по OUT-OF-SAMPLE (вне выборки). Вариант надёжен, только если там матожидание > 0 при ≥20 сделках.",
    }
    _bt_cache[key] = (now, result)
    return result


@app.get("/api/agent/{ticker}/chart", summary="Визуальный анализ графика через Claude Vision")
async def get_chart_analysis(ticker: str):
    """
    Claude смотрит на свечной график и делает визуальный технический анализ:
    паттерны, уровни, тренд, сигнал RSI — всё что видно только глазами.
    """
    from src.agent.chart_generator import generate_chart_b64
    ticker = ticker.upper()
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")

    try:
        candles = await ta.fetch_candles(ticker, days=120)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MOEX недоступен: {e}")

    chart_b64 = await generate_chart_b64(
        ticker=ticker,
        closes=candles.get("close", []),
        highs=candles.get("high", []),
        lows=candles.get("low", []),
        opens=candles.get("open", []),
        dates=candles.get("dates", []),
        days=120,
    )
    if not chart_b64:
        raise HTTPException(status_code=503, detail="Не удалось сгенерировать график (mplfinance не установлен?)")

    idx = aggregator.get_ticker_index(ticker)
    result = await claude.analyze_chart(
        ticker=ticker,
        image_b64=chart_b64,
        sentiment_index=idx.sentiment_index if idx else None,
    )
    return result




# Хранилище статуса бэктеста (один за раз)
_bt_claude_status: dict = {"running": False, "progress": None, "result": None, "error": None}


@app.post("/api/backtest-claude/{ticker}", summary="Запустить настоящий бэктест Claude")
async def start_claude_backtest(
    ticker: str,
    hold_days: int = 10,
    min_confidence: int = 50,
    max_calls: int = 60,
    commission: float = 0.05,
    atr_stop: float = 1.5,
    atr_target: float = 3.0,
    require_agreement: bool = True,
    block_counter_trend: bool = True,
    dry_run: bool = False,
):
    """
    Запускает реальный бэктест: Claude анализирует каждую историческую дату,
    сделки исполняются с риск-менеджментом (ATR-стоп/цель, intrabar-выход).
    Фоновый процесс — результат получить через GET /api/backtest-claude/status

    dry_run=true прогоняет ту же механику без вызовов Claude (решение из техсигнала) —
    для быстрой офлайн-проверки логики и фильтров.
    """
    from src.agent.historical_backtest import run_real_claude_backtest

    ticker = ticker.upper()
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    if _bt_claude_status["running"]:
        return {"status": "already_running", **_bt_claude_status}

    _bt_claude_status.update({"running": True, "progress": None, "result": None, "error": None})

    def on_progress(p):
        _bt_claude_status["progress"] = p

    async def run():
        try:
            result = await run_real_claude_backtest(
                ticker=ticker,
                hold_days=hold_days,
                min_confidence=min_confidence,
                max_calls=max_calls,
                commission_pct=commission,
                atr_stop_mult=atr_stop,
                atr_target_mult=atr_target,
                require_tech_agreement=require_agreement,
                block_counter_trend=block_counter_trend,
                dry_run=dry_run,
                progress_callback=on_progress,
            )
            _bt_claude_status["result"] = result
        except Exception as e:
            _bt_claude_status["error"] = str(e)
        finally:
            _bt_claude_status["running"] = False

    asyncio.create_task(run())
    return {"status": "started", "ticker": ticker, "max_calls": max_calls, "dry_run": dry_run}


@app.get("/api/backtest-claude/status", summary="Статус и результат бэктеста Claude")
async def get_claude_backtest_status():
    return _bt_claude_status


@app.get("/api/accuracy", summary="Честная точность: калибровка и ожидание в R")
async def get_accuracy(ticker: Optional[str] = None):
    """Статистика точности прогнозов агента (основа для оценки качества).

    ВНИМАНИЕ: у этой функции не было декоратора маршрута, поэтому самый полный
    расчёт качества — калибровка уверенности по корзинам и ожидание в R —
    считался, но наружу не отдавался. Снаружи были видны только две цифры из
    него в /api/live-signals. Теперь эндпоинт открыт.
    """
    return await db.accuracy_stats(ticker=ticker)


@app.post("/api/signals/{pred_id}/decision",
          summary="Решение человека по сценарию: accept / reject / wait")
async def post_signal_decision(pred_id: int, decision: str, note: Optional[str] = None):
    """Записать ваше решение по сценарию Claude (advisory-режим).

    Claude предлагает — решаете вы. Решение сохраняется, чтобы потом можно было
    сравнить: где прав Claude, где правы вы, и добавляет ли ваше вето ценность.
    Исход при этом считается для ВСЕХ сценариев одинаково, независимо от
    решения, — иначе сравнение было бы смещённым.

    Менять решение можно, пока сценарий не оценён. После оценки — нельзя:
    «переголосование» задним числом обесценило бы всю статистику.
    """
    try:
        updated = await db.set_human_decision(pred_id, decision, note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"прогноз {pred_id} не найден")
    return {"status": "ok", "prediction": updated}


@app.get("/api/setup-watch", summary="Быстрый наблюдатель сетапов: состояние и срабатывания")
async def setup_watch_status():
    """Состояние наблюдателя и последние срабатывания.

    Наблюдатель НЕ обращается к Claude, поэтому может ходить раз в 5 минут
    бесплатно. Смысл в скорости: сетап пробоя по Мечелу 30.07 сработал в 13:25, а
    сканер с подтверждением увидел бы его в 14:10 — опоздание стоило половины
    прибыли.
    """
    from src.agent import setup_watcher
    return setup_watcher.status()


@app.get("/api/analyst-brief/{ticker}", summary="Полная сводка по бумаге для аналитика")
async def analyst_brief(ticker: str):
    """ВСЁ, что нужно для решения по бумаге, одним вызовом.

    Нужен потому, что данные лежали в разных местах и собрать картину означало
    сделать шесть запросов и ничего не забыть. 30.07 я построил оценку на цене и
    средних, а стакан, поток и новости не посмотрел вовсе — при том что весь день
    чинил именно эти источники.

    Рядом с каждым числом стоит его свежесть, а список gaps прямо называет, чего не
    хватает: молчаливое отсутствие данных было главным источником неверных выводов.
    """
    from src.agent import analyst_brief as ab
    return await ab.build(ticker)


@app.get("/api/analyst-memory", summary="Память аналитика: правила и послужной список")
async def analyst_memory(days: int = 30):
    """Чему научили мои же сигналы: правила из разборов, track record, уроки.

    Обучение без памяти невозможно — каждая сессия начиналась бы с чистого листа, и
    одни и те же ошибки повторялись бы. Читать в начале работы.
    """
    from src.agent import analyst_memory as am
    return await am.build(days)


@app.get("/api/setup-stats", summary="Живая статистика наблюдений по сетапам")
async def setup_stats(days: int = 30):
    """Что сетапы дали НА ЖИВЫХ данных, а не в бэктесте.

    Сетап пробоя работает в режиме наблюдения: проверка на 181 торговом дне
    (январь-июль 2026) показала, что лонговая сторона убыточна во всех
    конфигурациях после издержек, лучшая -0.113R. Те +0.264R на 18 днях июля были
    эффектом режима. Решение включать его сигналом должно опираться на эти живые
    числа, а не на историю одного режима.
    """
    from src.agent import setup_watcher
    return await setup_watcher.stats(days)


@app.post("/api/setup-watch/evaluate", summary="Посчитать исходы наблюдений")
async def setup_watch_evaluate(after_min: Optional[int] = None):
    from src.agent import setup_watcher
    return await setup_watcher.evaluate_observations(after_min)


@app.post("/api/setup-watch/start", summary="Запустить наблюдателя сетапов")
async def setup_watch_start(interval_min: Optional[int] = None):
    from src.agent import setup_watcher
    return setup_watcher.start(interval_min)


@app.post("/api/setup-watch/stop", summary="Остановить наблюдателя сетапов")
async def setup_watch_stop():
    from src.agent import setup_watcher
    return setup_watcher.stop()


@app.post("/api/setup-watch/pass", summary="Один проход наблюдателя прямо сейчас")
async def setup_watch_pass():
    """Разовый проход без запуска цикла — для проверки и ручного опроса."""
    from src.agent import setup_watcher
    fires = await setup_watcher.one_pass()
    return {"fired": len(fires), "fires": fires, "status": setup_watcher.status()}


@app.post("/api/signals/{pred_id}/correct-position",
          summary="Пересчитать снимок позиции по записанным уровням")
async def correct_signal_position(pred_id: int, payload: dict = Body(default={})):
    """Привести рублёвый снимок позиции к записанным уровням. Работает и после оценки.

    Уровни решения после оценки неприкосновенны — их правка переписывает историю.
    Но снимок позиции (акции, рубли риска) — учётный факт об исполнении: он не
    влияет ни на R, ни на признак correct, а только масштабирует рубли, и законно
    может стать известен позже. У сигнала 664 снимок был посчитан от входа 39.00, и
    журнал занижал убыток вдвое.

    shares — если фактический объём отличался от расчётного.
    """
    res = await db.correct_prediction_position(
        pred_id, shares=payload.get("shares"), note=str(payload.get("note") or ""))
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("reason"))
    return res


@app.post("/api/signals/{pred_id}/correct", summary="Исправить уровни сигнала до оценки")
async def correct_signal_levels(pred_id: int, payload: dict = Body(...)):
    """Привести записанные уровни в соответствие с фактическим исполнением.

    Оценка считает R-мультипликатор по entry и stop ИЗ ЗАПИСИ, а не из контекста.
    Если аналитик записал вход 39.00, а сделка исполнена по 39.40, то R, ожидание и
    точность считаются по цене, которой не было — и обучение идёт на выдуманных
    числах. Пометки в контексте для этого мало.

    Меняем только до оценки. Прежние значения сохраняются в контексте как след
    правки: аудит должен видеть и что было, и почему изменили.
    """
    res = await db.correct_prediction_levels(
        pred_id,
        entry=payload.get("entry"), stop=payload.get("stop"),
        target=payload.get("target"), note=str(payload.get("note") or ""))
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("reason"))
    return res


@app.post("/api/analyst-signal", summary="Сценарий от внешнего аналитика в общий журнал")
async def post_analyst_signal(payload: dict = Body(...)):
    """Принять торговый сценарий от внешнего аналитика.

    Нужен, потому что прогноз в системе создавался только внутренним путём
    Claude. Пока бюджет API не пополнен, роль аналитика выполняет внешняя
    модель, и её сценарии обязаны попадать в ТОТ ЖЕ журнал: с планом, размером
    от Risk Engine, оценкой по исходу и сравнением с решением человека. Иначе
    это мнение в переписке, а не сделка.

    Внешний сценарий проходит ТЕ ЖЕ проверки, что внутренний: полнота плана,
    лимиты риска, стоп против спреда, глубина стакана. Обходного пути нет —
    иначе сравнить внешнего аналитика с API на равных будет невозможно.

    Обязательные поля: ticker, direction (up|down), entry, stop, target,
    confidence (>0), reason, invalidation. Поле analyst — ось сравнения
    (hyperagent | claude-api | human).
    """
    from src.agent import external_signal
    result = await external_signal.submit(payload or {})
    if result.get("status") == "rejected":
        code = 409 if result.get("stage") == "already_open" else 422
        raise HTTPException(status_code=code, detail=result)
    return result


@app.get("/api/paper-account", summary="Виртуальный счёт: paper trading по принятым сделкам")
async def get_paper_account():
    """Состояние виртуального счёта и активных лимитов риска.

    Счёт двигают ТОЛЬКО принятые сделки: отклонённый убыточный сигнал не должен
    ни уменьшать капитал, ни съедать дневной лимит — вы его не брали. Оцениваются
    при этом все сценарии, но это про качество сигналов, а не про деньги.

    Считается полным пересчётом по закрытым принятым сделкам, поэтому
    идемпотентно: повторный проход оценщика не может удвоить убыток и ложно
    уронить kill switch.
    """
    from src.risk import engine as _risk
    cfg = _risk.load_config()
    acc = await _risk.compute_paper_account(cfg)
    state = await _risk.load_state(cfg)
    gate = _risk.check_limits(state, cfg)
    acc["limits"] = {
        "trading_allowed": gate.approved,
        "blocking_reason": None if gate.approved else gate.reason,
        "blocking_detail": None if gate.approved else gate.detail,
        "risk_per_trade_pct": cfg.risk_per_trade_pct,
        "daily_loss_limit_r": cfg.daily_loss_limit_r,
        "weekly_loss_limit_r": cfg.weekly_loss_limit_r,
        "max_trades_per_day": cfg.max_trades_per_day,
        "max_open_positions": cfg.max_open_positions,
        "kill_switch_dd_pct": cfg.kill_switch_dd_pct,
        "open_positions": state.open_positions,
        "open_exposure_rub": round(state.open_exposure_rub, 2),
    }
    return acc


@app.get("/api/regime-audit", summary="Аудит детектора режима: метка против реальности")
async def get_regime_audit():
    """Совпадала ли метка режима с фактическим движением цены.

    Зачем: порог ADX >= 25 в technical.py объявляет трендом только выраженное
    движение. Плавный устойчивый рост держит ADX в низких двадцатых и получает
    метку «боковик», после чего стратегия боковика фадит верхнюю границу — то
    есть шортит хаи растущего рынка. Отчёт показывает, происходит ли это на
    фактических данных, и позволяет проверить любую правку детектора.

    Отчёт САМ предупреждает о своей выборке: при коротком окне и одном режиме
    рынка вывод касается метки режима, а не качества стратегии. Фейд хаёв обязан
    терять в растущем рынке — это тавтология, а не открытие.
    """
    rows = await db.regime_audit_rows()
    return db.analyze_regime_audit(rows)


@app.get("/api/decisions", summary="Claude против человека: чей вклад в результат")
async def get_decisions(ticker: Optional[str] = None):
    """Сравнение решений Claude и человека в R.

    Главное число — `human_edge_r`: ожидание по ПРИНЯТЫМ сделкам минус ожидание
    по ВСЕМ предложениям. Больше нуля — ваше вето добавляет ценность, меньше
    нуля — отнимает. Пока доля решённых сценариев (`decided_share`) низкая,
    читать сравнение нельзя: нерешённые — не случайная подвыборка.
    """
    return await db.decision_stats(ticker=ticker)


def _bucket(items: list[dict]) -> dict:
    """Срез точности по подвыборке прогнозов.

    Записи с direction=flat или confidence=0 — это отказ от мнения, а не
    прогноз, и в знаменатель они не идут: считать их ошибкой, когда цена
    сдвинулась, некорректно. Тот же принцип, что в db.accuracy_stats.
    """
    def _directional(p: dict) -> bool:
        return (str(p.get("direction") or "").lower() in ("up", "down")
                and (p.get("confidence") or 0) > 0)

    scored = [p for p in items if p.get("correct") is not None]
    ev = [p for p in scored if _directional(p)]
    cor = [p for p in ev if p["correct"]]
    abstained = len(scored) - len(ev)
    return {
        "total": len(items),
        "evaluated": len(ev),
        "abstained": abstained,
        "correct": len(cor),
        "accuracy": round(len(cor) / len(ev), 3) if ev else None,
    }


def _avg(vals: list) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


@app.get("/api/regime-stats", summary="Эффективность по режимам (Фаза B)")
async def get_regime_stats():
    """
    Винрейт, средняя доходность, средний R и средний конфлюенс — В РАЗРЕЗЕ РЕЖИМА
    дня (trend/range/squeeze_breakout/news_spike). Измерение плейбука: какие режимы
    реально дают эйдж. Наполняется по мере оценки прогнозов.
    """
    return await db.regime_stats()


@app.get("/api/track-record", summary="Трек-рекорд агента (флагман)")
async def get_track_record(limit: int = 500):
    """
    Расширенная статистика прогнозов: общая точность, разбивка по направлению
    и уверенности, средняя доходность верных/неверных прогнозов, история.
    """
    preds = await db.list_recent_predictions(limit=limit)
    overall = _bucket(preds)

    by_direction = {d: _bucket([p for p in preds if p["direction"] == d])
                    for d in ("up", "down", "flat")}
    high_conf = _bucket([p for p in preds if (p.get("confidence") or 0) >= 0.5])
    low_conf = _bucket([p for p in preds if (p.get("confidence") or 0) < 0.5])

    evaluated = [p for p in preds if p.get("correct") is not None]
    correct = [p for p in evaluated if p["correct"]]
    wrong = [p for p in evaluated if not p["correct"]]

    # Вклад настроения (форвард-тест): точность, когда настроение согласно с
    # техникой, против случаев, когда они расходятся.
    both = [p for p in evaluated
            if p.get("sentiment_signal") is not None and p.get("technical_score") is not None
            and p["sentiment_signal"] != 0 and p["technical_score"] != 0]
    agree = [p for p in both if (p["sentiment_signal"] > 0) == (p["technical_score"] > 0)]
    disagree = [p for p in both if (p["sentiment_signal"] > 0) != (p["technical_score"] > 0)]

    return {
        "overall": overall,
        "by_direction": by_direction,
        "by_confidence": {"high": high_conf, "low": low_conf},
        "sentiment_effect": {"agree": _bucket(agree), "disagree": _bucket(disagree)},
        "sentiment_history_days": await db.sentiment_history_days(),
        "avg_return_correct": _avg([p.get("realized_return") for p in correct]),
        "avg_return_wrong": _avg([p.get("realized_return") for p in wrong]),
        "avg_confidence": _avg([p.get("confidence") for p in preds]),
        "recent": preds[:40],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/agent/learn", summary="Оценить прогнозы и переобучить модель")
async def trigger_learning():
    """
    Запустить цикл обучения вручную: оценить прогнозы с истёкшим горизонтом
    по фактической цене и переобучить веса модели.
    """
    result = await analyst.evaluate_due_predictions()
    weights = await analyst._load_weights()
    return {**result, "model_weights": [round(w, 3) for w in weights]}


@app.get("/api/lessons", summary="Разбор ошибок и извлечённые уроки")
async def get_lessons(ticker: Optional[str] = None, limit: int = 15):
    """
    Журнал разбора закрытых сигналов (post-mortem): по каждому — причина
    успеха/провала и урок, плюс частота типовых причин ошибок. Эти уроки
    автоматически подмешиваются в промпт будущих прогнозов.
    """
    lessons = await db.recent_lessons(ticker=ticker, limit=limit)
    tag_stats = await db.lesson_tag_stats()
    return {
        "lessons": lessons,
        "count": len(lessons),
        "tag_stats": tag_stats,
    }


@app.post("/api/agent/post-mortem", summary="Разобрать закрытые сигналы сейчас")
async def trigger_post_mortem(limit: int = 25):
    """Разово сформировать разбор по оценённым, но ещё не разобранным прогнозам."""
    analyzed = await analyst.generate_post_mortems(limit=limit)
    return {"analyzed": analyzed}


# ─── Live-сигналы (форвард-тест в реальном времени) ───────────────────────────
#
# Почему это нужно: полный контекст (стакан, новости, макро) нельзя восстановить
# на прошлую дату, поэтому честно оценить систему можно только ВПЕРЁД. Движок по
# расписанию (или по кнопке) генерирует сигналы Claude, ФИКСИРУЕТ их в БД с меткой
# времени и ценой, а когда проходит горизонт — автоматически сверяет с фактической
# ценой. Так копится трек-рекорд, который невозможно подделать задним числом.

_live_status: dict = {
    "enabled": False,          # включён ли авто-цикл
    "running": False,          # крутится ли фоновая задача
    "interval_min": 15,        # период сканирования, мин (Claude каждые 15 мин)
    "tickers": None,           # None = все тикеры
    "last_scan": None,
    "last_eval": None,
    "next_scan": None,
    "scanned": 0,              # тикеров в кеше сканера
    "saved": 0,                # сохранено сигналов в последнем цикле
    "evaluated_total": 0,      # всего оценено прогнозов за сессию движка
    "error": None,
}

# Отдельный always-on контур ОБУЧЕНИЯ (оценка прогнозов без Claude). Работает
# независимо от сканера — самообучение продолжается, даже когда сканер выключен.
_learning_status: dict = {
    "enabled": False,
    "running": False,
    "interval_min": 30,
    "last_eval": None,
    "evaluated_total": 0,
    "error": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _live_scan_once() -> dict:
    """
    Один цикл СКАНЕРА (ручной вкл/выкл). ТОЛЬКО сканирование + подтверждение Claude,
    и ТОЛЬКО в торговую сессию (main/evening). Оценку прогнозов делает отдельный
    always-on learning-контур (_learning_loop) — он работает и при выключенном сканере.
    """
    from datetime import timedelta as _td
    from src.analysis import intraday as _iv
    _msk = datetime.now(timezone.utc).astimezone(timezone(_td(hours=3)))
    _phase = _iv.session_phase(_msk.hour * 60 + _msk.minute, _msk.weekday())
    if _phase not in ("main", "evening"):
        _live_status["last_scan"] = _now_iso()
        _live_status["scanned"] = 0
        _live_status["saved"] = 0
        _live_status["skipped"] = f"вне сессии ({_phase}) — Claude не звали"
        return {"saved": 0, "skipped": _phase}

    _live_status["skipped"] = None
    from config.settings import (SCAN_MIN_INTEREST, SCAN_MAX_CLAUDE,
                                 BATCH_SCAN_ENABLED, BATCH_SCAN_MAX_DEEP)
    if BATCH_SCAN_ENABLED:
        # Batch-скрин: 1 вызов Claude по всем тикерам (общая картина) → глубокий
        # разбор по шортлисту. Дёшево и ничего не теряется в триаже.
        triage = await scanner.scan_batch(
            aggregator, tickers=_live_status["tickers"], save=True,
            max_deep=BATCH_SCAN_MAX_DEEP)
        _live_status["batch_watch"] = triage.get("batch_watch", 0)
        _live_status["shortlist"] = triage.get("shortlist", [])
        _live_status["budget"] = triage.get("budget")
        if triage.get("skipped"):
            _live_status["skipped"] = triage["skipped"]
    else:
        triage = await scanner.scan_interesting(
            aggregator, tickers=_live_status["tickers"], save=True,
            min_interest=SCAN_MIN_INTEREST, max_claude=SCAN_MAX_CLAUDE)
        _live_status["skipped_open"] = triage.get("skipped_open", [])
    saved = triage.get("claude_confirmed", 0)
    _live_status["last_scan"] = _now_iso()
    _live_status["scanned"] = triage.get("screened", 0)
    _live_status["interesting"] = triage.get("interesting", [])
    _live_status["saved"] = saved
    return {"saved": saved}


async def _live_loop():
    _live_status["running"] = True
    try:
        while _live_status["enabled"]:
            try:
                await _live_scan_once()
                _live_status["error"] = None
            except Exception as e:
                _live_status["error"] = str(e)
                logger.warning(f"live-signals цикл: {e}")
            interval = max(5, _live_status["interval_min"]) * 60
            from datetime import timedelta
            _live_status["next_scan"] = (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()
            slept = 0
            while _live_status["enabled"] and slept < interval:
                await asyncio.sleep(2)
                slept += 2
    finally:
        _live_status["running"] = False
        _live_status["next_scan"] = None


async def _learning_once() -> dict:
    """
    Ядро самообучения БЕЗ Claude: оценить созревшие прогнозы по фактической цене.
    Обновляет точность, реализованный R и regime-stats. Работает всегда — и когда
    сканер выключен — поэтому обучение не прерывается.
    """
    ev = await analyst.evaluate_due_predictions()
    _learning_status["last_eval"] = _now_iso()
    _learning_status["evaluated_total"] += ev.get("evaluated", 0)
    return ev


async def _learning_loop():
    _learning_status["running"] = True
    try:
        while _learning_status["enabled"]:
            try:
                await _learning_once()
                _learning_status["error"] = None
            except Exception as e:
                _learning_status["error"] = str(e)
                logger.warning(f"learning-цикл: {e}")
            interval = max(5, _learning_status["interval_min"]) * 60
            slept = 0
            while _learning_status["enabled"] and slept < interval:
                await asyncio.sleep(2)
                slept += 2
    finally:
        _learning_status["running"] = False


@app.post("/api/live-signals/start", summary="Запустить авто-генерацию live-сигналов")
async def live_start(interval_min: int = 15, tickers: Optional[str] = None):
    """
    Включает фоновый цикл: каждые interval_min минут система генерирует сигналы,
    фиксирует их в БД и оценивает созревшие. tickers — список через запятую
    (по умолчанию все). ⚠️ Каждый цикл делает реальные вызовы Claude/MOEX.
    """
    if _live_status["running"]:
        return {"status": "already_running", **_live_status}
    _live_status.update({
        "enabled": True,
        "interval_min": max(5, interval_min),
        "tickers": [t.strip().upper() for t in tickers.split(",") if t.strip()] if tickers else None,
        "error": None,
    })
    asyncio.create_task(_live_loop())
    return {"status": "started", **_live_status}


@app.post("/api/live-signals/stop", summary="Остановить авто-генерацию live-сигналов")
async def live_stop():
    _live_status["enabled"] = False
    return {"status": "stopping", **_live_status}


@app.get("/api/ai/budget", summary="Расход Claude за день (бюджет-гард)")
async def get_ai_budget():
    """Сколько ₽ Claude потратил за МСК-день и сколько осталось до дневного лимита."""
    from src.agent.claude_agent import budget_state, can_afford_deep
    st = await budget_state()
    st["deep_affordable"] = await can_afford_deep()
    return st


@app.get("/api/signal-attempts", summary="Журнал попыток: воронка сигналов и её цена")
async def get_signal_attempts(hours: int = 24, limit: int = 200,
                              ticker: Optional[str] = None):
    """
    ПОЧЕМУ нет сигналов — с точностью до фильтра.

    Раньше в БД попадали только направленные сигналы Claude, поэтому «за день 0
    сигналов» было необъяснимо: не видно, звали ли Claude, что он ответил и какой
    фильтр всё съел. Здесь каждая попытка с кодом причины и фактической ценой
    вызова в ₽ — плюс воронка и ₽ за сохранённый сигнал.
    """
    stats = await db.signal_attempt_stats(hours=hours)
    stats["attempts"] = await db.recent_signal_attempts(
        hours=hours, limit=limit, ticker=ticker)
    return stats


@app.get("/api/learning", summary="Статус контура обучения (always-on)")
async def get_learning_status():
    """Оценка прогнозов идёт независимо от сканера — самообучение не прерывается,
    даже когда сканер выключен (Claude не тратится)."""
    return _learning_status


@app.post("/api/live-signals/scan-now", summary="Сгенерировать сигналы один раз сейчас")
async def live_scan_now():
    """Разовый прогон: сгенерировать+зафиксировать сигналы и оценить созревшие."""
    if _live_status.get("scan_now_running"):
        return {"status": "already_running", **_live_status}
    _live_status["scan_now_running"] = True
    try:
        r = await _live_scan_once()
        return {"status": "ok", **r, **_live_status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _live_status["scan_now_running"] = False


@app.get("/api/live-signals", summary="Открытые live-сигналы + статус движка")
async def get_live_signals(limit: int = 200):
    """
    Открытые (ещё не оценённые) сигналы, зафиксированные в БД, обогащённые текущим
    торговым планом и обоснованием Claude из кеша сканера. Плюс статус движка и
    краткая сводка форвард-точности.
    """
    from datetime import timedelta
    preds = await db.list_recent_predictions(limit=limit)
    open_sig, closed_sig, legacy_flat = [], [], 0
    for p in preds:
        if p.get("correct") is not None:
            closed_sig.append(p)
            continue
        # «Открытый сигнал» = НАПРАВЛЕННЫЙ (up/down). Легаси-строки direction=flat
        # сигналами никогда не были, тикер не блокируют (см. get_open_signal_tickers)
        # и раньше врали в счётчике на дашборде — держим их отдельно.
        if p.get("direction") in ("up", "down"):
            open_sig.append(p)
        else:
            legacy_flat += 1

    for p in open_sig:
        a  = scanner.CACHE.results.get(p["ticker"]) or {}
        tp = (a.get("technical") or {}).get("trade_plan") or {}
        p["entry_low"]     = tp.get("entry_low")
        p["entry_high"]    = tp.get("entry_high")
        p["stop_loss"]     = tp.get("stop_loss")
        p["take_profit_1"] = tp.get("take_profit_1")
        p["current_price"] = (a.get("technical") or {}).get("price")
        p["recommendation"] = a.get("recommendation")
        p["narrative"]     = a.get("narrative") or ((a.get("claude") or {}).get("summary"))
        # когда прогноз созреет
        try:
            created = datetime.fromisoformat(p["created_at"])
            p["matures_at"] = (created + timedelta(hours=p.get("horizon_hours") or 24)).isoformat()
        except Exception:
            p["matures_at"] = None

    open_sig.sort(key=lambda x: (x.get("confidence") or 0), reverse=True)
    stats = await db.accuracy_stats()
    return {
        "status": _live_status,
        "open_signals": open_sig,
        "open_count": len(open_sig),
        "legacy_flat_open": legacy_flat,   # старые не-сигналы (flat), в счёт не идут
        "evaluated_count": stats.get("evaluated", 0) if isinstance(stats, dict) else 0,
        "accuracy": stats.get("accuracy") if isinstance(stats, dict) else None,
        # Точность считается только по направленным прогнозам, поэтому рядом с
        # ней обязаны стоять объём выборки и число отказов от мнения: без них
        # цифра снова начнёт вводить в заблуждение.
        "directional_count": stats.get("directional") if isinstance(stats, dict) else None,
        "abstained_count": stats.get("abstained") if isinstance(stats, dict) else None,
        "accuracy_legacy": stats.get("accuracy_legacy") if isinstance(stats, dict) else None,
        "calibration": stats.get("calibration") if isinstance(stats, dict) else None,
        "expectancy_r": stats.get("expectancy_r") if isinstance(stats, dict) else None,
        "profit_factor": stats.get("profit_factor") if isinstance(stats, dict) else None,
        "r_sample": stats.get("r_sample") if isinstance(stats, dict) else None,
        "recent_closed": closed_sig[:20],
    }


# ─── «Умные деньги»: реальные сделки отслеживаемых трейдеров Пульса ────────────

_smart_money_cache: dict = {"ts": 0.0, "snapshot": None}


async def _smart_money_snapshot(authors: Optional[list[str]] = None, ttl: int = 300) -> dict:
    """
    Свод сделок трейдеров ИЗ БАЗЫ (залиты агентом-скрейпером через /api/ingest/deals).
    Публичный веб-API Пульса закрыт анти-ботом, поэтому живой скрейп отсюда убран —
    сделки поступают из browser-скрейпа агента. Форма ответа прежняя (для дашборда).
    """
    from config.settings import PULSE_TRACKED_AUTHORS, INGEST_DEALS_WINDOW_H
    if not authors:
        db_nicks = await db.get_tracked_trader_nicks()
        authors = db_nicks or PULSE_TRACKED_AUTHORS
    snap = await db.recent_trader_deals(hours=INGEST_DEALS_WINDOW_H, limit=200)
    snap["authors"] = authors
    return snap


@app.get("/api/smart-money", summary="Реальные сделки отслеживаемых трейдеров Пульса")
async def get_smart_money(authors: Optional[str] = None, force: bool = False):
    """
    Сделки (покупки/продажи) отслеживаемых трейдеров + нетто-поток по тикерам.
    authors — список ников через запятую (по умолчанию из настроек).
    force=true — обновить, минуя кеш.
    ⚠️ Ходит в Пульс. Из некоторых сетей Пульс отдаёт 403 — тогда список будет пуст.
    """
    author_list = [a.strip() for a in authors.split(",") if a.strip()] if authors else None
    snap = await _smart_money_snapshot(author_list, ttl=0 if force else 300)
    return snap


@app.post("/api/ingest/deals", summary="Приём сделок трейдеров (скрейпер агента)")
async def ingest_deals(payload: dict = Body(...)):
    """
    Заливка сделок трейдеров, собранных агентом через браузер (Пульс-веб закрыт
    анти-ботом). Тело: {token?, deals:[{author,ticker,action,price,quantity,ts,note}]}.
    Пишет в market_events(source=pulse_deal) с дедупом → панель Smart Money + Claude.
    Если INGEST_TOKEN задан в окружении — требуется совпадение token.
    """
    from config.settings import INGEST_TOKEN
    if INGEST_TOKEN and payload.get("token") != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="Неверный или отсутствует token")
    deals = payload.get("deals")
    if not isinstance(deals, list):
        raise HTTPException(status_code=400, detail="Поле deals должно быть списком")
    res = await db.save_trader_deals(deals)
    _smart_money_cache.update({"ts": 0.0, "snapshot": None, "key": None})
    logger.info(f"📥 Ingest сделок: получено {res['received']}, сохранено {res['stored']}")
    return {"ok": True, **res}


# ─── Управление отслеживаемыми трейдерами (сохраняются в БД) ──────────────────

def _clean_nick(raw: str) -> str:
    """Достать чистый ник трейдера из ссылки/строки Пульса."""
    nick = (raw or "").strip()
    for prefix in (
        "https://www.tinkoff.ru/invest/social/profile/",
        "https://www.tbank.ru/invest/social/profile/",
        "https://tinkoff.ru/invest/social/profile/",
    ):
        nick = nick.replace(prefix, "")
    return nick.replace("@", "").strip("/").strip()


@app.get("/api/smart-money/traders", summary="Список отслеживаемых трейдеров")
async def list_traders():
    """
    Отслеживаемые трейдеры Пульса. Хранятся в БД, поэтому переживают
    перезагрузку страницы и редеплой.
    """
    traders = await db.list_tracked_traders()
    return {"traders": traders, "count": len(traders)}


@app.post("/api/smart-money/traders", summary="Добавить трейдера для отслеживания")
async def add_trader(req: TraderRequest):
    """Добавить трейдера Пульса в список отслеживания и сохранить в БД."""
    nickname = _clean_nick(req.nickname)
    if not nickname:
        raise HTTPException(status_code=400, detail="Пустой ник трейдера")
    if await db.trader_exists(nickname):
        raise HTTPException(status_code=400, detail=f"Трейдер {nickname} уже отслеживается")

    info = {
        "nickname": nickname,
        "source": "manual",
        "status": "active",
        "note": (req.note or "").strip(),
    }
    await db.upsert_tracked_trader(info)
    # сбрасываем кеш, чтобы новый трейдер попал в ближайший снимок «умных денег»
    _smart_money_cache.update({"ts": 0.0, "snapshot": None, "key": None})
    logger.info(f"✅ Трейдер добавлен в отслеживание: {nickname}")
    return {"success": True, "trader": info}


@app.delete("/api/smart-money/traders/{nickname}", summary="Удалить трейдера")
async def remove_trader(nickname: str):
    """Убрать трейдера из отслеживания (удаляет запись из БД)."""
    nickname = _clean_nick(nickname)
    deleted = await db.delete_tracked_trader(nickname)
    _smart_money_cache.update({"ts": 0.0, "snapshot": None, "key": None})
    if deleted:
        return {"success": True, "message": f"Трейдер {nickname} удалён из отслеживания"}
    raise HTTPException(status_code=404, detail=f"Трейдер {nickname} не найден")


# ─── WebSocket (реалтайм) ─────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket для получения обновлений в реальном времени.
    
    Клиент подключается и получает обновления индексов каждые 5 секунд.
    
    Формат сообщения:
    {
        "type": "market_update",
        "data": { ...MarketIndex... }
    }
    """
    await websocket.accept()
    connected_websockets.append(websocket)
    logger.info(f"WebSocket подключён. Всего: {len(connected_websockets)}")

    try:
        while True:
            # Отправляем обновление каждые 5 секунд
            market = aggregator.get_market_index()
            tickers = aggregator.get_all_indices()
            
            await websocket.send_json({
                "type": "market_update",
                "market": market.to_dict(),
                "tickers": [t.to_dict() for t in tickers.values()],
            })
            
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        connected_websockets.remove(websocket)
        logger.info(f"WebSocket отключён. Осталось: {len(connected_websockets)}")


# ─── Статика (дашборд) ────────────────────────────────────────────────────────

# ─── Статика (дашборд) ────────────────────────────────────────────────────────

# ─── AI-панели дашборда: КЕШ + учёт расхода ───────────────────────────────────
# Эти три эндпоинта зовут Claude по клику в интерфейсе и берут деньги из ТОГО ЖЕ
# дневного бюджета, что и сигналы. Без кеша каждое открытие вкладки стоило ~4₽ и
# нигде не учитывалось: десять клików — и бюджет сигналов на день съеден незаметно.
# Теперь: TTL-кеш (повторные клики бесплатны) + строка в журнале попыток со
# stage="dashboard", чтобы расход был виден и объясним.
_ai_panel_cache: dict = {}          # {key: (ts, payload)}
AI_PANEL_TTL = 600                  # 10 мин — свежо для панели, дёшево для бюджета


async def _ai_panel(key: str, ttl: int, producer, note: str):
    """Отдать AI-панель из кеша или посчитать, записав расход в журнал попыток."""
    import time
    hit = _ai_panel_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        out = dict(hit[1])
        out["cached"] = True
        out["cached_age_sec"] = int(now - hit[0])
        return out
    agent = ClaudeAgent()
    payload = await producer(agent)
    _ai_panel_cache[key] = (now, payload)
    call = getattr(agent, "last_call", {}) or {}
    await db.add_signal_attempt({
        "stage": "dashboard", "verdict": "panel", "reason": "manual",
        "cost_rub": call.get("cost_rub"), "tokens_in": call.get("tokens_in"),
        "tokens_out": call.get("tokens_out"), "note": f"{note} (кеш {ttl // 60} мин)",
    })
    out = dict(payload)
    out["cached"] = False
    return out


@app.get("/api/ai/ticker/{ticker}", summary="AI анализ тикера")
async def ai_analyze_ticker(ticker: str):
    """Claude анализирует все сигналы по тикеру и даёт инсайт"""
    ticker = ticker.upper()
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")

    idx = aggregator.get_ticker_index(ticker)
    if not idx:
        raise HTTPException(status_code=404, detail="Недостаточно данных по тикеру")

    # Берём топ сообщения по тикеру
    points = list(aggregator._history.get(ticker, []))[-20:]
    top_messages = [p.text_snippet for p in points if p.text_snippet]

    async def _produce(agent):
        return await agent.synthesize_ticker(
            ticker=ticker,
            company=MOEX_TICKERS.get(ticker, ticker),
            sentiment_index=idx.sentiment_index,
            message_count=idx.message_count,
            positive_pct=idx.positive_pct,
            negative_pct=idx.negative_pct,
            top_messages=top_messages,
        )

    return await _ai_panel(f"ticker:{ticker}", AI_PANEL_TTL, _produce,
                           f"AI-панель по {ticker}")


@app.get("/api/ai/market", summary="AI сводка рынка")
async def ai_market_summary():
    """Claude делает краткую сводку текущего настроения рынка"""
    market = aggregator.get_market_index()
    indices = aggregator.get_all_indices()
    anomalies = [idx.to_dict() for idx in indices.values() if idx.is_anomaly]

    async def _produce(agent):
        summary = await agent.market_summary(
            market_index=market.sentiment_index,
            top_bullish=market.top_bullish,
            top_bearish=market.top_bearish,
            total_messages=market.total_messages,
            anomalies=anomalies,
        )
        return {"summary": summary, "market_index": market.sentiment_index}

    return await _ai_panel("market", AI_PANEL_TTL, _produce, "AI-сводка рынка")


@app.get("/api/ai/correlations", summary="AI анализ корреляций")
async def ai_correlations():
    """Claude находит нелинейные паттерны в данных корреляции"""
    # Кеш проверяем ДО тяжёлой выкачки Пульса и свечей: иначе каждый клик — это и
    # десятки сетевых запросов, и вызов Claude из бюджета сигналов.
    import time
    _hit = _ai_panel_cache.get("correlations")
    if _hit and time.time() - _hit[0] < AI_PANEL_TTL * 3:
        _out = dict(_hit[1])
        _out["cached"] = True
        _out["cached_age_sec"] = int(time.time() - _hit[0])
        return _out
    from datetime import date, timedelta
    from src.collector.moex_price_collector import MOEXPriceCollector
    from src.collector.pulse_collector import PulseCollector, PULSE_TICKERS
    from src.aggregator.correlation import CorrelationAnalyzer
    from src.nlp.sentiment_analyzer import keyword_sentiment
    from src.aggregator.aggregator import SentimentPoint

    # Загружаем данные из Пульса
    pulse = PulseCollector()
    pulse_history = await pulse.fetch_history(tickers=PULSE_TICKERS[:10], limit_per_ticker=50)

    sentiment_history = {}
    for ticker, posts in pulse_history.items():
        points = []
        for post in posts:
            sent = keyword_sentiment(post.text)
            points.append(SentimentPoint(
                timestamp=post.timestamp, ticker=ticker,
                signal=sent.signal, label=sent.label,
                score=sent.score, channel="pulse",
                text_snippet=post.text[:100],
            ))
        if points:
            sentiment_history[ticker] = points

    price_collector = MOEXPriceCollector()
    price_history = {}
    from_date = date.today() - timedelta(days=7)
    for ticker in list(sentiment_history.keys())[:10]:
        try:
            candles = await price_collector.get_candles(ticker, interval=10, from_date=from_date)
            if candles:
                price_history[ticker] = candles
            await asyncio.sleep(0.1)
        except Exception:
            pass

    corr_analyzer = CorrelationAnalyzer()
    results = corr_analyzer.analyze_all(sentiment_history, price_history, MOEX_TICKERS)

    # Claude анализирует результаты (расход пишется в журнал попыток, кеш 30 мин)
    async def _produce(agent):
        ai_insights = await agent.find_correlations([r.to_dict() for r in results])
        return {
            "correlations": [r.to_dict() for r in results],
            "ai_insights": ai_insights,
            "analyzed_tickers": len(results),
        }

    return await _ai_panel("correlations", AI_PANEL_TTL * 3, _produce, "AI-корреляции")


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    return FileResponse("dashboard/index.html")


@app.get("/scan", include_in_schema=False)
async def serve_price_scan():
    """
    «Сканер цены». Отдельная страница от «Наблюдателя», потому что форма выдачи
    другая: список бумаг с тем, что сработало, а не карточка на бумагу. Впихнуть
    список в сетку карточек — значит потерять единственное, ради чего сканер
    существует: возможность окинуть взглядом весь рынок разом.

    Обновляется раз в пятнадцать секунд, а не раз в секунду: события живут на
    ЗАКРЫТЫХ барах, и чаще пересчитывать попросту нечего. Частый опрос создавал
    бы вид свежести, которой нет.
    """
    return FileResponse("dashboard/price-scan.html")


@app.get("/market", include_in_schema=False)
@app.get("/book-live", include_in_schema=False)
async def serve_market_watch():
    """
    «Наблюдатель рынка». Отдельная страница, а не вкладка в основном дашборде: у
    неё другой ритм обновления (раз в секунду против минут) и другая задача —
    смотреть, а не разбираться. Смешивать их значило бы гонять тяжёлый дашборд
    каждую секунду.

    Старый адрес /book-live оставлен рабочим намеренно: он в закладках и в моих
    проверочных скриптах, а ломать чужие ссылки ради косметики не стоит. Имя
    страницы сменилось, потому что стакана в ней давно меньше половины —
    таймфреймы, объём, лента сделок и контекст рынка появились позже.
    """
    return FileResponse("dashboard/market-watch.html")


# ─── Запуск ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from config.settings import API_HOST, API_PORT
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    
    uvicorn.run(
        "src.api.main:app",
        host=API_HOST,
        port=API_PORT,
        reload=True,
        log_level="info",
    )
