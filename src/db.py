"""
MOODEX — слой базы данных (SQLAlchemy async)

Работает "из коробки" на SQLite (файл в постоянном томе /app/data),
и на PostgreSQL, если задать DATABASE_URL, например:
    DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/moodex

Здесь хранятся каналы, добавленные вручную через дашборд, чтобы они
переживали перезапуски и редеплой.
"""
import os
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

    # ── Решение ЧЕЛОВЕКА по сценарию (advisory-режим) ────────────────────────
    # Claude предлагает, решает человек. Записываем решение, чтобы можно было
    # сравнить: где прав Claude, где прав человек, и добавляет ли вето человека
    # ценность или отнимает её. Ключевое требование к корректности сравнения:
    # исход считается для ВСЕХ сценариев одинаково, независимо от решения —
    # иначе отклонённые сделки оценивались бы мягче принятых, и сравнение
    # «человек против модели» было бы смещённым по построению.
    human_decision: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # accept|reject|wait
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def to_dict(self) -> dict:
        # МСК-время сигнала и размер позиции — из снимка контекста, чтобы были
        # видны в списке. Размер считает Risk Engine на момент сигнала; без него
        # карточка показывает сценарий, но не сделку.
        _sig_msk = None
        _risk: dict = {}
        if self.context_json:
            try:
                import json as _json
                _ctx = _json.loads(self.context_json) or {}
                _sig_msk = _ctx.get("signal_time_msk")
                _risk = {
                    "shares": _ctx.get("risk_shares"),
                    "risk_rub": _ctx.get("risk_rub"),
                    "risk_pct_of_account": _ctx.get("risk_pct_of_account"),
                    "notional_rub": _ctx.get("risk_notional_rub"),
                    "binding": _ctx.get("risk_binding"),
                    "spread_pct": _ctx.get("spread_pct_at_signal"),
                    "depth_near_mid": _ctx.get("depth_near_mid_at_signal"),
                    "data_source": _ctx.get("data_source"),
                    "data_delayed": _ctx.get("data_delayed"),
                }
                if _risk.get("shares") is None:
                    _risk = {}
            except Exception:
                _sig_msk, _risk = None, {}
        return {
            "position": _risk or None,
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
            "human_decision": self.human_decision,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decision_note": self.decision_note,
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


class FlowMinute(Base):
    """
    Поток сделок с разрешением в ОДНУ МИНУТУ. Дедуп по watermark.

    Зачем отдельно от session_footprint. Та таблица группирует объём по ЦЕНЕ и
    хранится три дня. Из неё нельзя получить ни 1m/5m/15m/30m, ни число сделок,
    ни размеры: сделка на 1000 лотов и десять по 100 дают одинаковую корзину.
    31.07 это заблокировало проверку потока целиком.

    Откуда данные. Tinkoff GetLastTrades отдаёт сделки за ОКНО ВРЕМЕНИ, а не
    «последние N». Прежний код запрашивал четыре часа и оставлял из ответа
    последние 50 сделок, остальное выбрасывал: данные были, но терялись в коде.
    Теперь берутся все сделки окна, новые определяются по времени последней
    учтённой.

    Производные — дельта, накопленная дельта, доли, средний размер, дисбаланс —
    здесь НЕ хранятся, а считаются на чтении. Хранить производное опасно: при
    смене порога «крупной сделки» пришлось бы переписывать всю историю.
    """
    __tablename__ = "flow_minute"

    key: Mapped[str] = mapped_column(String(72), primary_key=True)   # "ts:TICKER:source"
    ts: Mapped[str] = mapped_column(String(16), index=True)          # "YYYY-MM-DDTHH:MM" МСК
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    # exchange — биржевые сделки, dealer — внутренний рынок брокера,
    # mixed — собрано до 01.08, когда источник не различался вовсе.
    source: Mapped[str] = mapped_column(String(8), default="exchange", index=True)
    session: Mapped[str] = mapped_column(String(8), default="main")  # morning|main|evening
    buy_volume: Mapped[int] = mapped_column(Integer, default=0)
    sell_volume: Mapped[int] = mapped_column(Integer, default=0)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    max_trade: Mapped[int] = mapped_column(Integer, default=0)
    vwap_num: Mapped[float] = mapped_column(Float, default=0.0)      # сумма цена*объём
    watermark: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class BookMinute(Base):
    """
    Стакан, свёрнутый до ОДНОЙ МИНУТЫ.

    Почему не строка на пакет. В потоке стакан приходит до десяти раз в секунду
    на бумагу: на 80 бумагах это до 800 строк в секунду. Такая подробность не
    нужна ни для какого решения — важно, каким был перекос в течение минуты и
    менялся ли он внутри неё.

    Хранятся СУММЫ, а не средние: среднее нельзя усреднить повторно при склейке
    минут в пятиминутки, а сумму сложить можно. Доли и средний спред считаются
    на чтении.

    imb_min/imb_max — размах доли покупателей внутри минуты. Одно среднее
    скрывает разворот: минута, где стакан был сначала 80% на покупку, а потом
    20%, даёт то же среднее, что и ровные 50%.

    Объёмы в ЛОТАХ, как и всё остальное, что приходит от биржи.
    """
    __tablename__ = "book_minute"

    key: Mapped[str] = mapped_column(String(72), primary_key=True)   # "ts:TICKER:source"
    ts: Mapped[str] = mapped_column(String(16), index=True)          # МСК
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    # exchange — биржевой стакан, dealer — котировки брокера. Подписка по
    # умолчанию отдаёт ORDERBOOK_TYPE_ALL, то есть оба вперемешку.
    source: Mapped[str] = mapped_column(String(8), default="exchange", index=True)
    session: Mapped[str] = mapped_column(String(8), default="main")
    updates: Mapped[int] = mapped_column(Integer, default=0)         # пакетов за минуту
    bid_vol_sum: Mapped[float] = mapped_column(Float, default=0.0)
    ask_vol_sum: Mapped[float] = mapped_column(Float, default=0.0)
    spread_sum: Mapped[float] = mapped_column(Float, default=0.0)
    best_bid: Mapped[float] = mapped_column(Float, default=0.0)      # на конец минуты
    best_ask: Mapped[float] = mapped_column(Float, default=0.0)
    imb_min: Mapped[float] = mapped_column(Float, default=0.0)
    imb_max: Mapped[float] = mapped_column(Float, default=0.0)
    # Структура уровней. Сумма по всем 20 не отличает ПЛИТУ от ровной раскладки:
    # 263 тысячи лотов на продажу могут стоять одной заявкой на одной цене, а
    # могут быть размазаны по двадцати. Для вопроса «кто давит цену» это разные
    # картины. Сами уровни не храним — 20 цен по десять раз в секунду на 80
    # бумаг это уже не минутная таблица.
    bid5_sum: Mapped[float] = mapped_column(Float, default=0.0)   # пять лучших
    ask5_sum: Mapped[float] = mapped_column(Float, default=0.0)
    bid_top_max: Mapped[int] = mapped_column(Integer, default=0)  # крупнейшая заявка
    ask_top_max: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class CandleMinute(Base):
    """
    Минутный бар из ПОТОКА, а не из REST.

    Зачем отдельно от flow_minute. Там лежит поток сделок: объёмы, дельта,
    крупнейшая сделка, VWAP. Но там НЕТ open/high/low/close — по потоку их не
    восстановить. За OHLC система ходила в REST, а ISS отдаёт минутки с
    задержкой около 15 минут, что для интрадея бесполезно.

    ОБЪЁМ НАКОПИТЕЛЬНЫЙ. Биржа присылает свечу многократно по ходу минуты, и в
    каждой версии volume уже включает всё предыдущее. При обновлении строки
    объём ЗАМЕНЯЕТСЯ, а не прибавляется — иначе он вырос бы в разы.

    volume_buy и volume_sell приходят от биржи в самой свече. Это независимая
    сверка нашего разбора направлений в flow_minute: если разойдутся заметно,
    значит где-то ошибка в одном из двух.
    """
    __tablename__ = "candle_minute"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)   # "ts:TICKER"
    ts: Mapped[str] = mapped_column(String(16), index=True)          # МСК
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    session: Mapped[str] = mapped_column(String(8), default="main")
    open: Mapped[float] = mapped_column(Float, default=0.0)
    high: Mapped[float] = mapped_column(Float, default=0.0)
    low: Mapped[float] = mapped_column(Float, default=0.0)
    close: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[int] = mapped_column(Integer, default=0)          # лоты
    volume_buy: Mapped[int] = mapped_column(Integer, default=0)
    volume_sell: Mapped[int] = mapped_column(Integer, default=0)
    updates: Mapped[int] = mapped_column(Integer, default=0)         # версий свечи
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MicroMinute(Base):
    """
    Производные СЕКУНДНОГО ряда стакана, свёрнутые до минуты.

    Почему производные, а не сам ряд. Арифметика:
        каждую секунду по каждой бумаге     4.9 млн строк в день
        за 90 дней                          440 млн строк, около 35 ГБ
        свободно на сервере                 20 ГБ
    Не помещается и не будет. Здесь 80 тысяч строк в день и 0.9 ГБ за 90 дней.

    Что теряется: точный ряд трёхнедельной давности. Что сохраняется: НАСКОЛЬКО
    быстро и НАСКОЛЬКО сильно менялся стакан — а именно это и нужно, чтобы потом
    измерить, значит ли оно что-нибудь.

    Размах перекоса берётся крайними значениями, а не средним: одно среднее
    скрывает разворот, а резкая смена и интересна.

    Добавленное и снятое считаются РАЗДЕЛЬНО. Разность скрывает главное: стакан,
    где за минуту добавили и сняли по миллиону, и стакан, где не было ничего,
    дают одинаковый ноль.
    """
    __tablename__ = "micro_minute"

    key: Mapped[str] = mapped_column(String(72), primary_key=True)   # "ts:TICKER:source"
    ts: Mapped[str] = mapped_column(String(16), index=True)          # МСК
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(8), default="exchange", index=True)
    session: Mapped[str] = mapped_column(String(8), default="main")
    samples: Mapped[int] = mapped_column(Integer, default=0)         # секундных слепков
    # Размах изменения перекоса за 10 и 30 секунд ВНУТРИ минуты.
    imb_d10_max: Mapped[float] = mapped_column(Float, default=0.0)
    imb_d10_min: Mapped[float] = mapped_column(Float, default=0.0)
    imb_d30_max: Mapped[float] = mapped_column(Float, default=0.0)
    imb_d30_min: Mapped[float] = mapped_column(Float, default=0.0)
    # Скорость ликвидности: сколько лотов пришло и ушло, и пиковая секунда.
    bid_added: Mapped[float] = mapped_column(Float, default=0.0)
    bid_removed: Mapped[float] = mapped_column(Float, default=0.0)
    ask_added: Mapped[float] = mapped_column(Float, default=0.0)
    ask_removed: Mapped[float] = mapped_column(Float, default=0.0)
    bid_peak_add: Mapped[float] = mapped_column(Float, default=0.0)
    bid_peak_remove: Mapped[float] = mapped_column(Float, default=0.0)
    ask_peak_add: Mapped[float] = mapped_column(Float, default=0.0)
    ask_peak_remove: Mapped[float] = mapped_column(Float, default=0.0)
    # Исполнение относительно лучших цен, в лотах.
    traded_at_best: Mapped[int] = mapped_column(Integer, default=0)
    traded_near: Mapped[int] = mapped_column(Integer, default=0)
    traded_deep: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class LevelMinute(Base):
    """
    Жизнь ОДНОГО ЦЕНОВОГО УРОВНЯ за минуту.

    Чего не было. book_minute хранит РАЗМЕР крупнейшей заявки, но не её ЦЕНУ, и
    ничего про то, съели её или сняли. На вопрос «уровень 276.52 держали или
    убрали» ответить было нельзя.

    Почему не секунды. Двадцать цен десять раз в секунду на 80 бумагах — это
    миллионы строк в день против 20 ГБ свободных. Секундный ряд живёт в памяти
    (level_history), а сюда уходит минутный итог.

    СТРОКА ПИШЕТСЯ НЕ ВСЕГДА. Только когда на уровне что-то происходило выше
    порога в рублях. Иначе на каждую минуту пришлось бы по шесть уровней на
    бумагу на источник — под полмиллиона строк в день, и арифметика опять не
    сошлась бы. Сколько их будет на живом рынке, я не знаю: порог придётся
    калибровать по факту, а не назначать заранее.

    ГЛАВНОЕ ЗДЕСЬ — РАЗДЕЛЕНИЕ traded и pulled. «Съели» и «владелец передумал»
    означают противоположное, а разность размеров их не различает.

    Объёмы в ЛОТАХ, как всё от биржи. Рубли считаются на чтении: лотность у
    бумаг разная.
    """
    __tablename__ = "level_minute"

    # "ts:TICKER:source:side:price" — цена в ключе, иначе уровни одной минуты
    # затирали бы друг друга.
    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    ts: Mapped[str] = mapped_column(String(16), index=True)           # МСК
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(8), default="exchange", index=True)
    side: Mapped[str] = mapped_column(String(4))                      # bid|ask
    price: Mapped[float] = mapped_column(Float)
    peak: Mapped[int] = mapped_column(Integer, default=0)             # лотов
    end_size: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    # Съедено против снято — то, ради чего таблица.
    traded: Mapped[int] = mapped_column(Integer, default=0)
    pulled: Mapped[int] = mapped_column(Integer, default=0)
    restored: Mapped[int] = mapped_column(Integer, default=0)
    gone: Mapped[int] = mapped_column(Integer, default=0)
    events: Mapped[int] = mapped_column(Integer, default=0)
    # ТЕСТЫ — накопительные счётчики уровня на конец минуты, не приращение.
    # Тест это приход цены К уровню и её уход: отступила или прошла насквозь.
    # Нужны, чтобы однажды ИЗМЕРИТЬ, значит ли «выдержал» хоть что-нибудь для
    # будущего. Пока не измерено, слова «сильный» на карточке нет.
    tests: Mapped[int] = mapped_column(Integer, default=0)
    test_held: Mapped[int] = mapped_column(Integer, default=0)
    test_failed: Mapped[int] = mapped_column(Integer, default=0)
    alive_sec: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SignalAttempt(Base):
    """
    ЖУРНАЛ ПОПЫТОК (наблюдаемость воронки сигналов).

    Проблема, которую решает: раньше в БД попадали ТОЛЬКО направленные сигналы
    Claude (up/down). Всё остальное — «neutral» от Claude, вето фильтра качества,
    недоступность Claude, занятый тикер — исчезало без следа. Итог: за понедельник
    и вторник 0 сигналов, и НЕВОЗМОЖНО сказать, звали ли Claude, сколько раз и
    почему сигнала не было. Деньги при этом тратились.

    Теперь пишем КАЖДУЮ попытку с кодом причины и фактической ценой вызова в ₽.
    Токенов это не стоит (пишем то, что уже посчитано), а даёт:
      - воронку: сколько тикеров отсмотрено → сколько watch → сколько дошло до
        глубокого разбора → сколько сохранено, и где именно всё отсекается;
      - цену за сигнал (₽ на попытку и ₽ на сохранённый сигнал);
      - материал для калибровки порогов FILTER_* по факту, а не на глаз.
    """
    __tablename__ = "signal_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True,
        default=lambda: datetime.now(timezone.utc),
    )
    msk: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)     # "HH:MM МСК"
    stage: Mapped[str] = mapped_column(String(12), index=True, default="deep")  # batch|deep
    ticker: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    phase: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)   # pre|main|break|closed
    # Что решил Claude и что вышло в итоге:
    verdict: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # up|down|flat|unavailable|error
    final: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)    # up|down|flat (после вето)
    saved: Mapped[bool] = mapped_column(Boolean, default=False)
    prediction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)  # код причины
    # Параметры сетапа (для калибровки порогов):
    mode: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)     # pullback|momentum
    regime: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    confluence: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Цена наблюдаемости — фактический расход провайдера:
    cost_rub: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tokens_in: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "msk": self.msk,
            "stage": self.stage,
            "ticker": self.ticker,
            "phase": self.phase,
            "verdict": self.verdict,
            "final": self.final,
            "saved": self.saved,
            "prediction_id": self.prediction_id,
            "reason": self.reason,
            "mode": self.mode,
            "regime": self.regime,
            "confluence": self.confluence,
            "confidence": self.confidence,
            "rr": self.rr,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "cost_rub": round(self.cost_rub, 3) if self.cost_rub is not None else None,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "note": self.note,
        }


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
    # Advisory-режим: решение человека по сценарию Claude.
    "human_decision": "TEXT",
    "decided_at": "TIMESTAMP",
    "decision_note": "TEXT",
}


