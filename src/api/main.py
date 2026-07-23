"""
MOODEX — FastAPI Backend
REST API + WebSocket для дашборда и внешних интеграций.
"""
import asyncio
import logging
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, BackgroundTasks
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

    # Наполняем демо-данными для тестирования
    _fill_demo_data()
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


@app.get("/api/ticker/{ticker}", summary="Индекс конкретного тикера")
async def get_ticker_index(ticker: str):
    """Получить индекс настроения для тикера (например, SBER, GAZP)"""
    ticker = ticker.upper()
    if ticker not in MOEX_TICKERS:
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
    if ticker not in MOEX_TICKERS:
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
    if ticker not in MOEX_TICKERS:
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    return await analyst.analyze(ticker, aggregator, save=save)


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
    if ticker not in MOEX_TICKERS:
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
    if ticker not in MOEX_TICKERS:
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


async def get_accuracy(ticker: Optional[str] = None):
    """Статистика точности прогнозов агента (основа для оценки качества)."""
    return await db.accuracy_stats(ticker=ticker)


def _bucket(items: list[dict]) -> dict:
    ev = [p for p in items if p.get("correct") is not None]
    cor = [p for p in ev if p["correct"]]
    return {
        "total": len(items),
        "evaluated": len(ev),
        "correct": len(cor),
        "accuracy": round(len(cor) / len(ev), 3) if ev else None,
    }


def _avg(vals: list) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


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

@app.get("/api/ai/ticker/{ticker}", summary="AI анализ тикера")
async def ai_analyze_ticker(ticker: str):
    """Claude анализирует все сигналы по тикеру и даёт инсайт"""
    ticker = ticker.upper()
    if ticker not in MOEX_TICKERS:
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")

    idx = aggregator.get_ticker_index(ticker)
    if not idx:
        raise HTTPException(status_code=404, detail="Недостаточно данных по тикеру")

    # Берём топ сообщения по тикеру
    points = list(aggregator._history.get(ticker, []))[-20:]
    top_messages = [p.text_snippet for p in points if p.text_snippet]

    result = await claude.synthesize_ticker(
        ticker=ticker,
        company=MOEX_TICKERS.get(ticker, ticker),
        sentiment_index=idx.sentiment_index,
        message_count=idx.message_count,
        positive_pct=idx.positive_pct,
        negative_pct=idx.negative_pct,
        top_messages=top_messages,
    )
    return result


@app.get("/api/ai/market", summary="AI сводка рынка")
async def ai_market_summary():
    """Claude делает краткую сводку текущего настроения рынка"""
    market = aggregator.get_market_index()
    indices = aggregator.get_all_indices()
    anomalies = [idx.to_dict() for idx in indices.values() if idx.is_anomaly]

    summary = await claude.market_summary(
        market_index=market.sentiment_index,
        top_bullish=market.top_bullish,
        top_bearish=market.top_bearish,
        total_messages=market.total_messages,
        anomalies=anomalies,
    )
    return {"summary": summary, "market_index": market.sentiment_index}


@app.get("/api/ai/correlations", summary="AI анализ корреляций")
async def ai_correlations():
    """Claude находит нелинейные паттерны в данных корреляции"""
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

    # Claude анализирует результаты
    ai_insights = await claude.find_correlations([r.to_dict() for r in results])

    return {
        "correlations": [r.to_dict() for r in results],
        "ai_insights": ai_insights,
        "analyzed_tickers": len(results),
    }


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    return FileResponse("dashboard/index.html")


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
