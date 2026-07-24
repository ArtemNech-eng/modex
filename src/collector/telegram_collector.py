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
    TELEGRAM_SESSION, TELEGRAM_STRING_SESSION, TELEGRAM_CHANNELS,
    TELEGRAM_PROXY, TELEGRAM_CONNECTION_RETRIES,
)

logger = logging.getLogger(__name__)


def _build_proxy():
    """
    Разобрать TELEGRAM_PROXY в параметры Telethon.
    Возвращает (proxy, connection_cls):
      • SOCKS5/SOCKS4/HTTP:  socks5://[user:pass@]host:port  → (PySocks-tuple, None)
      • MTProto (для Telegram в РФ): mtproto://<secret>@host:port → ((host,port,secret), ConnCls)
    При проблемах — (None, None).
    """
    if not TELEGRAM_PROXY:
        return None, None
    from urllib.parse import urlparse
    try:
        u = urlparse(TELEGRAM_PROXY)
        scheme = (u.scheme or "socks5").lower()

        # MTProto-прокси — Telegram-специфичный, обычно надёжнее в РФ
        if scheme in ("mtproto", "mtproxy"):
            secret = u.username or ""
            if not (u.hostname and u.port and secret):
                logger.warning("TELEGRAM_PROXY (mtproto) должен быть вида mtproto://<secret>@host:port")
                return None, None
            try:
                from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate as Conn
            except Exception as e:
                logger.warning(f"MTProxy недоступен в этой версии Telethon: {e}")
                return None, None
            return (u.hostname, u.port, secret), Conn

        # SOCKS5/SOCKS4/HTTP через PySocks
        import socks  # PySocks
        ptype = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4,
                 "http": socks.HTTP}.get(scheme, socks.SOCKS5)
        if not u.hostname or not u.port:
            logger.warning(f"TELEGRAM_PROXY задан некорректно: {TELEGRAM_PROXY!r}")
            return None, None
        if u.username and u.password:
            return (ptype, u.hostname, u.port, True, u.username, u.password), None
        return (ptype, u.hostname, u.port), None
    except ImportError:
        logger.warning("TELEGRAM_PROXY задан, но не установлен PySocks (pip install PySocks) — игнорирую прокси")
        return None, None
    except Exception as e:
        logger.warning(f"Не удалось разобрать TELEGRAM_PROXY: {e}")
        return None, None


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

        # Общие параметры устойчивости соединения (авто-реконнект, повторы,
        # опциональный прокси) — снижают «то подключается, то нет».
        proxy, conn_cls = _build_proxy()
        common = dict(
            connection_retries=TELEGRAM_CONNECTION_RETRIES,
            retry_delay=2,
            timeout=30,
            request_retries=5,
            auto_reconnect=True,
        )
        if proxy:
            common["proxy"] = proxy
            if conn_cls:
                common["connection"] = conn_cls
            logger.info("🌐 Telegram через прокси (TELEGRAM_PROXY задан)")

        if TELEGRAM_STRING_SESSION:
            from telethon.sessions import StringSession
            # Очищаем от лишних символов (пробелы, переносы, не-ASCII)
            clean_session = ''.join(
                c for c in TELEGRAM_STRING_SESSION.strip()
                if ord(c) < 128
            )
            logger.info(f"🔑 Строковая сессия загружена ({len(clean_session)} символов)")
            session = StringSession(clean_session)
            self.client = TelegramClient(session, TELEGRAM_API_ID, TELEGRAM_API_HASH, **common)
            # Подключаемся без интерактивного ввода, с несколькими попытками
            await self._connect_with_retry()
            if not await self.client.is_user_authorized():
                raise RuntimeError(
                    "❌ Сессия недействительна! Сгенерируй новую через Colab "
                    "и обнови TELEGRAM_STRING_SESSION в Coolify."
                )
            me = await self.client.get_me()
            logger.info(f"✅ Авторизован как {me.first_name} (@{me.username})")
        else:
            # Файловая сессия. Кладём файл на ПОСТОЯННЫЙ том (data/), чтобы он не
            # терялся при редеплое; иначе каждый раз требовалась бы новая авторизация.
            import os
            import sys
            session_path = TELEGRAM_SESSION
            if not os.path.dirname(session_path):
                os.makedirs("data", exist_ok=True)
                session_path = os.path.join("data", session_path)

            self.client = TelegramClient(session_path, TELEGRAM_API_ID, TELEGRAM_API_HASH, **common)
            await self._connect_with_retry()
            if not await self.client.is_user_authorized():
                # В контейнере нет интерактивного ввода кода из SMS — не зависаем,
                # а даём чёткую инструкцию. Надёжный способ для деплоя — строковая сессия.
                if not sys.stdin or not sys.stdin.isatty():
                    raise RuntimeError(
                        "Telegram не авторизован, а интерактивный ввод недоступен (контейнер). "
                        "Сгенерируй строковую сессию локально: `python scripts/auth_telegram.py`, "
                        "и добавь TELEGRAM_STRING_SESSION в переменные окружения — это убирает "
                        "проблему «то подключается, то нет»."
                    )
                await self.client.start(phone=TELEGRAM_PHONE)
            logger.info(f"🔑 Файловая сессия: {session_path}.session (на постоянном томе)")
        logger.info("✅ Подключено к Telegram")
        self._running = True

        # Регистрируем обработчик новых сообщений
        @self.client.on(events.NewMessage(chats=self.channels))
        async def handler(event: events.NewMessage.Event):
            msg = await self._parse_message(event.message)
            if msg and msg.text.strip():
                await self._message_queue.put(msg)

        logger.info(f"👂 Слушаем {len(self.channels)} каналов: {self.channels}")

    async def _connect_with_retry(self, attempts: int = 3, delay: float = 3.0):
        """
        Подключиться к Telegram с несколькими попытками. Спасает от разовых
        сетевых сбоев при старте (нестабильный доступ к дата-центрам Telegram).
        """
        last_err = None
        for i in range(1, attempts + 1):
            try:
                await self.client.connect()
                return
            except Exception as e:
                last_err = e
                logger.warning(f"Telegram connect: попытка {i}/{attempts} не удалась: {e}")
                if i < attempts:
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"Не удалось подключиться к Telegram за {attempts} попыток: {last_err}. "
            f"Если хостинг блокирует Telegram — задай TELEGRAM_PROXY (socks5://host:port)."
        )

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
