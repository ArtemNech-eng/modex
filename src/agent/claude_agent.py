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
        historical_context: Optional[str] = None,
        price_context: Optional[str] = None,
        momentum: Optional[float] = None,
        momentum_label: Optional[str] = None,
        source_diversity: Optional[float] = None,
        volume_zscore: Optional[float] = None,
        signal_confidence: Optional[float] = None,
    ) -> dict:
        """
        Синтез всех сигналов по тикеру → торговый инсайт.
        Если передан historical_context — Claude видит реальные паттерны этого рынка.
        """
        messages_text = "\n".join(f"- {m[:150]}" for m in top_messages[:8])
        price_info = f"Изменение цены за день: {price_change_1d:+.2f}%" if price_change_1d else "Цена: нет данных"
        tech_info  = f"RSI: {rsi:.0f}, Тренд: {trend}" if rsi else "Технические данные: нет"

        # Блок качества сигнала
        quality_lines = []
        if momentum_label:
            quality_lines.append(f"- Моментум настроения: {momentum_label} (Δ={momentum:+.3f})")
        if source_diversity is not None:
            div_label = "высокое" if source_diversity > 0.6 else "среднее" if source_diversity > 0.3 else "низкое (1-2 канала)"
            quality_lines.append(f"- Разнообразие источников: {div_label} ({source_diversity:.2f})")
        if volume_zscore is not None:
            vol_label = f"всплеск активности (+{volume_zscore:.1f}σ)" if volume_zscore > 2 else \
                        f"пониженная активность ({volume_zscore:.1f}σ)" if volume_zscore < -1 else \
                        f"нормальная активность ({volume_zscore:.1f}σ)"
            quality_lines.append(f"- Объём сообщений: {vol_label}")
        if signal_confidence is not None:
            quality_lines.append(f"- Уверенность сигнала: {signal_confidence:.0%}")
        quality_block = "\n".join(quality_lines) if quality_lines else "нет данных"

        system = """Ты опытный трейдер и аналитик Московской биржи.
Ты обучаешься на реальной истории рынка: тебе дают исторические данные о том,
как настроение трейдеров коррелировало с движением цены в прошлом.
Используй эти паттерны для принятия решений по текущей ситуации.
Отвечай по-русски, конкретно. Отвечай ТОЛЬКО валидным JSON."""

        history_block = ""
        if historical_context:
            history_block = f"""
🎓 ПАТТЕРНЫ НАСТРОЕНИЕ → ЦЕНА (реальная история):
{historical_context}
"""

        price_block = ""
        if price_context:
            price_block = f"""
{price_context}
"""

        user = f"""Прими торговое решение по акции {ticker} ({company}).

{price_block}{history_block}
📊 ТЕКУЩЕЕ НАСТРОЕНИЕ ТОЛПЫ (собрано из Telegram + Пульс):
- Индекс настроения: {sentiment_index:.1f}/100
- Сообщений за последний час: {message_count}
- Позитивных: {positive_pct:.0f}% | Негативных: {negative_pct:.0f}%

📐 КАЧЕСТВО СИГНАЛА:
{quality_block}

💹 ТЕКУЩИЙ РЫНОК (MOEX):
- {price_info}
- {tech_info}

💬 ЧТО ПИШУТ В ЧАТАХ ПРЯМО СЕЙЧАС:
{messages_text}

Используй всю историю выше и дай решение в JSON:
{{
  "signal": "bullish|bearish|neutral",
  "confidence": 0-100,
  "summary": "вывод в 1-2 предложения с опорой на историю цены и паттерны",
  "key_insight": "что говорит история о такой ситуации",
  "risk": "главный риск",
  "crowd_behavior": "моментум|контртренд|неопределённость",
  "history_based": true
}}"""

        try:
            result = await self._ask(system, user, max_tokens=600)
            import json
            start = result.find("{")
            end   = result.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(result[start:end])
                data["ticker"]      = ticker
                data["analyzed_at"] = datetime.now(timezone.utc).isoformat()
                return data
        except Exception as e:
            logger.warning(f"Claude synthesis failed for {ticker}: {e}")

        return {
            "ticker":         ticker,
            "signal":         "neutral",
            "confidence":     0,
            "summary":        "Анализ недоступен",
            "key_insight":    "Ошибка запроса к Claude",
            "risk":           "Нет данных",
            "crowd_behavior": "неопределённость",
            "history_based":  False,
        }
        """
        Синтез всех сигналов по тикеру → торговый инсайт.
        Если передан historical_context — Claude видит реальные паттерны этого рынка.
        """
        messages_text = "\n".join(f"- {m[:150]}" for m in top_messages[:8])
        price_info = f"Изменение цены за день: {price_change_1d:+.2f}%" if price_change_1d else "Цена: нет данных"
        tech_info  = f"RSI: {rsi:.0f}, Тренд: {trend}" if rsi else "Технические данные: нет"

        system = """Ты опытный трейдер и аналитик Московской биржи.
Ты обучаешься на реальной истории рынка: тебе дают исторические данные о том,
как настроение трейдеров коррелировало с движением цены в прошлом.
Используй эти паттерны для принятия решений по текущей ситуации.
Отвечай по-русски, конкретно. Отвечай ТОЛЬКО валидным JSON."""

        history_block = ""
        if historical_context:
            history_block = f"""
🎓 ОБУЧЕНИЕ НА ИСТОРИИ (реальные данные этого рынка):
{historical_context}

Используй эти паттерны как основу для решения.
"""

        user = f"""Прими торговое решение по акции {ticker} ({company}).

{history_block}
📊 ТЕКУЩЕЕ НАСТРОЕНИЕ ТОЛПЫ (собрано из Telegram + Пульс):
- Индекс настроения: {sentiment_index:.1f}/100
- Сообщений за последний час: {message_count}
- Позитивных: {positive_pct:.0f}% | Негативных: {negative_pct:.0f}%

💹 ТЕКУЩИЙ РЫНОК (MOEX):
- {price_info}
- {tech_info}

💬 ЧТО ПИШУТ В ЧАТАХ ПРЯМО СЕЙЧАС:
{messages_text}

Сопоставь текущую ситуацию с историческими паттернами выше и дай решение в JSON:
{{
  "signal": "bullish|bearish|neutral",
  "confidence": 0-100,
  "summary": "вывод в 1-2 предложения опираясь на историю",
  "key_insight": "что говорит история о такой ситуации",
  "risk": "главный риск",
  "crowd_behavior": "моментум|контртренд|неопределённость",
  "history_based": true
}}"""

        try:
            result = await self._ask(system, user, max_tokens=600)
            import json
            start = result.find("{")
            end   = result.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(result[start:end])
                data["ticker"]      = ticker
                data["analyzed_at"] = datetime.now(timezone.utc).isoformat()
                return data
        except Exception as e:
            logger.warning(f"Claude synthesis failed for {ticker}: {e}")

        return {
            "ticker":         ticker,
            "signal":         "neutral",
            "confidence":     0,
            "summary":        "Анализ недоступен",
            "key_insight":    "Ошибка запроса к Claude",
            "risk":           "Нет данных",
            "crowd_behavior": "неопределённость",
            "history_based":  False,
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
