"""
MOODEX — Бэкфилл истории настроений

Выкачивает историю сообщений из Telegram-каналов (Telethon хранит её за годы),
размечает тональность и тикеры, агрегирует в ДНЕВНОЕ настроение по тикерам и
складывает в БД (таблица sentiment_daily) с реальными историческими датами.

Это даёт честный исторический датасет настроений (без заглядывания в будущее),
на котором потом можно бэктестить связку «настроение + техника».
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone

from src.nlp.ticker_extractor import extract_tickers, is_noise
from src.nlp.sentiment_analyzer import keyword_sentiment
from src import db

logger = logging.getLogger(__name__)


def messages_to_daily_sentiment(messages) -> dict:
    """
    Превратить список сообщений (с .text и .timestamp) в дневное настроение.
    Возвращает {(date, ticker): {"avg_signal","count","sentiment_index"}}.
    Чистая функция — легко тестировать.
    """
    buckets: dict = defaultdict(list)   # (date, ticker) -> [signals]
    for m in messages:
        text = getattr(m, "text", None) or (m.get("text") if isinstance(m, dict) else None)
        ts = getattr(m, "timestamp", None) or (m.get("timestamp") if isinstance(m, dict) else None)
        if not text or not ts:
            continue
        if is_noise(text):
            continue
        tickers = extract_tickers(text)
        if not tickers or len(tickers) > 4:
            continue
        signal = keyword_sentiment(text).signal
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
        date = ts.strftime("%Y-%m-%d")
        for t in tickers:
            buckets[(date, t)].append(signal)

    result = {}
    for (date, ticker), signals in buckets.items():
        avg = sum(signals) / len(signals)
        result[(date, ticker)] = {
            "avg_signal": round(avg, 4),
            "count": len(signals),
            "sentiment_index": round((avg + 1) / 2 * 100, 1),
        }
    return result


async def run_backfill(collector, days: int = 730, per_channel_limit: int = 3000,
                       progress=None) -> dict:
    """
    Выкачать историю из всех каналов коллектора и сохранить дневное настроение.
    progress — необязательный callback(dict) для отчёта о ходе.
    """
    if collector is None or collector.client is None:
        return {"error": "Telegram коллектор не запущен"}

    offset = datetime.now(timezone.utc)
    all_messages = []
    channels = list(collector.channels)
    for idx, ch in enumerate(channels):
        try:
            msgs = await collector.fetch_history(ch, limit=per_channel_limit, offset_date=None)
            # оставляем только за нужный период
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
            msgs = [m for m in msgs if m.timestamp.timestamp() >= cutoff]
            all_messages.extend(msgs)
            logger.info(f"📥 Бэкфилл @{ch}: {len(msgs)} сообщений за период")
            if progress:
                progress({"channel": ch, "done": idx + 1, "total": len(channels),
                          "messages": len(all_messages)})
        except Exception as e:
            logger.warning(f"Бэкфилл @{ch}: {e}")

    daily = messages_to_daily_sentiment(all_messages)
    rows = 0
    for (date, ticker), v in daily.items():
        try:
            await db.upsert_sentiment_daily(date, ticker, v["sentiment_index"],
                                            v["avg_signal"], v["count"])
            rows += 1
        except Exception as e:
            logger.debug(f"upsert {date}:{ticker}: {e}")

    dates = {d for (d, _) in daily.keys()}
    summary = {
        "messages_processed": len(all_messages),
        "rows_written": rows,
        "days_covered": len(dates),
        "tickers": len({t for (_, t) in daily.keys()}),
    }
    logger.info(f"✅ Бэкфилл завершён: {summary}")
    return summary


async def run_pulse_backfill(days: int = 730, tickers=None,
                             per_ticker_pages: int = 60, progress=None) -> dict:
    """
    Выкачать историю Пульса по тикерам и сохранить дневное настроение.
    Не требует Telegram-коллектора — Пульс тянется по HTTP.
    """
    from src.collector.pulse_collector import PulseCollector, PULSE_TICKERS
    from config.settings import MOEX_TICKERS

    tickers = tickers or PULSE_TICKERS or list(MOEX_TICKERS.keys())
    collector = PulseCollector(tickers=tickers)
    all_messages = []
    for idx, t in enumerate(tickers):
        try:
            msgs = await collector.fetch_history(t, days=days, max_pages=per_ticker_pages)
            all_messages.extend(msgs)
            logger.info(f"📥 Пульс @{t}: {len(msgs)} постов")
            if progress:
                progress({"channel": f"pulse:{t}", "done": idx + 1,
                          "total": len(tickers), "messages": len(all_messages)})
        except Exception as e:
            logger.warning(f"Пульс история {t}: {e}")

    daily = messages_to_daily_sentiment(all_messages)
    rows = 0
    for (date, ticker), v in daily.items():
        try:
            # объединяем с уже накопленным настроением за день (Telegram + Пульс)
            await db.upsert_sentiment_daily(date, ticker, v["sentiment_index"],
                                            v["avg_signal"], v["count"])
            rows += 1
        except Exception as e:
            logger.debug(f"upsert {date}:{ticker}: {e}")

    dates = {d for (d, _) in daily.keys()}
    summary = {
        "source": "pulse",
        "messages_processed": len(all_messages),
        "rows_written": rows,
        "days_covered": len(dates),
        "tickers": len({t for (_, t) in daily.keys()}),
    }
    logger.info(f"✅ Пульс-бэкфилл завершён: {summary}")
    return summary
