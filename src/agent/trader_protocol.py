"""
Trader Protocol 19.08 + 21.08 — read-only сигналы без записи
Обновление индексного протокола 21.08.2026 13:50 MSK на выборке 14 дней — заменяет раздел «Индексный протокол» и «Триггер старта» из 19.08.

19.08:
  - Только чтение, без POST /api/analyst-signal, /api/ingest/deals, /api/live-signals/start/stop
  - Не использовать /api/screen, /api/volume-scan, /api/setup-watch, Claude-сигналы
  - Анализ на сырых данных: дельта ISS trades.json BUYSELL B/S ground truth, стакан /api/live/{TICKER}, агрегат ISS marketdata, свечи/VWAP/ATR /api/candles/brief, юниверс /api/universe
  - 3 подтверждения книга/лента/VWAP — УСТАРЕЛО 21.08

21.08 КТО ДВИГАЕТ ЦЕНУ (главный принцип):
  ⚖️ Лимитная книга не доказательство. Заявку отменяют бесплатно именно тогда, когда на неё начали ориентироваться.
  Доказательство только исполненные сделки: дельта агрессора ISS BUYSELL, LAST vs WAPRICE, VOLTODAY/NUMTRADES, TRADETIME/PRICE.
  Отменено: bid5_sum/ask5_sum, bid_top_max/ask_top_max, BIDDEPTHT/OFFERDEPTHT, /api/orderbook-index (TATN 37.0 PLZL 37.3), imb_min/imb_max — только для оценки спреда/шага.
  Порядок дельты: 1) NUMTRADES marketdata 2) start=NUMTRADES-100 3) trades.json?start=START&limit=100&iss.only=trades&fast_mode=false
  Три вопроса Q1 перевес <15% шум, Q2 двигает ли поток цену асимметрия, Q3 принты нисходящие/айсберг/гигант/исчерпание.
  Стоп за провалившийся экстремум, отмена в сделках >500 + 300+ перестают двигать, R/R≥2 с комиссией 0.05%.

21.08 ИНДЕКСНЫЙ ПРОТОКОЛ — обновление на 14 днях (3–7.08, 11–14.08, 17–21.08) срез 13:50 MSK, IMOEX2 10м ISS:
  Главное: порог 3× выжил как детектор старта движения и провалился как источник прибыли — вся положительная сумма выборки сидит в одном дне из четырнадцати.

  Статистика сжатых дней: было 3/3, стало 4 победы /2 убытка /1 ноль
    14.08 4.24× 11:20 вниз +3.4%
    18.08 4.66× 11:40 вверх +0.66% макс +1.4%
    19.08 4.2× 15:10 вверх +0.5%
    20.08 3.62× 13:00 вниз +1.56% макс +1.93%
    06.08 4.51× 11:50 вниз -0.03% макс +0.78%
    04.08 3.20× 14:50 вниз -1.15% макс +0.12%
    07.08 3.98× 10:40 вверх -1.21% макс +0.05%
    Сумма +3.73% на 7 сделок. Без 14.08: +2.72% vs -2.39% = +0.33% на 6 сделок = ноль до издержек и минус после. Точность 57% монета.

  Утренний оборот как предварительный ранг — ОТМЕНЁН:
    04.08 утро 15.1 млрд по старому «предупреждение сигнал не работает», первый час 6.50 сжатый триггер сработал.
    06.08 утро 17.2 млрд второе по величине за 14д после 17.08, первый час 7.24 формально сжатый триггер 4.51×.
    Связи между утренним оборотом и режимом первого часа нет. В 09:50 режим предсказать нельзя. Границу ~10.3 млрд и всю утреннюю колонку убрать; решающий и единственный фильтр — оборот 09:50–10:49 снимается в 10:50.

  Исправлена арифметическая ошибка: первое возможное срабатывание 10:40 а не 10:50
    База триггера 5 бакетов основной сессии. Основная начинается 09:50 → 5 полных бакетов уже к 10:40.
    Это решает исход дня 07.08: бакет 10:40 2343 млн против базы 589 млн =3.98× вверх вход 2309.30 дальше макс +0.05% закрытие 2281.37 убыток -1.21%. По конвенции «не раньше 10:50» этого входа нет (макс дня 2.18×).
    Решение: фиксируем 10:40 как первое арифметически возможное. Если фиксируем 10:50 статистика 4/1/1 net +1.54% но весь плюс из 14.08.

  Что выдержало:
    Фильтр режима работает как фильтр молчания. На плотном фоне триггер не сработал ни разу: 03.08 макс 1.95×, 05.08 макс 1.99× добавились к 11.08,13.08,17.08 — 5 плотных дней 0 срабатываний. Цена полноты: пропущено 4 трендовых дня 03.08 +1.5% и 05.08 +1.27%.
    Порог 7.5 млрд по первому часу самое хрупкое место. 3 дня в коридоре ±0.6 млрд: 06.08 7.24, 20.08 7.44, 03.08 8.06. От классификации зависит торгуем ли вообще: 20.08 в коридоре дал лучшую сделку +1.56%, 06.08 ноль. Граница подобрана на глаз и не проверена.
    Относительный критерий 0.75 медианы первого часа за 5 дней ест сам себя — медиана считается по выборке где большинство сжатые. Пользоваться абсолютной шкалой и помнить произвольность.

  Отвергнутая гипотеза: размах бакета-триггера как предиктор следования — не работает (04.08 0.54% убыток, 07.08 0.66% убыток, 20.08 0.81% победа, 06.08 1.06% ноль).

  Полная таблица 14 дней:
    03.08 утро 14.0 1й час 8.06 плотный макс 1.95× нет пропущен +1.5%
    04.08 утро 15.1 1й час 6.50 сжатый макс 3.20× 14:50 вниз -1.15%
    05.08 утро 14.4 1й час 11.28 плотный макс 1.99× нет пропущен +1.27%
    06.08 утро 17.2 1й час 7.24 сжатый граница макс 4.51× 11:50 вниз -0.03%
    07.08 утро 8.72 1й час 5.29 сжатый макс 3.98× 10:40 вверх -1.21%
    11.08 утро 11.8 1й час 9.5 плотный макс 2.4× нет верно
    12.08 утро 13.2 1й час 8.5 плотный макс 4.16× в 17:10 отсечено временем
    13.08 утро 10.6 1й час 8.9 плотный макс 1.9× нет пропущен -3.01%
    14.08 утро 9.7 1й час 6.2 сжатый макс 4.24× 11:20 вниз +3.4%
    17.08 утро 38.2 1й час 12.5 плотный макс 1.6× нет пропущен -2.02%
    18.08 утро 8.8 1й час 6.0 сжатый макс 4.66× 11:40 вверх +0.66%
    19.08 утро 10.1 1й час 4.9 сжатый макс 4.2× 15:10 вверх +0.5%
    20.08 утро 7.89 1й час 7.44 сжатый граница макс 3.62× 13:00 вниз +1.56%
    21.08 утро 6.38 1й час 6.12 сжатый макс 2.62× на 13:40 нет

  Действующий порядок у терминала (с правками 21.08):
    1. 10:50 снять оборот первого часа 09:50–10:49 IMOEX2. Ниже ~7.5 млрд = сжатый работаем. Выше = молчим весь день. Утренний оборот не смотреть.
    2. С 10:40 до 16:00 следить 10м бакеты IMOEX2 value ≥3× среднего 5 пред бакетов основной сессии. База не должна захватывать утреннюю.
    3. Направление знак close-open бакета-триггера. Вход по его закрытию. После 16:00 не торговать.
    4. Инструмент нормированная эффективность % хода на 1% дневного объёма в чистой дельте в сторону сигнала, только бумаги с дельтой того же знака. Сортировщик не предиктор.
    5. Подтверждение взрыв trade_count по бумаге, не величина дельты. Стоп за экстремум кум дельты.
    6. R/R≥2 арифметически иначе входа нет. На такой статистике единственное что удерживает серию от отрицательного МО.
    7. Размер минимальный. Журнал: режим, кратность, время, инструмент, результат в R.

  Технические находки:
    - Веб-загрузчик ISS только при fast_mode:false. При true Unable to load / Content not available. Воспроизведено на candles.json и marketdata.
    - ISS кэширует ответ ~10 мин: повтор через 3 мин байт-в-байт тот же срез. Чаще раза в 10 мин не дёргать.
    - /api/flow res=5m обрывается около 12:45–12:50 isTruncated true totalLineCount 1. Последний час дельты через /api/flow недоступен в принципе, свежую брать только ISS trades.json или /api/live.
    - Выгрузка одного дня индекса: iss/engines/stock/markets/index/securities/IMOEX2/candles.json?from=ДАТА&till=ДАТА&interval=10&iss.only=candles&iss.meta=off — один день целиком без обрезки.

  Живой кейс поглощения SIBN 21.08 13:42:
    orderbook_index 85.7 покупатели доминируют 31 снимок фильтр пройден
    bid5_sum 24641 vs ask5_sum 4528 =5.4:1 в пользу бида
    cumulative_delta -199443 VWAP 12:20 476.01 vs цены 470.4 текущая минута volume_buy 11 / volume_sell 337
    Книга и лента расходятся → поглощение не разворот не бычий. Лонг прямая ошибка. Подтверждает: книга не доказательство.

  Открытые вопросы: зафиксировать 10:40 vs 10:50, проверить 3× и 7.5 млрд на 30+ днях июль и ранее не подгоняя, перепроверить «нет входов после 22:00» источник неизвестен, измеренных вероятностей нет /api/early-moves/stats пустой total_events 0, пробойные лонги убыточны 181д лучшая -0.113R.

Этот модуль НЕ делает POST, НЕ вызывает запрещённые эндпоинты, только читает и считает.
"""

