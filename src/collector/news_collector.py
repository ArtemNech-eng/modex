"""
MOODEX — Коллектор новостей (RSS)

Периодически опрашивает финансовые RSS-ленты, превращает заголовок+описание
каждой новости в SourceMessage и отдаёт в общий pipeline.

Ленты настраиваются через NEWS_FEEDS (env, через запятую). Парсинг — stdlib
xml.etree, без внешних зависимостей.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import AsyncGenerator, Optional
import xml.etree.ElementTree as ET

import httpx

from config.settings import NEWS_FEEDS
from src.collector.base import SourceMessage

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MOODEX/1.0)"}
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _feed_name(url: str) -> str:
    m = re.sub(r"^https?://(www\.)?", "", url)
    return m.split("/")[0]


def _parse_pubdate(raw: Optional[str]) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)


def parse_rss(xml_text: str, feed_url: str) -> list[SourceMessage]:
    """Разобрать RSS/Atom в список SourceMessage."""
    out: list[SourceMessage] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        logger.debug(f"RSS parse error {feed_url}: {e}")
        return out

    name = _feed_name(feed_url)
    # RSS 2.0: channel/item ; Atom: entry
    items = root.findall(".//item")
    if not items:
        # Atom namespace-agnostic поиск entry
        items = [e for e in root.iter() if e.tag.endswith("entry")]

    for it in items:
        def _find(tag):
            el = it.find(tag)
            if el is None:
                el = next((c for c in it.iter() if c.tag.endswith(tag)), None)
            return el.text if el is not None else ""

        title = _find("title")
        desc = _find("description") or _find("summary")
        link = _find("link") or _find("id")
        pub = _find("pubDate") or _find("published") or _find("updated")

        text = f"{_strip_html(title)}. {_strip_html(desc)}".strip(". ").strip()
        if not text:
            continue
        msg = SourceMessage(
            text=text,
            channel=f"news:{name}",
            timestamp=_parse_pubdate(pub),
            source="news",
        )
        msg.link = (link or text[:60]).strip()  # type: ignore[attr-defined]
        out.append(msg)
    return out


class NewsCollector:
    """Опрашивает RSS-ленты и отдаёт новые новости."""

    def __init__(self, feeds: Optional[list[str]] = None, interval: int = 300):
        self.feeds = feeds or NEWS_FEEDS
        self.interval = interval
        self._seen: set[str] = set()
        self._running = False

    async def _fetch_feed(self, client: httpx.AsyncClient, url: str) -> list[SourceMessage]:
        try:
            resp = await client.get(url, headers=HEADERS)
            resp.raise_for_status()
            msgs = parse_rss(resp.text, url)
        except Exception as e:
            logger.debug(f"Новости {url}: {e}")
            return []
        fresh = []
        for m in msgs:
            key = getattr(m, "link", "") or m.text[:60]
            if key in self._seen:
                continue
            self._seen.add(key)
            fresh.append(m)
        return fresh

    async def listen(self) -> AsyncGenerator[SourceMessage, None]:
        self._running = True
        if not self.feeds:
            logger.info("📰 Новостные ленты не заданы (NEWS_FEEDS пуст)")
            return
        logger.info(f"📰 Новости: следим за {len(self.feeds)} лентами")
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # Первый проход — пометить существующее
            for url in self.feeds:
                await self._fetch_feed(client, url)
            while self._running:
                for url in self.feeds:
                    if not self._running:
                        break
                    for msg in await self._fetch_feed(client, url):
                        yield msg
                await asyncio.sleep(self.interval)

    def stop(self):
        self._running = False
