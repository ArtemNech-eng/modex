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


# ─── БЮДЖЕТ: жёсткий дневной лимит расхода на Claude ──────────────────────────
# Считаем стоимость каждого вызова (по usage от провайдера, иначе оценкой) и
# копим в настройке БД по МСК-дате. Перед вызовом проверяем остаток — так дневной
# лимит (напр. 100₽) физически не может быть превышен, и деньги живут весь день.
_BUDGET_KEY = "ai_spend_day"


def _prices():
    try:
        from config.settings import (AI_DAILY_BUDGET_RUB, AI_PRICE_IN_RUB_1K,
                                     AI_PRICE_OUT_RUB_1K, AI_DEEP_MIN_RESERVE_RUB)
        return AI_DAILY_BUDGET_RUB, AI_PRICE_IN_RUB_1K, AI_PRICE_OUT_RUB_1K, AI_DEEP_MIN_RESERVE_RUB
    except Exception:
        return 100.0, 0.5, 2.5, 12.0


def _msk_day() -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")


async def budget_state() -> dict:
    """Текущий расход за МСК-день: {date, rub, calls, budget, left}."""
    import json as _j
    from src import db
    limit, *_ = _prices()
    try:
        raw = await db.get_setting(_BUDGET_KEY)
        st = _j.loads(raw) if raw else {}
    except Exception:
        st = {}
    if st.get("date") != _msk_day():
        st = {"date": _msk_day(), "rub": 0.0, "calls": 0}
    st["budget"] = limit
    st["left"] = round(max(0.0, limit - float(st.get("rub") or 0)), 2)
    return st


async def _budget_add(rub: float) -> None:
    import json as _j
    from src import db
    st = await budget_state()
    st = {"date": st["date"], "rub": round(float(st.get("rub") or 0) + rub, 4),
          "calls": int(st.get("calls") or 0) + 1}
    try:
        await db.set_setting(_BUDGET_KEY, _j.dumps(st))
    except Exception:
        pass


async def budget_left() -> float:
    return (await budget_state())["left"]


async def can_afford_deep() -> bool:
    """Хватает ли остатка на ГЛУБОКИЙ разбор (иначе — только дешёвый batch)."""
    _, _, _, reserve = _prices()
    return (await budget_left()) >= reserve