import logging
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Пороги — обновление 21.08 на 14 днях
# Утренний фильтр ОТМЕНЁН 21.08: связи утро vs первый час нет (04.08 утро 15.1 млрд сжатый, 06.08 утро 17.2 млрд сжатый)
MORNING_VALUE_THRESHOLD = 10.3e9  # DEPRECATED 21.08 — не смотреть, оставлен для совместимости
FIRST_HOUR_THRESHOLD = 7.5e9  # абсолютная шкала, хрупкое место ±0.6 млрд (06.08 7.24, 20.08 7.44, 03.08 8.06), 0.75 медианы ест сам себя — не использовать
EXPANSION_MULTIPLIER = 3.0  # выжил как детектор старта, провалился как источник прибыли: вся + сумма в 1 дне из 14
NO_TRADE_AFTER = 16 * 60  # 16:00 МСК
FIRST_TRIGGER_TIME = 10 * 60 + 40  # 10:40 — исправлено 21.08, было 10:50 арифметическая ошибка: 09:50+5 бакетов=10:40. Конвенция 10:50 даёт 4/1/1 net +1.54% но весь плюс из 14.08

# Комиссия для R/R 21.08
COMMISSION_PCT = 0.05


def msk_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=3)


def parse_hhmm(s: str) -> int:
    try:
        h, m = map(int, s.split(":")[:2])
        return h * 60 + m
    except Exception:
        return 0


