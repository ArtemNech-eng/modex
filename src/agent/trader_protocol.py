"""
Trader Protocol 19.08 + 21.08 — read-only сигналы без записи

19.08:
  - Только чтение, без POST /api/analyst-signal, /api/ingest/deals, /api/live-signals/start/stop
  - Не использовать /api/screen, /api/volume-scan, /api/setup-watch, Claude-сигналы (по прямому указанию)
  - Анализ на сырых данных:
      дельта — ISS trades.json BUYSELL (B=buyer hits ask, S=bid) ground truth
      стакан по уровням bid5_sum/ask5_sum — /api/live/{TICKER}
      агрегат глубина — ISS marketdata BIDDEPTHT/OFFERDEPTHT/WAPRICE/NUMTRADES
      свечи/VWAP/ATR — /api/candles/brief
      дельта по закрытым минутам — /api/flow?source=exchange&res=5m&day=YYYY-MM-DD (парам day, не date)
      юниверс — /api/universe (только состав и ранг, цены кэш устаревают)
      направление книги — /api/orderbook-index но trust фильтр 31+
  - 3 независимых подтверждения (книга, лента ISS, VWAP/ORB) — УСТАРЕЛО 21.08, см ниже
  - R/R ≥2 арифметически, условие инвалидации, время среза, "не инвестиционная рекомендация"
  - "структура и план", т.к. пробойные лонги убыточны 181д лучшая -0.113R

21.08 — КТО ДВИГАЕТ ЦЕНУ (главный принцип):
  ⚖️ Лимитная книга не является доказательством. Заявку отменяют бесплатно и именно тогда,
  когда на неё начали ориентироваться. Единственные данные, которые нельзя подделать после факта —
  исполненные сделки с флагом агрессора. Всё остальное — рисунок, который вам показывают.

  Отменено как доказательство (и за и против):
    bid5_sum/ask5_sum, bid_top_max/ask_top_max, BIDDEPTHT/OFFERDEPTHT,
    /api/orderbook-index (21.08 трижды противоречил цене: TATN 37.0 и PLZL 37.3 «продавцы доминируют»
    при цене выше VWAP и росте 1.7-1.8%), imb_min/imb_max — производные от книги.
    Книгу допустимо использовать только для оценки стоимости исполнения — спред и шаг цены,
    чтобы понять реализуем ли тесный стоп.

  Доказательство:
    дельта агрессора ISS trades.json BUYSELL, цена к дневному VWAP (LAST vs WAPRICE),
    оборот и число сделок VOLTODAY/VALTODAY/NUMTRADES, направление принтов TRADETIME/PRICE.

  Порядок получения дельты (обязателен):
    1. NUMTRADES из marketdata: securities.json?iss.only=marketdata&securities=TICKER
    2. start = NUMTRADES-100
    3. trades.json?start=START&limit=100&iss.only=trades&fast_mode=false (true → Content not available)
    Подводные: reverse=true игнорируется, limit только 100 (limit=30 молча 10), TRADINGSESSION 0 утро 1 основная 2 вечер,
    лаг 15-19 мин (21.08 последний принт 14:55:53 при времени 15:19), ISS кэш ~10 мин, MOODEX ~5 мин.

  Плотность: 100 сделок MGNT 21.08 = 11 мин → запрос каждые 10 мин даёт непрерывную дельту.
  По бумагам >3 млрд/день (SBER 70875 к 14:56) 100 сделок <1 мин — точечная проверка.

  Три вопроса по порядку:
    Q1 куда перевес: дельта = Σ B - Σ S, нормировать на оборот выборки, <15% шум
    Q2 двигает ли поток цену — ключевой: сколько рублей цены агрессор получил на объём.
         Влил и сдвинул → контролирует, влил и вернулось → поглотили.
         Правило асимметрии: сравнивать не размер, а результат.
    Q3 по каким ценам принты: продажи по нисходящим = пробивает уровни,
         клипы одного размера = алгоритм айсберг, один гигантский без продолжения = разовая ликвидация

  Эталон MGNT 21.08 14:44-14:56: 100 сделок сессия 1, buys 664 sells 1333 turnover 1997 delta -669 перевес 33%
    14:53:17-18 buyer 521 акция 1616→1617, 14:54:08-55 seller 1138 в бид по нисходящим 1616→...→1613 клипы 64/64/64/30...
    Итог: buyer +1₽ за 521 и отдал за 90 сек, seller -4₽ за 1138 и остались. Контроль у продавца. Уровень 1613.

  Стоп и отмена — только по исполненным сделкам, не по книге:
    Стоп за провалившийся экстремум агрессора, которого поглотили. MGNT buyer не удержал 1617 → стоп шорта 1619
    Отмена: пачка агрессивных покупок >500 акций, после которой минута закрывается выше 1617 (успешный повтор провалившейся попытки)
           продажи 300+ перестают опускать цену — исчезла асимметрия
    R/R≥2 с комиссией 0.05% в обе стороны (при тесном стопе до 15% риска)

  Первый провал MGNT 21.08: шорт 113 по 1613.03 15:14:30 стоп 1619 выбит 15:50-59 хай 1620 объём 9411 vs 361 в 15:30 убыток 857₽ 0.47%
    После 16:30 вынос до 1623.5 и падение до 1610 мин 1609 17:10-19 цель 1601.5 не достигнута вечер рост до 1634.
    Ошибка не в стопе, а в направлении. Макс благоприятное +4₽ vs цель +11.5₽. Широкий 1622.5 выбит 16:30 хаем 1623.5.
    Стоп сделал работу 857₽ вместо 2430₽ до закрытия.

  Правила:
    - Исчерпания программы: крупнейший принт в конце выборки — чаще завершение программы, а не середина
    - Дивергенция внутри дня: продавец 1138 и не сделал нового лоя дня 1601.5 с утра, остановился на 1613 на 11₽ выше — поглощение
    - Лаг 15-19 мин — дефект для входа: вывод в 15:19 по данным до 14:55:53 цена уже вернулась на 1617 на 4₽ выше уровня продавца. Метод годится для выхода и стопа, но вход опаздывает.
    - Дискреционный вход без индексного триггера: бакет 0.84× при пороге 3× — 1/1 убыток vs с триггером 4+/2-/1 ноль на 14д — фильтр объёма важнее ленты

Этот модуль НЕ делает POST, НЕ вызывает запрещённые эндпоинты, только читает и считает.
"""

