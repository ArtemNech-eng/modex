"""
Разовый патч: таблица market_minute — рыночный фон поминутно.

ЗАЧЕМ. Фон сейчас считается на лету в market_context.py и нигде не оседает.
Значит на вопрос «бумага росла сама или её тащил рынок» задним числом
ответить невозможно, а именно так и проверяются вердикты.

ИСТОЧНИК И ЗАДЕРЖКА. MOEX ISS, без токена. Замерено 06.08.2026 на живых
данных дважды: 22 секунды и 14 секунд отставания. Это быстрее, чем наш
собственный сброс потока в базу (до 20 секунд), то есть индекс не будет
отставать от котировок бумаг.

ПОЧЕМУ ВОЗРАСТ ХРАНИТСЯ В СТРОКЕ. Минута со свежим значением и минута,
куда лёгло застрявшее число, выглядят одинаково — обе заполнены. Через сутки
отличить их можно только по сохранённому возрасту.

МИГРАЦИЯ НЕ НУЖНА: таблица новая целиком, её создаст create_all. Миграция
нужна только для НОВЫХ КОЛОНОК в СУЩЕСТВУЮЩИХ таблицах.

Скрипт идемпотентен и удаляет себя после успешного применения.
"""
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "db.py"

MODEL = '''class MarketMinute(Base):
    """
    Рыночный фон поминутно: индекс и его возраст.

    Зачем хранить, если market_context.py считает всё на лету. Потому что
    «бумага росла против рынка» проверяется ЗАДНИМ ЧИСЛОМ, когда известен
    исход. Не сохранив фон, мы навсегда теряем возможность отличить собственное
    движение бумаги от общего подъёма.

    Источник — MOEX ISS, без токена. Замеренное отставание 06.08.2026:
    22 секунды и 14 секунд на двух пробах.

    age_sec хранится РЯДОМ Со значением и считается от метки БИРЖИ. Минута со
    свежим числом и минута с застрявшим выглядят одинаково — обе заполнены,
    и без возраста различить их через сутки невозможно.

    Таблица рассчитана не только на IMOEX: ключ включает имя, так что рядом
    лягут отраслевые индексы без изменения схемы.
    """
    __tablename__ = "market_minute"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)   # "ts:NAME"
    ts: Mapped[str] = mapped_column(String(16), index=True)          # МСК
    name: Mapped[str] = mapped_column(String(16), index=True)        # IMOEX
    source: Mapped[str] = mapped_column(String(8), default="iss")
    value: Mapped[float] = mapped_column(Float, default=0.0)
    change_pct: Mapped[float] = mapped_column(Float, default=0.0)
    open: Mapped[float] = mapped_column(Float, default=0.0)
    high: Mapped[float] = mapped_column(Float, default=0.0)
    low: Mapped[float] = mapped_column(Float, default=0.0)
    prev_close: Mapped[float] = mapped_column(Float, default=0.0)
    valtoday_rub: Mapped[float] = mapped_column(Float, default=0.0)
    #  Возраст ЗНАЧЕНИЯ по метке биржи в момент записи. -1 = неизвестен.
    age_sec: Mapped[int] = mapped_column(Integer, default=-1)
    exch_ts: Mapped[str] = mapped_column(String(24), default="")     # SYSTIME
    updates: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


'''

