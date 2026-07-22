"""
MOODEX — FastAPI Backend
REST API + WebSocket для дашборда и внешних интеграций.
"""
import asyncio
import logging
import json
from pathlib import Path
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
from config.settings import MOEX_TICKERS

logger = logging.getLogger(__name__)

# ─── Приложение ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="MOODEX API",
    description="Market Mood Index для Московской биржи",
    version="0.1.0",
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


# ─── Хранилище каналов (JSON-файл, переживает перезапуски) ───────────────────
from config.settings import CHANNELS_FILE

_channels: list[dict] = []
_collector_ref = None   # ссылка на TelegramCollector, устанавливается из main.py


def set_collector(collector):
    """Вызывается из main.py чтобы дать API доступ к коллектору"""
    global _collector_ref
    _collector_ref = collector


def _load_channels():
    """Загрузить добавленные вручную каналы из файла в память."""
    global _channels
    path = Path(CHANNELS_FILE)
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                _channels = json.load(f)
            logger.info(f"📂 Загружено сохранённых каналов: {len(_channels)}")
    except Exception as e:
        logger.warning(f"Не удалось загрузить каналы из {CHANNELS_FILE}: {e}")
        _channels = []


def _save_channels():
    """Сохранить добавленные вручную каналы в файл."""
    path = Path(CHANNELS_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_channels, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Не удалось сохранить каналы в {CHANNELS_FILE}: {e}")


def get_saved_channel_usernames() -> list[str]:
    """Username-ы сохранённых вручную каналов (для восстановления в коллекторе)."""
    return [c["username"] for c in _channels]


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("🚀 MOODEX API запускается...")
    # Загружаем NLP-модель
    try:
        await analyzer.load()
    except Exception as e:
        logger.warning(f"NLP-модель не загружена (используем словарный метод): {e}")
    
    # Восстанавливаем каналы, добавленные вручную ранее
    _load_channels()

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
    # Объединяем дефолтные и добавленные вручную
    all_channels = []
    for ch in TELEGRAM_CHANNELS:
        all_channels.append({
            "username": ch,
            "status": "active",
            "source": "config",
        })
    for ch in _channels:
        all_channels.append(ch)
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

    # Проверяем дубликаты
    from config.settings import TELEGRAM_CHANNELS
    existing = [c["username"] for c in _channels] + TELEGRAM_CHANNELS
    if username in existing:
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

        # Добавляем в список мониторинга
        channel_info = {
            "username": username,
            "title": title,
            "members": members,
            "status": "active",
            "source": "manual",
            "joined": joined,
        }
        _channels.append(channel_info)
        _save_channels()

        # Подписываем коллектор на новый канал (перерегистрирует обработчик)
        await _collector_ref.add_channel(username)

        logger.info(f"✅ Канал добавлен: @{username} ({title}), вступили: {joined}")
        return {"success": True, "channel": channel_info}

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось добавить @{username}: {str(e)}")


@app.delete("/api/channels/{username}", summary="Удалить канал")
async def remove_channel(username: str):
    """Удалить канал из мониторинга"""
    global _channels
    before = len(_channels)
    _channels = [c for c in _channels if c["username"] != username]

    if _collector_ref and username in _collector_ref.channels:
        await _collector_ref.remove_channel(username)

    if len(_channels) < before:
        _save_channels()
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