def is_compressed_day(first_hour_value: float, median_5d: Optional[float] = None) -> bool:
    """
    Фильтр режима 21.08: только первый час 09:50–10:49 IMOEX2.
    Ниже ~7.5 млрд = сжатый, работаем. Выше = молчим весь день.
    Утренний оборот не смотреть — ОТМЕНЁН 21.08.
    Относительный критерий 0.75 медианы ест сам себя — не использовать, пользоваться абсолютной шкалой.
    Порог хрупкий ±0.6 млрд: 06.08 7.24, 20.08 7.44 (лучшая сделка +1.56%), 03.08 8.06.
    """
    # median_5d игнорируем сознательно 21.08 — относительный критерий ест сам себя
    if median_5d is not None:
        logger.debug("median_5d DEPRECATED 21.08 — относительный критерий ест сам себя, используем абсолютный 7.5 млрд")
    return first_hour_value < FIRST_HOUR_THRESHOLD


def detect_expansion_triggers(buckets: List[dict]) -> List[dict]:
    """
    Триггер старта 21.08: value ≥3× среднего 5 пред бакетов основной сессии, с 10:40 до 16:00.
    База не должна захватывать утреннюю сессию — фильтруем prev только 09:50+.
    Направление close-open бакета, вход по закрытию.
    """
    triggers = []

    def _minutes(b):
        try:
            t = b.get("begin") or ""
            if " " in t:
                time_part = t.split(" ", 1)[1]
                h, m = map(int, time_part.split(":")[:2])
                return h * 60 + m
        except Exception:
            pass
        return 0

    buckets_sorted = sorted(buckets, key=_minutes)

    for i, b in enumerate(buckets_sorted):
        mins = _minutes(b)
        if mins < FIRST_TRIGGER_TIME:  # 10:40 исправлено 21.08
            continue
        if mins >= NO_TRADE_AFTER:
            continue
        if not (10 * 60 <= mins <= 18 * 60 + 50):
            continue
        if i < 5:
            continue
        prev = buckets_sorted[i - 5:i]
        # База не должна захватывать утреннюю — фильтруем только основная 09:50+
        prev_main = [x for x in prev if _minutes(x) >= 9 * 60 + 50]
        if len(prev_main) < 5:
            # если в базе есть утро — пропускаем, чтобы не подмешивать утренний объём
            continue
        try:
            avg_prev = sum(float(x.get("value") or 0) for x in prev_main) / 5
            cur_val = float(b.get("value") or 0)
            if avg_prev <= 0:
                continue
            mult = cur_val / avg_prev
        except Exception:
            continue
        if mult >= EXPANSION_MULTIPLIER:
            try:
                open_p = float(b.get("open") or 0)
                close_p = float(b.get("close") or 0)
                if open_p <= 0 or close_p <= 0:
                    continue
                direction = "up" if close_p > open_p else "down" if close_p < open_p else None
                if direction is None:
                    continue
                triggers.append({
                    "begin": b.get("begin"),
                    "minutes": mins,
                    "time_msk": f"{mins // 60:02d}:{mins % 60:02d}",
                    "value": cur_val,
                    "avg_prev_5": avg_prev,
                    "multiplier": round(mult, 2),
                    "direction": direction,
                    "open": open_p,
                    "close": close_p,
                    "entry": close_p,
                    "change_pct": round((close_p - open_p) / open_p * 100, 3),
                })
            except Exception as e:
                logger.debug(f"trigger parse error: {e}")
                continue
    return triggers


def compute_flow_efficiency(ticker: str, price_move_pct: float, delta_lots: int, day_volume_lots: int) -> Optional[float]:
    if day_volume_lots <= 0 or delta_lots == 0:
        return None
    delta_pct = abs(delta_lots) / day_volume_lots * 100
    if delta_pct == 0:
        return None
    return round(price_move_pct / delta_pct, 4)


def select_instrument_by_efficiency(candidates: List[dict], signal_direction: str) -> Optional[dict]:
    filtered = []
    for c in candidates:
        d = c.get("delta")
        if d is None:
            continue
        if signal_direction == "up" and d <= 0:
            continue
        if signal_direction == "down" and d >= 0:
            continue
        if c.get("efficiency") is None:
            continue
        filtered.append(c)
    if not filtered:
        return None
    filtered.sort(key=lambda x: x.get("efficiency") or 0, reverse=True)
    return filtered[0]


