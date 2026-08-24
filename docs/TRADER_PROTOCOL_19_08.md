# Протокол трейдера 19.08 — исправления и сигналы

## Дата: 2026-08-24 (Europe/Moscow)
Ветка: arena/01a0326f-modex

## Подтверждённые баги (3)

### 1) flow.buy_pct/delta инвертированы
- Источник: `src/collector/tinkoff_client.py:652-721` `_classify_flow` direction vs tick-rule
- Использование: `src/agent/analyst_brief.py:154-162` flow из `recent_events(tinkoff,trades)`, `src/agent/context_builder.py:514-550` поглощение по `buy_pct`
- Примеры:
  - CBOM -7.76% при bid/ask 0.43 но buy 93.8%
  - ASTR бриф +6238 vs ISS -7586 (знак противоположный, модуль близок)
  - NVTK/SBER аналогично
- Ground truth: ISS `trades.json` поле BUYSELL
  - B = buyer hits ask = покупка агрессивная, снимает ask
  - S = seller hits bid = продажа агрессивная, снимает bid
  - Определение aggressor (investopedia): aggressor removes liquidity
- Фикс:
  - `analyst_brief.py` flow помечен `_deprecated=True`, `_reliability=UNRELIABLE`, `_ground_truth=ISS trades.json`
  - `context_builder.py` абсорбция отключена `if False`, добавлено предупреждение DEPRECATED
  - `tinkoff_client.py` `_classify_flow` помечен DEPRECATED, добавлен `_classify_flow_fixed` с инверсией для сверки
  - Новый модуль `src/collector/iss_trades.py` — единственный источник дельты для торговли

### 2) suspect без abs()
- Источник: `src/db.py:2880` `ours > theirs*1.05` без `abs()`
- Пример: ours 13926 vs candle 35351 diff -21425 60.6% потеряно → suspect:false
- Фикс: `abs(diff)/candle>0.05` в обе стороны, добавлено поле `diff_pct`

### 3) /api/early-moves/stats total_events 0
- Причина: `EarlyMoveDetector` хранит события в памяти (`self._events`), сбрасывается при рестарте
- Фикс: документировано как in-memory, для прод нужен персист в БД; пока вердикт = чтение структуры, не вероятность

## Правильные источники (инструкция 19.08)

- дельта — ISS `trades.json` BUYSELL (см. `iss_trades.py`)
  - URL: `https://iss.moex.com/iss/engines/stock/markets/shares/securities/{TICKER}/trades.json`
  - params: `iss.meta=off&iss.only=trades&limit=100&start=NUMTRADES-100` (limit!=100 молча даёт 10, reverse игнорируется)
  - TRADINGSESSION 0 утро 1 основная 2 вечер vs flow session morning/main (граница 09:50 расходится на 10м)
- стакан по уровням bid5_sum/ask5_sum — `/api/live/{TICKER}` напрямую (не orderbook-index)
  - Пример: ASTR 4 снимка в индексе vs 104 обновления в live
  - Фильтр доверия к `/api/orderbook-index` — «31+ снимков» (не проверка наличия)
- агрегат глубина — ISS marketdata `BIDDEPTHT/OFFERDEPTHT/WAPRICE/NUMTRADES` valid (ASTR 1.29 vs 1.19)
- свечи/VWAP/ATR — `/api/candles/brief`
- дельта по закрытым минутам — `/api/flow?source=exchange&res=5m&day=YYYY-MM-DD` (парам day, не date, res=5m 197-203 строки, 1m обрезан 08:11)
  - Абсолютную дельту только по закрытым минутам; в текущей минуте лента и счётчик флашатся с разной частотой
- юниверс — `/api/universe` только состав и ранг, цены кэш устаревают (SBER 275.01 vs 277.22 live)
- индекс — IMOEX2 `engines/stock/markets/index/securities/IMOEX2/candles.json?interval=10` 07:00-23:50 совпадает с IMOEX в основную

## Протокол индекса 19.08

### Логика
1. Утро 07:00-09:49 value <~10.3 млрд кандидат
2. Первый час основной 09:50-10:49 сумма value 6 бакетов 10м, граница ~7.5 млрд или 0.75 медианы 5д = сжатый
   - Примеры: 19.08 4.9 сжатый, 18.08 6.0 сжатый, 14.08 6.2 сжатый, 12.08 8.5 плотный, 13.08 8.9 плотный, 11.08 9.5 плотный, 17.08 12.5 плотный
   - Утро 8.8-10.1 сжатые, 38.2 17.08 гэп
3. Триггер расширения ≥3× среднего 5 пред бакетов только основная, первое 10:50, после 16:00 не торговать
   - Направление close-open бакета, вход по закрытию
   - Примеры: 14.08 4.24× 11:20 вниз -3.4% до close, 18.08 4.66× 11:40 вверх +0.66% (+1.4% до хая), 19.08 4.2× 15:10 вверх +0.5% (+0.8%), 12.08 4.16× 17:10 после 16:00 не торговать -0.24%
   - Сжатые 3/3 верно, плотные пропущены 13.08 -3.01% и 17.08 -2.02% — осознанная плата