# Колонки, дописываемые в УЖЕ СУЩЕСТВУЮЩИЕ таблицы.
#
# Про 'mixed' у flow_minute и book_minute. Строки, собранные до 01.08, писались
# без различения источника: опрос REST отдавал поле trade_source в каждой
# сделке, а код его ни разу не читал. Значит в них смешаны биржевые и дилерские
# сделки в НЕИЗВЕСТНОЙ пропорции. Пометить их 'exchange' было бы неправдой,
# поэтому им ставится 'mixed': данные никуда не деваются, но по умолчанию в
# анализ не попадают.
_ADDED_COLUMNS = {
    "predictions": _PREDICTION_ADDED_COLUMNS,
    "flow_minute": {"source": "VARCHAR(8) DEFAULT 'mixed'"},
    "book_minute": {"source": "VARCHAR(8) DEFAULT 'mixed'",
                    "bid5_sum": "DOUBLE PRECISION DEFAULT 0",
                    "ask5_sum": "DOUBLE PRECISION DEFAULT 0",
                    "bid_top_max": "INTEGER DEFAULT 0",
                    "ask_top_max": "INTEGER DEFAULT 0"},
    # Счётчики тестов добавлены после того, как таблица уже появилась на живом
    # сервере, — create_all их не допишет, только миграция.
    #
    # Зачем они в базе. Слова «сильный» на карточке нет намеренно: связь
    # «выдержал сегодня» с «удержит завтра» не измерялась. Измерить её можно
    # только по накопленным тестам, и без этих трёх колонок считать будет нечего.
    "level_minute": {"tests": "INTEGER DEFAULT 0",
                     "test_held": "INTEGER DEFAULT 0",
                     "test_failed": "INTEGER DEFAULT 0",
                     "alive_sec": "INTEGER DEFAULT 0"},
}


async def migrate_schema():
    """
    Мягкая миграция: дописать новые колонки в существующие таблицы.

    create_all() создаёт недостающие ТАБЛИЦЫ, но не добавляет колонки в уже
    существующие. Здесь добавляем их post-mortem, если их ещё нет — и на
    SQLite, и на PostgreSQL, идемпотентно.
    """
    is_sqlite = "sqlite" in DATABASE_URL
    async with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            if is_sqlite:
                res = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
                existing = {row[1] for row in res.fetchall()}
                if not existing:
                    continue                      # таблицы ещё нет — создаст create_all
                for col, typ in cols.items():
                    if col not in existing:
                        await conn.exec_driver_sql(
                            f'ALTER TABLE {table} ADD COLUMN "{col}" {typ}')
                        logger.info(f"🧩 Добавлена колонка {table}.{col}")
            else:
                for col, typ in cols.items():
                    await conn.exec_driver_sql(
                        f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col}" {typ}')


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
        # Явное создание позже добавленной таблицы. На живом сервере она не
        # появилась, хотя create_all вызывается здесь же строкой выше, — причина
        # пока неизвестна, а данные терять нельзя. Вызов идемпотентный и
        # логирует исход, так что причина всплывёт в логе, а не в пятисотке.
        res = await ensure_level_table()
        if not res.get("ok"):
            logger.warning(f"level_minute не создана: {res.get('error')}")
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


def _is_directional(p) -> bool:
    """Был ли это НАПРАВЛЕННЫЙ прогноз, который вообще можно оценивать.

    Запись с direction=flat или confidence=0 — это отказ от мнения, а не
    прогноз. Считать её ошибкой, когда цена сдвинулась, некорректно:
    предсказания не было.
    """
    return (p.direction or "").lower() in ("up", "down") and (p.confidence or 0) > 0


async def accuracy_stats(ticker: Optional[str] = None) -> dict:
    """Честная точность прогнозов: калибровка и ожидание в R.

    ПОЧЕМУ ПЕРЕСЧИТАНО. Прежняя формула делила верные на ВСЕ оценённые записи,
    включая flat и confidence=0. На выборке из 20 последних закрытых: 19 записей
    flat, 11 с нулевой уверенностью, и 7 из них помечены correct=false. То есть
    цифра «точность 25.5%» измеряла в основном, как часто запись «без мнения»
    совпала со спокойным рынком, а не предсказательную способность.
    Ровно этот класс бага уже был починен для open_count (легаси flat-строки
    больше не считаются открытыми сигналами) — здесь он оставался.

    Что считается авторитетным вердиктом: механический исход (флаг correct,
    выставляемый по пути цены до цели или стопа). Теги пост-мортема (correct_read
    и прочие) генерирует та же модель, чей прогноз оценивается, — это самооценка,
    и метрикой она быть не может. Пост-мортем остаётся диагностикой «почему», но
    не оценкой «верно ли».

    Ключ accuracy теперь считается ТОЛЬКО по направленным прогнозам. Старая
    формула сохранена как accuracy_legacy, чтобы разницу было видно, а не чтобы
    ей пользоваться. Если направленных прогнозов нет, accuracy = None — это
    честный ответ «оценивать пока нечего», а не ноль.
    """
    async with async_session() as session:
        stmt = select(Prediction)
        if ticker:
            stmt = stmt.where(Prediction.ticker == ticker.upper())
        result = await session.execute(stmt)
        preds = result.scalars().all()

    total = len(preds)
    evaluated = [p for p in preds if p.correct is not None]
    directional = [p for p in evaluated if _is_directional(p)]
    abstained = len(evaluated) - len(directional)

    hits = [p for p in directional if p.correct]
    accuracy = (len(hits) / len(directional)) if directional else None

    legacy_correct = [p for p in evaluated if p.correct]
    accuracy_legacy = (len(legacy_correct) / len(evaluated)) if evaluated else None

    # ── КАЛИБРОВКА: значит ли что-нибудь заявленная уверенность ────────────────
    # Главный вопрос к любой модели-прогнозисту: когда она говорит 0.7, попадает
    # ли она в 70% случаев. gap > 0 — модель скромничает, gap < 0 — самоуверенна.
    calibration = []
    for lo, hi in ((0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.01)):
        grp = [p for p in directional if lo <= (p.confidence or 0) < hi]
        if not grp:
            continue
        hit_rate = sum(1 for p in grp if p.correct) / len(grp)
        avg_conf = sum(p.confidence or 0 for p in grp) / len(grp)
        calibration.append({
            "bucket": f"{lo:.1f}-{min(hi, 1.0):.1f}",
            "n": len(grp),
            "avg_confidence": round(avg_conf, 3),
            "hit_rate": round(hit_rate, 3),
            "gap": round(hit_rate - avg_conf, 3),
        })

    # ── ОЖИДАНИЕ В R: метрика, которая относится к деньгам ────────────────────
    # Можно попадать в 40% случаев и зарабатывать, если выигрыши крупнее
    # проигрышей. Доля попаданий сама по себе ни о чём не говорит.
    rs = [p.realized_r for p in directional if p.realized_r is not None]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    loss_sum = abs(sum(losses))
    return {
        "total": total,
        "evaluated": len(evaluated),
        "directional": len(directional),
        "abstained": abstained,
        "correct": len(hits),
        "accuracy": round(accuracy, 3) if accuracy is not None else None,
        "accuracy_legacy": round(accuracy_legacy, 3) if accuracy_legacy is not None else None,
        "accuracy_basis": "только направленные прогнозы с уверенностью > 0",
        "calibration": calibration,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "avg_win_r": round(sum(wins) / len(wins), 3) if wins else None,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else None,
        "profit_factor": round(sum(wins) / loss_sum, 3) if wins and loss_sum else None,
        "r_sample": len(rs),
        "pending": total - len(evaluated),
    }