def build_signal_structure_21_08(
    ticker: str,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    three_q: dict,
    stop_info: dict,
    trigger_info: dict,
    vwap_info: dict,
    time_slice: str,
) -> dict:
    """
    Структура сигнала по методу 21.08 — только исполненные сделки как доказательство.
    Книга НЕ является доказательством, только для оценки стоимости исполнения (спред/шаг).
    Индексный протокол обновлён на 14 днях: 4/2/1 +3.73% но без 14.08 +0.33% ноль, порог 7.5 хрупкий, 10:40 первое.
    """
    try:
        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = round(reward / risk, 2) if risk > 0 else None
    except Exception:
        rr = None

    rr_ok = rr is not None and rr >= 2.0

    q1 = three_q.get("q1_imbalance") or {}
    q2 = three_q.get("q2_moves_price") or {}
    q3 = three_q.get("q3_print_levels") or {}

    proofs = {
        "delta_aggressor": f"ISS trades.json BUYSELL B/S: buy {q1.get('buy_volume')} sell {q1.get('sell_volume')} delta {q1.get('delta')} перевес {q1.get('imbalance_pct')}% {'шум <15%' if q1.get('is_noise') else 'значимо'}",
        "vwap": vwap_info.get("text") if vwap_info else "LAST vs WAPRICE — из исполненных сделок",
        "turnover": f"VOLTODAY/VALTODAY/NUMTRADES факт: sample {three_q.get('sample',{}).get('count')} сделок {three_q.get('sample',{}).get('time_from')}→{three_q.get('sample',{}).get('time_to')}",
        "prints": f"TRADETIME/PRICE: {q3.get('price_sequence')} qty {q3.get('qty_sequence')} descending_sales={q3.get('descending_sales')} iceberg={q3.get('iceberg_clips')} exhaustion={q3.get('exhaustion')}",
    }

    not_proof = [
        "bid5_sum/ask5_sum и плиты из /api/live — не доказательство (отменяют бесплатно)",
        "bid_top_max/ask_top_max — не доказательство",
        "BIDDEPTHT/OFFERDEPTHT из marketdata — не доказательство",
        "/api/orderbook-index — не доказательство (TATN 37.0 и PLZL 37.3 продавцы доминируют при цене выше VWAP и росте 1.7-1.8%, SIBN 21.08 13:42 85.7 покупатели доминируют 31 снимок при cumulative_delta -199443 VWAP 476.01 vs 470.4 buy 11 sell 337)",
        "imb_min/imb_max — производные от книги, не доказательство",
        "Книгу можно только для оценки стоимости исполнения — спред и шаг цены для тесного стопа",
        "SIBN 21.08 13:42 живой кейс поглощения: bid5_sum 24641 vs ask5_sum 4528 5.4:1 в пользу бида, orderbook_index 85.7, но cumulative_delta -199443 — книга и лента расходятся → поглощение, не разворот",
    ]

    signal = {
        "ticker": ticker.upper(),
        "direction": direction,
        "structure_and_plan": True,
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk_per_share": round(abs(entry - stop), 4) if entry and stop else None,
        "risk_with_commission": stop_info.get("risk_with_commission"),
        "reward_per_share": round(abs(target - entry), 4) if entry and target else None,
        "rr": rr,
        "rr_ok": rr_ok,
        "commission_pct": COMMISSION_PCT,
        "three_questions": three_q,
        "proofs_only_trades": proofs,
        "not_proof_book": not_proof,
        "stop_logic": stop_info,
        "trigger": trigger_info,
        "vwap": vwap_info,
        "invalidation": stop_info.get("invalidation"),
        "time_slice_msk": time_slice,
        "lag_warning": "ISS анонимная выдача отстаёт 15-19 мин (замер 21.08: последний принт 14:55:53 при времени 15:19) — метод видит недавнее прошлое точно, но не текущую секунду. ISS кэш ~10 мин, MOODEX ~5 мин, /api/flow res=5m обрывается 12:45-12:50 isTruncated true — последний час дельты недоступен, свежую только ISS trades.json или /api/live. Годен для выхода и стопа, вход опаздывает.",
        "disclaimer": "не инвестиционная рекомендация",
        "protocol": "21.08 who moves price + 21.08 index 14d 4/2/1 +3.73% без 14.08 +0.33% ноль, порог 7.5 хрупкий ±0.6, первое 10:40, read-only, book NOT proof",
        "journal_fields": ["дата", "тикер", "дельта выборки", "перевес %", "результат Q2", "уровень работы агрессора", "что сделали", "результат в R", "режим первого часа", "кратность", "время триггера"],
        "index_protocol_14d": {
            "note": "Порог 3× выжил как детектор старта и провалился как источник прибыли: вся + сумма в 1 дне из 14",
            "stats": "4 победы /2 убытка /1 ноль на сжатых, 5 плотных 0 срабатываний, сумма +3.73% без 14.08 +0.33% ноль до издержек минус после, точность 57% монета",
            "morning_cancelled": "Утренний оборот ОТМЕНЁН 21.08: 04.08 утро 15.1 млрд сжатый, 06.08 утро 17.2 млрд сжатый — связи нет, в 09:50 режим предсказать нельзя",
            "first_trigger": "10:40 исправлено, было 10:50 арифметическая ошибка: 09:50+5 бакетов=10:40, решает исход 07.08 3.98× 10:40 -1.21%, по конвенции 10:50 этого входа нет",
            "fragile_threshold": "7.5 млрд хрупкий ±0.6 млрд: 06.08 7.24, 20.08 7.44 лучшая +1.56%, 03.08 8.06, граница на глаз не проверена, 0.75 медианы ест сам себя",
            "table_14d": [
                {"date": "03.08", "morning": 14.0, "first_hour": 8.06, "regime": "плотный", "max_mult": "1.95×", "trigger": "нет пропущен +1.5%"},
                {"date": "04.08", "morning": 15.1, "first_hour": 6.50, "regime": "сжатый", "max_mult": "3.20×", "trigger": "14:50 вниз -1.15%"},
                {"date": "05.08", "morning": 14.4, "first_hour": 11.28, "regime": "плотный", "max_mult": "1.99×", "trigger": "нет пропущен +1.27%"},
                {"date": "06.08", "morning": 17.2, "first_hour": 7.24, "regime": "сжатый граница", "max_mult": "4.51×", "trigger": "11:50 вниз -0.03%"},
                {"date": "07.08", "morning": 8.72, "first_hour": 5.29, "regime": "сжатый", "max_mult": "3.98×", "trigger": "10:40 вверх -1.21%"},
                {"date": "11.08", "morning": 11.8, "first_hour": 9.5, "regime": "плотный", "max_mult": "2.4×", "trigger": "нет верно"},
                {"date": "12.08", "morning": 13.2, "first_hour": 8.5, "regime": "плотный", "max_mult": "4.16× в 17:10", "trigger": "отсечено временем"},
                {"date": "13.08", "morning": 10.6, "first_hour": 8.9, "regime": "плотный", "max_mult": "1.9×", "trigger": "нет пропущен -3.01%"},
                {"date": "14.08", "morning": 9.7, "first_hour": 6.2, "regime": "сжатый", "max_mult": "4.24×", "trigger": "11:20 вниз +3.4%"},
                {"date": "17.08", "morning": 38.2, "first_hour": 12.5, "regime": "плотный", "max_mult": "1.6×", "trigger": "нет пропущен -2.02%"},
                {"date": "18.08", "morning": 8.8, "first_hour": 6.0, "regime": "сжатый", "max_mult": "4.66×", "trigger": "11:40 вверх +0.66%"},
                {"date": "19.08", "morning": 10.1, "first_hour": 4.9, "regime": "сжатый", "max_mult": "4.2×", "trigger": "15:10 вверх +0.5%"},
                {"date": "20.08", "morning": 7.89, "first_hour": 7.44, "regime": "сжатый граница", "max_mult": "3.62×", "trigger": "13:00 вниз +1.56%"},
                {"date": "21.08", "morning": 6.38, "first_hour": 6.12, "regime": "сжатый", "max_mult": "2.62× на 13:40", "trigger": "нет"},
            ],
        },
        "notes": [
            "Встречный поток при движущейся цене — поглощение, не разворот (дважды ошиблись 30.07)",
            "Кумулятивная дельта экстремум = экстремум цены дня 11/13 — метрика выхода, не прогноза (промахи ASTR 19.08 LKOH 18.08)",
            "Дивергенция: новый экстремум дельты без нового экстремума цены → входа нет",
            "Поглощение важнее направления: ASTR 19.08 07:45 продали 182186 лотов vs 2008 покупок без сдвига цены, кумулятивная дельта max +738270 ровно на максимуме дня 211.8",
            "Правило исчерпания: крупнейший принт в конце выборки — чаще завершение программы (MGNT 339 и 100 в конце айсберга 64/64/64/30...)",
            "Правило дивергенции внутри дня: продавец 1138 и не сделал нового лоя дня 1601.5 — поглощение",
            "Лаг 15-19 мин — дефект для входа, не только неудобство",
            "Дискреционный вход без индексного триггера 0.84× при пороге 3× — 1/1 убыток vs с триггером 4+/2-/1 ноль на 14д — фильтр объёма важнее ленты",
            "Пробойные лонги убыточны 181д лучшая -0.113R после издержек",
            "Выборка 14д индекса: 4 победы /2 убытка /1 ноль +3.73% без 14.08 +0.33% ноль, 5 плотных 0 срабатываний пропущено 4 тренда +1.5% +1.27% -3.01% -2.02% — фильтр молчания работает, цена полноты пропуск трендов",
            "Порог 7.5 млрд хрупкий ±0.6 млрд: 06.08 7.24 ноль, 20.08 7.44 лучшая +1.56%, 03.08 8.06 плотный пропущен +1.5% — граница на глаз не проверена, 0.75 медианы ест сам себя",
            "Утренний оборот ОТМЕНЁН 21.08: 04.08 15.1 млрд сжатый, 06.08 17.2 млрд сжатый — связи нет",
            "Первое срабатывание 10:40 исправлено, было 10:50 арифметическая ошибка: 09:50+5 бакетов=10:40 решает 07.08 3.98× -1.21%",
            "Размах бакета-триггера как предиктор следования не работает: 04.08 0.54% убыток, 07.08 0.66% убыток, 20.08 0.81% победа, 06.08 1.06% ноль",
            "ISS только fast_mode:false иначе Unable to load / Content not available, кэш ~10 мин не дёргать чаще, /api/flow res=5m обрывается 12:45-12:50 isTruncated true последний час дельты недоступен только ISS trades.json или /api/live",
            "SIBN 21.08 13:42 живой кейс поглощения: orderbook_index 85.7 31 снимок bid5_sum 24641 vs ask5_sum 4528 5.4:1 bid но cumulative_delta -199443 VWAP 476.01 vs 470.4 buy 11 sell 337 — книга и лента расходятся → поглощение не разворот",
        ],
    }
    return signal


