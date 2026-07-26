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

from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, select, delete
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


class TrackedTrader(Base):
    """
    Трейдер Пульса, чьи РЕАЛЬНЫЕ сделки мы отслеживаем («умные деньги»).

    Раньше список отслеживаемых трейдеров жил только в поле ввода дашборда
    (и в PULSE_TRACKED_AUTHORS из .env), поэтому после перезагрузки страницы
    и редеплоя терялся. Теперь он хранится здесь и переживает перезапуски.
    """
    __tablename__ = "tracked_traders"

    nickname: Mapped[str] = mapped_column(String(255), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    source: Mapped[str] = mapped_column(String(32), default="pulse")  # config | manual
    status: Mapped[str] = mapped_column(String(32), default="active")
    note: Mapped[str] = mapped_column(String(1024), default="")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "nickname": self.nickname,
            "title": self.title,
            "source": self.source,
            "status": self.status,
            "note": self.note,
            "added_at": self.added_at.isoformat() if self.added_at else None,
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

    # Решение по плейбуку Claude (Фаза B — измерение эффективности по режимам)
    regime: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)   # trend/range/squeeze_breakout/news_spike/unclear
    confluence_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rr_planned: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Результат (заполняется позже)
    realized_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    correct: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Интрадей-исход по пути цены: target|stop|breakeven|session (Фаза B + №3)
    outcome: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    realized_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # ВЗВЕШЕННЫЙ факт. R (риск = 1% от входа)
    mfe_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)       # макс. ход в нашу сторону, R (MFE)
    legs_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)      # ноги выхода [{frac,r,price,reason}] (JSON)
    evaluated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Разбор ошибок (post-mortem) — «почему сработало / не сработало»
    context_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # снимок драйверов на момент прогноза (JSON)
    post_mortem: Mapped[Optional[str]] = mapped_column(String, nullable=True)    # причина успеха/провала
    lesson: Mapped[Optional[str]] = mapped_column(String, nullable=True)         # короткий вывод-правило
    pm_tags: Mapped[Optional[str]] = mapped_column(String, nullable=True)        # теги причин через запятую
    pm_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        # МСК-время сигнала из снимка контекста — чтобы было видно в списке.
        _sig_msk = None
        if self.context_json:
            try:
                import json as _json
                _sig_msk = _json.loads(self.context_json).get("signal_time_msk")
            except Exception:
                _sig_msk = None
        return {
            "id": self.id,
            "ticker": self.ticker,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "signal_time_msk": _sig_msk,
            "horizon_hours": self.horizon_hours,
            "sentiment_index": self.sentiment_index,
            "sentiment_signal": self.sentiment_signal,
            "technical_score": self.technical_score,
            "combined_score": round(self.combined_score, 3),
            "confidence": round(self.confidence, 3),
            "direction": self.direction,
            "price_at": self.price_at,
            "regime": self.regime,
            "confluence_score": self.confluence_score,
            "rr_planned": self.rr_planned,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "realized_price": self.realized_price,
            "realized_return": (
                round(self.realized_return, 3)
                if self.realized_return is not None else None
            ),
            "outcome": self.outcome,
            "realized_r": (round(self.realized_r, 3)
                           if self.realized_r is not None else None),
            "mfe_r": (round(self.mfe_r, 3) if self.mfe_r is not None else None),
            "legs": (json.loads(self.legs_json) if self.legs_json else None),
            "correct": self.correct,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "post_mortem": self.post_mortem,
            "lesson": self.lesson,
            "pm_tags": [t for t in (self.pm_tags or "").split(",") if t],
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


class MarketEvent(Base):
    """
    Единое событие «базы знаний» для Claude: любое входящее данное с меткой
    времени — сообщение из чата, новость, пост/сделка Пульса, снимок стакана /
    цены / потока сделок Tinkoff. Реальное время + история в одном месте.

    Claude (и дашборд «Рынок») читают отсюда: по тикеру/источнику/времени.
    """
    __tablename__ = "market_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True,
        default=lambda: datetime.now(timezone.utc),
    )
    source: Mapped[str] = mapped_column(String(24), index=True)  # telegram|pulse|rss|pulse_deal|tinkoff
    kind: Mapped[str] = mapped_column(String(24), default="message")  # message|news|deal|orderbook|quote|trades
    ticker: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    channel: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)  # канал/автор/источник
    text: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    label: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON для структурных данных

    def to_dict(self) -> dict:
        import json as _json
        pl = None
        if self.payload:
            try:
                pl = _json.loads(self.payload)
            except Exception:
                pl = None
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "source": self.source,
            "kind": self.kind,
            "ticker": self.ticker,
            "channel": self.channel,
            "text": self.text,
            "label": self.label,
            "score": round(self.score, 3) if self.score is not None else None,
            "signal": round(self.signal, 3) if self.signal is not None else None,
            "payload": pl,
        }


