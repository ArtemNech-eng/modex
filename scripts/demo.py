"""
MOODEX — Демо без Telegram и нейросети
Показывает как работает система на реалистичных данных.
Запуск: python scripts/demo.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import random
from datetime import datetime, timedelta, timezone

from src.nlp.ticker_extractor import extract_tickers, is_market_related
from src.nlp.sentiment_analyzer import keyword_sentiment
from src.aggregator.aggregator import SentimentAggregator

# ── Реалистичные сообщения из трейдерских чатов ──────────────────────────────
DEMO_MESSAGES = [
    # Сбер — смешанно, но позитивно
    "Сбер сегодня очень сильный, покупаю на всё!",
    "Сбербанк пробил сопротивление 320 рублей, отличный вход",
    "Держу Сбер на долгосрок, дивиденды хорошие будут в следующем году",
    "Немного продал Сбер, зафиксировал часть прибыли",
    "SBER выглядит слабовато сегодня, пауза перед движением",
    "Купил ещё Сбербанк на просадке, добавил в портфель",

    # Газпром — негативно
    "Газпром летит вниз, шорчу без сожалений 📉",
    "Газик слабый, на фоне новостей давление продолжается",
    "Газпром, думаю, ещё поупадёт до уровня поддержки 120",
    "Не держу Газпром — слишком много рисков сейчас",
    "Продал весь GAZP, переложился в Лукойл",
    "Газпром дно или ещё нет? Думаю, ещё есть куда падать...",

    # Яндекс — сильно позитивно
    "Яндекс ракета 🚀 обновил годовой хай, держу всё",
    "YNDX берёт хай, отличный импульс! Покупаю на откате",
    "Яндекс стрельнул на новостях, кто не успел — жаль",
    "Добавил Яндекс в портфель на коррекции, 5% от портфеля",
    "Яндекс сильный, технологический сектор тащит рынок",

    # Лукойл — нейтрально/позитивно
    "Лукойл держится хорошо, нефть поддерживает котировки",
    "LKOH без изменений пока, жду пробоя уровня",
    "Лукойл на дивидендах будет интересен, держу долгосрок",
    "Нефть выросла → Лукойл, Роснефть должны подтянуться",

    # ВТБ — негативно
    "ВТБ всё, пора избавляться 📉 слабый банк",
    "VTBR разочарование, продаю остаток",
    "ВТБ хуже рынка стабильно, не понимаю зачем держат",

    # Т-Банк — позитивно
    "Тинькофф снова обновил максимум! Держу с удовольствием 🚀",
    "TCSG летит, молодцы ребята из Тинькофф",
    "Т-банк сильный, добавляю на коррекциях",

    # Полюс — позитивно (золото)
    "Полюс держится, золото растёт → PLZL должен расти",
    "Купил Полюс, золото сейчас выглядит сильно",

    # Прочее
    "Рынок в целом нейтральный, IMOEX на месте стоит",
    "Интересно как Магнит отчитается, жду результатов",
    "Северсталь дивиденды хорошие, держу CHMF",
    "OZON хороший потенциал, электронная коммерция растёт",
    "Аэрофлот слабый, не берите пока",
    "NLMK нейтрально сегодня, без новостей",
    "Новатэк отличный долгосрочный актив, держу",
]


async def run_demo():
    """Запустить демонстрацию системы"""
    print("\n" + "═" * 60)
    print("     MOODEX — Market Mood Index Demo")
    print("     Анализ настроения трейдерских чатов")
    print("═" * 60 + "\n")

    # 1. Инициализация агрегатора
    agg = SentimentAggregator(window_minutes=60, min_messages=3)
    now = datetime.now(timezone.utc)

    print("📥 Обрабатываем демо-сообщения...\n")

    processed = 0
    for i, text in enumerate(DEMO_MESSAGES):
        # Извлекаем тикеры
        tickers = extract_tickers(text)
        
        # Определяем тональность (без нейросети)
        sentiment = keyword_sentiment(text)
        
        # Добавляем в агрегатор
        ts = now - timedelta(minutes=random.randint(1, 59))
        channels = ["markettwits", "rdv_investor", "smart_lab", "daytrader"]

        for ticker in tickers:
            agg.add_point(
                ticker=ticker,
                signal=sentiment.signal,
                label=sentiment.label,
                score=sentiment.score,
                channel=random.choice(channels),
                text=text,
                timestamp=ts,
            )
            processed += 1

        # Печатаем несколько примеров
        if tickers and i < 8:
            arrow = "📈" if sentiment.signal > 0 else "📉" if sentiment.signal < 0 else "➡️"
            print(f"  {arrow} [{sentiment.label:<8}] [{', '.join(tickers):<12}] {text[:55]}...")

    print(f"\n  ... и ещё {len(DEMO_MESSAGES) - 8} сообщений")
    print(f"\n✅ Обработано точек данных: {processed}")

    # 2. Рыночный индекс
    market = agg.get_market_index()
    print("\n" + "─" * 60)
    print(f"  🌍 ОБЩИЙ ИНДЕКС РЫНКА:  {market.sentiment_index:.1f}/100")
    
    mood = "Сильный бычий 🚀" if market.sentiment_index >= 70 else \
           "Умеренно бычий 📈" if market.sentiment_index >= 55 else \
           "Нейтральный ➡️" if market.sentiment_index >= 45 else \
           "Умеренно медвежий 📉" if market.sentiment_index >= 30 else \
           "Сильный медвежий 🩸"
    
    print(f"  Настроение: {mood}")
    print(f"  Сообщений за час: {market.total_messages}")
    print(f"  Активных тикеров: {market.active_tickers}")
    if market.top_bullish:
        print(f"  Топ бычьих: {', '.join(market.top_bullish)}")
    if market.top_bearish:
        print(f"  Топ медвежьих: {', '.join(market.top_bearish)}")

    # 3. Индексы по тикерам
    print("\n" + "─" * 60)
    print("  📊 ИНДЕКСЫ ПО ТИКЕРАМ:\n")
    
    indices = agg.get_all_indices()
    sorted_indices = sorted(indices.values(), key=lambda x: x.sentiment_index, reverse=True)
    
    for idx in sorted_indices:
        bar_len = int(idx.sentiment_index / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        anomaly = " ⚠️ АНОМАЛИЯ" if idx.is_anomaly else ""
        
        mood_short = "🚀" if idx.sentiment_index >= 70 else \
                     "📈" if idx.sentiment_index >= 55 else \
                     "➡️" if idx.sentiment_index >= 45 else \
                     "📉" if idx.sentiment_index >= 30 else "🩸"
        
        print(
            f"  {idx.ticker:<6} {mood_short} [{bar}] "
            f"{idx.sentiment_index:5.1f}  "
            f"+{idx.positive_pct:.0f}% -{idx.negative_pct:.0f}%  "
            f"({idx.message_count} сообщ.){anomaly}"
        )

    # 4. Тест анализа текста
    print("\n" + "─" * 60)
    print("  🔬 ТЕСТ АНАЛИЗА ТЕКСТА:\n")
    
    test_texts = [
        "Сбер летит вверх, покупаю на всё! 🚀",
        "Газпром полный слив, продаю всё немедленно",
        "Яндекс нейтрально пока, жду отчёта",
        "$LKOH сегодня сильная свеча, хороший вход",
        "Рынок падает, паника, все продают",
    ]
    
    for text in test_texts:
        tickers = extract_tickers(text)
        sentiment = keyword_sentiment(text)
        arrow = "📈" if sentiment.signal > 0 else "📉" if sentiment.signal < 0 else "➡️"
        print(f"  {arrow} [{sentiment.label:<8} {sentiment.score:.2f}]  \"{text[:50]}\"")
        if tickers:
            print(f"       Тикеры: {', '.join(tickers)}")

    print("\n" + "═" * 60)
    print("  ✅ Демо завершено!")
    print("  🌐 Для веб-дашборда запустите: python -m uvicorn src.api.main:app --reload")
    print("═" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(run_demo())