def build_signal_structure(
    ticker: str,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    trigger_info: dict,
    confirmations: dict,
    invalidation: str,
    time_slice: str,
) -> dict:
    three_q_mock = {
        "q1_imbalance": {"buy_volume": None, "sell_volume": None, "delta": None, "imbalance_pct": None},
        "q2_moves_price": {"control": confirmations.get("tape", "")},
        "q3_print_levels": {},
        "sample": {},
    }
    stop_info_mock = {
        "risk_with_commission": abs(entry - stop),
        "invalidation": [invalidation],
    }
    vwap_mock = {"text": confirmations.get("vwap_orb")}
    return build_signal_structure_21_08(
        ticker=ticker,
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        three_q=three_q_mock,
        stop_info=stop_info_mock,
        trigger_info=trigger_info,
        vwap_info=vwap_mock,
        time_slice=time_slice,
    )


def example_signals_from_memory() -> List[dict]:
    examples = [
        {
            "date": "2026-08-14",
            "index_trigger": "4.24× 11:20 вниз",
            "result": "-3.4% до close — единственная + сумма выборки 14д",
            "instrument": "NVTK",
            "efficiency": 0.65,
            "trade_count": "242→381→1830 7.6× в 11:25 дельта -20120 VWAP 979.28 шорт до 928.1 +5.2%",
            "stop": "за экстремум кумулятивной дельты max +49097 10:15 VWAP 990.83 хай 992.6 стоп туда риск 1.2%",
        },
        {
            "date": "2026-08-18",
            "index_trigger": "4.66× 11:40 вверх +0.66% (+1.4% до хая)",
            "instrument": "NVTK 0.65 +5.18% vs LKOH 0.42 +1.58% vs SIBN дельта - пропуск",
            "result": "выбор по эффективности",
        },
        {
            "date": "2026-08-19",
            "index_trigger": "4.2× 15:10 вверх +0.5% (+0.8%)",
            "regime": "4.9 сжатый",
        },
        {
            "date": "2026-08-20",
            "index_trigger": "3.62× 13:00 вниз +1.56% макс +1.93%",
            "regime": "7.44 сжатый граница — лучшая сделка выборки, хрупкий порог",
        },
        {
            "date": "2026-08-07",
            "index_trigger": "3.98× 10:40 вверх -1.21% макс +0.05%",
            "note": "Решает арифметическая ошибка первого срабатывания 10:40 vs 10:50: по 10:50 этого входа нет макс дня 2.18×",
        },
        {
            "date": "2026-08-04",
            "index_trigger": "3.20× 14:50 вниз -1.15% макс +0.12%",
            "morning": "15.1 млрд — по старому правилу предупреждение, по новому сжатый — утро ОТМЕНЁНО",
        },
        {
            "date": "2026-08-06",
            "index_trigger": "4.51× 11:50 вниз -0.03% макс +0.78%",
            "morning": "17.2 млрд второе по величине за 14д — утро ОТМЕНЁНО, первый час 7.24 сжатый граница",
        },
    ]
    return examples