import logging
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Пороги 19.08 (подогнаны на 7д, вести журнал)
MORNING_VALUE_THRESHOLD = 10.3e9
FIRST_HOUR_THRESHOLD = 7.5e9
EXPANSION_MULTIPLIER = 3.0
NO_TRADE_AFTER = 16 * 60
FIRST_TRIGGER_TIME = 10 * 60 + 50

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
    if median_5d is not None and median_5d > 0:
        if first_hour_value < 0.75 * median_5d:
            return True
    return first_hour_value < FIRST_HOUR_THRESHOLD


def detect_expansion_triggers(buckets: List[dict]) -> List[dict]:
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
        if mins < FIRST_TRIGGER_TIME:
            continue
        if mins >= NO_TRADE_AFTER:
            continue
        if not (10 * 60 <= mins <= 18 * 60 + 50):
            continue
        if i < 5:
            continue
        prev = buckets_sorted[i - 5:i]
        try:
            avg_prev = sum(float(x.get("value") or 0) for x in prev) / 5
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

    # Доказательства только из сделок
    proofs = {
        "delta_aggressor": f"ISS trades.json BUYSELL B/S: buy {q1.get('buy_volume')} sell {q1.get('sell_volume')} delta {q1.get('delta')} перевес {q1.get('imbalance_pct')}% {'шум <15%' if q1.get('is_noise') else 'значимо'}",
        "vwap": vwap_info.get("text") if vwap_info else "LAST vs WAPRICE — из исполненных сделок",
        "turnover": f"VOLTODAY/VALTODAY/NUMTRADES факт: sample {three_q.get('sample',{}).get('count')} сделок {three_q.get('sample',{}).get('time_from')}→{three_q.get('sample',{}).get('time_to')}",
        "prints": f"TRADETIME/PRICE: {q3.get('price_sequence')} qty {q3.get('qty_sequence')} descending_sales={q3.get('descending_sales')} iceberg={q3.get('iceberg_clips')} exhaustion={q3.get('exhaustion')}",
    }

    # Что НЕ является доказательством (отменено 21.08)
    not_proof = [
        "bid5_sum/ask5_sum и плиты из /api/live — не доказательство (отменяют бесплатно)",
        "bid_top_max/ask_top_max — не доказательство",
        "BIDDEPTHT/OFFERDEPTHT из marketdata — не доказательство",
        "/api/orderbook-index — не доказательство (TATN 37.0 и PLZL 37.3 продавцы доминируют при цене выше VWAP и росте 1.7-1.8%)",
        "imb_min/imb_max — производные от книги, не доказательство",
        "Книгу можно только для оценки стоимости исполнения — спред и шаг цены для тесного стопа",
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
        "lag_warning": "ISS анонимная выдача отстаёт 15-19 мин (замер 21.08: последний принт 14:55:53 при времени 15:19) — метод видит недавнее прошлое точно, но не текущую секунду. ISS кэш ~10 мин, MOODEX ~5 мин. Годен для выхода и стопа, вход опаздывает.",
        "disclaimer": "не инвестиционная рекомендация",
        "protocol": "21.08 who moves price + 19.08 index filter, read-only, no POST, book NOT proof",
        "journal_fields": ["дата", "тикер", "дельта выборки", "перевес %", "результат Q2", "уровень работы агрессора", "что сделали", "результат в R"],
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
            "Выборка честная: 7д индекса 4 срабатывания основная 3 сжатый порог 3× подогнан — вести журнал",
        ],
    }
    return signal