class SessionFootprint(Base):
    """
    Кумулятивный footprint за торговый день: объём по РЕАЛЬНОЙ цене сделок со
    сплитом buy/sell, накопленный дедупом по watermark. Одна строка на тикер/день —
    компактно (сырые сделки не храним). Ключ: "YYYY-MM-DD:TICKER".
    """
    __tablename__ = "session_footprint"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    buckets: Mapped[str] = mapped_column(Text, default="{}")  # JSON {price: [buy, sell, total]}
    watermark: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # ISO ts последней сделки
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


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


# Колонки разбора ошибок, которые могли отсутствовать в старой схеме predictions.
_PREDICTION_ADDED_COLUMNS = {
    "context_json": "TEXT",
    "post_mortem": "TEXT",
    "lesson": "TEXT",
    "pm_tags": "TEXT",
    "pm_at": "TIMESTAMP",
    # Фаза B — плейбук-поля для измерения по режимам:
    "regime": "TEXT",
    "confluence_score": "INTEGER",
    "entry": "DOUBLE PRECISION",
    "stop": "DOUBLE PRECISION",
    "target": "DOUBLE PRECISION",
    "rr_planned": "DOUBLE PRECISION",
    # Фаза B — исход по пути цены:
    "outcome": "TEXT",
    "realized_r": "DOUBLE PRECISION",
    # №3 — управление сделкой:
    "mfe_r": "DOUBLE PRECISION",
    "legs_json": "TEXT",
}


async def migrate_schema():
    """
    Мягкая миграция: дописать новые колонки в существующую таблицу predictions.

    create_all() создаёт недостающие ТАБЛИЦЫ, но не добавляет колонки в уже
    существующие. Здесь добавляем колонки post-mortem, если их ещё нет —
    и на SQLite, и на PostgreSQL, идемпотентно.
    """
    is_sqlite = "sqlite" in DATABASE_URL
    async with engine.begin() as conn:
        if is_sqlite:
            res = await conn.exec_driver_sql("PRAGMA table_info(predictions)")
            existing = {row[1] for row in res.fetchall()}
            for col, typ in _PREDICTION_ADDED_COLUMNS.items():
                if col not in existing:
                    await conn.exec_driver_sql(
                        f'ALTER TABLE predictions ADD COLUMN "{col}" {typ}')
                    logger.info(f"🧩 Добавлена колонка predictions.{col}")
        else:
            for col, typ in _PREDICTION_ADDED_COLUMNS.items():
                await conn.exec_driver_sql(
                    f'ALTER TABLE predictions ADD COLUMN IF NOT EXISTS "{col}" {typ}')


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
        await migrate_schema()
        await migrate_from_json()
        # При первом запуске наполняем список трейдеров ников из конфига,
        # дальше он живёт в БД и редактируется через дашборд.
        try:
            from config.settings import PULSE_TRACKED_AUTHORS
            await seed_tracked_traders(PULSE_TRACKED_AUTHORS)
        except Exception as e:
            logger.warning(f"Не удалось засеять трейдеров из конфига: {e}")
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