def generate_readonly_signal(
    ticker: str,
    direction: str,
    entry: float,
    atr: Optional[float] = None,
    cum_delta_extreme_price: Optional[float] = None,
    vwap: Optional[float] = None,
    trade_count_explosion: Optional[dict] = None,
    book_data: Optional[dict] = None,
    iss_delta: Optional[dict] = None,
) -> dict:
    spread_note = ""
    if book_data:
        spread = book_data.get("spread_pct")
        if spread is not None:
            spread_note = f"спред {spread}% — стоимость исполнения, тесный стоп реализуем? (книга НЕ доказательство 21.08)"

    if iss_delta:
        buy_vol = iss_delta.get("buy_volume") or 0
        sell_vol = iss_delta.get("sell_volume") or 0
        tot = buy_vol + sell_vol
        imb = round(abs(buy_vol - sell_vol) / tot * 100, 2) if tot else 0
        three_q = {
            "q1_imbalance": {
                "buy_volume": buy_vol,
                "sell_volume": sell_vol,
                "delta": iss_delta.get("delta"),
                "total": tot,
                "imbalance_pct": imb,
                "is_noise": imb < 15,
            },
            "q2_moves_price": {
                "control": "продавец" if direction == "down" else "покупатель",
                "overall_move_pct": None,
            },
            "q3_print_levels": {
                "price_sequence": [],
                "qty_sequence": [],
                "descending_sales": direction == "down",
                "iceberg_clips": [],
                "exhaustion": False,
            },
            "sample": {
                "count": iss_delta.get("trade_count"),
                "time_from": "",
                "time_to": "",
            },
        }
    else:
        three_q = {
            "q1_imbalance": {},
            "q2_moves_price": {},
            "q3_print_levels": {},
            "sample": {},
        }

    if cum_delta_extreme_price is not None and cum_delta_extreme_price > 0:
        stop = cum_delta_extreme_price
        risk_pct = abs(entry - stop) / entry if entry else 0
        if risk_pct > 0.03:
            if atr:
                stop = entry - atr if direction == "up" else entry + atr
    else:
        if atr is None:
            atr = entry * 0.01
        stop = entry - atr if direction == "up" else entry + atr

    risk = abs(entry - stop)
    commission_entry = entry * COMMISSION_PCT / 100
    commission_exit = stop * COMMISSION_PCT / 100
    risk_with_comm = risk + commission_entry + commission_exit

    if direction == "up":
        target = entry + risk_with_comm * 2.2
    else:
        target = entry - risk_with_comm * 2.2

    stop_info = {
        "risk_with_commission": round(risk_with_comm, 4),
        "failed_extreme": cum_delta_extreme_price,
        "invalidation": [
            f"пачка агрессивных {'покупок' if direction=='down' else 'продаж'} >500 акций, после которой минута закрывается {'выше' if direction=='down' else 'ниже'} {cum_delta_extreme_price} (успешный повтор провалившейся попытки)",
            f"{'продажи' if direction=='down' else 'покупки'} 300+ перестают {'опускать' if direction=='down' else 'поднимать'} цену — исчезла асимметрия (Q2)",
        ],
    }

    vwap_info = {"text": f"LAST vs WAPRICE: entry {entry} vs VWAP {vwap} — из исполненных сделок {spread_note}"}

    trigger_info = {
        "source": "IMOEX2 10m value ≥3× avg 5 prev main session buckets, 10:40-16:00, первое арифметически 10:40 исправлено 21.08",
        "entry_rule": "вход по закрытию бакета расширения + Q1≥15% + Q2 асимметрия + Q3 уровни принтов",
        "regime_filter": f"first_hour 09:50-10:49 <{FIRST_HOUR_THRESHOLD/1e9:.1f} млрд = сжатый, утро ОТМЕНЁН 21.08, порог хрупкий ±0.6 млрд",
        "index_filter_note": "14д 4/2/1 +3.73% без 14.08 +0.33% ноль 57% монета, вся + сумма в 1 дне, 5 плотных 0 срабатываний пропущено 4 тренда, порог 7.5 хрупкий",
        "fragile_note": "06.08 7.24 ноль, 20.08 7.44 лучшая +1.56%, 03.08 8.06 плотный пропущен +1.5% — граница на глаз",
    }

    time_slice = msk_now().strftime("%Y-%m-%d %H:%M МСК")

    return build_signal_structure_21_08(
        ticker=ticker,
        direction=direction,
        entry=round(entry, 4),
        stop=round(stop, 4),
        target=round(target, 4),
        three_q=three_q,
        stop_info=stop_info,
        trigger_info=trigger_info,
        vwap_info=vwap_info,
        time_slice=time_slice,
    )


