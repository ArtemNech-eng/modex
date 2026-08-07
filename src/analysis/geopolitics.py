"""
MOODEX — Геополитический монитор

Российский рынок сильно зависит от геополитики. Модуль анализирует поток
новостей и сообщений на геополитические сигналы и строит общий фон рынка:
score ∈ [-1, +1] (негатив ↔ позитив) + уровень риска.

Прозрачно и объяснимо: скоринг по ключевым словам (без «чёрного ящика»).
Фон учитывается AI-агентом при формировании рекомендации.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Негативные сигналы (рост риска / давление на рынок)
NEGATIVE = [
    # Санкции, ограничения, платежи
    "санкц", "эмбарго", "вторичн", "ограничен", "запрет", "заморозк", "swift",
    "потолок цен", "дефолт", "кризис", "отключ", "изъят", "угроз", "черный список",
    # Геополитика, военный фон
    "эскалац", "война", "военн", "конфликт", "обстрел", "удар", "ракет", "дрон",
    "атак", "теракт", "мобилизац", "курск", "белгород", "шебекино", "бпл", "взрыв",
    "пожар на нпз", "нпз", "мид", "опек",
    # Денежно-кредитная политика, налоги, бюджет
    "ставк", "ключевая ставка", "повышение ставки", "жестк", "инфляц", "рост цен",
    "минфин", "налог", "ндпи", "налог на прибыль", "дефицит", "офз", "распродаж",
    # Рынок, валюта, сырьё, корпоративный негатив
    "девальвац", "доллар", "юань", "обвал", "падение нефти", "нефть упад",
    "нефть дешев", "дисконт", "убыток", "отказ от див", "дивиденд не",
    "дивиденд отмен", "падение рынка", "слив", "снижение прогноза",
]
# Позитивные сигналы (снижение риска / поддержка рынка)
POSITIVE = [
    # Переговоры, мир, смягчение санкций
    "переговор", "перемир", "деэскалац", "снятие санкц", "смягчение санкц",
    "соглашение", "урегулир", "мирн", "разрядк", "компромисс",
    # ДКП, экономический рост, поддержка
    "смягчен", "снижение ставки", "дкп", "профицит", "укрепление рубля",
    "приток", "рост экономики", "приток капитала", "восстановлен", "поддержк",
    # Корпоративный позитив, дивиденды, сырьё
    "дивиденд", "рекордн", "прибыль", "байбэк", "buyback", "рост выручки",
    "выплата дивидендов", "рекомендовал дивиденд", "нефть раст",
    "повышение прогноза", "одобрил",
]


def score_text(text: str) -> tuple[int, int]:
    """Вернуть (кол-во позитивных, кол-во негативных) геополитических сигналов в тексте."""
    if not text:
        return 0, 0
    low = text.lower()
    pos = sum(1 for kw in POSITIVE if kw in low)
    neg = sum(1 for kw in NEGATIVE if kw in low)

    # Ставка ЦБ — по стемам (устойчиво к формам слов)
    if "ставк" in low:
        if "сниж" in low or "сниз" in low or "смягч" in low:
            pos += 1
        elif "повыс" in low or "повыш" in low or "подня" in low:
            neg += 1
    return pos, neg


def _risk_label(score: float) -> tuple[str, str]:
    """(человекочитаемый фон, уровень риска)."""
    if score <= -0.5:
        return "Резко негативный 🔴", "high"
    if score <= -0.15:
        return "Негативный 📉", "elevated"
    if score < 0.15:
        return "Нейтральный ➡️", "normal"
    if score < 0.5:
        return "Позитивный 📈", "low"
    return "Резко позитивный 🟢", "low"


class GeopoliticsMonitor:
    """Скользящий геополитический фон рынка по потоку сообщений/новостей."""

    def __init__(self, window_hours: int = 24, maxlen: int = 2000):
        self.window = timedelta(hours=window_hours)
        self._events: deque = deque(maxlen=maxlen)  # (ts, net, headline)

    def add(self, text: str, timestamp: datetime | None = None):
        pos, neg = score_text(text)
        if pos == 0 and neg == 0:
            return
        ts = timestamp or datetime.now(timezone.utc)
        net = pos - neg
        headline = text.strip().replace("\n", " ")[:140]
        self._events.append((ts, net, headline))

    def _recent(self):
        cutoff = datetime.now(timezone.utc) - self.window
        return [(ts, net, h) for (ts, net, h) in self._events
                if (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)) >= cutoff]

    def snapshot(self) -> dict:
        events = self._recent()
        pos_hits = sum(max(0, net) for _, net, _ in events)
        neg_hits = sum(max(0, -net) for _, net, _ in events)
        net_sum = sum(net for _, net, _ in events)

        # Нормируем в [-1, 1] через tanh (насыщение при сильном перекосе)
        score = math.tanh(net_sum / 6.0) if events else 0.0
        label, risk = _risk_label(score)

        # Свежие «горячие» заголовки (сначала самые значимые по модулю)
        hot = sorted(events, key=lambda e: (abs(e[1]), e[0]), reverse=True)[:15]
        headlines = [{"net": net, "text": h} for _, net, h in hot]

        return {
            "score": round(score, 3),
            "label": label,
            "risk_level": risk,
            "positive_hits": pos_hits,
            "negative_hits": neg_hits,
            "events_analyzed": len(events),
            "headlines": headlines,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


# Глобальный синглтон — наполняется из пайплайнов новостей/сообщений
MONITOR = GeopoliticsMonitor()


def ingest_event(text: str, channel: str = "",
                 timestamp: datetime | None = None) -> dict | None:
    """
    Прогнать текст через геополитический скоринг. Если геосигнал есть:
      1) обновить скользящий монитор MONITOR (для «Геофона» и score в промпте),
      2) вернуть готовое событие для записи в базу знаний отдельной категорией
         (market_events, source="geopolitics", ticker=None — это фон рынка).
    Если сигнала нет — вернуть None (ничего не пишем).

    Персистит НЕ здесь: запись в БД делает вызывающий пайплайн (main.py),
    чтобы модуль анализа не зависел от слоя БД (без циклических импортов).
    """
    pos, neg = score_text(text)
    if pos == 0 and neg == 0:
        return None
    ts = timestamp or datetime.now(timezone.utc)
    MONITOR.add(text, ts)
    net = pos - neg
    label = "positive" if net > 0 else "negative" if net < 0 else "neutral"
    signal = math.tanh(net / 3.0)   # -1..+1 с насыщением при сильном перекосе
    return {
        "source": "geopolitics",
        "kind": "geo",
        "ticker": None,                 # рыночно-широкий фон, не привязан к тикеру
        "channel": channel or "geo",
        "text": (text or "").strip().replace("\n", " ")[:500],
        "label": label,
        "score": float(max(pos, neg)),
        "signal": round(signal, 3),
        "payload": {"pos": pos, "neg": neg, "net": net},
        "ts": ts,
    }
