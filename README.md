# 🚀 MOODEX — Market Mood Index для Мосбиржи

> Платформа анализа настроений трейдерских чатов в реальном времени.  
> *Не является инвестиционной рекомендацией.*

---

## Что это

MOODEX собирает сообщения из Telegram-чатов трейдеров, анализирует их тональность
нейросетью и выдаёт **индекс настроения толпы (0-100)** для каждого тикера Мосбиржи:

| Значение | Настроение |
|----------|-----------|
| 80-100 | Сильный бычий 🚀 |
| 60-80  | Умеренно бычий 📈 |
| 40-60  | Нейтральный ➡️ |
| 20-40  | Умеренно медвежий 📉 |
| 0-20   | Сильный медвежий 🩸 |

---

## Быстрый старт (демо без Telegram)

```bash
# 1. Клонировать и установить зависимости
git clone https://github.com/ArtemNech-eng/modex.git
cd modex
pip install -r requirements.txt

# 2. Запустить демо (без Telegram, без нейросети)
python scripts/demo.py

# 3. Запустить веб-дашборд
uvicorn src.api.main:app --reload --port 8000
# Открыть: http://localhost:8000
```

---

## Полный запуск (с Telegram)

### 1. Получить Telegram API credentials

1. Зайди на [my.telegram.org/apps](https://my.telegram.org/apps)
2. Создай приложение → получи `api_id` и `api_hash`

### 2. Настроить окружение

```bash
cp .env.example .env
# Открыть .env и заполнить TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE
```

### 3. Первая авторизация

```python
from telethon import TelegramClient
import asyncio

async def auth():
    client = TelegramClient('moodex_session', API_ID, API_HASH)
    await client.start(phone=PHONE)  # введёт код из SMS
    print("✅ Авторизован!")
    await client.disconnect()

asyncio.run(auth())
```

### 4. Запустить сервер

```bash
uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
```

---

## Архитектура

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ TelegramCollect │───▶│ SentimentAnalyzer│───▶│   Aggregator    │
│  (Telethon)     │    │  (RuBERT NLP)    │    │  (индекс 0-100) │
└─────────────────┘    └──────────────────┘    └────────┬────────┘
                                                         │
                                        ┌────────────────▼──────────┐
                                        │     FastAPI + WebSocket    │
                                        │  REST API / Дашборд        │
                                        └───────────────────────────┘
```

## Структура проекта

```
modex/
├── config/
│   └── settings.py          # Конфигурация и список тикеров
├── src/
│   ├── collector/
│   │   └── telegram_collector.py   # Парсинг Telegram-чатов
│   ├── nlp/
│   │   ├── sentiment_analyzer.py   # NLP-анализ тональности
│   │   └── ticker_extractor.py     # Извлечение тикеров MOEX
│   ├── aggregator/
│   │   └── aggregator.py           # Расчёт индексов настроения
│   └── api/
│       └── main.py                 # FastAPI backend
├── dashboard/
│   └── index.html                  # Веб-дашборд (React-free)
├── scripts/
│   └── demo.py                     # Демо без Telegram
├── .env.example
├── requirements.txt
└── README.md
```

---

## API Endpoints

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/api/market` | Общий индекс рынка |
| GET | `/api/tickers` | Все тикеры с индексами |
| GET | `/api/ticker/{TICKER}` | Индекс конкретного тикера |
| POST | `/api/analyze` | Анализ произвольного текста |
| GET | `/api/anomalies` | Текущие аномалии |
| WS | `/ws` | WebSocket реалтайм-обновления |
| GET | `/api/docs` | Swagger UI документация |

---

## NLP-модели

| Модель | Размер | Скорость | Точность |
|--------|--------|----------|----------|
| `cointegrated/rubert-tiny-sentiment-balanced` | ~45MB | ~5ms/текст | ⭐⭐⭐ |
| `blanchefort/rubert-base-cased-sentiment` | ~512MB | ~50ms/текст | ⭐⭐⭐⭐⭐ |

**Рекомендация:** начни с `rubert-tiny`, при масштабировании перейди на `rubert-base`.

---

## Roadmap

- [x] MVP: парсер + NLP + агрегатор + API + дашборд
- [ ] Telegram-бот для алертов
- [ ] Исторические данные + бэктестинг
- [ ] PostgreSQL + ClickHouse для масштаба
- [ ] Система подписок (Stripe/ЮКасса)
- [ ] Интеграция с QUIK / Тинькофф API
- [ ] Анализ крипто-рынков

---

## Лицензия

MIT — используй свободно, но помни:  
> Данный инструмент предоставляет аналитические данные на основе публичной информации.  
> **Не является инвестиционной рекомендацией.**  
> Торговля на бирже связана с риском потери капитала.