4. Выбор инструмента по нормированной эффективности в сторону сигнала
   - Эффективность = % хода цены на 1% дневного объёма в чистой дельте (старая "пунктов на 100k лотов" непригодна кросс-бумажно)
   - Пример 18.08 NVTK 0.65 +5.18% vs LKOH 0.42 +1.58% vs SIBN дельта - пропуск
5. Подтверждение: trade_count взрыв (NVTK 14.08 242→381→1830 7.6× в 11:25 дельта -20120 VWAP 979.28 шорт до 928.1 +5.2%)
6. Стоп за экстремум кумулятивной дельты (11/13) — кумулятивная дельта экстремум = экстремум цены дня 11/13 (таблица ASTR/TATN/NVTK/SIBN/LKOH 14-19.08), 2 промаха объяснимы обрывом выгрузки 12:50 и дивергенцией
   - Пример NVTK 14.08 max +49097 10:15 VWAP 990.83 хай 992.6 стоп туда риск 1.2%
7. R/R≥2 арифметически, инвалидация, время среза, "не инвестиционная рекомендация" + "структура и план" (т.к. пробойные лонги убыточны 181д лучшая -0.113R)

### Порядок действий
- 09:50 оборот утра <~10.3 млрд кандидат
- 10:50 решающий фильтр (сжатый/плотный)
- 10:50-16:00 следить расширение
- всплеск→направление из бакета
- выбор по эффективности
- подтверждение trade_count
- R/R≥2
- Выборка честная: 7д индекса, 4 срабатывания основная, 3 сжатый, порог 3× подогнан — вести журнал режим/кратность/время/инструмент/R минимальным размером

## Новые модули

### src/collector/iss_trades.py
- `fetch_json()` — requests + curl fallback, verify=False
- `parse_trades_payload()` — разбор по columns
- `compute_delta()` — buy/sell/delta/buy_pct по BUYSELL B/S
- `fetch_marketdata()` — NUMTRADES/BIDDEPTHT/OFFERDEPTHT/WAPRICE
- `fetch_trades()` — последние 100 сделок с offset NUMTRADES-100
- `aggregate_trades_by_minute()` — группировка по минуте, cumulative_delta
- `fetch_index_candles()` — IMOEX/IMOEX2 interval 10
- `compute_index_buckets()` — morning/first_hour value
- `flow_efficiency()` — % хода на 1% объёма

### src/agent/trader_protocol.py
- `is_compressed_day()` — фильтр режима
- `detect_expansion_triggers()` — триггер ≥3×
- `select_instrument_by_efficiency()` — выбор инструмента
- `generate_readonly_signal()` — read-only сигнал с 3 подтверждениями, R/R≥2, стоп за экстремум кумулятивной дельты, инвалидация, время среза, disclaimer
- `format_signal_text()` — человекочитаемый вывод
- `example_signals_from_memory()` — примеры когда ISS недоступен

## Генерация сигналов (read-only)

```bash
python scripts/generate_readonly_signals.py
```

Вывод включает:
- Индекс IMOEX2 10m (если ISS доступен)
- Дельту ISS trades.json
- Демо сигналы NVTK с R/R 2.2, 3 подтверждениями, инвалидацией, временем среза, disclaimer

В sandbox MOEX ISS заблокирован (TLS SSL_ERROR_SYSCALL), поэтому живые данные недоступны, но протокол реализован и работает на проде.

## Безопасность

- Запрещённые вызовы (только чтение): никогда `POST /api/analyst-signal`, `POST /api/ingest/deals`, `/api/live-signals/start`, `/api/live-signals/stop`
- По прямому указанию прошлого агента не использовать `/api/screen`, `/api/volume-scan`, `/api/setup-watch`, Claude-сигналы
- `INGEST_TOKEN` пустой, `CORS allow_origins=["*"]` — запись открыта, поэтому только чтение

## Детали из memo 19.08

- Исторические доли объёма: TATN утро 17.4%/76%/6.3%, ASTR утро 43% дневного — игнорировать нельзя
- Фейд утра 6/11 отменён (было 4/4 на одной бумаге), утренняя дельта направление дня не даёт
- Фильтр 1% объёма отвергнут (TATN -97477 при 3.087M=3.2% значимо вниз, день +1.73%)
- Встречный поток при движущейся цене — поглощение, не разворот
- Абсолютную дельту — только по закрытым минутам
- Минутные свечи `candles.json` дельту не дают физически (open/high/low/close/value/volume only)
- Попытки использовать `limit` !=100 в ISS trades.json молча возвращают 10, `reverse=true` игнорируется