# ─── CRUD по отслеживаемым трейдерам («умные деньги») ─────────────────────────

async def list_tracked_traders() -> list[dict]:
    """Все сохранённые трейдеры (по времени добавления)."""
    async with async_session() as session:
        result = await session.execute(
            select(TrackedTrader).order_by(TrackedTrader.added_at)
        )
        return [t.to_dict() for t in result.scalars().all()]


async def get_tracked_trader_nicks() -> list[str]:
    """Только ники сохранённых трейдеров."""
    async with async_session() as session:
        result = await session.execute(
            select(TrackedTrader.nickname).order_by(TrackedTrader.added_at)
        )
        return [row[0] for row in result.all()]


async def trader_exists(nickname: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(TrackedTrader.nickname).where(TrackedTrader.nickname == nickname)
        )
        return result.first() is not None


async def upsert_tracked_trader(info: dict) -> None:
    """Добавить или обновить отслеживаемого трейдера."""
    async with async_session() as session:
        await session.merge(TrackedTrader(
            nickname=info["nickname"],
            title=info.get("title", ""),
            source=info.get("source", "pulse"),
            status=info.get("status", "active"),
            note=info.get("note", ""),
        ))
        await session.commit()


async def delete_tracked_trader(nickname: str) -> bool:
    """Удалить трейдера из отслеживания. True, если что-то удалилось."""
    async with async_session() as session:
        result = await session.execute(
            delete(TrackedTrader).where(TrackedTrader.nickname == nickname)
        )
        await session.commit()
        return result.rowcount > 0


async def seed_tracked_traders(nicknames: list[str]) -> int:
    """
    Одноразовый посев: если таблица трейдеров пуста, наполнить её ников из
    конфига (PULSE_TRACKED_AUTHORS). Возвращает число добавленных.
    """
    if await get_tracked_trader_nicks():
        return 0
    added = 0
    async with async_session() as session:
        for nick in nicknames:
            nick = (nick or "").strip()
            if not nick:
                continue
            await session.merge(TrackedTrader(nickname=nick, source="config"))
            added += 1
        await session.commit()
    if added:
        logger.info(f"🌱 Посев отслеживаемых трейдеров из конфига: {added}")
    return added


# ─── Прогнозы (память агента) ─────────────────────────────────────────────────

async def add_prediction(data: dict) -> int:
    """Сохранить новый прогноз. Возвращает id."""
    context = data.get("context")
    context_json = None
    if context is not None:
        try:
            context_json = json.dumps(context, ensure_ascii=False)
        except Exception:
            context_json = None
    try:
        _cs = data.get("confluence_score")
        _cs = int(_cs) if _cs is not None else None
    except (TypeError, ValueError):
        _cs = None
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
            regime=data.get("regime"),
            confluence_score=_cs,
            entry=data.get("entry"),
            stop=data.get("stop"),
            target=data.get("target"),
            rr_planned=data.get("rr_planned"),
            context_json=context_json,
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


async def get_open_intraday_predictions() -> list[Prediction]:
    """
    Открытые (ещё не оценённые) интрадей Claude-сигналы: есть entry/stop/target и
    направление up/down, результат ещё не проставлен. Оцениваются НА КАЖДОМ тике
    learning-цикла — исход target/stop фиксируем СРАЗУ при касании, не дожидаясь
    горизонта или следующего дня.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Prediction)
            .where(Prediction.correct.is_(None))
            .where(Prediction.direction.in_(("up", "down")))
            .where(Prediction.entry.is_not(None))
            .where(Prediction.stop.is_not(None))
            .where(Prediction.target.is_not(None))
        )
        return list(result.scalars().all())


async def get_open_signal_tickers() -> set[str]:
    """
    Тикеры с ОТКРЫТЫМ (неоценённым) направленным сигналом. Пока сигнал не отработал
    (target/stop/session), новый по тому же тикеру не создаём — не плодим дубли.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Prediction.ticker)
            .where(Prediction.correct.is_(None))
            .where(Prediction.direction.in_(("up", "down")))
        )
        return {t for (t,) in result.all() if t}