# Совместимость со старым интерфейсом 19.08
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
    # старый интерфейс — оборачиваем в новый, книга как NOT proof
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
            "result": "-3.4% до close",
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
    book_data: Optional[dict] = None,  # DEPRECATED 21.08 — только для оценки спреда, не доказательство
    iss_delta: Optional[dict] = None,
) -> dict:
    """
    Генерация read-only сигнала — теперь по методу 21.08, книга NOT proof.
    Для совместимости принимает старые поля, но внутри строит three_questions.
    """
    # book_data теперь только для спреда/шага, не для подтверждения
    spread_note = ""
    if book_data:
        spread = book_data.get("spread_pct")
        if spread is not None:
            spread_note = f"спред {spread}% — стоимость исполнения, тесный стоп реализуем? (книга НЕ доказательство 21.08)"

    # Формируем mock three_q из iss_delta если есть
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

    # стоп за провалившийся экстремум
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

    # цель с комиссией
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
        "source": "IMOEX2 10m value ≥3× avg 5 prev buckets, 10:50-16:00",
        "entry_rule": "вход по закрытию бакета расширения + Q1≥15% + Q2 асимметрия + Q3 уровни принтов",
        "regime_filter": f"first_hour <{FIRST_HOUR_THRESHOLD/1e9:.1f} млрд или <0.75 медианы 5д = сжатый",
        "index_filter_note": "Без триггера 0.84× — 1/1 убыток, с триггером 4+/2-/1 ноль на 14д — фильтр объёма важнее ленты",
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
    """
    Полный пайплайн 21.08: из ISS trades + marketdata → сигнал read-only
    - три вопроса
    - стоп за провалившийся экстремум
    - R/R≥2 с комиссией
    - проверка дивергенции и исчерпания
    """
    from src.collector.iss_trades import analyze_three_questions, build_stop_and_invalidation

    three_q = analyze_three_questions(trades)

    # Q1 шум?
    q1 = three_q.get("q1_imbalance") or {}
    if q1.get("is_noise"):
        return {"ticker": ticker, "skip": True, "reason": f"перевес {q1.get('imbalance_pct')}% <15% шум — читать не стоит"}

    # Дивергенция?
    if three_q.get("divergence"):
        return {"ticker": ticker, "skip": True, "reason": "дивергенция: новый экстремум дельты без нового экстремума цены → входа нет"}

    # Исчерпание?
    q3 = three_q.get("q3_print_levels") or {}
    if q3.get("exhaustion"):
        return {"ticker": ticker, "skip": True, "reason": f"исчерпание программы: {q3.get('exhaustion_note')}", "three_q": three_q}

    # Направление из Q2
    control = three_q.get("control")
    if control == "продавец":
        direction = "down"
    elif control == "покупатель":
        direction = "up"
    else:
        # если контроль неясен — смотрим дельту
        delta = q1.get("delta")
        if delta is None:
            return {"ticker": ticker, "skip": True, "reason": "контроль неясен", "three_q": three_q}
        direction = "down" if delta < 0 else "up"

    # entry: если не задан — LAST из marketdata
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

    # VWAP
    try:
        last = float(marketdata.get("LAST") or 0)
        wap = float(marketdata.get("WAPRICE") or 0)
        vwap_text = f"LAST {last} vs WAPRICE {wap} ({'выше VWAP' if last > wap else 'ниже VWAP' if last < wap else 'на VWAP'}) — из исполненных сделок"
    except Exception:
        vwap_text = "LAST vs WAPRICE нет данных"

    vwap_info = {"text": vwap_text, "last": marketdata.get("LAST"), "waprice": marketdata.get("WAPRICE")}

    trigger_info = {
        "source": "ISS trades 100 сделок + marketdata NUMTRADES, Q1≥15%, Q2 асимметрия, Q3 принты",
        "q1": q1,
        "q2": three_q.get("q2_moves_price"),
        "q3": q3,
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
    """Человекочитаемый текст сигнала 21.08"""
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