async def correct_prediction_position(pred_id: int, shares: Optional[int] = None,
                                      note: str = "") -> dict:
    """Пересчитать снимок позиции по УЖЕ ЗАПИСАННЫМ уровням. Можно и после оценки.

    Разделение важно по смыслу:
      • уровни решения (entry/stop/target) после оценки НЕПРИКОСНОВЕННЫ — их правка
        переписывает и решение, и посчитанный исход;
      • снимок позиции (сколько акций, сколько рублей риска) — учётный ФАКТ об
        исполнении. Он не влияет ни на R, ни на признак correct, а только
        масштабирует рублёвые числа, и он законно может стать известен позже.

    Сигнал 664: снимок был посчитан от входа 39.00 со стопом 38.85 (риск 0.15 на
    акцию, 192₽). Записанные уровни — 39.40 и 39.10, риск 0.30 на акцию, то есть
    385₽. Без этой правки журнал занижал убыток ровно вдвое, а вместе с ним и
    рублёвое ожидание по счёту. При этом realized_r = -1.0 верен: R нормирован.

    shares задаётся, если фактический объём отличался от расчётного. Иначе размер
    берётся от Risk Engine по записанным уровням.
    """
    async with async_session() as session:
        pred = await session.get(Prediction, pred_id)
        if pred is None:
            return {"ok": False, "reason": "прогноз не найден"}
        if pred.entry is None or pred.stop is None:
            return {"ok": False, "reason": "у прогноза нет уровней — считать нечего"}

        ctx = {}
        if pred.context_json:
            try:
                ctx = json.loads(pred.context_json) or {}
            except Exception:
                ctx = {}
        before = {k: ctx.get(k) for k in
                  ("risk_shares", "risk_rub", "risk_pct_of_account",
                   "risk_notional_rub", "risk_binding", "stop_pct")}

        r_per_share = abs(pred.entry - pred.stop)
        after = None
        try:
            from src.risk.engine import RiskConfig, size_position
            cfg = RiskConfig()
            d = size_position(
                entry=pred.entry, stop=pred.stop,
                direction=("up" if pred.direction == "up" else "down"),
                cfg=cfg, lot_size=int(ctx.get("lot_size") or 1),
                spread_pct=ctx.get("spread_pct_at_signal"),
                depth_near_mid=ctx.get("depth_near_mid_at_signal"))
            qty = int(shares) if shares else d.shares
            after = {
                "risk_shares": qty,
                "risk_rub": round(qty * r_per_share, 2),
                "risk_pct_of_account": round(qty * r_per_share / cfg.account_rub * 100, 4),
                "risk_notional_rub": round(qty * pred.entry, 2),
                "risk_binding": ("fact" if shares else d.binding_constraint),
                "stop_pct": round(r_per_share / pred.entry, 4),
            }
            ctx.update(after)
        except Exception as e:                       # noqa: BLE001
            return {"ok": False, "reason": f"пересчёт не удался: {str(e)[:120]}"}

        trail = ctx.get("position_corrections") or []
        trail.append({"at": datetime.now(timezone.utc).isoformat(),
                      "before": before, "after": after,
                      "levels": {"entry": pred.entry, "stop": pred.stop},
                      "after_evaluation": pred.correct is not None,
                      "note": note or "снимок позиции приведён к записанным уровням"})
        ctx["position_corrections"] = trail
        pred.context_json = json.dumps(ctx, ensure_ascii=False)
        await session.commit()
        return {"ok": True, "before": before, "after": after,
                "r_per_share": round(r_per_share, 4),
                "after_evaluation": pred.correct is not None}


async def correct_prediction_levels(pred_id: int, entry: Optional[float] = None,
                                    stop: Optional[float] = None,
                                    target: Optional[float] = None,
                                    note: str = "") -> dict:
    """Исправить уровни прогноза, если исполнение отличалось от записанного.

    Нужно потому, что оценка считает R-мультипликатор по p.entry и p.stop, а не по
    контексту. Если аналитик записал вход 39.00, а сделка исполнена по 39.40, то
    всякий R, ожидание и точность считаются по цене, которой не было. Пометки в
    контексте недостаточно — правка обязана лечь в те же поля, что читает оценка.

    Исправление возможно ТОЛЬКО до оценки: менять уровни после того, как исход
    посчитан, значит переписывать историю. Прежние значения сохраняются в контексте
    как след правки, чтобы аудит видел и что было, и почему изменили.
    """
    async with async_session() as session:
        pred = await session.get(Prediction, pred_id)
        if pred is None:
            return {"ok": False, "reason": "прогноз не найден"}
        if getattr(pred, "correct", None) is not None or pred.realized_price is not None:
            return {"ok": False, "reason": "прогноз уже оценён — уровни не меняем"}

        before = {"entry": pred.entry, "stop": pred.stop, "target": pred.target,
                  "rr_planned": pred.rr_planned}
        if entry is not None:
            pred.entry = float(entry)
        if stop is not None:
            pred.stop = float(stop)
        if target is not None:
            pred.target = float(target)
        # R/R пересчитываем от новых уровней: иначе в журнале останется план,
        # не соответствующий записанным ценам.
        try:
            risk = abs(pred.entry - pred.stop)
            pred.rr_planned = round(abs(pred.target - pred.entry) / risk, 2) if risk else None
        except (TypeError, ZeroDivisionError):
            pred.rr_planned = None

        ctx = {}
        if pred.context_json:
            try:
                ctx = json.loads(pred.context_json) or {}
            except Exception:
                ctx = {}

        # СНИМОК ПОЗИЦИИ ПЕРЕСЧИТЫВАЕМ. Рублёвый результат считается по
        # risk_shares/risk_rub из контекста (см. _position_from_context), а не по
        # уровням записи. Если поправить только уровни, журнал покажет верный R и
        # НЕВЕРНЫЕ рубли: у сигнала 664 снимок был посчитан от входа 39.00 со стопом
        # 38.85 (риск 0.15 на акцию, 1282 шт, 192₽), тогда как при входе 39.40 и
        # стопе 39.10 риск 0.30 на акцию — то есть 1269 шт и 381₽. Расхождение
        # вдвое, и оно попало бы в ожидание и в отчёт по счёту.
        pos_before = {k: ctx.get(k) for k in
                      ("risk_shares", "risk_rub", "risk_pct_of_account",
                       "risk_notional_rub", "risk_binding", "stop_pct")}
        pos_after = None
        try:
            from src.risk.engine import RiskConfig, size_position
            d = size_position(
                entry=pred.entry, stop=pred.stop,
                direction=("up" if pred.direction == "up" else "down"),
                cfg=RiskConfig(), lot_size=int(ctx.get("lot_size") or 1),
                spread_pct=ctx.get("spread_pct_at_signal"),
                depth_near_mid=ctx.get("depth_near_mid_at_signal"))
            pos_after = {
                "risk_shares": d.shares,
                "risk_rub": round(d.risk_rub, 2),
                "risk_pct_of_account": round(d.risk_pct_of_account, 4),
                "risk_notional_rub": round(d.notional_rub, 2),
                "risk_binding": d.binding_constraint,
                "risk_approved_after_correction": d.approved,
                "stop_pct": (round(abs(pred.entry - pred.stop) / pred.entry, 4)
                             if pred.entry else None),
            }
            ctx.update(pos_after)
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"correct_prediction_levels: снимок позиции не пересчитан: {e}")

        # Копии уровней в контексте держим согласованными с полями записи, иначе
        # пост-мортем и аудит прочитают разные цены одного и того же сигнала.
        ctx["entry"], ctx["stop"], ctx["target"] = pred.entry, pred.stop, pred.target
        ctx["rr_planned"] = pred.rr_planned

        trail = ctx.get("level_corrections") or []
        trail.append({"at": datetime.now(timezone.utc).isoformat(),
                      "before": before,
                      "after": {"entry": pred.entry, "stop": pred.stop,
                                "target": pred.target, "rr_planned": pred.rr_planned},
                      "position_before": pos_before,
                      "position_after": pos_after,
                      "note": note or "исполнение отличалось от записанного"})
        ctx["level_corrections"] = trail
        pred.context_json = json.dumps(ctx, ensure_ascii=False)
        await session.commit()
        return {"ok": True, "before": before,
                "after": {"entry": pred.entry, "stop": pred.stop,
                          "target": pred.target, "rr_planned": pred.rr_planned},
                "position_before": pos_before, "position_after": pos_after}


async def merge_prediction_context(pred_id: int, extra: dict) -> bool:
    """Дописать поля в снимок контекста прогноза, не затирая существующие.

    Нужно, чтобы результаты оценки (например, касалась ли цена входа) ложились
    рядом с обстоятельствами сигнала, а не в отдельную таблицу: пост-мортем и
    аудит читают именно контекст. Слияние, а не перезапись — иначе оценка стёрла
    бы снимок на момент сигнала, и разбор потерял бы половину смысла.
    """
    if not extra:
        return False
    async with async_session() as session:
        pred = await session.get(Prediction, pred_id)
        if pred is None:
            return False
        ctx = {}
        if pred.context_json:
            try:
                ctx = json.loads(pred.context_json) or {}
            except Exception:
                ctx = {}
        ctx.update({k: v for k, v in extra.items() if v is not None})
        try:
            pred.context_json = json.dumps(ctx, ensure_ascii=False)
        except Exception:
            return False
        await session.commit()
        return True


HUMAN_DECISIONS = ("accept", "reject", "wait")

# Корзины технического score. Подпись «фейд верхней границы диапазона» — это
# score от −0.5 до −0.9: в боковике при range_position ≥ 0.70 правило голосует
# в шорт с силой 0.5 + (rpos − 0.70), обрезанной на 0.9 (src/analysis/technical.py).
_SCORE_BUCKETS = (
    (-1.01, -0.70, "сильный шорт (score ≤ −0.70)"),
    (-0.70, -0.50, "фейд верха диапазона (−0.70…−0.50)"),
    (-0.50, -0.20, "слабый шорт (−0.50…−0.20)"),
    (-0.20, 0.20, "нейтрально (−0.20…+0.20)"),
    (0.20, 0.50, "слабый лонг (+0.20…+0.50)"),
    (0.50, 1.01, "сильный лонг (score > +0.50)"),
)