async def has_open_signal(ticker: str) -> bool:
    """Есть ли открытый (неоценённый) направленный сигнал по тикеру."""
    async with async_session() as session:
        result = await session.execute(
            select(Prediction.id)
            .where(Prediction.correct.is_(None))
            .where(Prediction.direction.in_(("up", "down")))
            .where(Prediction.ticker == ticker.upper())
            .limit(1)
        )
        return result.first() is not None


async def evaluate_prediction(
    pred_id: int, realized_price: float, realized_return: float, correct: bool,
    outcome: Optional[str] = None, realized_r: Optional[float] = None,
    mfe_r: Optional[float] = None, legs: Optional[list] = None,
) -> None:
    async with async_session() as session:
        pred = await session.get(Prediction, pred_id)
        if pred is None:
            return
        pred.realized_price = realized_price
        pred.realized_return = realized_return
        pred.correct = correct
        if outcome is not None:
            pred.outcome = outcome
        if realized_r is not None:
            pred.realized_r = realized_r
        if mfe_r is not None:
            pred.mfe_r = mfe_r
        if legs is not None:
            try:
                pred.legs_json = json.dumps(legs, ensure_ascii=False)
            except Exception:
                pred.legs_json = None
        pred.evaluated_at = datetime.now(timezone.utc)
        await session.commit()


async def get_evaluated_predictions() -> list[dict]:
    """Все оценённые прогнозы (для обучения и статистики)."""
    async with async_session() as session:
        result = await session.execute(
            select(Prediction).where(Prediction.correct.is_not(None))
        )
        return [p.to_dict() for p in result.scalars().all()]


# ─── Разбор ошибок (post-mortem) ──────────────────────────────────────────────

async def get_predictions_for_post_mortem(limit: int = 50) -> list[dict]:
    """
    Оценённые прогнозы, для которых ещё не сделан разбор (pm_at пустой).
    Возвращает dict + сырой context_json для передачи в разбор.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Prediction)
            .where(Prediction.correct.is_not(None))
            .where(Prediction.pm_at.is_(None))
            .order_by(Prediction.evaluated_at.desc())
            .limit(limit)
        )
        out = []
        for p in result.scalars().all():
            d = p.to_dict()
            d["context_json"] = p.context_json
            out.append(d)
        return out


async def set_post_mortem(pred_id: int, post_mortem: str, lesson: str,
                          tags: Optional[list[str]] = None) -> None:
    """Сохранить результат разбора по прогнозу."""
    async with async_session() as session:
        pred = await session.get(Prediction, pred_id)
        if pred is None:
            return
        pred.post_mortem = (post_mortem or "")[:2000]
        pred.lesson = (lesson or "")[:1000]
        pred.pm_tags = ",".join(t.strip() for t in (tags or []) if t.strip())[:512]
        pred.pm_at = datetime.now(timezone.utc)
        await session.commit()


async def recent_lessons(ticker: Optional[str] = None, limit: int = 15) -> list[dict]:
    """Последние извлечённые уроки (по разобранным прогнозам)."""
    async with async_session() as session:
        stmt = (
            select(Prediction)
            .where(Prediction.pm_at.is_not(None))
            .order_by(Prediction.pm_at.desc())
            .limit(limit)
        )
        if ticker:
            stmt = stmt.where(Prediction.ticker == ticker.upper())
        result = await session.execute(stmt)
        out = []
        for p in result.scalars().all():
            out.append({
                "id": p.id,
                "ticker": p.ticker,
                "direction": p.direction,
                "correct": p.correct,
                "realized_return": (round(p.realized_return, 2)
                                    if p.realized_return is not None else None),
                "lesson": p.lesson,
                "post_mortem": p.post_mortem,
                "tags": [t for t in (p.pm_tags or "").split(",") if t],
                "created_at": p.created_at.isoformat() if p.created_at else None,
            })
        return out


async def lesson_tag_stats() -> dict:
    """
    Частота тегов причин по разобранным прогнозам, отдельно для провалов и успехов.
    Помогает увидеть типовые причины ошибок.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Prediction).where(Prediction.pm_at.is_not(None))
        )
        preds = result.scalars().all()

    fail_tags: dict[str, int] = {}
    win_tags: dict[str, int] = {}
    for p in preds:
        tags = [t for t in (p.pm_tags or "").split(",") if t]
        bucket = win_tags if p.correct else fail_tags
        for t in tags:
            bucket[t] = bucket.get(t, 0) + 1

    def _top(d: dict) -> list[dict]:
        return [{"tag": k, "count": v}
                for k, v in sorted(d.items(), key=lambda x: x[1], reverse=True)]

    return {
        "analyzed": len(preds),
        "failure_tags": _top(fail_tags),
        "success_tags": _top(win_tags),
    }


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