class ClaudeAgent:

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.provider = _PROVIDER
        self.base_url = _BASE_URL.rstrip("/")
        self.model = _MODEL
        # Телеметрия последнего вызова (цена/токены/обрезка) — для журнала попыток.
        self.last_call: dict = {}

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
            url = f"{self.base_url}/chat/completions"
            payload = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }

        # ── БЮДЖЕТ-ГАРД: не выходим за дневной лимит ──────────────────────────
        _limit, p_in, p_out, _res = _prices()
        est_in = int((len(system) + len(user)) / 2.5)          # ~2.5 симв/токен (рус.)
        est_cost = (est_in * p_in + max_tokens * p_out) / 1000  # худший случай
        # Телеметрия вызова для журнала попыток: цена и токены пишутся в БД, чтобы
        # было видно «₽ за сигнал» и обрезку JSON (из-за неё раньше молча не было
        # сигналов — max_tokens не хватало на ответ).
        self.last_call = {"cost_rub": None, "tokens_in": None, "tokens_out": None,
                          "max_tokens": max_tokens, "truncated": False, "error": None}
        left = await budget_left()
        if left < est_cost:
            self.last_call["error"] = f"budget_guard: остаток {left:.2f}₽ < {est_cost:.2f}₽"
            raise RuntimeError(
                f"AI budget guard: остаток {left:.2f}₽ < оценки вызова {est_cost:.2f}₽ "
                f"(дневной лимит {_limit:.0f}₽) — вызов пропущен")

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code != 200:
                    raise RuntimeError(f"AI API error {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
        except Exception as e:
            self.last_call["error"] = str(e)[:300]
            raise

        # Фактический расход: из usage провайдера, иначе — по оценке.
        try:
            u = data.get("usage") or {}
            in_tok = int(u.get("prompt_tokens") or u.get("input_tokens") or est_in)
            out_tok = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
            cost = (in_tok * p_in + out_tok * p_out) / 1000
            self.last_call.update({"cost_rub": round(cost, 4),
                                   "tokens_in": in_tok, "tokens_out": out_tok})
            await _budget_add(cost)
        except Exception:
            self.last_call["cost_rub"] = round(est_cost, 4)
            await _budget_add(est_cost)

        # Обрезан ли ответ по лимиту токенов (главная причина «нет сигнала» ранее).
        try:
            if self.provider == "anthropic":
                self.last_call["truncated"] = data.get("stop_reason") == "max_tokens"
            else:
                self.last_call["truncated"] = (
                    ((data.get("choices") or [{}])[0].get("finish_reason") == "length"))
            if self.last_call["truncated"]:
                logger.warning(f"✂️ Ответ AI обрезан по max_tokens={max_tokens} — JSON неполный")
        except Exception:
            pass

        if self.provider == "anthropic":
            return data["content"][0]["text"]
        else:
            return data["choices"][0]["message"]["content"]

    async def batch_scan(self, market_context: str, briefs: list,
                         max_tokens: int = 1600) -> list:
        """
        ОДИН запрос по МНОГИМ тикерам («общая картина»): по кратким данным Claude
        решает, у каких есть торгуемый интрадей-сетап. Дёшево (input мал, output
        терсовый). Возвращает список {ticker, watch(bool), bias, regime, reason}.
        Это УМНЫЙ СКРИН (шортлист), а не финальное entry-решение — глубокий разбор
        по шортлисту делает synthesize_ticker отдельно.
        """
        import json
        lines = []
        for b in (briefs or []):
            if not isinstance(b, dict) or not b.get("ticker"):
                continue
            lines.append(
                f"{b.get('ticker')}: price {b.get('price')}, {b.get('vwap_rel') or 'VWAP н/д'}, "
                f"режим {b.get('regime')}/ADX {b.get('adx')}, RSI {b.get('rsi')}, "
                f"ATR {b.get('atr')}, вола {b.get('vol')}, ROC {b.get('price_roc')}%/об×{b.get('vol_ratio')}, "
                f"фаза {b.get('phase')}, сетап {b.get('setup')}, ORB {b.get('orb_lo')}–{b.get('orb_hi')}, "
                f"R/R {b.get('rr')}, вход {b.get('entry_status')}, "
                f"стакан {b.get('ob_pressure')}/bid-ask {b.get('bid_ask')}/ликв {b.get('liquidity')}, "
                f"поток {b.get('flow')} Δ{b.get('delta')} buy {b.get('buy_pct')}%, настр {b.get('si')}")
        if not lines:
            return []
        tickers_block = "\n".join(lines)
        system = (
            "Ты интрадей-скринер MOEX. По КРАТКИМ данным по каждому тикеру реши, есть ли "
            "ТОРГУЕМЫЙ внутридневной сетап: тренд-откат к VWAP, пробой ORB с удержанием, "
            "сильный МОМЕНТУМ по тренду (высокий ADX + расширение + цена за VWAP), или фейд "
            "границы с признаком разворота. Будь СЕЛЕКТИВЕН: watch=true только там, где сетап "
            "реально стоит глубокого разбора. Не входи против сильного фона без веских причин. "
            "Отвечай ТОЛЬКО валидным JSON-массивом, по одному объекту на тикер.")
        user = (
            f"{market_context}\n\nТИКЕРЫ (кратко):\n{tickers_block}\n\n"
            "Верни JSON-массив: "
            '[{"ticker":"SBER","watch":true,"bias":"long|short|none",'
            '"regime":"trend|trend_momentum|range|squeeze_breakout|news_spike|unclear",'
            '"reason":"кратко почему"}]. watch=true — ТОЛЬКО реальные сетапы, остальным false.')
        try:
            result = await self._ask(system, user, max_tokens=max_tokens)
            s = result.find("[")
            e = result.rfind("]") + 1
            if s >= 0 and e > s:
                arr = json.loads(result[s:e])
                return [x for x in arr if isinstance(x, dict) and x.get("ticker")]
        except Exception as ex:
            logger.warning(f"batch_scan failed: {ex}")
        return []

    async def _ask_with_image(self, system: str, user: str,
                              image_b64: str, max_tokens: int = 1024) -> str:
        """Отправить запрос с картинкой (vision). Поддерживает оба формата API."""
        headers = self._build_headers()

        if self.provider == "anthropic":
            url = f"{self.base_url}/v1/messages"
            payload = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": user},
                    ],
                }],
            }
        else:
            # OpenAI vision формат (gen-api.ru / openrouter)
            url = f"{self.base_url}/chat/completions"
            payload = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_b64}",
                                },
                            },
                            {"type": "text", "text": user},
                        ],
                    },
                ],
            }

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"AI Vision error {resp.status_code}: {resp.text[:300]}")
            data = resp.json()

        if self.provider == "anthropic":
            return data["content"][0]["text"]
        else:
            return data["choices"][0]["message"]["content"]

    async def analyze_chart(self, ticker: str, image_b64: str,
                            sentiment_index: Optional[float] = None,
                            extra_context: Optional[str] = None) -> dict:
        """
        Claude смотрит на свечной график и выдаёт визуальный технический анализ.
        Это дополняет числовые индикаторы — Claude видит паттерны, уровни,
        формации свечей которые сложно формализовать в числах.
        """
        system = """Ты опытный технический аналитик Московской биржи с 15-летним опытом.
Тебе показывают свечной график акции. Анализируй визуально:
паттерны свечей, уровни поддержки/сопротивления, тренды, дивергенции RSI.
Отвечай по-русски. Отвечай ТОЛЬКО валидным JSON."""

        context_block = ""
        if extra_context:
            context_block = f"\nДополнительный контекст:\n{extra_context}\n"
        sentiment_block = ""
        if sentiment_index is not None:
            mood = "бычье" if sentiment_index > 60 else "медвежье" if sentiment_index < 40 else "нейтральное"
            sentiment_block = f"\nТекущее настроение толпы в Telegram/Пульс: {sentiment_index:.0f}/100 ({mood})\n"

        user = f"""Посмотри на этот свечной график акции {ticker}.{context_block}{sentiment_block}
Дай технический анализ в JSON:
{{
  "chart_signal": "bullish|bearish|neutral",
  "chart_confidence": 0-100,
  "trend": "краткое описание тренда",
  "key_levels": "важные уровни поддержки и сопротивления которые видишь",
  "patterns": "паттерны свечей или графика если есть (голова-плечи, флаг, клин и т.д.)",
  "rsi_comment": "что говорит RSI на графике",
  "visual_insight": "главное наблюдение которое видно только на графике",
  "action": "что делать трейдеру прямо сейчас"
}}"""

        try:
            result = await self._ask_with_image(system, user, image_b64, max_tokens=700)
            import json
            start = result.find("{")
            end   = result.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(result[start:end])
                data["ticker"]      = ticker
                data["analyzed_at"] = datetime.now(timezone.utc).isoformat()
                return data
        except Exception as e:
            _err = str(e)[:200]
            logger.warning(f"Claude chart analysis failed for {ticker}: {e}")
        else:
            _err = "в ответе нет JSON"

        # `e` вне блока except уже не существует (Python 3 её удаляет) — раньше
        # здесь падал NameError вместо честного сообщения об ошибке.
        return {
            "ticker":          ticker,
            "chart_signal":    "neutral",
            "chart_confidence": 0,
            "visual_insight":  f"Визуальный анализ недоступен: {_err}",
        }

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
        tinkoff_context: Optional[str] = None,
        macro_context: Optional[str] = None,
        fundamental_context: Optional[str] = None,
        memory_context: Optional[str] = None,
        multiframe_context: Optional[str] = None,
        smart_money_context: Optional[str] = None,
        lessons_context: Optional[str] = None,
        intraday_context: Optional[str] = None,
        knowledge_context: Optional[str] = None,
        chart_context: Optional[str] = None,
        momentum: Optional[float] = None,
        momentum_label: Optional[str] = None,
        source_diversity: Optional[float] = None,
        volume_zscore: Optional[float] = None,
        signal_confidence: Optional[float] = None,
    ) -> dict:
        messages_text = "\n".join(f"- {m[:150]}" for m in top_messages[:8])
        price_info = f"Изменение цены за день: {price_change_1d:+.2f}%" if price_change_1d else "нет данных"
        tech_info  = f"RSI: {rsi:.0f}, Тренд: {trend}" if rsi else "нет данных"

        quality_lines = []
        if momentum_label:
            quality_lines.append(f"- Моментум настроения: {momentum_label} (Δ={momentum:+.3f})")
        if source_diversity is not None:
            div_label = "высокое" if source_diversity > 0.6 else "среднее" if source_diversity > 0.3 else "низкое (1-2 канала)"
            quality_lines.append(f"- Разнообразие источников: {div_label} ({source_diversity:.2f})")
        if volume_zscore is not None:
            vol_label = f"всплеск (+{volume_zscore:.1f}σ)" if volume_zscore > 2 else \
                        f"пониженная ({volume_zscore:.1f}σ)" if volume_zscore < -1 else \
                        f"норма ({volume_zscore:.1f}σ)"
            quality_lines.append(f"- Объём сообщений: {vol_label}")
        if signal_confidence is not None:
            quality_lines.append(f"- Уверенность сигнала: {signal_confidence:.0%}")
        quality_block = "\n".join(quality_lines) if quality_lines else "нет данных"

        def _block(text, default=""):
            return f"\n{text}\n" if text else default

        system = """Ты — дисциплинированный ИНТРАДЕЙ-трейдер Московской биржи. Торгуешь ТОЛЬКО внутри дня: вход и выход в одной сессии, флэт к закрытию. Таймфрейм решений — 5 минут; старшие ТФ только как фон-уклон, на них НЕ торгуешь и позицию через сессию не держишь.

ГЛАВНЫЙ ПРИНЦИП: по умолчанию — НЕ торговать (signal=neutral). Сделка только при явном перевесе; большинство ситуаций → «наблюдать». Лучше пропустить, чем войти в слабый сетап.

Иди СТРОГО по плейбуку:

ШАГ 0 — РИСК-ГЕЙТ (go/no-go). Геошок / резкий негативный фон, тонкий стакан / широкий спред, конец сессии (<15 мин до закрытия) или превышен дневной лимит убытка → НЕ торговать, дальше не иди.

ШАГ 1 — УКЛОН СТАРШЕГО ТФ (только фон). Дневной/недельный тренд → bias long/short/neutral. Интрадей-контртренд против СИЛЬНОГО дневного тренда — только при высоком конфлюенсе.

ШАГ 2 — РЕЖИМ ДНЯ (ключевое ветвление). Определи режим и применяй его логику:
 • trend — вход на ОТКАТЕ к VWAP/уровню В СТОРОНУ тренда (mode=pullback);
 • trend_momentum — СИЛЬНЫЙ однонаправленный тренд (ADX высокий + расширение волатильности + агрессивный поток ПО тренду + цена уверенно за VWAP): вход по ПРОДОЛЖЕНИЮ (пробой-и-удержание нового экстремума/микро-консолидации по тренду), БЕЗ ожидания глубокого отката (mode=momentum). НЕ входи на исходе движения: если цена уже сильно растянута от VWAP (> ~2×ATR) — поздно, наблюдай;
 • range — фейд границ (покупка у поддержки / продажа у сопротивления) ТОЛЬКО с признаком разворота (абсорбция в стакане/footprint);
 • squeeze_breakout — вход по факту РАСШИРЕНИЯ (пробой+удержание), не на сжатии;
 • news_spike — вход «на разрешении» (пробой и удержание пост-новостного диапазона у VWAP), не лови сам прострел.
 Режим не ясен → наблюдать.

ШАГ 3 — КОНФЛЮЕНС на уровне входа: сколько НЕЗАВИСИМЫХ сигналов сошлись — (1) структура/VWAP/ORB/уровень, (2) стены стакана, (3) поток/агрессор (с учётом надёжности), (4) footprint/POC (абсорбция), (5) настроение не против, (6) старший фон согласован. Скор 0–6: ≥4 сильный (полный размер), 3 средний (½ размера), ≤2 → наблюдать.

ШАГ 4 — НАСТРОЕНИЕ (контекст, вес 1): толпа/новости подтверждают или предупреждают; на экстремумах — контр-сигнал. НЕ триггер.

ШАГ 5 — ПАМЯТЬ/УРОКИ: совпадает с прошлой ошибкой → минус к уверенности или пропуск.

ШАГ 6 — РЕШЕНИЕ (инвалидация-first). Определи "mode" и действуй по нему:

A) mode="pullback" (режимы trend-откат / range / squeeze_breakout / news_spike): стоп ФИКСИРОВАННЫЙ −1% от входа (ставится автоматически, сам стоп НЕ придумывай).
 (1) уровень слома (инвалидация); (2) фильтр: слом НЕ ближе 1% от входа (иначе фикс-стоп выбьет раньше → наблюдать); (3) цель — ближайший логичный уровень, R:R = |цель−вход|/(0.01·вход) ≥ 1.5; вход ЛИМИТ у уровня (не дальше 0.5×ATR).

B) mode="momentum" (режим trend_momentum): вход по ПРОДОЛЖЕНИЮ тренда. Стоп СТРУКТУРНЫЙ — за базой пробоя / последним свингом в поле "stop", но НЕ шире 1% от входа (потолок; обычно туже, 0.3–0.7%). R:R = |цель−вход|/|вход−стоп| ≥ 1.2 (порог мягче: прибыль берём ТРЕЙЛОМ раннера, не фикс-целью). Анти-чейз ослаблен, НО не входи, если цена растянута > ~2×ATR от VWAP.

Общее (оба mode): горизонт — конец сессии; вход ТОЛЬКО если конфлюенс ≥ 3 И риск-гейт пройден И цель достижима внутри сессии; иначе — наблюдать.

Отвечай по-русски. Отвечай ТОЛЬКО валидным JSON."""

        user = f"""Прими торговое решение по акции {ticker} ({company}).
{_block(macro_context)}
{_block(fundamental_context)}
{_block(multiframe_context)}
{_block(price_context)}
{_block(tinkoff_context)}
{_block(smart_money_context)}
{_block(knowledge_context)}
{_block(chart_context)}
{_block(intraday_context)}
{_block(historical_context)}
{_block(memory_context)}
{_block(lessons_context)}
📊 ТЕКУЩЕЕ НАСТРОЕНИЕ ТОЛПЫ (Telegram + Пульс):
- Индекс: {sentiment_index:.1f}/100 | Сообщений: {message_count}
- Позитивных: {positive_pct:.0f}% | Негативных: {negative_pct:.0f}%

📐 КАЧЕСТВО СИГНАЛА:
{quality_block}

💹 ТЕКУЩИЙ РЫНОК (MOEX):
- {price_info}
- {tech_info}

💬 ЧТО ПИШУТ В ЧАТАХ:
{messages_text if messages_text else "нет данных"}

Пройди плейбук по шагам (0→6) и дай решение СТРОГО в JSON:
{{
  "risk_gate": "passed или blocked + причина",
  "htf_bias": "long|short|neutral — фон старшего ТФ",
  "regime": "trend|trend_momentum|range|squeeze_breakout|news_spike|unclear",
  "mode": "pullback|momentum — pullback=фикс-стоп −1%; momentum=структурный стоп (поле stop)",
  "setup": "1 предложение: сетап по режиму (или почему наблюдаем)",
  "confluence_score": 0,
  "confluence_factors": "какие независимые сигналы сошлись",
  "signal": "bullish|bearish|neutral",
  "confidence": 0-100,
  "entry": "уровень входа (лимитка) числом или null",
  "stop": "уровень стопа числом: при pullback — уровень слома (стоп фиксируется −1%); при momentum — РЕАЛЬНЫЙ структурный стоп (за базой пробоя, ≤1% от входа)",
  "target": "уровень цели числом или null (достижимой до конца сессии)",
  "rr": "R:R = |target−entry|/|entry−stop|, числом или null",
  "size": "full|half|none",
  "invalidation": "где сделка неверна (уровень/условие)",
  "summary": "итоговый вывод 2-3 предложения",
  "key_insight": "главное, что не видно без глубокого анализа",
  "risk": "главный риск для этого решения"
}}

Помни: дефолт — neutral/none. Конфлюенс ≥ 3 И риск-гейт passed И цель достижима до закрытия сессии.
- mode=pullback: стоп фикс −1%, R:R ≥ 1.5; вход ЛИМИТ у уровня, не дальше 0.5×ATR — не догоняй.
- mode=momentum (только в сильном тренде trend_momentum): структурный стоп ≤1% в поле stop, R:R ≥ 1.2, вход по продолжению; анти-чейз ослаблен, но НЕ входи при растяжении > ~2×ATR от VWAP.
Против HTF-биаса нужен конфлюенс ≥ 5; фейд границ диапазона (range) — конфлюенс ≥ 4."""

        _err = None
        try:
            # 1800 токенов: полный JSON плейбука реально ~900–1300 токенов (в 1300
            # обрезался). 1800 — с запасом, но втрое дешевле резерва 3000 → бюджет
            # 100₽/день доживает до конца сессии.
            result = await self._ask(system, user, max_tokens=1800)
            import json
            start = result.find("{")
            end   = result.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(result[start:end])
                data["ticker"]      = ticker
                data["analyzed_at"] = datetime.now(timezone.utc).isoformat()
                data["ok"]          = True   # Claude реально ответил (для честной деградации)
                data["_call"]       = dict(self.last_call)   # цена/токены → журнал попыток
                return data
            _err = ("ответ обрезан по max_tokens — JSON неполный"
                    if (self.last_call or {}).get("truncated") else "в ответе нет JSON")
        except Exception as e:
            _err = str(e)[:300]
            logger.warning(f"Claude synthesis failed for {ticker}: {e}")

        return {
            "ticker":         ticker,
            "signal":         "neutral",
            "confidence":     0,
            "ok":             False,   # Claude недоступен/не распарсили → честный флаг
            "summary":        "Анализ недоступен",
            "key_insight":    "Ошибка запроса к Claude",
            "risk":           "Нет данных",
            "crowd_behavior": "неопределённость",
            "history_based":  False,
            "_call":          dict(self.last_call or {}),
            "_error":         _err,    # ПОЧЕМУ не ответил — в журнал попыток
        }

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

        tinkoff_block = ""
        if tinkoff_context:
            tinkoff_block = f"\n{tinkoff_context}\n"

        user = f"""Прими торговое решение по акции {ticker} ({company}).

{price_block}{tinkoff_block}{history_block}
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
            "ok":             False,   # Claude недоступен/не распарсили → честный флаг
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
            "ok":             False,   # Claude недоступен/не распарсили → честный флаг
            "summary":        "Анализ недоступен",
            "key_insight":    "Ошибка запроса к Claude",
            "risk":           "Нет данных",
            "crowd_behavior": "неопределённость",
            "history_based":  False,
        }

    # Разрешённые теги причин (единый словарь для агрегации по журналу)
    POST_MORTEM_TAGS = [
        "correct_read",     # верное прочтение ситуации
        "false_sentiment",  # настроение толпы обмануло
        "crowd_trap",       # пошли за толпой на развороте
        "news_shock",       # внешняя новость/событие перебили сигнал
        "regime_change",    # сменился режим рынка (боковик↔тренд)
        "late_entry",       # поздний вход, движение уже реализовалось
        "tech_break_fail",  # ложный пробой уровня/индикатора
        "low_liquidity",    # тонкий рынок / мало данных
        "overconfidence",   # завышенная уверенность на слабых данных
        "luck",             # результат — скорее шум, чем следствие анализа
    ]

    async def post_mortem(
        self,
        ticker: str,
        direction: str,
        confidence: float,
        context: dict,
        realized_return: Optional[float],
        correct: Optional[bool],
        horizon_hours: int = 24,
    ) -> dict:
        """
        Разбор закрытого сигнала: ПОЧЕМУ он сработал / не сработал.
        Возвращает {"cause","lesson","tags"}.
        При недоступности Claude — эвристический разбор по числам (без LLM).
        """
        outcome = ("верный" if correct else "неверный") if correct is not None else "неизвестен"
        ret_str = f"{realized_return:+.2f}%" if realized_return is not None else "н/д"

        # ── Попытка через Claude ─────────────────────────────────────────────
        if self.api_key:
            try:
                import json
                ctx_text = json.dumps(context or {}, ensure_ascii=False)[:2500]
                system = (
                    "Ты трейдер-наставник. Разбираешь ЗАКРЫТЫЙ прогноз по акции MOEX: "
                    "почему он сработал или не сработал, и какой из этого урок на будущее. "
                    "Пиши по-русски, коротко и по делу. Отвечай ТОЛЬКО валидным JSON."
                )
                user = f"""Разбери прогноз по {ticker}.