def analyze_regime_audit(rows: list) -> dict:
    """Аудит детектора режима: совпадала ли метка с фактическим движением.

    Чистая функция — покрывается тестами. Ожидает список словарей с полями
    realized_return, technical_score, regime, range_position, adx, created_at.

    ЗАЧЕМ. Порог `ADX >= 25` в technical.py объявляет трендом только выраженное
    движение; плавный устойчивый рост держит ADX в низких двадцатых и получает
    метку «боковик». Дальше стратегия боковика фадит верхнюю границу — то есть
    шортит хаи растущего рынка. Этот отчёт показывает, происходит ли так на
    фактических данных, и позволяет проверить любую правку детектора.

    ВАЖНО: функция сама предупреждает о своей выборке. Один режим рынка и
    перекрывающиеся горизонты не позволяют делать вывод о стратегии — только о
    том, что метка режима не совпала с реальностью в этом окне.
    """
    import math
    from datetime import datetime as _dt

    ev = [r for r in (rows or []) if r.get("realized_return") is not None]
    out: dict = {
        "n": len(ev),
        "regime_recorded_share": 0.0,
        "drift": {}, "by_regime": [], "by_tech_score": [],
        "range_fade_signature": None, "window": {}, "caveats": [],
    }
    if not ev:
        out["caveats"].append("нет оценённых прогнозов — мерить нечего")
        return out

    rets = [r["realized_return"] for r in ev]
    n = len(rets)
    mean_ret = sum(rets) / n
    share_up = sum(1 for x in rets if x > 0) / n
    out["drift"] = {"mean_pct": round(mean_ret, 3),
                    "median_pct": round(sorted(rets)[n // 2], 3),
                    "share_up": round(share_up, 3)}

    # Окно данных: сколько суток и недель покрыто. От этого зависит, можно ли
    # вообще что-то заключать, поэтому считаем и выводим явно.
    days, weeks = set(), set()
    for r in ev:
        c = r.get("created_at")
        if isinstance(c, str):
            try:
                c = _dt.fromisoformat(c.replace("Z", "+00:00"))
            except ValueError:
                c = None
        if c is not None:
            days.add(c.strftime("%Y-%m-%d"))
            y, w, _ = c.isocalendar()
            weeks.add(f"{y}-W{w:02d}")
    out["window"] = {"days": len(days), "weeks": len(weeks),
                     "from": min(days) if days else None,
                     "to": max(days) if days else None}

    def _grp(rows_):
        if not rows_:
            return None
        rr = [x["realized_return"] for x in rows_]
        m = len(rr)
        up = sum(1 for x in rr if x > 0)
        se = math.sqrt(0.25 / m)
        ts = [x["technical_score"] for x in rows_ if x.get("technical_score") is not None]
        return {"n": m,
                "mean_return_pct": round(sum(rr) / m, 3),
                "median_return_pct": round(sorted(rr)[m // 2], 3),
                "share_up": round(up / m, 3),
                "sigma_from_random": round((up / m - 0.5) / se, 2) if se else None,
                "mean_tech_score": round(sum(ts) / len(ts), 3) if ts else None}

    # По метке режима — работает только если метка записана.
    labelled = [r for r in ev if r.get("regime")]
    out["regime_recorded_share"] = round(len(labelled) / n, 3)
    if labelled:
        by = {}
        for r in labelled:
            by.setdefault(str(r["regime"]), []).append(r)
        for reg, rows_ in sorted(by.items(), key=lambda kv: -len(kv[1])):
            g = _grp(rows_)
            g["regime"] = reg
            # Метка «боковик» при устойчивом одностороннем движении — признак
            # того, что детектор не увидел тренд.
            g["looks_mislabelled"] = bool(
                reg in ("range", "боковик") and g["share_up"] is not None
                and (g["share_up"] >= 0.70 or g["share_up"] <= 0.30))
            out["by_regime"].append(g)
    else:
        out["caveats"].append(
            "метка режима не записана ни у одного прогноза — прямой аудит "
            "детектора невозможен, используется подпись technical_score")

    # По силе технического сигнала — работает всегда, score заполнен.
    for lo, hi, name in _SCORE_BUCKETS:
        sel = [r for r in ev if r.get("technical_score") is not None
               and lo <= r["technical_score"] < hi]
        g = _grp(sel)
        if g:
            g["bucket"] = name
            out["by_tech_score"].append(g)

    # Подпись фейда верхней границы: сюда попадают именно шорты от хая боковика.
    fade = [r for r in ev if r.get("technical_score") is not None
            and -0.90 <= r["technical_score"] <= -0.50]
    g = _grp(fade)
    if g:
        g["interpretation"] = (
            "система шортила от верхней границы; доля роста показывает, как часто "
            "она была неправа")
        out["range_fade_signature"] = g

    # Предупреждения о выборке — чтобы отчёт нельзя было прочесть как приговор.
    if out["window"].get("days", 0) < 14:
        out["caveats"].append(
            f"окно всего {out['window'].get('days')} дн. — это ОДИН режим рынка; "
            "фейд хаёв обязан терять в растущем рынке, поэтому вывод касается "
            "метки режима, а НЕ качества стратегии")
    if out["window"].get("weeks", 0) < 3:
        out["caveats"].append(
            "меньше трёх календарных недель — нужен хотя бы один нисходящий "
            "отрезок, иначе сравнение односторонее")
    out["caveats"].append(
        "горизонты прогнозов перекрываются по времени и тикерам, поэтому "
        "эффективная выборка МЕНЬШЕ n: считать сигмы буквально нельзя")
    return out


async def regime_audit_rows() -> list[dict]:
    """Данные для аудита режима: метка на момент сигнала + фактическое движение."""
    async with async_session() as session:
        stmt = (select(Prediction)
                .where(Prediction.realized_return.is_not(None))
                .order_by(Prediction.created_at.asc()))
        rows = (await session.execute(stmt)).scalars().all()

    out = []
    for p in rows:
        ctx = {}
        if p.context_json:
            try:
                import json as _json
                ctx = _json.loads(p.context_json) or {}
            except Exception:
                ctx = {}
        out.append({
            "id": p.id, "ticker": p.ticker,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "direction": p.direction, "confidence": p.confidence,
            "technical_score": p.technical_score,
            "realized_return": p.realized_return,
            "realized_r": p.realized_r,
            "correct": p.correct,
            # Метка технического слоя пишется в снимок контекста; колонка
            # predictions.regime хранит режим ОТ CLAUDE и для старых строк пуста.
            "regime": ctx.get("regime") or p.regime,
            "regime_claude": ctx.get("regime_claude"),
            "range_position": ctx.get("range_position"),
            "adx": ctx.get("adx"),
            "strategy": ctx.get("strategy"),
        })
    return out


def _position_from_context(p) -> dict:
    """Размер позиции из снимка контекста. Нужен, чтобы посчитать P&L в рублях:
    R сам по себе денег не даёт, нужен фактический риск позиции."""
    if not p.context_json:
        return {}
    try:
        import json as _json
        ctx = _json.loads(p.context_json) or {}
        return {"risk_rub": ctx.get("risk_rub"),
                "shares": ctx.get("risk_shares"),
                "notional_rub": ctx.get("risk_notional_rub")}
    except Exception:
        return {}


async def accepted_closed_trades() -> list[dict]:
    """Закрытые сделки, которые человек ПРИНЯЛ — в порядке закрытия.

    Порядок по времени оценки, а не создания: виртуальный счёт двигается в
    момент закрытия сделки, и пик капитала должен считаться по той же шкале.
    """
    async with async_session() as session:
        stmt = (select(Prediction)
                .where(Prediction.human_decision == "accept")
                .where(Prediction.correct.is_not(None))
                .order_by(Prediction.evaluated_at.asc()))
        rows = (await session.execute(stmt)).scalars().all()

    out = []
    for p in rows:
        pos = _position_from_context(p)
        out.append({
            "id": p.id, "ticker": p.ticker, "direction": p.direction,
            "realized_r": p.realized_r, "realized_return": p.realized_return,
            "correct": p.correct, "outcome": p.outcome,
            "evaluated_at": p.evaluated_at,
            "risk_rub": pos.get("risk_rub"),
            "shares": pos.get("shares"),
            "notional_rub": pos.get("notional_rub"),
        })
    return out


async def accepted_open_trades() -> list[dict]:
    """Принятые сценарии, которые ещё не закрыты — то, что реально держим."""
    async with async_session() as session:
        stmt = (select(Prediction)
                .where(Prediction.human_decision == "accept")
                .where(Prediction.correct.is_(None)))
        rows = (await session.execute(stmt)).scalars().all()

    out = []
    for p in rows:
        pos = _position_from_context(p)
        out.append({
            "id": p.id, "ticker": p.ticker, "direction": p.direction,
            "entry": p.entry, "stop": p.stop, "target": p.target,
            "shares": pos.get("shares"),
            "risk_rub": pos.get("risk_rub"),
            "notional_rub": pos.get("notional_rub"),
        })
    return out


def decision_bucket_stats(rows: list) -> dict:
    """Статистика по подвыборке сценариев. Вынесено на уровень модуля, чтобы
    покрывалось тестами: считалка, которую нельзя проверить, — источник тихих
    ошибок в отчётах.

    `hit_rate` считается по всем строкам подвыборки, `expectancy_r` — только по
    тем, где посчитан фактический R, поэтому `r_sample` выводится отдельно:
    ожидание по трём сделкам и по тридцати читаются по-разному.
    """
    if not rows:
        return {"n": 0, "hit_rate": None, "expectancy_r": None,
                "r_sample": 0, "avg_return_pct": None}
    hits = sum(1 for p in rows if p.correct)
    rs = [p.realized_r for p in rows if p.realized_r is not None]
    rets = [p.realized_return for p in rows if p.realized_return is not None]
    return {
        "n": len(rows),
        "hit_rate": round(hits / len(rows), 3),
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "r_sample": len(rs),
        "avg_return_pct": round(sum(rets) / len(rets), 3) if rets else None,
    }


async def set_human_decision(pred_id: int, decision: str,
                             note: Optional[str] = None) -> Optional[dict]:
    """Записать решение человека по сценарию Claude.

    Возвращает обновлённую запись или None, если прогноз не найден.
    Решение можно менять до момента оценки: пока исход не посчитан, человек
    вправе передумать. После оценки менять нельзя — иначе появится соблазн
    «переголосовать» задним числом, и вся статистика сравнения обесценится.
    """
    d = (decision or "").strip().lower()
    if d not in HUMAN_DECISIONS:
        raise ValueError(f"decision должен быть одним из {HUMAN_DECISIONS}, получено {decision!r}")

    async with async_session() as session:
        pred = await session.get(Prediction, pred_id)
        if pred is None:
            return None
        if pred.correct is not None:
            raise ValueError(
                f"прогноз {pred_id} уже оценён — решение задним числом не принимается")
        pred.human_decision = d
        pred.decision_note = (note or None)
        pred.decided_at = datetime.now(timezone.utc)
        await session.commit()
        return pred.to_dict()


async def decision_stats(ticker: Optional[str] = None) -> dict:
    """Сравнение: где прав Claude, где прав человек, что даёт вето человека.

    КОРРЕКТНОСТЬ СРАВНЕНИЯ. Исход считается для ВСЕХ сценариев одинаково —
    оценщик работает по пути цены и горизонту, а не по факту исполнения.
    Поэтому отклонённые сделки оцениваются той же меркой, что принятые, и
    сравнение «человек против модели» не смещено по построению. Если бы
    отклонённые оценивались на глазок («цена ушла +3%»), а принятые по стопам и
    целям, любой вывод был бы артефактом методики.

    ГЛАВНОЕ ЧИСЛО — `human_edge_r`: разница между ожиданием по ПРИНЯТЫМ сделкам
    и ожиданием по ВСЕМ предложениям. Больше нуля — вето человека добавляет
    ценность, меньше нуля — отнимает. Это и есть ответ на вопрос «где человек
    лучше модели», выраженный в деньгах, а не в ощущениях.

    ОГОВОРКА О ВЫБОРКЕ. Нерешённые сценарии (`undecided`) — не случайная
    подвыборка: человек мог просто не успеть к экрану. Пока их доля велика,
    сравнение читать нельзя, поэтому число выводится явно.
    """
    async with async_session() as session:
        stmt = select(Prediction)
        if ticker:
            stmt = stmt.where(Prediction.ticker == ticker.upper())
        result = await session.execute(stmt)
        preds = result.scalars().all()

    directional = [p for p in preds if _is_directional(p)]
    scored = [p for p in directional if p.correct is not None]

    by = {d: decision_bucket_stats([p for p in scored if (p.human_decision or "") == d])
          for d in HUMAN_DECISIONS}
    all_stats = decision_bucket_stats(scored)
    undecided = decision_bucket_stats([p for p in scored if not p.human_decision])

    # Вклад человека: ожидание по принятым минус ожидание по всем предложениям.
    human_edge = None
    if by["accept"]["expectancy_r"] is not None and all_stats["expectancy_r"] is not None:
        human_edge = round(by["accept"]["expectancy_r"] - all_stats["expectancy_r"], 3)

    return {
        "all_proposals": all_stats,          # сырой эйдж Claude
        "accepted": by["accept"],            # что вы фактически получили
        "rejected": by["reject"],            # на что вы не пошли
        "waited": by["wait"],
        "undecided": undecided,              # решения не было — читать с осторожностью
        "human_edge_r": human_edge,
        "human_edge_note": (
            "разница ожидания по принятым и по всем предложениям; "
            "> 0 — вето человека добавляет ценность, < 0 — отнимает"),
        "decided_share": (round(1 - undecided["n"] / all_stats["n"], 3)
                          if all_stats["n"] else None),
        "pending_evaluation": len(directional) - len(scored),
        "comparability": (
            "исход считается одинаково для принятых и отклонённых — "
            "оценщик не знает о решении человека"),
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


# Политика «что считается новостью» живёт в config/settings.py — это решение о
# смысле данных, а не деталь слоя БД.
try:
    from config.settings import NEWS_KINDS, NEWS_EXCLUDE_SOURCES
except Exception:                                   # noqa: BLE001
    NEWS_KINDS = ("news", "message")
    NEWS_EXCLUDE_SOURCES = ("tinkoff", "pulse_deal")


async def known_news_guids(since_hours: int = 48, limit: int = 4000) -> set:
    """Идентификаторы уже сохранённых новостей — для дедупликации при старте.

    Дедупликация коллектора жила в памяти процесса (_seen_ids) и обнулялась при
    каждом перезапуске. 30.07 замер показал: из 200 новостных записей 132 (66%)
    были повторами, один заголовок вставлен ЧЕТЫРНАДЦАТЬ раз — по одному разу на
    каждый цикл опроса после деплоя.
    """
    out: set = set()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        async with async_session() as session:
            stmt = (select(MarketEvent.payload)
                    .where(MarketEvent.kind.in_(NEWS_KINDS))
                    .where(MarketEvent.ts >= cutoff)
                    .order_by(MarketEvent.ts.desc())
                    .limit(min(limit, 10000)))
            for (pl,) in (await session.execute(stmt)).all():
                if not pl:
                    continue
                try:
                    g = json.loads(pl).get("guid")
                except Exception:
                    continue
                if g:
                    out.add(str(g))
    except Exception as e:
        logger.debug(f"known_news_guids failed: {e}")
    return out


async def fresh_news(ticker: str, since_minutes: int = 90,
                     limit: int = 20) -> list[dict]:
    """Свежие ТЕКСТОВЫЕ новости по бумаге: RSS, чаты, Пульс.

    Возвращает сами записи, а не флаг: детектору событий нужна метка времени для
    проверки причинности, а человеку и Claude — заголовок, чтобы понять, на каком
    основании момент назван новостным.

    ts — время ПУБЛИКАЦИИ (pubDate из ленты), а не время сбора, поэтому по нему
    можно сравнивать с барами свечей.
    """
    if not ticker:
        return []
    async with async_session() as session:
        stmt = (select(MarketEvent)
                .where(MarketEvent.ticker == ticker.upper())
                .where(MarketEvent.kind.in_(NEWS_KINDS))
                .where(MarketEvent.source.notin_(NEWS_EXCLUDE_SOURCES))
                .order_by(MarketEvent.ts.desc())
                .limit(min(limit, 100)))
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


# ─── Поток сделок с минутным разрешением ──────────────────────────────────────

async def get_flow_watermark(ticker: str, day: str) -> Optional[str]:
    """
    Время последней учтённой сделки за день. По нему отбираются НОВЫЕ сделки.

    Без этого пересекающиеся опросы задваивают объём: Tinkoff отдаёт окно
    времени, и соседние снимки перекрываются почти целиком.
    """
    async with async_session() as session:
        q = (select(FlowMinute.watermark)
             .where(FlowMinute.ticker == ticker.upper(),
                    FlowMinute.ts.like(f"{day}%"),
                    FlowMinute.watermark.is_not(None))
             .order_by(FlowMinute.watermark.desc()).limit(1))
        return (await session.execute(q)).scalar_one_or_none()


async def merge_flow_minutes(ticker: str, rows: list[dict],
                             watermark: Optional[str],
                             source: str = "exchange",
                             instance: str = "") -> int:
    """
    Влить минутные срезы потока. rows: [{ts, session, buy_volume, sell_volume,
    trade_count, max_trade, vwap_num}], ts вида "YYYY-MM-DDTHH:MM" (МСК).

    Складывает в существующие минуты. Защита от двойного счёта — НЕ здесь, а
    на входе: сюда попадают только сделки новее прежнего watermark.
    Пишем «мягко»: ошибка записи не должна ронять сбор.
    """
    if not rows:
        return 0
    n = 0
    try:
        async with async_session() as session:
            for r in rows:
                key = f"{r['ts']}:{ticker.upper()}:{source}"
                if instance:
                    key += f":{instance}"
                row = await session.get(FlowMinute, key)
                if row is None:
                    # Значения задаются ЯВНО, а не через default колонки:
                    # до записи в базу они остались бы None, и накопление
                    # падало бы на «NoneType + int».
                    row = FlowMinute(key=key, ts=r["ts"], ticker=ticker.upper(),
                                     source=source,
                                     session=r.get("session") or "main",
                                     buy_volume=0, sell_volume=0, trade_count=0,
                                     max_trade=0, vwap_num=0.0)
                    session.add(row)
                row.buy_volume += int(r.get("buy_volume") or 0)
                row.sell_volume += int(r.get("sell_volume") or 0)
                row.trade_count += int(r.get("trade_count") or 0)
                row.max_trade = max(row.max_trade, int(r.get("max_trade") or 0))
                row.vwap_num += float(r.get("vwap_num") or 0.0)
                if watermark:
                    row.watermark = watermark
                row.updated_at = datetime.now(timezone.utc)
                n += 1
            await session.commit()
    except Exception as e:                                       # noqa: BLE001
        logger.debug(f"merge_flow_minutes {ticker}: {e}")
        return 0
    return n


def pick_fullest(rows: list[dict], by: str) -> list[dict]:
    """
    Из нескольких наблюдений ОДНОЙ минуты оставить самое полное.

    Зачем. При перекате деплоя Coolify держит два контейнера, у каждого свой
    стрим, и оба получают ПОЛНЫЙ поток независимо. Складывать их наблюдения
    нельзя: 01.08 по SBER за минуту 14:03 сумма дала 736 лотов против настоящих
    468 — завышение в полтора раза, и с виду совершенно правдоподобное.

    Правильный ответ — не сумма, а ОДНО из наблюдений. Для полностью прожитой
    минуты они совпадают; если контейнер поднялся посреди минуты, его наблюдение
    беднее. Поэтому берётся то, где событий больше.

    Недосчёт при этом возможен: если один контейнер умер посреди минуты, а
    второй в ней же поднялся, ни одно наблюдение не полное. Это видно сверкой с
    объёмом свечи и лучше задвоения — недостача заметна, а задвоение нет.

    ЧИСТАЯ функция: ни базы, ни сети.
    """
    best: dict = {}
    for r in rows:
        ts = r.get("ts")
        cur = best.get(ts)
        if cur is None or (r.get(by) or 0) > (cur.get(by) or 0):
            best[ts] = r
    return [best[k] for k in sorted(best)]


FLOW_RES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "session": 10 ** 6}


def aggregate_flow(rows: list[dict], step: int, ticker: str = "") -> list[dict]:
    """
    Склейка минут в бары нужного шага и расчёт производных. ЧИСТАЯ функция:
    ни базы, ни сети — поэтому проверяется тестом без подмены модулей.

    rows: [{ts "YYYY-MM-DDTHH:MM", session, buy_volume, sell_volume,
            trade_count, max_trade, vwap_num}] в порядке возрастания времени.

    Производные считаются ЗДЕСЬ, а не хранятся: при смене определения «крупной
    сделки» историю не придётся переписывать.

    VWAP склеивается через сумму цена*объём, а не усреднением минутных VWAP:
    средние усреднять нельзя, суммы складываются.
    """
    if not rows:
        return []
    buckets: dict = {}
    order: list = []
    for r in rows:
        ts = r["ts"]
        mm = int(ts[11:13]) * 60 + int(ts[14:16])
        k = mm // step
        if k not in buckets:
            buckets[k] = {"ts": ts, "buy": 0, "sell": 0, "n": 0,
                          "max": 0, "num": 0.0,
                          "session": r.get("session") or "main"}
            order.append(k)
        b = buckets[k]
        b["buy"] += r.get("buy_volume") or 0
        b["sell"] += r.get("sell_volume") or 0
        b["n"] += r.get("trade_count") or 0
        b["max"] = max(b["max"], r.get("max_trade") or 0)
        b["num"] += r.get("vwap_num") or 0.0
    out, cum = [], 0
    for k in order:
        b = buckets[k]
        tot = b["buy"] + b["sell"]
        delta = b["buy"] - b["sell"]
        cum += delta
        out.append({
            "ts": b["ts"], "ticker": ticker.upper(), "session": b["session"],
            "buy_volume": b["buy"], "sell_volume": b["sell"],
            "delta": delta, "cumulative_delta": cum,
            "buy_ratio": round(b["buy"] / tot, 4) if tot else None,
            "sell_ratio": round(b["sell"] / tot, 4) if tot else None,
            "trade_count": b["n"],
            "average_trade_size": round(tot / b["n"], 2) if b["n"] else None,
            "max_trade": b["max"],
            "vwap": round(b["num"] / tot, 6) if tot else None,
            "imbalance": round(delta / tot, 4) if tot else None,
        })
    return out


async def flow_series(ticker: str, day: str, res: str = "1m",
                      source: str = "exchange") -> list[dict]:
    """
    Поток по бумаге за день. res: 1m | 5m | 15m | 30m | session.

    source: exchange (по умолчанию) | dealer | mixed | all.

    По умолчанию БИРЖЕВОЙ. Дилерские сделки — внутренний рынок брокера, цена
    там не формируется биржевым стаканом: 01.08 при закрытой бирже пришло 2812
    таких сделок, и старый код записал бы их как настоящие. mixed — то, что
    собрано до 01.08, когда источник не различался.
    """
    step = FLOW_RES.get(res, 1)
    async with async_session() as session:
        conds = [FlowMinute.ticker == ticker.upper(), FlowMinute.ts.like(f"{day}%")]
        if source != "all":
            conds.append(FlowMinute.source == source)
        q = select(FlowMinute).where(*conds).order_by(FlowMinute.ts)
        rows = (await session.execute(q)).scalars().all()
    plain = [{"ts": r.ts, "session": r.session, "buy_volume": r.buy_volume,
              "sell_volume": r.sell_volume, "trade_count": r.trade_count,
              "max_trade": r.max_trade, "vwap_num": r.vwap_num} for r in rows]
    # Одну минуту могли записать ДВА экземпляра стрима при перекате деплоя.
    # Берём самое полное наблюдение, а не сумму.
    plain = pick_fullest(plain, "trade_count")
    return aggregate_flow(plain, step, ticker)


async def prune_flow_minute(keep_days: int = 90) -> int:
    """
    Чистка минутного потока.

    Три дня, как у session_footprint, здесь не годятся: ради минутного потока
    всё и затевалось, а на трёх днях ничего не проверить. При 80 бумагах и
    ~1000 минут в день это ~2.4 млн строк за 90 дней — SQLite тянет.
    """
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=3)
              - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    async with async_session() as session:
        result = await session.execute(
            delete(FlowMinute).where(FlowMinute.ts < cutoff))
        await session.commit()
        return result.rowcount or 0


async def merge_book_minutes(ticker: str, rows: list[dict],
                             source: str = "exchange",
                             instance: str = "") -> int:
    """
    Влить минутные срезы стакана. Складывает в существующие минуты.

    Задвоения здесь бояться не нужно: поток отдаёт каждый пакет один раз, а
    накопитель обнуляется при выгрузке. Опасность обратная — дыра при обрыве,
    и её видно по разрыву минут и счётчику обрывов в диагностике.
    """
    if not rows:
        return 0
    n = 0
    try:
        async with async_session() as session:
            for r in rows:
                key = f"{r['ts']}:{ticker.upper()}:{source}"
                if instance:
                    key += f":{instance}"
                row = await session.get(BookMinute, key)
                if row is None:
                    # Значения ЯВНО, а не через default колонки: до записи в
                    # базу они остались бы None и накопление упало бы на
                    # «NoneType + float». Та же ошибка уже была в flow_minute.
                    row = BookMinute(key=key, ts=r["ts"], ticker=ticker.upper(),
                                     source=source,
                                     session=r.get("session") or "main",
                                     updates=0, bid_vol_sum=0.0, ask_vol_sum=0.0,
                                     spread_sum=0.0, best_bid=0.0, best_ask=0.0,
                                     imb_min=float(r.get("imb_min") or 0.0),
                                     imb_max=float(r.get("imb_max") or 0.0),
                                     bid5_sum=0.0, ask5_sum=0.0,
                                     bid_top_max=0, ask_top_max=0)
                    session.add(row)
                row.updates += int(r.get("updates") or 0)
                row.bid_vol_sum += float(r.get("bid_vol_sum") or 0.0)
                row.ask_vol_sum += float(r.get("ask_vol_sum") or 0.0)
                row.spread_sum += float(r.get("spread_sum") or 0.0)
                row.bid5_sum += float(r.get("bid5_sum") or 0.0)
                row.ask5_sum += float(r.get("ask5_sum") or 0.0)
                row.bid_top_max = max(row.bid_top_max,
                                      int(r.get("bid_top_max") or 0))
                row.ask_top_max = max(row.ask_top_max,
                                      int(r.get("ask_top_max") or 0))
                if r.get("best_bid"):
                    row.best_bid = float(r["best_bid"])
                if r.get("best_ask"):
                    row.best_ask = float(r["best_ask"])
                row.imb_min = min(row.imb_min, float(r.get("imb_min") or 0.0))
                row.imb_max = max(row.imb_max, float(r.get("imb_max") or 0.0))
                row.updated_at = datetime.now(timezone.utc)
                n += 1
            await session.commit()
    except Exception as e:                                       # noqa: BLE001
        logger.debug(f"merge_book_minutes {ticker}: {e}")
        return 0
    return n


def aggregate_book(rows: list[dict], step: int, ticker: str = "") -> list[dict]:
    """
    Склейка минут стакана в бары нужного шага. ЧИСТАЯ функция — тестируется
    без базы.

    bid_share — доля покупателей в объёме стакана. Это и есть ответ на вопрос
    «объёмы продавцы или покупатели». Считается из СУММ, поэтому склейка
    корректна: доли усреднять нельзя, суммы складываются.

    flipped — был ли внутри интервала разворот перекоса через середину. Ровная
    минута и минута с разворотом дают одинаковое среднее, но означают разное.
    """
    if not rows:
        return []
    buckets: dict = {}
    order: list = []
    for r in rows:
        ts = r["ts"]
        mm = int(ts[11:13]) * 60 + int(ts[14:16])
        k = mm // step
        if k not in buckets:
            buckets[k] = {"ts": ts, "upd": 0, "bid": 0.0, "ask": 0.0,
                          "spread": 0.0, "bb": 0.0, "ba": 0.0,
                          "lo": r.get("imb_min", 0.5), "hi": r.get("imb_max", 0.5),
                          "b5": 0.0, "a5": 0.0, "btop": 0, "atop": 0,
                          "session": r.get("session") or "main"}
            order.append(k)
        b = buckets[k]
        b["upd"] += r.get("updates") or 0
        b["bid"] += r.get("bid_vol_sum") or 0.0
        b["ask"] += r.get("ask_vol_sum") or 0.0
        b["spread"] += r.get("spread_sum") or 0.0
        b["b5"] += r.get("bid5_sum") or 0.0
        b["a5"] += r.get("ask5_sum") or 0.0
        b["btop"] = max(b["btop"], r.get("bid_top_max") or 0)
        b["atop"] = max(b["atop"], r.get("ask_top_max") or 0)
        if r.get("best_bid"):
            b["bb"] = r["best_bid"]
        if r.get("best_ask"):
            b["ba"] = r["best_ask"]
        b["lo"] = min(b["lo"], r.get("imb_min", 0.5))
        b["hi"] = max(b["hi"], r.get("imb_max", 0.5))
    out = []
    for k in order:
        b = buckets[k]
        tot = b["bid"] + b["ask"]
        out.append({
            "ts": b["ts"], "ticker": ticker.upper(), "session": b["session"],
            "updates": b["upd"],
            "bid_volume": round(b["bid"], 1), "ask_volume": round(b["ask"], 1),
            "bid_share": round(b["bid"] / tot, 4) if tot else None,
            "imbalance": round((b["bid"] - b["ask"]) / tot, 4) if tot else None,
            "imb_min": round(b["lo"], 4), "imb_max": round(b["hi"], 4),
            "flipped": bool(b["lo"] < 0.5 < b["hi"]),
            "avg_spread": round(b["spread"] / b["upd"], 6) if b["upd"] else None,
            "best_bid": b["bb"] or None, "best_ask": b["ba"] or None,
            # Доля объёма в ПЯТИ лучших уровнях: близко к 1 — заявки прижаты к
            # цене, близко к 0 — размазаны вглубь. Считается из сумм, поэтому
            # склейка минут в пятиминутки остаётся верной.
            "bid_near_share": round(b["b5"] / b["bid"], 4) if b["bid"] else None,
            "ask_near_share": round(b["a5"] / b["ask"], 4) if b["ask"] else None,
            # Крупнейшая одиночная заявка за интервал и её доля в стороне.
            "bid_top": b["btop"] or None, "ask_top": b["atop"] or None,
            "bid_top_share": (round(b["btop"] / (b["bid"] / b["upd"]), 4)
                              if b["upd"] and b["bid"] else None),
            "ask_top_share": (round(b["atop"] / (b["ask"] / b["upd"]), 4)
                              if b["upd"] and b["ask"] else None),
        })
    return out


async def book_series(ticker: str, day: str, res: str = "1m",
                      source: str = "exchange") -> list[dict]:
    """
    Стакан по бумаге за день. res: 1m | 5m | 15m | 30m | session.

    source: exchange (по умолчанию) | dealer | mixed | all.
    """
    step = FLOW_RES.get(res, 1)
    async with async_session() as session:
        conds = [BookMinute.ticker == ticker.upper(), BookMinute.ts.like(f"{day}%")]
        if source != "all":
            conds.append(BookMinute.source == source)
        q = select(BookMinute).where(*conds).order_by(BookMinute.ts)
        rows = (await session.execute(q)).scalars().all()
    plain = [{"ts": r.ts, "session": r.session, "updates": r.updates,
              "bid_vol_sum": r.bid_vol_sum, "ask_vol_sum": r.ask_vol_sum,
              "spread_sum": r.spread_sum, "best_bid": r.best_bid,
              "best_ask": r.best_ask, "imb_min": r.imb_min,
              "imb_max": r.imb_max, "bid5_sum": r.bid5_sum,
              "ask5_sum": r.ask5_sum, "bid_top_max": r.bid_top_max,
              "ask_top_max": r.ask_top_max} for r in rows]
    plain = pick_fullest(plain, "updates")
    return aggregate_book(plain, step, ticker)


async def merge_candle_minutes(ticker: str, rows: list[dict]) -> int:
    """
    Влить минутные свечи. Объём ЗАМЕНЯЕТСЯ, границы расширяются.

    Складывать объём нельзя: в свече он накопительный за интервал, и каждая
    следующая версия уже содержит предыдущую.
    """
    if not rows:
        return 0
    n = 0
    try:
        async with async_session() as session:
            for r in rows:
                key = f"{r['ts']}:{ticker.upper()}"
                row = await session.get(CandleMinute, key)
                if row is None:
                    row = CandleMinute(
                        key=key, ts=r["ts"], ticker=ticker.upper(),
                        session=r.get("session") or "main",
                        open=float(r.get("open") or 0.0),
                        high=float(r.get("high") or 0.0),
                        low=float(r.get("low") or 0.0),
                        close=float(r.get("close") or 0.0),
                        volume=0, volume_buy=0, volume_sell=0, updates=0)
                    session.add(row)
                else:
                    row.high = max(row.high, float(r.get("high") or 0.0))
                    lo = float(r.get("low") or 0.0)
                    row.low = min(row.low, lo) if row.low > 0 and lo > 0 else (lo or row.low)
                    row.close = float(r.get("close") or row.close)
                # ЗАМЕНА, не сложение: объём в свече накопительный
                row.volume = int(r.get("volume") or 0)
                row.volume_buy = int(r.get("volume_buy") or 0)
                row.volume_sell = int(r.get("volume_sell") or 0)
                row.updates += int(r.get("updates") or 1)
                row.updated_at = datetime.now(timezone.utc)
                n += 1
            await session.commit()
    except Exception as e:                                       # noqa: BLE001
        logger.debug(f"merge_candle_minutes {ticker}: {e}")
        return 0
    return n


def aggregate_candles(rows: list[dict], step: int, ticker: str = "") -> list[dict]:
    """
    Склейка минутных свечей в бары нужного шага. ЧИСТАЯ функция.

    open берётся ПЕРВОЙ минуты, close — ПОСЛЕДНЕЙ, high и low крайними,
    объём складывается. Здесь объём складывать МОЖНО и НУЖНО: накопительный он
    внутри одной минуты, а разные минуты не пересекаются.
    """
    if not rows:
        return []
    buckets: dict = {}
    order: list = []
    for r in rows:
        ts = r["ts"]
        mm = int(ts[11:13]) * 60 + int(ts[14:16])
        k = mm // step
        if k not in buckets:
            buckets[k] = {"ts": ts, "o": r.get("open") or 0.0,
                          "h": r.get("high") or 0.0, "l": r.get("low") or 0.0,
                          "c": r.get("close") or 0.0, "v": 0, "vb": 0, "vs": 0,
                          "session": r.get("session") or "main"}
            order.append(k)
        b = buckets[k]
        b["h"] = max(b["h"], r.get("high") or 0.0)
        lo = r.get("low") or 0.0
        b["l"] = min(b["l"], lo) if b["l"] > 0 and lo > 0 else (lo or b["l"])
        b["c"] = r.get("close") or b["c"]
        b["v"] += r.get("volume") or 0
        b["vb"] += r.get("volume_buy") or 0
        b["vs"] += r.get("volume_sell") or 0
    out = []
    for k in order:
        b = buckets[k]
        tot = b["vb"] + b["vs"]
        out.append({
            "ts": b["ts"], "ticker": ticker.upper(), "session": b["session"],
            "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
            "volume": b["v"], "volume_buy": b["vb"], "volume_sell": b["vs"],
            "buy_ratio": round(b["vb"] / tot, 4) if tot else None,
            "range": round(b["h"] - b["l"], 6) if b["h"] and b["l"] else None,
            "change": round(b["c"] - b["o"], 6) if b["o"] else None,
        })
    return out


async def candle_series(ticker: str, day: str, res: str = "1m") -> list[dict]:
    """Минутные бары из потока за день. res: 1m | 5m | 15m | 30m | session."""
    step = FLOW_RES.get(res, 1)
    async with async_session() as session:
        q = (select(CandleMinute)
             .where(CandleMinute.ticker == ticker.upper(),
                    CandleMinute.ts.like(f"{day}%"))
             .order_by(CandleMinute.ts))
        rows = (await session.execute(q)).scalars().all()
    plain = [{"ts": r.ts, "session": r.session, "open": r.open, "high": r.high,
              "low": r.low, "close": r.close, "volume": r.volume,
              "volume_buy": r.volume_buy, "volume_sell": r.volume_sell}
             for r in rows]
    return aggregate_candles(plain, step, ticker)


async def minute_rows(ticker: str, day: str,
                      source: str = "exchange") -> list[dict]:
    """
    Свести поток, стакан и свечи одной минуты в ОДНУ строку.

    Нужно детектору событий: он смотрит на связки между потоком, стаканом и
    ценой, а они лежат в трёх таблицах.

    Минуты, которых нет ни в одной таблице, не появляются — пропуск означает
    отсутствие торгов, и подставлять туда нули нельзя: это создало бы события
    из ничего.
    """
    flow = await flow_series(ticker, day, "1m", source=source)
    book = await book_series(ticker, day, "1m", source=source)
    candles = await candle_series(ticker, day, "1m")
    merged: dict = {}
    for src in (flow, book, candles):
        for r in src:
            merged.setdefault(r["ts"], {"ts": r["ts"]}).update(
                {k: v for k, v in r.items() if k not in ("ts", "ticker")})
    return [merged[k] for k in sorted(merged)]


async def flow_candle_check(ticker: str, day: str,
                            source: str = "exchange") -> dict:
    """
    Сверка НАШЕГО разбора направлений с объёмом свечи биржи.

    Зачем. volume_buy и volume_sell приходят от биржи в самой свече, а
    flow_minute считается нами из отдельных сделок. Два независимых счёта одного
    и того же — значит расхождение указывает на ошибку в одном из них.

    01.08 эта сверка нашла задвоение через две минуты после того, как появилась:
    по SBER за 14:03 наш поток дал 736 лотов против 468 в свече, потому что при
    перекате деплоя два контейнера писали в одну строку. Минуты после переката
    совпали до лота.

    ВАЖНО ПРО ИСТОЧНИК. Свеча строится из БИРЖЕВЫХ сделок, поэтому сверять с ней
    осмысленно только source=exchange. Для dealer объёмы просто не связаны:
    дилерские сделки в биржевую свечу не входят, и расхождение там ничего не
    означает. Поэтому флаг выставляется только для биржевого источника.
    """
    flow = await flow_series(ticker, day, "1m", source=source)
    candles = await candle_series(ticker, day, "1m")
    cd = {r["ts"]: r for r in candles}
    out, bad = [], 0
    for f in flow:
        c = cd.get(f["ts"])
        if not c:
            continue
        ours = (f["buy_volume"] or 0) + (f["sell_volume"] or 0)
        theirs = c["volume"] or 0
        # Подозрительно ПРЕВЫШЕНИЕ: это подпись задвоения. Недосчёт возможен
        # законно — контейнер мог подняться посреди минуты.
        suspect = bool(source == "exchange" and theirs and ours > theirs * 1.05)
        if suspect:
            bad += 1
        out.append({
            "ts": f["ts"], "ours": ours, "candle": theirs,
            "diff": ours - theirs,
            "ours_buy": f["buy_volume"], "candle_buy": c["volume_buy"],
            "ours_sell": f["sell_volume"], "candle_sell": c["volume_sell"],
            "suspect": suspect,
        })
    return {"ticker": ticker.upper(), "day": day, "source": source,
            "minutes": len(out), "suspect_minutes": bad, "rows": out}


async def merge_micro_minutes(ticker: str, rows: list[dict],
                              source: str = "exchange") -> int:
    """
    Влить производные секундного ряда. Размах расширяется, суммы складываются,
    пиковая скорость берётся максимумом.
    """
    if not rows:
        return 0
    n = 0
    SUMS = ("bid_added", "bid_removed", "ask_added", "ask_removed")
    PEAKS = ("bid_peak_add", "bid_peak_remove", "ask_peak_add", "ask_peak_remove")
    COUNTS = ("traded_at_best", "traded_near", "traded_deep", "samples")
    try:
        async with async_session() as session:
            for r in rows:
                key = f"{r['ts']}:{ticker.upper()}:{source}"
                row = await session.get(MicroMinute, key)
                if row is None:
                    row = MicroMinute(
                        key=key, ts=r["ts"], ticker=ticker.upper(), source=source,
                        session=r.get("session") or "main", samples=0,
                        imb_d10_max=0.0, imb_d10_min=0.0,
                        imb_d30_max=0.0, imb_d30_min=0.0,
                        **{k: 0.0 for k in SUMS + PEAKS},
                        traded_at_best=0, traded_near=0, traded_deep=0)
                    session.add(row)
                for k in ("imb_d10", "imb_d30"):
                    if r.get(f"{k}_max") is not None:
                        setattr(row, f"{k}_max",
                                max(getattr(row, f"{k}_max"), float(r[f"{k}_max"])))
                    if r.get(f"{k}_min") is not None:
                        setattr(row, f"{k}_min",
                                min(getattr(row, f"{k}_min"), float(r[f"{k}_min"])))
                for k in SUMS:
                    setattr(row, k, getattr(row, k) + float(r.get(k) or 0.0))
                for k in PEAKS:
                    setattr(row, k, max(getattr(row, k), float(r.get(k) or 0.0)))
                for k in COUNTS:
                    setattr(row, k, getattr(row, k) + int(r.get(k) or 0))
                row.updated_at = datetime.now(timezone.utc)
                n += 1
            await session.commit()
    except Exception as e:                                       # noqa: BLE001
        logger.debug(f"merge_micro_minutes {ticker}: {e}")
        return 0
    return n


#  Порог записи уровня в базу, в РУБЛЯХ за минуту. Ниже него строка не пишется:
#  иначе на каждую минуту пришлось бы по шесть уровней на бумагу на источник,
#  около полумиллиона строк в день, и арифметика не сошлась бы с 20 ГБ.
#
#  Значение — ДОГАДКА. Сколько уровней его перейдут на живом рынке, я не знаю;
#  калибровать надо по факту, поэтому оно вынесено в переменную окружения.
LEVEL_MINUTE_FLOOR_RUB = float(os.getenv("LEVEL_FLOOR_RUB", "100000"))


class LevelStoreError(RuntimeError):
    """
    Чтение истории уровней не удалось, и причина известна.

    Отдельный тип нужен, чтобы маршрут вернул ПРИЧИНУ, а не голую пятисотку:
    «no such table» и «база занята» требуют разных действий, а Internal Server
    Error не отличает их ничем.
    """


async def ensure_level_table() -> dict:
    """
    Создать level_minute, если её нет.

    Зачем отдельно от init_db. Таблица добавлена позже остальных, и на живом
    сервере она не появилась, хотя create_all вызывается при старте. Причину
    надо увидеть, а не угадать: здесь создание вызывается явно и отдаёт результат
    наружу вместе с текстом ошибки, если он есть.
    """
    try:
        async with engine.begin() as conn:
            await conn.run_sync(LevelMinute.__table__.create, checkfirst=True)
        return {"ok": True}
    except Exception as e:                                       # noqa: BLE001
        logger.warning(f"ensure_level_table: {e}")
        return {"ok": False, "error": str(e)[:300]}


async def merge_level_minutes(rows: list[dict],
                              floor_rub: Optional[float] = None) -> dict:
    """
    Влить минутные итоги уровней. Суммы складываются, пик берётся максимумом.

    `rows` — то, что отдал LevelLog.drop_minute, дополненное тикером, ценой,
    стороной, источником и лотностью.

    Возвращает и записанное, и ОТБРОШЕННОЕ: без второго числа нельзя понять,
    порог отсекает шум или половину полезного.
    """
    if not rows:
        return {"written": 0, "skipped": 0}
    floor = LEVEL_MINUTE_FLOOR_RUB if floor_rub is None else float(floor_rub)
    n = skipped = 0
    SUMS = ("added", "traded", "pulled", "restored", "gone", "events")
    try:
        async with async_session() as session:
            for r in rows:
                tk = (r.get("ticker") or "").upper()
                price = float(r.get("price") or 0)
                lot = max(1, int(r.get("lot") or 1))
                if not tk or price <= 0 or not r.get("ts"):
                    skipped += 1
                    continue
                # Значимость считается в РУБЛЯХ: «1000 лотов» у SBER и у UGLD —
                # это 276 тысяч и 666 тысяч, сравнивать в лотах нельзя.
                money = price * lot
                moved = (int(r.get("traded") or 0) + int(r.get("pulled") or 0)
                         + int(r.get("added") or 0)) * money
                if moved < floor and not r.get("restored"):
                    skipped += 1
                    continue
                src = r.get("source") or "exchange"
                side = r.get("side") or "bid"
                key = f"{r['ts']}:{tk}:{src}:{side}:{price:.6f}"
                row = await session.get(LevelMinute, key)
                if row is None:
                    row = LevelMinute(key=key, ts=r["ts"], ticker=tk,
                                      source=src, side=side, price=price,
                                      peak=0, end_size=0,
                                      **{k: 0 for k in SUMS})
                    session.add(row)
                for k in SUMS:
                    setattr(row, k, getattr(row, k) + int(r.get(k) or 0))
                row.peak = max(row.peak, int(r.get("peak") or 0))
                row.end_size = int(r.get("end_size") or 0)
                # Счётчики тестов НАКОПИТЕЛЬНЫЕ: берётся последнее состояние, а
                # не сумма. Сложение удвоило бы их при повторном вливе минуты.
                for k in ("tests", "test_held", "test_failed", "alive_sec"):
                    if r.get(k) is not None:
                        setattr(row, k, int(r[k]))
                row.updated_at = datetime.now(timezone.utc)
                n += 1
            await session.commit()
    except Exception as e:                                       # noqa: BLE001
        logger.debug(f"merge_level_minutes: {e}")
        return {"written": 0, "skipped": skipped, "error": str(e)[:120]}
    return {"written": n, "skipped": skipped, "floor_rub": floor}


async def level_series(ticker: str, day: str, source: str = "exchange",
                       limit: int = 400) -> list[dict]:
    """
    История уровней бумаги за день, по минутам. Порядок — по времени.

    Отдаётся в ЛОТАХ, как лежит. Рубли считает вызывающий: лотность у бумаг
    разная и в базе её нет.
    """
    # Таблица могла не создаться: она добавлена позже остальных, и если старт
    # прошёл мимо create_all, чтение падает пятисоткой без объяснения. Пустой
    # ответ вместо падения, но с записью причины — молчаливая пустота уже дважды
    # обходилась дорого.
    try:
        async with async_session() as session:
            conds = [LevelMinute.ticker == ticker.upper(),
                     LevelMinute.ts.like(f"{day}%")]
            if source:
                conds.append(LevelMinute.source == source)
            q = (select(LevelMinute).where(*conds)
                 .order_by(LevelMinute.ts).limit(max(1, int(limit))))
            rows = (await session.execute(q)).scalars().all()
    except Exception as e:                                       # noqa: BLE001
        logger.warning(f"level_series {ticker}: {e}")
        raise LevelStoreError(str(e)[:300]) from e
    return [{"ts": r.ts, "side": r.side, "price": r.price, "peak": r.peak,
             "end_size": r.end_size, "added": r.added, "traded": r.traded,
             "pulled": r.pulled, "restored": r.restored, "gone": r.gone,
             "events": r.events, "source": r.source,
             "tests": r.tests, "test_held": r.test_held,
             "test_failed": r.test_failed, "alive_sec": r.alive_sec}
            for r in rows]


async def micro_series(ticker: str, day: str,
                       source: str = "exchange") -> list[dict]:
    """Производные секундного ряда за день, по минутам."""
    async with async_session() as session:
        conds = [MicroMinute.ticker == ticker.upper(),
                 MicroMinute.ts.like(f"{day}%")]
        if source != "all":
            conds.append(MicroMinute.source == source)
        q = select(MicroMinute).where(*conds).order_by(MicroMinute.ts)
        rows = (await session.execute(q)).scalars().all()
    out = []
    for r in rows:
        traded = r.traded_at_best + r.traded_near + r.traded_deep
        out.append({
            "ts": r.ts, "ticker": r.ticker, "source": r.source,
            "session": r.session, "samples": r.samples,
            "imb_d10_max": r.imb_d10_max, "imb_d10_min": r.imb_d10_min,
            "imb_d30_max": r.imb_d30_max, "imb_d30_min": r.imb_d30_min,
            # Наибольший сдвиг перекоса за минуту, знак сохранён.
            "imb_swing_10s": (r.imb_d10_max if abs(r.imb_d10_max) >= abs(r.imb_d10_min)
                              else r.imb_d10_min),
            "bid_added": r.bid_added, "bid_removed": r.bid_removed,
            "ask_added": r.ask_added, "ask_removed": r.ask_removed,
            "bid_peak_add": r.bid_peak_add, "bid_peak_remove": r.bid_peak_remove,
            "ask_peak_add": r.ask_peak_add, "ask_peak_remove": r.ask_peak_remove,
            "traded_at_best": r.traded_at_best, "traded_near": r.traded_near,
            "traded_deep": r.traded_deep,
            "at_best_share": round(r.traded_at_best / traded, 4) if traded else None,
            "deep_share": round(r.traded_deep / traded, 4) if traded else None,
        })
    return out


async def prune_micro_minute(keep_days: int = 90) -> int:
    """Чистка производных тем же сроком, что поток, стакан и свечи."""
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=3)
              - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    async with async_session() as session:
        result = await session.execute(
            delete(MicroMinute).where(MicroMinute.ts < cutoff))
        await session.commit()
        return result.rowcount or 0


async def prune_level_minute(keep_days: int = 90) -> int:
    """
    Чистка истории уровней тем же сроком. Без неё таблица растёт весь срок жизни
    сервера — а именно на переполнении диска деплой уже вставал трижды.
    """
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=3)
              - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    async with async_session() as session:
        result = await session.execute(
            delete(LevelMinute).where(LevelMinute.ts < cutoff))
        await session.commit()
        return result.rowcount or 0


async def prune_candle_minute(keep_days: int = 90) -> int:
    """Чистка минутных свечей тем же сроком, что поток и стакан."""
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=3)
              - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    async with async_session() as session:
        result = await session.execute(
            delete(CandleMinute).where(CandleMinute.ts < cutoff))
        await session.commit()
        return result.rowcount or 0


