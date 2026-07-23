"""
MOODEX — слой базы данных (SQLAlchemy async)

Работает "из коробки" на SQLite (файл в постоянном томе /app/data),
и на PostgreSQL, если задать DATABASE_URL, например:
    DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/moodex

Здесь хранятся каналы, добавленные вручную через дашборд, чтобы они
переживали перезапуски и редеплой.
"""
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import String, Integer, Float, Boolean, DateTime, select, delete
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config.settings import DATABASE_URL, CHANNELS_FILE

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Channel(Base):
    """Telegram-канал, добавленный вручную для мониторинга."""
    __tablename__ = "channels"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    members: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    joined: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "title": self.title,
            "members": self.members,
            "status": self.status,
            "source": self.source,
            "joined": self.joined,
        }


class Prediction(Base):
    """
    Прогноз AI-агента по тикеру — «память» системы.

    После истечения горизонта прогноз оценивается по реальной цене,
    что даёт материал для обучения и расчёта точности (backtest).
    """
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    horizon_hours: Mapped[int] = mapped_column(Integer, default=24)

    # Признаки на момент прогноза
    sentiment_index: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sentiment_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    technical_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    combined_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    direction: Mapped[str] = mapped_column(String(8), default="flat")  # up/down/flat
    price_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Результат (заполняется позже)
    realized_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    correct: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    evaluated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "horizon_hours": self.horizon_hours,
            "sentiment_index": self.sentiment_index,
            "sentiment_signal": self.sentiment_signal,
            "technical_score": self.technical_score,
            "combined_score": round(self.combined_score, 3),
            "confidence": round(self.confidence, 3),
            "direction": self.direction,
            "price_at": self.price_at,
            "realized_price": self.realized_price,
            "realized_return": (
                round(self.realized_return, 3)
                if self.realized_return is not None else None
            ),
            "correct": self.correct,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
        }


class Setting(Base):
    """Простое key-value хранилище (веса модели, флаги и т.п.)."""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(4096), default="")