Прогноз: направление={direction}, уверенность={confidence:.0%}, горизонт={horizon_hours}ч.
Факт: результат {outcome}, доходность за горизонт: {ret_str}.

Драйверы на момент прогноза (снимок):
{ctx_text}

Верни JSON:
{{
  "cause": "1-2 предложения: почему сигнал {('сработал' if correct else 'не сработал')}",
  "lesson": "короткое правило-вывод на будущее (1 предложение)",
  "tags": ["выбери 1-3 из: {', '.join(self.POST_MORTEM_TAGS)}"]
}}"""
                raw = await self._ask(system, user, max_tokens=400)
                start, end = raw.find("{"), raw.rfind("}") + 1
                if start >= 0 and end > start:
                    data = json.loads(raw[start:end])
                    tags = [t for t in data.get("tags", []) if t in self.POST_MORTEM_TAGS]
                    return {
                        "cause": (data.get("cause") or "").strip(),
                        "lesson": (data.get("lesson") or "").strip(),
                        "tags": tags or self._heuristic_pm(direction, context, correct)["tags"],
                    }
            except Exception as e:
                logger.warning(f"post-mortem Claude failed for {ticker}: {e}")

        # ── Fallback: эвристика без LLM ───────────────────────────────────────
        h = self._heuristic_pm(direction, context, correct)
        h["cause"] = h["cause"] + f" (факт: {ret_str} за {horizon_hours}ч)."
        return h

    @staticmethod
    def _heuristic_pm(direction: str, context: dict, correct: Optional[bool]) -> dict:
        """Простой разбор по числам, когда LLM недоступен."""
        context = context or {}
        sent = context.get("sentiment_index")
        regime = context.get("regime")
        entry = context.get("entry_status")

        if correct:
            return {
                "cause": "Сигнал совпал с последующим движением цены.",
                "lesson": "Похожая конфигурация уже отрабатывала — доверять при схожих условиях.",
                "tags": ["correct_read"],
            }

        tags: list[str] = []
        # Сильное настроение в сторону прогноза, но рынок пошёл против → толпа обманула
        if sent is not None and (
            (direction == "up" and sent >= 60) or (direction == "down" and sent <= 40)
        ):
            tags += ["false_sentiment", "crowd_trap"]
        if regime in ("range", "боковик"):
            tags.append("tech_break_fail")
        elif regime in ("uptrend", "downtrend"):
            tags.append("regime_change")
        if entry in ("late", "invalid"):
            tags.append("late_entry")
        if not tags:
            tags = ["luck"]
        return {
            "cause": "Прогноз разошёлся с движением цены; ведущие драйверы не подтвердились.",
            "lesson": "Снижать уверенность, когда сигнал держится на одном факторе без подтверждения техникой.",
            "tags": tags[:3],
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
            _err = str(e)[:200]
            logger.warning(f"Claude correlation analysis failed: {e}")
        else:
            _err = "в ответе нет JSON"

        # То же, что и в analyze_chart: `e` вне except недоступна → был NameError.
        return {"insights": [], "summary": f"Ошибка анализа: {_err}"}

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