async def prune_book_minute(keep_days: int = 90) -> int:
    """Чистка минутного стакана, тем же сроком, что и поток сделок."""
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=3)
              - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    async with async_session() as session:
        result = await session.execute(
            delete(BookMinute).where(BookMinute.ts < cutoff))
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


# ─── Журнал попыток (наблюдаемость воронки сигналов) ──────────────────────────

# Единый словарь кодов причин: почему попытка НЕ стала сигналом. Держим его
# здесь, чтобы агрегация по журналу и дашборд говорили на одном языке.
ATTEMPT_REASONS = {
    "saved":               "сигнал сохранён",
    "claude_flat":         "Claude сам сказал neutral (нет перевеса)",
    "claude_unavailable":  "Claude не ответил (баланс/ошибка/обрезка JSON)",
    "analysis_error":      "ошибка анализа до Claude",
    "no_plan":             "нет валидного плана (вход/стоп/цель)",
    "already_open":        "по тикеру уже открыт сигнал",
    "veto_window":         "вето: окно торговли (первые минуты / нет ORB)",
    "veto_chase":          "вето: чейз (вход далеко от уровня / растянут от VWAP)",
    "veto_confluence":     "вето: конфлюенс ниже порога",
    "veto_session_closed": "вето: рынок закрыт / пауза / пре-аукцион",
    "veto_session_end":    "вето: конец сессии (флэт к закрытию)",
    "veto_other":          "вето: прочее",
    "batch":               "batch-скрин (общая картина)",
    "batch_no_watch":      "batch: ни одного тикера в watch",
    "manual":              "ручной вызов карточки (вне воронки сканера)",
    "external":            "сценарий от внешнего аналитика",
    "budget":              "остановлено бюджет-гардом",
    # Risk Engine (src/risk/) — независимый контур, его вето приоритетнее Claude.
    # Отдельные коды нужны, чтобы было видно, КАКОЕ ограничение съело сделку:
    # «сигнал был хороший, но торговать было нельзя» — это другая история, чем
    # «сигнала не было».
    "risk_kill_switch":    "риск: kill switch — просадка от пика",
    "risk_daily_loss":     "риск: достигнут дневной лимит убытка",
    "risk_weekly_loss":    "риск: достигнут недельный лимит убытка",
    "risk_max_trades":     "риск: лимит числа сделок за день",
    "risk_max_positions":  "риск: лимит одновременных позиций",
    "risk_sector_limit":   "риск: лимит позиций в одном секторе",
    "risk_exposure_full":  "риск: суммарная экспозиция исчерпана",
    "risk_zero_size":      "риск: размер вышел нулевым (стоп далеко / лот велик)",
    "risk_no_levels":      "риск: нет входа или стопа — размер не считается",
    "risk_stop_wrong_side": "риск: стоп по неверную сторону от входа",
    "risk_spread_too_wide": "риск: стоп уже спреда — выбьет спредом, не движением",
    "risk_book_too_thin":  "риск: стакан не переварит даже минимальный размер",
    "risk_other":          "риск: движок отклонил или недоступен",
}


