"""
MOODEX — Telegram Collector
Асинхронный сборщик сообщений из торговых Telegram-чатов.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional
from dataclasses import dataclass, asdict
import json

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, Message

from config.settings import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE,
    TELEGRAM_SESSION, TELEGRAM_STRING_SESSION, TELEGRAM_CHANNELS
)

logger = logging.getLogger(__name__)


@dataclass
class RawMessage:
    """Сырое сообщение из Telegram"""
    message_id: int
    channel: str          # username канала
    channel_title: str    # Название канала
    text: str
    timestamp: datetime
    views: Optional[int] = None
    forwards: Optional[int] = None
    reply_to: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


class TelegramCollector:
    """
    Собирает сообщения из Telegram-каналов в реальном времени.
    
    Использование:
        collector = TelegramCollector()
        await collector.start()
        
        # Слушаем новые сообщения
        async for msg in collector.listen():
            print(msg)
    """

    def __init__(self, channels: list[str] = None):
        self.channels = channels or TELEGRAM_CHANNELS
        self.client: Optional[TelegramClient] = None
        self._message_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._running = False

    async def start(self):
        """Подключиться к Telegram и начать слушать каналы"""
        logger.info("Подключение к Telegram...")

        # Если есть строковая сессия (для Docker/Coolify) — используем её
        # Иначе — файловая сессия (для локального запуска)
        if TELEGRAM_STRING_SESSION:
            from telethon.sessions import StringSession
            session = StringSession(TELEGRAM_STRING_SESSION)
            logger.info("🔑 Используем строковую сессию (Docker-режим)")
        else:
            session = TELEGRAM_SESSION
            logger.info("🔑 Используем файловую сессию (локальный режим)")

        self.client = TelegramClient(session, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await self.client.start(phone=TELEGRAM_PHONE if not TELEGRAM_STRING_SESSION else None)
        logger.info("✅ Подключено к Telegram")
        self._running = True

        # Регистрируем обработчик новых сообщений
        @self.client.on(events.NewMessage(chats=self.channels))
        async def handler(event: events.NewMessage.Event):
            msg = await self._parse_message(event.message)
            if msg and msg.text.strip():
                await self._message_queue.put(msg)

        logger.info(f"👂 Слушаем {len(self.channels)} каналов: {self.channels}")

    async def stop(self):
        """Отключиться от Telegram"""
        self._running = False
        if self.client:
            await self.client.disconnect()
        logger.info("Коллектор остановлен")

    async def _parse_message(self, message: Message) -> Optional[RawMessage]:
        """Конвертируем Telethon-сообщение в наш датакласс"""
        try:
            if not message.text:
                return None

            # Получаем информацию о канале
            chat = await message.get_chat()
            if hasattr(chat, "username") and chat.username:
                channel = chat.username
            else:
                channel = str(chat.id)

            channel_title = getattr(chat, "title", channel)

            return RawMessage(
                message_id=message.id,
                channel=channel,
                channel_title=channel_title,
                text=message.text,
                timestamp=message.date.replace(tzinfo=timezone.utc),
                views=getattr(message, "views", None),
                forwards=getattr(message, "forwards", None),
                reply_to=message.reply_to_msg_id if message.is_reply else None,
            )
        except Exception as e:
            logger.warning(f"Ошибка при парсинге сообщения: {e}")
            return None

    async def listen(self) -> AsyncGenerator[RawMessage, None]:
        """
        Асинхронный генератор новых сообщений.
        
        Usage:
            async for msg in collector.listen():
                process(msg)
        """
        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self._message_queue.get(), timeout=1.0
                )
                yield msg
            except asyncio.TimeoutError:
                continue

    async def fetch_history(
        self,
        channel: str,
        limit: int = 1000,
        offset_date: Optional[datetime] = None
    ) -> list[RawMessage]:
        """
        Загрузить историю сообщений из канала (для бэктестинга).
        
        Args:
            channel: username канала
            limit: сколько сообщений загрузить
            offset_date: до какой даты брать сообщения
        
        Returns:
            Список RawMessage, отсортированных по времени
        """
        if not self.client:
            raise RuntimeError("Клиент не запущен. Вызовите start() сначала.")

        messages = []
        logger.info(f"📥 Загружаем историю из @{channel} (limit={limit})...")

        async for message in self.client.iter_messages(
            channel,
            limit=limit,
            offset_date=offset_date
        ):
            msg = await self._parse_message(message)
            if msg:
                messages.append(msg)

        messages.sort(key=lambda m: m.timestamp)
        logger.info(f"✅ Загружено {len(messages)} сообщений из @{channel}")
        return messages

    async def fetch_all_history(
        self,
        limit_per_channel: int = 500
    ) -> list[RawMessage]:
        """Загрузить историю из всех каналов"""
        all_messages = []
        for channel in self.channels:
            try:
                msgs = await self.fetch_history(channel, limit=limit_per_channel)
                all_messages.extend(msgs)
            except Exception as e:
                logger.warning(f"Не удалось загрузить историю из @{channel}: {e}")

        all_messages.sort(key=lambda m: m.timestamp)
        logger.info(f"📊 Всего загружено: {len(all_messages)} сообщений")
        return all_messages