class SentimentDaily(Base):
    """
    Ежедневный снимок настроения по тикеру — копим историю, чтобы в будущем
    честно бэктестить связку «настроение + техника» на реальных данных.
    """
    __tablename__ = "sentiment_daily"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)  # "YYYY-MM-DD:TICKER"
    date: Mapped[str] = mapped_column(String(10), index=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    sentiment_index: Mapped[float] = mapped_column(Float, default=50.0)
    avg_signal: Mapped[float] = mapped_column(Float, default=0.0)
    msg_count: Mapped[int] = mapped_column(Integer, default=0)


def _ensure_sqlite_dir():
    """Для SQLite создаём директорию под файл БД (иначе connect упадёт)."""
    if "sqlite" in DATABASE_URL:
        # sqlite+aiosqlite:///./data/moodex.db -> ./data/moodex.db
        db_path = DATABASE_URL.split(":///", 1)[-1]
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir()
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Защита от повторной/параллельной инициализации БД при старте
_setup_lock = asyncio.Lock()
_setup_done = False


# ─── Инициализация и миграция ────────────────────────────────────────────────

async def init_db():
    """Создать таблицы, если их ещё нет (идемпотентно)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info(f"🗄️  База данных готова ({DATABASE_URL.split('://', 1)[0]})")


async def migrate_from_json():
    """
    Перенести каналы из старого data/channels.json в БД (одноразово).
    После импорта файл переименовывается, чтобы не импортировать повторно.
    """
    path = Path(CHANNELS_FILE)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Не удалось прочитать {CHANNELS_FILE} для миграции: {e}")
        return
    if not data:
        return

    async with async_session() as session:
        for info in data:
            if not info.get("username"):
                continue
            await session.merge(Channel(
                username=info["username"],
                title=info.get("title", ""),
                members=info.get("members"),
                status=info.get("status", "active"),
                source=info.get("source", "manual"),
                joined=bool(info.get("joined", False)),
            ))
        await session.commit()

    try:
        path.rename(path.with_suffix(".json.imported"))
    except Exception:
        pass
    logger.info(f"🔄 Импортировано каналов из JSON в БД: {len(data)}")


async def setup_db():
    """
    Полная подготовка БД: создать таблицы + перенести старый JSON.

    Защищено от повторного/параллельного запуска: при старте setup_db()
    вызывается и из API, и из Telegram-пайплайна в одном event loop —
    без блокировки PostgreSQL падал бы на гонке CREATE TABLE.
    """
    global _setup_done
    async with _setup_lock:
        if _setup_done:
            return
        await init_db()
        await migrate_from_json()
        _setup_done = True


# ─── CRUD по каналам ─────────────────────────────────────────────────────────

async def list_channels() -> list[dict]:
    """Все сохранённые каналы (по времени добавления)."""
    async with async_session() as session:
        result = await session.execute(select(Channel).order_by(Channel.added_at))
        return [c.to_dict() for c in result.scalars().all()]


async def get_channel_usernames() -> list[str]:
    """Только username-ы сохранённых каналов."""
    async with async_session() as session:
        result = await session.execute(select(Channel.username).order_by(Channel.added_at))
        return [row[0] for row in result.all()]


async def channel_exists(username: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(Channel.username).where(Channel.username == username)
        )
        return result.first() is not None


async def upsert_channel(info: dict) -> None:
    """Добавить или обновить канал."""
    async with async_session() as session:
        await session.merge(Channel(
            username=info["username"],
            title=info.get("title", ""),
            members=info.get("members"),
            status=info.get("status", "active"),
            source=info.get("source", "manual"),
            joined=bool(info.get("joined", False)),
        ))
        await session.commit()


async def delete_channel(username: str) -> bool:
    """Удалить канал. Возвращает True, если что-то удалилось."""
    async with async_session() as session:
        result = await session.execute(
            delete(Channel).where(Channel.username == username)
        )
        await session.commit()
        return result.rowcount > 0


# ─── Прогнозы (память агента) ─────────────────────────────────────────────────

async def add_prediction(data: dict) -> int:
    """Сохранить новый прогноз. Возвращает id."""
    async with async_session() as session:
        pred = Prediction(
            ticker=data["ticker"],
            horizon_hours=data.get("horizon_hours", 24),
            sentiment_index=data.get("sentiment_index"),
            sentiment_signal=data.get("sentiment_signal"),
            technical_score=data.get("technical_score"),
            combined_score=data.get("combined_score", 0.0),
            confidence=data.get("confidence", 0.0),
            direction=data.get("direction", "flat"),
            price_at=data.get("price_at"),
        )
        session.add(pred)
        await session.commit()
        return pred.id


async def list_recent_predictions(limit: int = 50, ticker: Optional[str] = None) -> list[dict]:
    async with async_session() as session:
        stmt = select(Prediction).order_by(Prediction.created_at.desc()).limit(limit)
        if ticker:
            stmt = stmt.where(Prediction.ticker == ticker.upper())
        result = await session.execute(stmt)
        return [p.to_dict() for p in result.scalars().all()]


async def get_due_predictions() -> list[Prediction]:
    """Прогнозы, у которых истёк горизонт и ещё нет оценки результата."""
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        result = await session.execute(
            select(Prediction).where(Prediction.correct.is_(None))
        )
        due = []
        for p in result.scalars().all():
            created = p.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created is None:
                continue
            if now - created >= timedelta(hours=p.horizon_hours):
                due.append(p)
        return due


async def evaluate_prediction(
    pred_id: int, realized_price: float, realized_return: float, correct: bool
) -> None:
    async with async_session() as session:
        pred = await session.get(Prediction, pred_id)
        if pred is None:
            return
        pred.realized_price = realized_price
        pred.realized_return = realized_return
        pred.correct = correct
        pred.evaluated_at = datetime.now(timezone.utc)
        await session.commit()


async def get_evaluated_predictions() -> list[dict]:
    """Все оценённые прогнозы (для обучения и статистики)."""
    async with async_session() as session:
        result = await session.execute(
            select(Prediction).where(Prediction.correct.is_not(None))
        )
        return [p.to_dict() for p in result.scalars().all()]


async def accuracy_stats(ticker: Optional[str] = None) -> dict:
    """Точность прогнозов: сколько всего, оценено, верных, доля."""
    async with async_session() as session:
        stmt = select(Prediction)
        if ticker:
            stmt = stmt.where(Prediction.ticker == ticker.upper())
        result = await session.execute(stmt)
        preds = result.scalars().all()

    total = len(preds)
    evaluated = [p for p in preds if p.correct is not None]
    correct = [p for p in evaluated if p.correct]
    accuracy = (len(correct) / len(evaluated)) if evaluated else None
    return {
        "total": total,
        "evaluated": len(evaluated),
        "correct": len(correct),
        "accuracy": round(accuracy, 3) if accuracy is not None else None,
        "pending": total - len(evaluated),
    }


# ─── Key-value настройки (веса модели и пр.) ──────────────────────────────────

async def get_setting(key: str) -> Optional[str]:
    async with async_session() as session:
        row = await session.get(Setting, key)
        return row.value if row else None


async def set_setting(key: str, value: str) -> None:
    async with async_session() as session:
        await session.merge(Setting(key=key, value=value))
        await session.commit()


# ─── Ежедневные снимки настроения (для будущего бэктеста) ─────────────────────

async def upsert_sentiment_daily(date: str, ticker: str, sentiment_index: float,
                                 avg_signal: float, msg_count: int) -> None:
    async with async_session() as session:
        await session.merge(SentimentDaily(
            key=f"{date}:{ticker}", date=date, ticker=ticker.upper(),
            sentiment_index=sentiment_index, avg_signal=avg_signal, msg_count=msg_count,
        ))
        await session.commit()


async def sentiment_history(ticker: Optional[str] = None, limit: int = 2000) -> list[dict]:
    async with async_session() as session:
        stmt = select(SentimentDaily).order_by(SentimentDaily.date)
        if ticker:
            stmt = stmt.where(SentimentDaily.ticker == ticker.upper())
        result = await session.execute(stmt.limit(limit))
        return [{"date": r.date, "ticker": r.ticker, "sentiment_index": r.sentiment_index,
                 "avg_signal": r.avg_signal, "msg_count": r.msg_count}
                for r in result.scalars().all()]


async def sentiment_history_days() -> int:
    """Сколько уникальных дней уже накоплено (для оценки готовности к бэктесту)."""
    async with async_session() as session:
        result = await session.execute(select(SentimentDaily.date).distinct())
        return len(result.all())