async def add_signal_attempt(data: dict) -> Optional[int]:
    """
    Записать ОДНУ попытку сигнала. Вызывается всегда — и когда сигнал сохранён,
    и когда отклонён (вето / neutral / Claude недоступен / занятый тикер).
    Никогда не роняет вызывающий код: журнал не должен ломать торговый цикл.
    """
    try:
        msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M МСК")
        row = SignalAttempt(
            msk=data.get("msk") or msk,
            stage=(data.get("stage") or "deep")[:12],
            ticker=(data.get("ticker") or None),
            phase=(data.get("phase") or None),
            verdict=(data.get("verdict") or None),
            final=(data.get("final") or None),
            saved=bool(data.get("saved")),
            prediction_id=data.get("prediction_id"),
            reason=(data.get("reason") or None),
            mode=(data.get("mode") or None),
            regime=(data.get("regime") or None),
            confluence=data.get("confluence"),
            confidence=data.get("confidence"),
            rr=data.get("rr"),
            entry=data.get("entry"),
            stop=data.get("stop"),
            target=data.get("target"),
            cost_rub=data.get("cost_rub"),
            tokens_in=data.get("tokens_in"),
            tokens_out=data.get("tokens_out"),
            note=(data.get("note") or None),
        )
        if row.note:
            row.note = str(row.note)[:500]
        async with async_session() as session:
            session.add(row)
            await session.commit()
            return row.id
    except Exception as e:
        logger.debug(f"add_signal_attempt: {e}")
        return None


