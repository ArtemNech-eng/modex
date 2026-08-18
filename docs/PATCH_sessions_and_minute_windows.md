# Патч: метка сессии в потоке и окна для минутных свечей

Эта ветка меняет только чистые модули (`src/analysis/sessions.py`,
`src/analysis/day_slice.py`, тесты). Два файла правятся руками: `src/collector/stream.py`
и `src/api/main.py` весят по 50–175 КБ, и вносить в них правку вслепую нельзя.
Ниже точные замены — обе короткие.

## 1. Метка сессии в потоке — `src/collector/stream.py`

Было:

```python
def session_of(mk: str) -> str:
    """
    Утро / основная / вечер. Границы те же, что в minute_buckets — если они
    разойдутся, одна и та же минута получит разные метки в двух таблицах.
    """
    m = int(mk[11:13]) * 60 + int(mk[14:16])
    if m < 9 * 60 + 50:
        return "morning"
    if m < 19 * 60:
        return "main"
    return "evening"
```

Что с этим не так. Метка попадает В БАЗУ (`candle_minute.session`,
`flow_minute.session`, `book_minute.session`) и уезжает наружу в `/api/candles`.
При таких границах:

* ночь до 06:50 записывается как **утренняя сессия**;
* аукцион открытия 09:50–09:59 записывается как **основная сессия**;
* перерыв 18:50–18:59 записывается как **основная сессия**;
* после 23:50 всё записывается как **вечерняя сессия**;
* выходные не отличаются от будней, хотя Tinkoff в субботу отдаёт дилерские
  сделки при закрытой бирже (15.08.2026 по SBER так набралось 278 488 акций).

Стало:

```python
from src.analysis.sessions import phase_of_ts


def session_of(mk: str) -> str:
    """
    Фаза дня для метки минуты: morning | pre | main | break | evening | closed.

    Границы берутся из src/analysis/sessions.py — одного источника на весь
    проект. Раньше они были зашиты здесь и расходились с config/settings.py
    и с day_slice, поэтому одна и та же минута получала разные метки в
    разных таблицах.
    """
    return phase_of_ts(mk)
```

День недели здесь сознательно не передаётся: в `mk` только время, а дата есть
— если добавлять проверку выходных, то через
`datetime.strptime(mk[:10], "%Y-%m-%d").weekday()`, и тогда субботние дилерские
сделки получат метку `closed` вместо `main`. Это отдельное решение: строки в
базе останутся, но перестанут попадать в статистику по времени суток.

**Колонка узкая.** `session: Mapped[str] = mapped_column(String(8), ...)` —
самое длинное новое значение `evening` (7 символов), `closed` и `break`
короче. Расширять не нужно.

**Старые строки не переписываются.** В базе останутся минуты с прежними
метками. Перекладывать их можно одним UPDATE, но проще помнить: метка
достоверна с даты деплоя, а до неё сессию надо считать по времени в `ts`.

## 2. Окна для минутных свечей — `src/api/main.py`

Было: `/api/candles/{ticker}` принимает только `day` и `res`, поэтому
`res=1m` отдаёт весь день целиком — 436 строк на 18.08, и потребитель с
ограничением на размер ответа получает обрезанный JSON (обрыв на 08:20).
Единственное разрешение, доходящее полностью, — `res=15m`.

Стало (заменить тело `get_stream_candles`):

```python
@app.get("/api/candles/{ticker}", summary="Минутные бары из потока (OHLC)")
async def get_stream_candles(ticker: str, day: Optional[str] = None,
                             res: str = "1m", limit: Optional[int] = None,
                             offset: int = 0,
                             from_time: Optional[str] = None,
                             to_time: Optional[str] = None):
    """
    OHLC каждой минуты из ПОТОКА, без задержки.

    res: 1m | 5m | 15m | 30m | session

    ОКНО. Минутный день это 400+ строк, и целиком он не всегда доезжает до
    потребителя. Поэтому ряд можно резать:

      from_time, to_time — "HH:MM" МСК включительно (окно сессии);
      limit              — сколько баров отдать;
      offset             — с какого бара, ОТРИЦАТЕЛЬНЫЙ = с конца
                           (offset=-60 с limit=60 это последний час).

    total всегда показывает длину ПОЛНОГО ряда до нарезки: иначе окно
    невозможно отличить от конца данных.
    """
    if not ticker_known(ticker):
        raise HTTPException(status_code=404, detail=f"Тикер {ticker} не найден")
    if res not in ("1m", "5m", "15m", "30m", "session"):
        raise HTTPException(status_code=400,
                            detail="res должен быть 1m, 5m, 15m, 30m или session")
    d = day or (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    rows = await db.candle_series(ticker, d, res)
    total = len(rows)
    if from_time:
        rows = [r for r in rows if str(r.get("ts", ""))[11:16] >= from_time]
    if to_time:
        rows = [r for r in rows if str(r.get("ts", ""))[11:16] <= to_time]
    if offset:
        rows = rows[offset:] if offset < 0 else rows[offset:]
    if limit is not None and limit > 0:
        rows = rows[:limit]
    return {"ticker": ticker.upper(), "day": d, "res": res,
            "total": total, "count": len(rows),
            "window": {"from": from_time, "to": to_time,
                       "limit": limit, "offset": offset},
            "rows": rows}
```

Нарезка сознательно сделана ПОСЛЕ чтения из базы, а не в SQL: `candle_series`
склеивает минуты в 5m/15m/30m, и резать до склейки значит получить неполный
крайний бар.

Примеры после правки:

```
/api/candles/MAGN?res=1m&from_time=07:00&to_time=07:30   диапазон открытия утра
/api/candles/MAGN?res=1m&limit=60&offset=-60             последний час поминутно
/api/candles/MAGN?res=1m&from_time=19:00                 вечерняя сессия
```

## 3. Что стоит проверить после деплоя

1. `/api/candles/{T}?res=1m&from_time=09:45&to_time=10:05` — метки должны идти
   `morning` до 09:49, `pre` с 09:50 по 09:59, `main` с 10:00.
2. `/api/candles/{T}?res=1m&from_time=19:00&limit=5` — метка `evening`.
3. Полнота дня в срезе: основная сессия считается от 530 минут, утро от 170,
   вечер от 290.
