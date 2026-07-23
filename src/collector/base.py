"""
MOODEX — общий тип сообщения для всех источников.

Позволяет разным коллекторам (Telegram, Пульс Тинькофф, новости) отдавать
данные в единый pipeline. Дублирует минимальный интерфейс RawMessage
(поля text/channel/timestamp), но не зависит от Telethon.
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class SourceMessage:
    text: str
    channel: str          # идентификатор источника (напр. "pulse:SBER", "news:smartlab")
    timestamp: datetime
    source: str = "generic"   # telegram / pulse / news

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "channel": self.channel,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
        }
