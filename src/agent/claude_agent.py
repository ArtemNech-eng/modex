"""
MOODEX — Claude AI Агент
Мозг системы. Анализирует все сигналы и выдаёт торговый инсайт.

Что делает:
1. Глубокий анализ тональности — понимает сарказм, слэнг, контекст
2. Синтез сигналов — настроение + техника + новости → один вывод
3. Поиск корреляций — находит паттерны которые линейные методы пропускают
4. Торговый инсайт — конкретный вывод с обоснованием
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Поддерживаются два формата API:
#   "anthropic" — нативный Anthropic (api.anthropic.com)
#   "openai"    — OpenAI-совместимые прокси (gen-api.ru, openrouter.ai и др.)
# Задаётся через AI_PROVIDER в .env (по умолчанию "openai" для прокси-сервисов)
_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()
_BASE_URL  = os.getenv(
    "AI_BASE_URL",
    "https://proxy.gen-api.ru/v1" if _PROVIDER == "openai" else "https://api.anthropic.com",
)
_MODEL = os.getenv("AI_MODEL", "claude-sonnet-5")


class ClaudeAgent:

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.provider = _PROVIDER
        self.base_url = _BASE_URL.rstrip("/")
        self.model = _MODEL

    def _build_headers(self) -> dict:
        if self.provider == "anthropic":
            return {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        # OpenAI-совместимый формат (gen-api.ru, openrouter и др.)
        return {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

    async def _ask(self, system: str, user: str, max_tokens: int = 1024) -> str:
        """Отправить запрос к AI (Anthropic или OpenAI-совместимый прокси)"""
        headers = self._build_headers()

        if self.provider == "anthropic":
            url = f"{self.base_url}/v1/messages"
            payload = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        else:
            # OpenAI chat/completions формат
            url = f"{self.base_url}/chat/completions"
            payload = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"AI API error {resp.status_code}: {resp.text[:300]}")
            data = resp.json()

        if self.provider == "anthropic":
            return data["content"][0]["text"]
        else:
            return data["choices"][0]["message"]["content"]

    async def analyze_sentiment_batch(self, messages: list[str]) -> list[dict]:
        """
        Глубокий анализ тональности пакета сообщений.
        Понимает сарказм, сленг трейдеров, эмодзи, контекст.
        """
        text_block = "\n".join(f"{i+1}. {m[:200]}" for i, m in enumerate(messages[:20]))

        system = """Ты эксперт по анализу настроений трейдеров Московской биржи.
Твоя задача — точно определить тональность каждого сообщения из русскоязычных трейдерских чатов.

Правила:
- Понимай сарказм (например "отличный слив" = негатив)
- Учитывай эмодзи (🚀📈 = позитив, 📉💀 = негатив)  
- Трейдерский сленг: "лонг/лонгую" = позитив, "шорт/шорчу" = негатив
- "держу" = умеренно позитив, "фиксирую прибыль" = нейтральный
- Ответь ТОЛЬКО JSON массивом, без пояснений"""

        user = f"""Проанализируй тональность каждого сообщения.
Верни JSON массив: [{{"i":1,"label":"positive|negative|neutral","score":0.0-1.0,"tickers":[]}}]

Сообщения:
{text_block}"""

        try:
            result = await self._ask(system, user, max_tokens=800)
            import json
            # Извлекаем JSON из ответа
            start = result.find("[")
            end = result.rfind("]") + 1
            if start >= 0 and end > start:
                return json.loads(result[start:end])
        except Exception as e:
            logger.warning(f"Claude sentiment batch failed: {e}")
        return []

    async def synthesize_ticker(
        self,
        ticker: str,
        company: str,
        sentiment_index: float,
        message_count: int,
        positive_pct: float,
        negative_pct: float,
        top_messages: list[str],
        price_change_1d: Optional[float] = None,
        rsi: Optional[float] = None,
        trend: Optional[str] = None,
    ) -> dict:
        """
        Синтез всех сигналов по тикеру → торговый инсайт.
        """
        messages_text = "\n".join(f"- {m[:150]}" for m in top_messages[:8])
        price_info = f"Изменение цены за день: {price_change_1d:+.2f}%" if price_change_1d else "Цена: нет данных"
        tech_info = f"RSI: {rsi:.0f}, Тренд: {trend}" if rsi else "Технические данные: нет"

        system = """Ты опытный трейдер и аналитик Московской биржи.
