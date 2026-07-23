"""
MOODEX — RSS/News Collector
Собирает финансовые новости из RSS-лент:
- Smart-lab.ru
- РБК (rbc.ru)
- Investing.com (ru)
- Финам

Новости анализируются на тональность и упоминания тикеров.
"""
import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Optional
import xml.etree.ElementTree as ET
import httpx

logger = logging.getLogger(__name__)

RSS_SOURCES = [
    {
        "name": "Smart-lab",
        "url": "https://smart-lab.ru/rss/",   # обновлённый URL
        "weight": 1.5,
    },
    {
        "name": "РБК Инвестиции",
        "url": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
        "weight": 1.2,
    },
    {
        "name": "Investing.com RU",
        "url": "https://ru.investing.com/rss/news.rss",
        "weight": 1.0,
    },
    {
        "name": "Финам Новости",
        "url": "https://www.finam.ru/analysis/newsitem/rss/",   # обновлённый URL
        "weight": 1.0,
    },
    {
        "name": "БКС Экспресс",
        "url": "https://bcs-express.ru/rss",   # замена Коммерсанту (работающий)
        "weight": 1.1,
    },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MOODEX/1.0)",
    "Accept": "application/rss+xml, application/xml, text/xml",
}


@dataclass
class NewsItem:
    item_id: str
    source: str
    title: str
    description: str
    url: str
    timestamp: datetime
    weight: float = 1.0

    @property
    def full_text(self) -> str:
        """Полный текст для анализа"""
        return f"{self.title}. {self.description}"


class RSSCollector:
    """
    Коллектор новостей из RSS-лент.
    Опрашивает источники каждые 5 минут.
    """

    def __init__(
        self,
        sources: list[dict] = None,
        poll_interval: int = 300,  # 5 минут
    ):
        self.sources = sources or RSS_SOURCES
        self.poll_interval = poll_interval
        self._seen_ids: set[str] = set()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._running = False

    async def fetch_feed(
        self, client: httpx.AsyncClient, source: dict
    ) -> list[NewsItem]:
        """Загрузить и распарсить RSS-ленту"""
        try:
            resp = await client.get(
                source["url"],
                headers=HEADERS,
                timeout=15,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                return []

            root = ET.fromstring(resp.text)
            channel = root.find("channel")
            if channel is None:
                channel = root  # Atom format

            items = []
            for item in channel.findall("item")[:20]:  # последние 20 новостей
                title = (item.findtext("title") or "").strip()
                desc = (item.findtext("description") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = item.findtext("pubDate") or ""
                guid = item.findtext("guid") or link or title

                if not title or guid in self._seen_ids:
                    continue

                # Парсим дату
                try:
                    ts = parsedate_to_datetime(pub_date)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)

                items.append(NewsItem(
                    item_id=guid,
                    source=source["name"],
                    title=title,
                    description=desc[:300],
                    url=link,
                    timestamp=ts,
                    weight=source.get("weight", 1.0),
                ))
                self._seen_ids.add(guid)

            return items

        except Exception as e:
            logger.debug(f"RSS fetch error for {source['name']}: {e}")
            return []

    async def _poll_loop(self):
        async with httpx.AsyncClient() as client:
            while self._running:
                total = 0
                for source in self.sources:
                    items = await self.fetch_feed(client, source)
                    for item in items:
                        await self._queue.put(item)
                        total += 1
                    await asyncio.sleep(1)

                if total > 0:
                    logger.info(f"📰 RSS: +{total} новых новостей")

                await asyncio.sleep(self.poll_interval)

    async def start(self):
        self._running = True
        logger.info(f"📰 RSS коллектор запущен ({len(self.sources)} источников)")
        asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._running = False

    async def listen(self):
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield item
            except asyncio.TimeoutError:
                continue