def generate_signal_from_iss_trades(
    ticker: str,
    trades: List[dict],
    marketdata: dict,
    entry_price: Optional[float] = None,
) -> dict:
    from src.collector.iss_trades import analyze_three_questions, build_stop_and_invalidation

    three_q = analyze_three_questions(trades)

    q1 = three_q.get("q1_imbalance") or {}
    if q1.get("is_noise"):
        return {"ticker": ticker, "skip": True, "reason": f"перевес {q1.get('imbalance_pct')}% <15% шум — читать не стоит"}

    if three_q.get("divergence"):
        return {"ticker": ticker, "skip": True, "reason": "дивергенция: новый экстремум дельты без нового экстремума цены → входа нет"}

    q3 = three_q.get("q3_print_levels") or {}
    if q3.get("exhaustion"):
        return {"ticker": ticker, "skip": True, "reason": f"исчерпание программы: {q3.get('exhaustion_note')}", "three_q": three_q}

    control = three_q.get("control")
    if control == "продавец":
        direction = "down"
    elif control == "покупатель":
        direction = "up"
    else:
        delta = q1.get("delta")
        if delta is None:
            return {"ticker": ticker, "skip": True, "reason": "контроль неясен", "three_q": three_q}
        direction = "down" if delta < 0 else "up"

    if entry_price is None:
        try:
            entry_price = float(marketdata.get("LAST") or marketdata.get("last") or 0)
        except Exception:
            entry_price = 0
    if not entry_price:
        return {"ticker": ticker, "skip": True, "reason": "нет цены LAST", "three_q": three_q}

    stop_info = build_stop_and_invalidation(three_q, entry_price, direction)

    if not stop_info.get("rr_ok"):
        return {"ticker": ticker, "skip": True, "reason": f"R/R {stop_info.get('rr')} <2 с комиссией", "three_q": three_q, "stop_info": stop_info}

    try:
        last = float(marketdata.get("LAST") or 0)
        wap = float(marketdata.get("WAPRICE") or 0)
        vwap_text = f"LAST {last} vs WAPRICE {wap} ({'выше VWAP' if last > wap else 'ниже VWAP' if last < wap else 'на VWAP'}) — из исполненных сделок"
    except Exception:
        vwap_text = "LAST vs WAPRICE нет данных"

    vwap_info = {"text": vwap_text, "last": marketdata.get("LAST"), "waprice": marketdata.get("WAPRICE")}

    trigger_info = {
        "source": "ISS trades 100 сделок + marketdata NUMTRADES, Q1≥15%, Q2 асимметрия, Q3 принты, индекс 14д 4/2/1",
        "q1": q1,
        "q2": three_q.get("q2_moves_price"),
        "q3": q3,
        "index_14d": "4/2/1 +3.73% без 14.08 +0.33% ноль, порог 3× детектор не прибыль, 7.5 хрупкий",
    }

    time_slice = msk_now().strftime("%Y-%m-%d %H:%M МСК")

    return build_signal_structure_21_08(
        ticker=ticker,
        direction=direction,
        entry=round(entry_price, 4),
        stop=stop_info["stop"],
        target=stop_info["target"],
        three_q=three_q,
        stop_info=stop_info,
        trigger_info=trigger_info,
        vwap_info=vwap_info,
        time_slice=time_slice,
    )