async def recent_signal_attempts(hours: int = 24, limit: int = 200,
                                 ticker: Optional[str] = None) -> list[dict]:
    """Последние попытки (свежие сверху)."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with async_session() as session:
        q = select(SignalAttempt).where(SignalAttempt.ts >= since)
        if ticker:
            q = q.where(SignalAttempt.ticker == ticker.upper())
        q = q.order_by(SignalAttempt.ts.desc()).limit(limit)
        rows = (await session.execute(q)).scalars().all()
        return [r.to_dict() for r in rows]


async def signal_attempt_stats(hours: int = 24) -> dict:
    """
    Воронка сигналов за период: где именно всё отсекается и сколько это стоило.

    Возвращает:
      funnel   — попытки, дошедшие до Claude, сохранённые сигналы;
      reasons  — счётчик по кодам причин (с человеческой расшифровкой);
      cost     — ₽ по стадиям, ₽ за попытку и ₽ за сохранённый сигнал;
      by_hour  — попытки/сохранения по часу МСК (для оценки времени дня).
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with async_session() as session:
        rows = (await session.execute(
            select(SignalAttempt).where(SignalAttempt.ts >= since)
        )).scalars().all()

    reasons: dict[str, int] = {}
    cost_by_stage: dict[str, float] = {}
    by_hour: dict[str, dict] = {}
    deep = saved = 0
    tickers_seen: set[str] = set()
    for r in rows:
        reasons[r.reason or "?"] = reasons.get(r.reason or "?", 0) + 1
        cost_by_stage[r.stage or "?"] = round(
            cost_by_stage.get(r.stage or "?", 0.0) + (r.cost_rub or 0.0), 3)
        if r.stage == "deep":
            deep += 1
            if r.ticker:
                tickers_seen.add(r.ticker)
        if r.saved:
            saved += 1
        h = (r.ts + timedelta(hours=3)).strftime("%H") if r.ts else "??"
        b = by_hour.setdefault(h, {"attempts": 0, "saved": 0})
        b["attempts"] += 1
        if r.saved:
            b["saved"] += 1

    total_cost = round(sum(cost_by_stage.values()), 2)
    return {
        "hours": hours,
        "funnel": {
            "attempts_total": len(rows),
            "deep_analyses": deep,
            "unique_tickers": len(tickers_seen),
            "saved_signals": saved,
        },
        "reasons": [{"code": k, "count": v, "label": ATTEMPT_REASONS.get(k, k)}
                    for k, v in sorted(reasons.items(), key=lambda x: -x[1])],
        "cost": {
            "total_rub": total_cost,
            "by_stage": cost_by_stage,
            "per_attempt_rub": round(total_cost / len(rows), 2) if rows else None,
            "per_saved_signal_rub": round(total_cost / saved, 2) if saved else None,
        },
        "by_hour_msk": dict(sorted(by_hour.items())),
    }