# ─── База знаний: события рынка (реальное время + история) ────────────────────

async def add_event(ev: dict) -> None:
    """
    Записать одно событие в базу знаний. Поля: source, kind, ticker, channel,
    text, label, score, signal, payload(dict), ts(datetime, необязательно).
    Пишем «мягко»: любые ошибки не должны ронять пайплайн сбора.
    """
    try:
        payload = ev.get("payload")
        payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        async with async_session() as session:
            session.add(MarketEvent(
                ts=ev.get("ts") or datetime.now(timezone.utc),
                source=ev.get("source", "unknown"),
                kind=ev.get("kind", "message"),
                ticker=(ev.get("ticker") or None),
                channel=ev.get("channel"),
                text=(ev.get("text") or "")[:2000] or None,
                label=ev.get("label"),
                score=ev.get("score"),
                signal=ev.get("signal"),
                payload=payload_json,
            ))
            await session.commit()
    except Exception as e:
        logger.debug(f"add_event failed: {e}")


async def add_events(evs: list[dict]) -> int:
    """Массовая запись событий (например, снимок сделок/стакана). Возвращает число."""
    n = 0
    try:
        async with async_session() as session:
            for ev in evs:
                payload = ev.get("payload")
                payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
                session.add(MarketEvent(
                    ts=ev.get("ts") or datetime.now(timezone.utc),
                    source=ev.get("source", "unknown"),
                    kind=ev.get("kind", "message"),
                    ticker=(ev.get("ticker") or None),
                    channel=ev.get("channel"),
                    text=(ev.get("text") or "")[:2000] or None,
                    label=ev.get("label"),
                    score=ev.get("score"),
                    signal=ev.get("signal"),
                    payload=payload_json,
                ))
                n += 1
            await session.commit()
    except Exception as e:
        logger.debug(f"add_events failed: {e}")
    return n


async def recent_events(ticker: Optional[str] = None, source: Optional[str] = None,
                        kind: Optional[str] = None, since_minutes: Optional[int] = None,
                        limit: int = 200) -> list[dict]:
    """Последние события базы знаний с фильтрами (для дашборда и Claude)."""
    async with async_session() as session:
        stmt = select(MarketEvent).order_by(MarketEvent.ts.desc()).limit(min(limit, 1000))
        if ticker:
            stmt = stmt.where(MarketEvent.ticker == ticker.upper())
        if source:
            stmt = stmt.where(MarketEvent.source == source)
        if kind:
            stmt = stmt.where(MarketEvent.kind == kind)
        if since_minutes:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
            stmt = stmt.where(MarketEvent.ts >= cutoff)
        result = await session.execute(stmt)
        return [e.to_dict() for e in result.scalars().all()]