FUNC = '''async def merge_market_minutes(rows: list[dict], source: str = "iss") -> int:
    """
    Влить минутные значения индекса. Одна минута — одна строка на имя.

    В минуте опрос проходит несколько раз, и каждый следующий ответ СВЕЖЕЕ
    предыдущего. Поэтому значение ЗАМЕНЯЕТСЯ, а не усредняется: усреднённый
    индекс не равен ни одному реальному значению и сглаживает ровно те резкие
    движения, ради которых всё делается.

    СТАРОЕ ЗНАЧЕНИЕ НЕ ЗАТИРАЕТСЯ БОЛЕЕ СТАРЫМ. ISS может отдать
    повторно то же самое число с прежней меткой времени; если писать его поверх
    более свежего, минута постареет задним числом.

    Строка без ts или без значения пропускается целиком. Нулевой индекс — это
    не спокойный рынок, это отсутствие ответа, и в базе ему места нет.
    """
    if not rows:
        return 0
    n = 0
    try:
        async with async_session() as session:
            for r in rows:
                ts = (r or {}).get("ts")
                name = ((r or {}).get("name") or "IMOEX").upper()
                try:
                    value = float(r.get("value") or 0.0)
                except (TypeError, ValueError):
                    continue
                if not ts or value <= 0:
                    continue
                key = f"{ts}:{name}"
                row = await session.get(MarketMinute, key)
                if row is None:
                    row = MarketMinute(key=key, ts=ts, name=name, updates=0)
                    session.add(row)
                else:
                    #  Пришёл повтор старого значения — оставляем то, что свежее.
                    old = row.exch_ts or ""
                    new = str(r.get("exch_ts") or r.get("ts_exchange") or "")
                    if old and new and new < old:
                        continue
                row.source = source
                row.value = value
                for fld, key_in in (("change_pct", "change_pct"),
                                    ("open", "open"), ("high", "high"),
                                    ("low", "low"), ("prev_close", "prev_close"),
                                    ("valtoday_rub", "valtoday_rub")):
                    try:
                        v = r.get(key_in)
                        if v is not None:
                            setattr(row, fld, float(v))
                    except (TypeError, ValueError):
                        pass
                age = r.get("age_sec")
                row.age_sec = int(age) if isinstance(age, (int, float)) else -1
                row.exch_ts = str(r.get("exch_ts") or r.get("ts_exchange") or "")
                row.updates += 1
                row.updated_at = datetime.now(timezone.utc)
                n += 1
            await session.commit()
    except Exception as e:                                       # noqa: BLE001
        logger.debug(f"merge_market_minutes: {e}")
        return 0
    return n


async def market_series(name: str, day: str) -> list[dict]:
    """Ряд индекса за день. Возраст едет вместе со значением, а не теряется."""
    nm = (name or "IMOEX").upper()
    async with async_session() as session:
        res = await session.execute(
            select(MarketMinute)
            .where(MarketMinute.name == nm)
            .where(MarketMinute.ts.like(f"{day}%"))
            .order_by(MarketMinute.ts))
        rows = res.scalars().all()
    return [{"ts": r.ts, "name": r.name, "value": r.value,
             "change_pct": r.change_pct, "open": r.open, "high": r.high,
             "low": r.low, "prev_close": r.prev_close,
             "valtoday_rub": r.valtoday_rub, "age_sec": r.age_sec,
             "exch_ts": r.exch_ts, "updates": r.updates,
             "source": r.source} for r in rows]


'''

EDITS = [
    ("модель MarketMinute", "class MicroMinute(Base):", MODEL + "class MicroMinute(Base):"),
    ("запись и чтение фона",
     'def aggregate_candles(rows: list[dict], step: int, ticker: str = "") -> list[dict]:',
     FUNC + 'def aggregate_candles(rows: list[dict], step: int, ticker: str = "") -> list[dict]:'),
]


def main() -> int:
    if not TARGET.exists():
        print(f"НЕТ ФАЙЛА: {TARGET}")
        return 1
    text = TARGET.read_text(encoding="utf-8")
    done = skip = fail = 0
    for name, old, new in EDITS:
        if new in text:
            print(f"market: {name}: уже было")
            skip += 1
            continue
        cnt = text.count(old)
        if cnt != 1:
            print(f"market: {name}: ОТКАЗ, якорь встретился {cnt} раз вместо 1")
            fail += 1
            continue
        text = text.replace(old, new, 1)
        print(f"market: {name}: применено")
        done += 1

    if fail:
        print(f"итог: применено {done}, уже было {skip}, отказов {fail}")
        print("файл НЕ тронут: частичный патч базы хуже, чем никакого")
        return 1

    if done:
        TARGET.write_text(text, encoding="utf-8")

    print(f"итог: применено {done}, уже было {skip}, отказов {fail}")
    try:
        os.remove(__file__)
        print("скрипт удалил себя")
    except OSError as e:
        print(f"себя не удалил: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