def format_signal_text(sig: dict) -> str:
    if sig.get("skip"):
        return f"=== SKIP {sig.get('ticker')} ===\nПричина: {sig.get('reason')}\n"

    lines = []
    lines.append(f"=== СИГНАЛ {sig['ticker']} {sig['direction'].upper()} ===")
    lines.append(f"Структура и план (не пробойный лонг — они убыточны 181д лучшая -0.113R):")
    lines.append(f"  Вход: {sig['entry']}")
    lines.append(f"  Стоп: {sig['stop']} (за провалившийся экстремум агрессора, которого поглотили, 11/13)")
    lines.append(f"  Цель: {sig['target']}")
    lines.append(f"  R/R: {sig['rr']} {'✅ ≥2' if sig['rr_ok'] else '❌ <2'} с комиссией {sig.get('commission_pct')}% (риск с комиссией {sig.get('risk_with_commission')})")
    lines.append(f"  Риск: {sig['risk_per_share']} на акцию, награда {sig['reward_per_share']}")
    lines.append("")
    lines.append(f"Три вопроса (метод 21.08 — только исполненные сделки):")
    three_q = sig.get("three_questions") or {}
    q1 = three_q.get("q1_imbalance") or {}
    lines.append(f"  Q1 куда перевес: buy {q1.get('buy_volume')} sell {q1.get('sell_volume')} delta {q1.get('delta')} перевес {q1.get('imbalance_pct')}% {'шум <15%' if q1.get('is_noise') else 'значимо'}")
    q2 = three_q.get("q2_moves_price") or {}
    lines.append(f"  Q2 двигает ли поток цену: {q2} — правило асимметрии: сравнивать не размер, а результат")
    q3 = three_q.get("q3_print_levels") or {}
    lines.append(f"  Q3 по каким ценам принты: descending_sales={q3.get('descending_sales')} iceberg={q3.get('iceberg_clips')} exhaustion={q3.get('exhaustion')} {q3.get('exhaustion_note')}")
    if three_q.get("divergence"):
        lines.append(f"  ⚠️ Дивергенция: {three_q.get('divergence_note')}")
    lines.append("")
    lines.append(f"Доказательства (только исполненные сделки):")
    for k, v in (sig.get("proofs_only_trades") or {}).items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append(f"Отменено как доказательство (21.08):")
    for n in sig.get("not_proof_book") or []:
        lines.append(f"  - {n}")
    lines.append("")
    lines.append(f"Триггер: {sig.get('trigger')}")
    lines.append("")
    lines.append(f"Индекс 14д (21.08 обновление): {sig.get('index_protocol_14d')}")
    lines.append("")
    lines.append(f"Стоп-логика:")
    stop_logic = sig.get("stop_logic") or {}
    lines.append(f"  failed_extreme: {stop_logic.get('failed_extreme')} control={stop_logic.get('control')}")
    lines.append(f"  комиссия: {stop_logic.get('commission_note')}")
    lines.append(f"  exhaustion: {stop_logic.get('exhaustion_warning')}")
    lines.append("")
    lines.append(f"Инвалидация (в сделках, не в заявках):")
    for inv in sig.get("invalidation") or []:
        lines.append(f"  - {inv}")
    lines.append("")
    lines.append(f"VWAP: {sig.get('vwap')}")
    lines.append(f"Время среза: {sig['time_slice_msk']}")
    lines.append(f"Лаг: {sig.get('lag_warning')}")
    lines.append(f"{sig['disclaimer']}")
    lines.append("")
    lines.append("Примечания:")
    for n in sig.get("notes", []):
        lines.append(f"  - {n}")
    lines.append("")
    lines.append(f"Журнал: {', '.join(sig.get('journal_fields') or [])}")
    return "\n".join(lines)