async def save_trader_deals(deals: list[dict]) -> dict:
    """
    Сохранить сделки трейдеров (скрейп агента) в market_events (source=pulse_deal,
    kind=deal). Дедуп по сигнатуре против последних ~14 дней. Нормализует action
    к buy/sell и тикер (upper, без $). Возвращает {received, stored}.
    """
    if not deals:
        return {"received": 0, "stored": 0}
    existing: set = set()
    try:
        recent = await recent_events(source="pulse_deal", since_minutes=14 * 24 * 60, limit=1000)
        for e in recent:
            pl = e.get("payload") or {}
            if isinstance(pl, dict) and pl.get("sig"):
                existing.add(pl["sig"])
    except Exception:
        pass

    def _act(v):
        s = str(v or "").strip().lower()
        if s in ("buy", "b", "купил", "покупка", "покупка лонг", "лонг"):
            return "buy"
        if s in ("sell", "s", "продал", "продажа", "шорт"):
            return "sell"
        if "куп" in s or "buy" in s:
            return "buy"
        if "прод" in s or "sell" in s:
            return "sell"
        return None

    to_add = []
    for d in deals:
        author = (d.get("author") or "").strip()
        ticker = (d.get("ticker") or "").strip().upper().lstrip("$")
        action = _act(d.get("action"))
        if not (author and ticker and action):
            continue
        ts_raw = d.get("timestamp") or d.get("ts")
        sig = "|".join([author, ticker, action, str(ts_raw or ""), str(d.get("price") or "")])
        if sig in existing:
            continue
        existing.add(sig)
        ev_ts = None
        if ts_raw:
            try:
                ev_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except Exception:
                ev_ts = None
        to_add.append({
            "source": "pulse_deal", "kind": "deal", "ticker": ticker, "channel": author,
            "ts": ev_ts,
            "text": f"{author} {action} {ticker}"
                    + (f" @ {d.get('price')}" if d.get('price') is not None else ""),
            "payload": {"action": action, "price": d.get("price"),
                        "quantity": d.get("quantity"), "note": d.get("note"), "sig": sig},
        })
    stored = await add_events(to_add) if to_add else 0
    return {"received": len(deals), "stored": stored}


async def recent_trader_deals(hours: int = 72, limit: int = 200) -> dict:
    """
    Свод залитых сделок трейдеров для дашборда — форма /api/smart-money:
    {deals:[{author,ticker,action,price,quantity,timestamp}], by_ticker:[...],
     deal_count, updated_at, source}.
    """
    evs = await recent_events(source="pulse_deal", since_minutes=hours * 60, limit=limit)
    deals, agg = [], {}
    for e in evs:
        pl = e.get("payload") or {}
        if not isinstance(pl, dict):
            pl = {}
        action = pl.get("action")
        if action not in ("buy", "sell"):
            continue
        t = e.get("ticker")
        deals.append({"author": e.get("channel"), "ticker": t, "action": action,
                      "price": pl.get("price"), "quantity": pl.get("quantity"),
                      "timestamp": e.get("ts")})
        a = agg.setdefault(t, {"buys": 0, "sells": 0})
        a["buys" if action == "buy" else "sells"] += 1
    by_ticker = []
    for t, a in agg.items():
        net = a["buys"] - a["sells"]
        by_ticker.append({"ticker": t, "buys": a["buys"], "sells": a["sells"], "net": net,
                          "bias": "покупки" if net > 0 else "продажи" if net < 0 else "нейтр"})
    by_ticker.sort(key=lambda x: abs(x["net"]), reverse=True)
    return {"deals": deals, "by_ticker": by_ticker, "deal_count": len(deals),
            "updated_at": datetime.now(timezone.utc).isoformat(), "source": "ingested"}