Анализируй данные объективно. Не давай прямых инвестиционных рекомендаций,
но делай конкретные выводы о настроении рынка и возможных сценариях.
Отвечай по-русски, кратко и по делу."""

        user = f"""Анализируй сигналы по акции {ticker} ({company}):

📊 НАСТРОЕНИЕ ТОЛПЫ:
- Индекс настроения: {sentiment_index:.1f}/100
- Сообщений за час: {message_count}
- Позитивных: {positive_pct:.0f}% | Негативных: {negative_pct:.0f}%

💹 РЫНОК:
- {price_info}
- {tech_info}

💬 ТОП СООБЩЕНИЯ ИЗ ЧАТОВ:
{messages_text}

Дай анализ в формате JSON:
{{
  "signal": "bullish|bearish|neutral",
  "confidence": 0-100,
  "summary": "краткий вывод в 1-2 предложения",
  "key_insight": "главный инсайт который не видно без AI",
  "risk": "главный риск",
  "crowd_behavior": "моментум|контртренд|неопределённость"
}}"""

        try:
            result = await self._ask(system, user, max_tokens=512)
            import json
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(result[start:end])
                data["ticker"] = ticker
                data["analyzed_at"] = datetime.now(timezone.utc).isoformat()
                return data
        except Exception as e:
            logger.warning(f"Claude synthesis failed for {ticker}: {e}")

        return {
            "ticker": ticker,
            "signal": "neutral",
            "confidence": 0,
            "summary": "Анализ недоступен",
            "key_insight": str(e) if 'e' in dir() else "Ошибка",
            "risk": "Нет данных",
            "crowd_behavior": "неопределённость",
        }

    async def find_correlations(
        self,
        correlation_data: list[dict],
    ) -> dict:
        """
        Claude анализирует таблицу корреляций и находит нелинейные паттерны.
        """
        if not correlation_data:
            return {"insights": [], "summary": "Нет данных"}

        data_text = "\n".join(
            f"{r['ticker']}: корр={r['correlation']:.2f}, точность={r['signal_accuracy']:.0f}%, "
            f"опережение={r['lead_minutes']}мин, после_бычий={r['avg_price_after_bull']:+.2f}%"
            for r in correlation_data[:15]
        )

        system = """Ты квантовый аналитик. Анализируешь данные о связи настроений трейдеров с ценами акций.
Ищи нелинейные паттерны, аномалии, неочевидные связи. Отвечай по-русски."""

        user = f"""Вот данные о корреляции настроения толпы и движения цен акций MOEX:

{data_text}

Найди:
1. Тикеры где настроение РЕАЛЬНО предсказывает цену (trading edge)
2. Тикеры где толпа систематически ошибается (контртренд)
3. Аномалии и неочевидные паттерны
4. Конкретную торговую стратегию основанную на этих данных

Ответ в JSON:
{{
  "best_momentum": ["тикеры где следовать за толпой"],
  "best_contrarian": ["тикеры где торговать против толпы"],
  "key_findings": ["находка 1", "находка 2", "находка 3"],
  "strategy": "конкретная стратегия в 2-3 предложения",
  "warning": "главное предупреждение"
}}"""

        try:
            result = await self._ask(system, user, max_tokens=800)
            import json
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(result[start:end])
        except Exception as e:
            logger.warning(f"Claude correlation analysis failed: {e}")

        return {"insights": [], "summary": f"Ошибка анализа: {e}"}

    async def market_summary(
        self,
        market_index: float,
        top_bullish: list[str],
        top_bearish: list[str],
        total_messages: int,
        anomalies: list[dict],
    ) -> str:
        """Краткая утренняя/вечерняя сводка рынка от AI"""

        anomaly_text = ""
        if anomalies:
            anomaly_text = "⚠️ АНОМАЛИИ: " + ", ".join(
                f"{a['ticker']} ({a.get('anomaly_type', '?')})" for a in anomalies[:5]
            )

        system = "Ты рыночный аналитик. Пиши кратко, по делу, на русском языке. Без воды."

        user = f"""Сделай краткую сводку настроений рынка MOEX:

Индекс настроения: {market_index:.1f}/100
Топ бычьих: {', '.join(top_bullish) or 'нет данных'}
Топ медвежьих: {', '.join(top_bearish) or 'нет данных'}
Сообщений проанализировано: {total_messages}
{anomaly_text}

Напиши 2-3 предложения: что происходит на рынке прямо сейчас по мнению толпы."""

        try:
            return await self._ask(system, user, max_tokens=256)
        except Exception as e:
            return f"AI сводка недоступна: {e}"
