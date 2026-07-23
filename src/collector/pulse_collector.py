"""
MOODEX — Pulse Collector (Т-Инвестиции Пульс)
Собирает посты из социальной сети Пульс по тикерам MOEX.
Публичный API — не требует авторизации.

Пульс: https://www.tinkoff.ru/invest/social/
"""
import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

PULSE_API = "https://www.tinkoff.ru/api/invest-gw/social/v1/post/instrument"

# Топ тикеры для мониторинга в Пульсе
PULSE_TICKERS = [
    "SBER", "GAZP", "LKOH", "YNDX", "NVTK", "ROSN", "TATN",
    "GMKN", "TCSG", "VTBR", "MGNT", "AFLT", "OZON", "PLZL",
    "CHMF", "NLMK", "MAGN", "POSI", "SMLT", "VKCO",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://www.tinkoff.ru",
    "Referer": "https://www.tinkoff.ru/invest/",
}


@dataclass
class PulsePost:
    post_id: str
    ticker: str
    text: str
    likes: int
    timestamp: datetime
    author: str = ""

    def to_raw_message_dict(self) -> dict:
        return {
            "message_id": hash(self.post_id),
            "channel": "pulse",
            "channel_title": "Пульс Т-Инвестиции",
            "text": self.text,
            "timestamp": self.timestamp,
            "views": self.likes,
        }


class PulseCollector:
    """
    Коллектор постов из Пульса по тикерам.
    Опрашивает API каждые N секунд.
    """

    def __init__(
        self,
        tickers: list[str] = None,
        poll_interval: int = 60,   # секунд между опросами
        posts_per_ticker: int = 20,
    ):
        self.tickers = tickers or PULSE_TICKERS
        self.poll_interval = poll_interval
        self.posts_per_ticker = posts_per_ticker
        self._seen_ids: set[str] = set()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._running = False

    async def fetch_ticker_posts(
        self, client: httpx.AsyncClient, ticker: str
    ) -> list[PulsePost]:
        """Загрузить последние посты по тикеру"""
        try:
            resp = await client.get(
                PULSE_API,
                params={"ticker": ticker, "limit": self.posts_per_ticker},
                headers=HEADERS,
                timeout=10,
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            posts = data.get("payload", {}).get("items", [])
            result = []

            for p in posts:
                post_id = p.get("id", "")
                if post_id in self._seen_ids:
                    continue

                text = p.get("content", {}).get("text", "")
                if not text:
                    continue

                likes = p.get("likesCount", 0)
                created = p.get("inserted", "")
                try:
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except Exception:
                    ts = datetime.now(timezone.utc)

                author = p.get("owner", {}).get("nickname", "")

                result.append(PulsePost(
                    post_id=post_id,
                    ticker=ticker,
                    text=text,
                    likes=likes,
                    timestamp=ts,
                    author=author,
                ))
                self._seen_ids.add(post_id)

            return result

        except Exception as e:
            logger.debug(f"Pulse fetch error for {ticker}: {e}")
            return []

    async def _poll_loop(self):
        """Основной цикл опроса"""
        async with httpx.AsyncClient() as client:
            while self._running:
                new_posts = 0
                for ticker in self.tickers:
                    posts = await self.fetch_ticker_posts(client, ticker)
                    for post in posts:
                        await self._queue.put(post)
                        new_posts += 1
                    await asyncio.sleep(0.3)  # пауза между запросами

                if new_posts > 0:
                    logger.info(f"📱 Пульс: +{new_posts} новых постов")

                await asyncio.sleep(self.poll_interval)

    async def start(self):
        self._running = True
        logger.info(f"📱 Пульс коллектор запущен ({len(self.tickers)} тикеров)")
        asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._running = False

    async def listen(self):
        """Асинхронный генератор новых постов"""
        while self._running:
            try:
                post = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield post
            except asyncio.TimeoutError:
                continue