async def event_source_stats(since_minutes: int = 60) -> dict:
    """Сколько событий по каждому источнику за последние N минут (панель источников)."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    async with async_session() as session:
        result = await session.execute(
            select(MarketEvent.source).where(MarketEvent.ts >= cutoff)
        )
        counts: dict[str, int] = {}
        for (src,) in result.all():
            counts[src] = counts.get(src, 0) + 1
        return {"since_minutes": since_minutes, "counts": counts,
                "total": sum(counts.values())}


async def knowledge_snapshot(ticker: str, since_minutes: int = 240,
                             per_kind: int = 8) -> dict:
    """
    Компактный срез базы знаний по тикеру для Claude: последние сообщения из
    чатов, новости, посты/сделки Пульса и последние снимки Tinkoff (стакан/цена).
    """
    ticker = ticker.upper()
    events = await recent_events(ticker=ticker, since_minutes=since_minutes, limit=400)
    buckets = {"messages": [], "news": [], "pulse": [], "deals": [], "geo": [],
               "orderbook": None, "quote": None, "trades": None}
    for e in events:
        if e["source"] == "telegram" and len(buckets["messages"]) < per_kind:
            buckets["messages"].append(e)
        elif e["source"] == "rss" and len(buckets["news"]) < per_kind:
            buckets["news"].append(e)
        elif e["source"] == "pulse" and len(buckets["pulse"]) < per_kind:
            buckets["pulse"].append(e)
        elif e["source"] == "pulse_deal" and len(buckets["deals"]) < per_kind:
            buckets["deals"].append(e)
        elif e["source"] == "tinkoff":
            if e["kind"] == "orderbook" and buckets["orderbook"] is None:
                buckets["orderbook"] = e
            elif e["kind"] == "quote" and buckets["quote"] is None:
                buckets["quote"] = e
            elif e["kind"] == "trades" and buckets["trades"] is None:
                buckets["trades"] = e
    # Геополитика — рыночно-широкий фон (ticker=None), поэтому её не вернёт
    # фильтр по конкретному тикеру. Тянем отдельно, чтобы фон попадал в срез
    # знаний Claude по ЛЮБОМУ тикеру.
    try:
        buckets["geo"] = await recent_events(
            source="geopolitics", since_minutes=since_minutes, limit=per_kind)
    except Exception:
        buckets["geo"] = []
    buckets["ticker"] = ticker
    buckets["event_count"] = len(events) + len(buckets["geo"])
    return buckets


async def prune_events(keep_days: int = 14) -> int:
    """Удалить события старше keep_days (ограничиваем рост базы). Возвращает rowcount."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    async with async_session() as session:
        result = await session.execute(delete(MarketEvent).where(MarketEvent.ts < cutoff))
        await session.commit()
        return result.rowcount or 0


# ─── Кумулятивный footprint за сессию (объём по цене из реальных сделок) ───────

async def get_session_footprint(ticker: str, date: str) -> Optional[dict]:
    """Вернуть {'buckets': {price: [buy, sell, total]}, 'watermark': str} или None."""
    async with async_session() as session:
        row = await session.get(SessionFootprint, f"{date}:{ticker.upper()}")
        if not row:
            return None
        try:
            buckets = json.loads(row.buckets or "{}")
        except Exception:
            buckets = {}
        return {"buckets": buckets, "watermark": row.watermark}


async def merge_session_footprint(date: str, ticker: str,
                                  inc_buckets: dict, watermark: Optional[str]) -> None:
    """
    Влить инкремент (price -> [buy, sell, total]) в дневной footprint и сдвинуть
    watermark. Пишем «мягко»: ошибки не должны ронять пайплайн сбора.
    """
    if not inc_buckets and watermark is None:
        return
    key = f"{date}:{ticker.upper()}"
    try:
        async with async_session() as session:
            row = await session.get(SessionFootprint, key)
            if row is None:
                row = SessionFootprint(key=key, date=date, ticker=ticker.upper(), buckets="{}")
                session.add(row)
            try:
                cur = json.loads(row.buckets or "{}")
            except Exception:
                cur = {}
            for price, cell in inc_buckets.items():
                c = cur.get(price) or [0, 0, 0]
                cur[price] = [c[0] + cell[0], c[1] + cell[1], c[2] + cell[2]]
            row.buckets = json.dumps(cur)
            if watermark:
                row.watermark = watermark
            row.updated_at = datetime.now(timezone.utc)
            await session.commit()
    except Exception as e:
        logger.debug(f"merge_session_footprint failed: {e}")


async def prune_session_footprint(keep_days: int = 3) -> int:
    """Удалить дневные footprint старше keep_days (по строковой дате)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    async with async_session() as session:
        result = await session.execute(
            delete(SessionFootprint).where(SessionFootprint.date < cutoff))
        await session.commit()
        return result.rowcount or 0


# ─── Эффективность по режимам (Фаза B — цикл измерения) ───────────────────────

def _r_multiple(direction: str, entry: Optional[float], stop: Optional[float],
                realized_price: Optional[float]) -> Optional[float]:
    """
    Реализованный R-мультипл = движение цены в единицах риска (|entry − stop|).
    Направленно: для лонга — (realized − entry)/risk, для шорта — (entry − realized)/risk.
    None, если нет уровней или риск нулевой. Чистая функция — тестируется.
    """
    if entry is None or stop is None or realized_price is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if direction == "up":
        return round((realized_price - entry) / risk, 3)
    if direction == "down":
        return round((entry - realized_price) / risk, 3)
    return None


async def regime_stats() -> dict:
    """
    Винрейт, средняя доходность, средний R и средний конфлюенс В РАЗРЕЗЕ РЕЖИМА
    дня — по ОЦЕНЁННЫМ направленным прогнозам. Это и есть измерение плейбука:
    видно, какие режимы реально дают эйдж, а какие сливают.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Prediction).where(Prediction.correct.is_not(None)))
        preds = result.scalars().all()

    groups: dict[str, dict] = {}
    for p in preds:
        if p.direction not in ("up", "down"):
            continue  # «наблюдать»/flat — не сделки, в статистику не берём
        reg = p.regime or "unknown"
        g = groups.setdefault(reg, {"correct": 0, "total": 0, "returns": [], "rs": [],
                                    "confl": [], "mfes": [],
                                    "target": 0, "stop": 0, "breakeven": 0, "session": 0})
        g["total"] += 1
        if p.correct:
            g["correct"] += 1
        if p.realized_return is not None:
            g["returns"].append(p.realized_return)
        # R: предпочитаем факт. путь до цели/стопа (realized_r); иначе оценка по close.
        r = p.realized_r if p.realized_r is not None else _r_multiple(
            p.direction, p.entry, p.stop, p.realized_price)
        if r is not None:
            g["rs"].append(r)
        if p.mfe_r is not None:
            g["mfes"].append(p.mfe_r)
        if p.confluence_score is not None:
            g["confl"].append(p.confluence_score)
        if p.outcome in ("target", "stop", "breakeven", "session"):
            g[p.outcome] += 1

    def _avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    by_regime = []
    for reg, g in groups.items():
        n = g["total"]
        by_regime.append({
            "regime": reg,
            "trades": n,
            "win_rate": round(g["correct"] / n * 100, 1) if n else None,
            "avg_return": _avg(g["returns"]),
            "avg_r": _avg(g["rs"]),
            "avg_mfe_r": _avg(g["mfes"]),
            "avg_confluence": _avg(g["confl"]),
            "outcomes": {"target": g["target"], "stop": g["stop"],
                         "breakeven": g["breakeven"], "session": g["session"]},
        })
    by_regime.sort(key=lambda x: x["trades"], reverse=True)
    return {"by_regime": by_regime,
            "total_trades": sum(x["trades"] for x in by_regime)}
